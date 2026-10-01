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
from aiogram.filters import CommandStart

from prompts import START_MESSAGE, SYSTEM_PROMPT
from stickers import STICKERS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")  # например, AmberAIBot (без @)

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY не задан")
if not WEBHOOK_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL не задан")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "qwen/qwen2.5-vl-72b-instruct:free"
MAIN_MODEL = "openrouter/free"
WEBHOOK_PATH = f"/webhook/{TOKEN}"

# ── Антифлуд ─────────────────────────────────────────
COOLDOWN_SECONDS = 3
user_cooldown: dict[int, float] = {}

# ── Объекты ──────────────────────────────────────────
bot = Bot(token=TOKEN)
dp = Dispatcher()
http_client: httpx.AsyncClient | None = None


# ── Хелперы ──────────────────────────────────────────
def is_addressed_to_bot(message: types.Message) -> bool:
    """Личка — всегда True. Группа — только упоминание @username или reply на бота."""
    if message.chat.type == "private":
        return True

    if message.chat.type in ("group", "supergroup"):
        text = message.text or message.caption or ""
        if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
            return True
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == bot.id:
                return True
        return False

    return False


def strip_bot_mention(text: str) -> str:
    """Убирает @username бота из текста, чтобы модель его не видела."""
    if not text or not BOT_USERNAME:
        return text
    return text.replace(f"@{BOT_USERNAME}", "").strip()


def parse_sticker_marker(text: str) -> tuple[str, str | None]:
    if not text:
        return "", None
    match = re.search(r"\[STICKER:(\w+)\]", text)
    if not match:
        return text, None
    emotion = match.group(1).lower()
    clean = re.sub(r"\[STICKER:\w+\]", "", text).strip()
    return clean, emotion


async def send_long_message(message: types.Message, text: str, chunk_size: int = 4000):
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
    file = await bot.get_file(file_id)
    buf = await bot.download_file(file.file_path)
    data = base64.b64encode(buf.read()).decode()
    return f"data:{mime};base64,{data}"


async def ask_openrouter(messages: list, model: str = MAIN_MODEL) -> str:
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
    logger.info("OpenRouter status=%s model=%s", resp.status_code, model)

    if resp.status_code != 200:
        logger.error("OpenRouter %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    if "choices" not in data or not data["choices"]:
        logger.error("OpenRouter без choices: %s", str(data)[:300])
        raise RuntimeError("Нет choices в ответе")

    content = data["choices"][0].get("message", {}).get("content")
    if not content:
        logger.error("OpenRouter пустой content: %s", str(data)[:300])
        raise RuntimeError("Пустой content от модели")

    return content


async def describe_image(base64_url: str, user_text: str) -> str:
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
    if not is_addressed_to_bot(message):
        return
    if is_on_cooldown(message.from_user.id):
        return
    try:
        file_id = message.photo[-1].file_id
        b64 = await download_as_base64(file_id, mime="image/jpeg")
        caption = strip_bot_mention(message.caption or "")
        description = await describe_image(b64, caption)
        await reply_as_amber(
            message,
            f"Пользователь прислал картинку. Вот её описание: {description}",
        )
    except Exception as e:
        logger.exception("Ошибка обработки фото")
        await message.answer(f"Ой... не разглядела картинку 🙈 [{type(e).__name__}]")


@dp.message(F.sticker)
async def handle_sticker(message: types.Message):
    if not is_addressed_to_bot(message):
        return
    if is_on_cooldown(message.from_user.id):
        return
    try:
        emoji = message.sticker.emoji or "❓"
        await reply_as_amber(
            message,
            f"Пользователь прислал стикер с эмодзи {emoji}. Отреагируй живо, коротко.",
        )
    except Exception as e:
        logger.exception("Ошибка обработки стикера")
        await message.answer(
            f"*прижимает ушки* ...не поняла стикер 🙈 [{type(e).__name__}]"
        )


@dp.message(F.animation)
async def handle_animation(message: types.Message):
    if not is_addressed_to_bot(message):
        return
    if is_on_cooldown(message.from_user.id):
        return
    try:
        await reply_as_amber(
            message,
            "Пользователь прислал гифку. Отреагируй коротко и живо, как друг.",
        )
    except Exception as e:
        logger.exception("Ошибка обработки гиф")
        await message.answer(f"Ой, гифка не открылась 😤 [{type(e).__name__}]")


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    if not is_addressed_to_bot(message):
        return
    if is_on_cooldown(message.from_user.id):
        return
    await message.answer("*наклоняет ушки* ...я пока не понимаю голосовые 😔")


@dp.message(F.text)
async def handle_message(message: types.Message):
    if not message.text:
        return
    if not is_addressed_to_bot(message):
        return
    if is_on_cooldown(message.from_user.id):
        return
    try:
        text = strip_bot_mention(message.text)
        if not text:
            return
        await reply_as_amber(message, text)
    except Exception as e:
        logger.exception("Ошибка обработки текста")
        await message.answer(
            f"Фыр! Что-то сломалось. Попробуй ещё раз 😤\n"
            f"Причина: {type(e).__name__}: {str(e)[:200]}"
        )


# ── FastAPI ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, BOT_USERNAME
    http_client = httpx.AsyncClient()

    # Автоматически получаем username бота, если не задан в env
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logger.info("BOT_USERNAME получен автоматически: @%s", BOT_USERNAME)

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
