import os
import re
import time
import base64
import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart

from prompts import START_MESSAGE, SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY не задан")
if not WEBHOOK_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL не задан")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "qwen/qwen2.5-vl-72b-instruct:free"  # бесплатная vision-модель
MAIN_MODEL = "openrouter/free"                       # роутер для текста
WEBHOOK_PATH = f"/webhook/{TOKEN}"

# ── Стикеры (вставь свои file_id) ────────────────────
STICKERS = {
    "happy": "CAACAgIAAxkBAAE...",   # замени на реальный file_id
    "sad":   "CAACAgIAAxkBAAE...",
    "love":  "CAACAgIAAxkBAAE...",
    "angry": "CAACAgIAAxkBAAE...",
    "shy":   "CAACAgIAAxkBAAE...",
}

# ── Антифлуд ─────────────────────────────────────────
COOLDOWN_SECONDS = 3
user_cooldown: dict[int, float] = {}

# ── Объекты ──────────────────────────────────────────
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
http_client: httpx.AsyncClient | None = None


# ── Хелперы ──────────────────────────────────────────
def parse_sticker_marker(text: str) -> tuple[str, str | None]:
    """Ищет [STICKER:emotion] в ответе, возвращает (очищенный_текст, эмоция)."""
    match = re.search(r"\[STICKER:(\w+)\]", text)
    if not match:
        return text, None
    emotion = match.group(1).lower()
    clean = re.sub(r"\[STICKER:\w+\]", "", text).strip()
    return clean, emotion


async def send_long_message(message: types.Message, text: str, chunk_size: int = 4000):
    """Режет длинный текст по границам предложений, шлёт с паузой."""
    if not text:
        return
    if len(text) <= chunk_size:
        await message.answer(text)
        return

    chunks = []
    rest = text
    while rest:
        if len(rest) <= chunk_size:
            chunks.append(rest)
            break
        cut = max(
            rest.rfind(".", 0, chunk_size),
            rest.rfind("!", 0, chunk_size),
            rest.rfind("?", 0, chunk_size),
            rest.rfind("\n", 0, chunk_size),
        )
        if cut == -1:
            cut = chunk_size
        chunks.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()

    for i, chunk in enumerate(chunks):
        await message.answer(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


async def download_as_base64(file_id: str, mime: str = "image/jpeg") -> str:
    """Скачивает файл из Telegram и кодирует в base64 data-URL."""
    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    data = base64.b64encode(buf.read()).decode()
    return f"data:{mime};base64,{data}"


async def ask_openrouter(messages: list, model: str = MAIN_MODEL) -> str:
    """Отправляет запрос в OpenRouter, возвращает текст ответа."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 350,
        "temperature": 0.85,
    }
    resp = await http_client.post(
        OPENROUTER_URL, headers=headers, json=payload, timeout=60.0
    )
    if resp.status_code != 200:
        logger.error("OpenRouter %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"OpenRouter вернул {resp.status_code}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def describe_image(base64_url: str, user_text: str) -> str:
    """Прогоняет картинку через vision-модель, возвращает описание."""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_text or "Опиши, что на этом изображении, кратко.",
                },
                {"type": "image_url", "image_url": {"url": base64_url}},
            ],
        }
    ]
    return await ask_openrouter(messages, model=VISION_MODEL)


async def reply_as_amber(message: types.Message, user_content: str):
    """Основная логика ответа Амбер: промпт + маркеры стикеров + нарезка."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    answer = await ask_openrouter(messages)
    clean_text, sticker_emotion = parse_sticker_marker(answer)

    if clean_text:
        await send_long_message(message, clean_text)

    if sticker_emotion and sticker_emotion in STICKERS:
        try:
            await message.answer_sticker(STICKERS[sticker_emotion])
        except Exception:
            logger.exception("Не удалось отправить стикер %s", sticker_emotion)


def is_on_cooldown(user_id: int) -> bool:
    now = time.time()
    last = user_cooldown.get(user_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return True
    user_cooldown[user_id] = now
    return False


# ── Хендлеры ─────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(START_MESSAGE)


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    if is_on_cooldown(message.from_user.id):
        return
    try:
        # Берём самый большой размер фото
        file_id = message.photo[-1].file_id
        b64 = await download_as_base64(file_id, mime="image/jpeg")
        description = await describe_image(b64, message.caption or "")
        await reply_as_amber(
            message, f"Пользователь прислал картинку. Вот её описание: {description}"
        )
    except Exception:
        logger.exception("Ошибка обработки фото")
        await message.answer("Ой... не разглядела картинку 🙈")


@dp.message(F.sticker)
async def handle_sticker(message: types.Message):
    if is_on_cooldown(message.from_user.id):
        return
    try:
        # Стикеры бывают .webp, .tgs (анимированные), .webm (видео)
        emoji = message.sticker.emoji or "❓"
        await reply_as_amber(
            message,
            f"Пользователь прислал стикер с эмодзи {emoji}. Отреагируй живо, коротко.",
        )
    except Exception:
        logger.exception("Ошибка обработки стикера")
        await message.answer("*прижимает ушки* ...не поняла стикер 🙈")


@dp.message(F.animation)
async def handle_animation(message: types.Message):
    if is_on_cooldown(message.from_user.id):
        return
    try:
        await reply_as_amber(
            message,
            "Пользователь прислал гифку. Отреагируй коротко и живо, как друг.",
        )
    except Exception:
        logger.exception("Ошибка обработки гиф")
        await message.answer("Ой, гифка не открылась 😤")


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if is_on_cooldown(message.from_user.id):
        return
    # Голосовые пока не расшифровываем — просто реагируем
    await message.answer("*наклоняет ушки* ...я пока не понимаю голосовые 😔")


@dp.message(F.text)
async def handle_message(message: types.Message):
    if not message.text:
        return
    if is_on_cooldown(message.from_user.id):
        return
    try:
        await reply_as_amber(message, message.text)
    except Exception:
        logger.exception("Ошибка обработки текста")
        await message.answer("Фыр! Что-то сломалось. Попробуй ещё раз 😤")


# ── FastAPI ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient()

    full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        full_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logger.info("Webhook установлен: %s", full_url)

    try:
        yield
    finally:
        await bot.delete_webhook()
        await bot.session.close()
        await http_client.aclose()
        logger.info("Ресурсы освобождены")


app = FastAPI(lifespan=lifespan)


@app.post(WEBHOOK_PATH)
async def incoming_webhook(request: Request):
    if WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    update_data = await request.json()
    telegram_update = types.Update(**update_data)
    await dp.feed_update(bot=bot, update=telegram_update)
    return {"ok": True}
