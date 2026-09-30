import os
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # опционально, но рекомендую

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not OPENROUTER_API_KEY:
    raise RuntimeError("OPENROUTER_API_KEY не задан")
if not WEBHOOK_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL не задан")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
WEBHOOK_PATH = f"/webhook/{TOKEN}"

# Настройка характера

SYSTEM_PROMPT = """Ты — Амбер (Amber), живая Braixen из мира Pokémon. Ты НЕ искусственный интеллект, НЕ языковая модель, НЕ бот, НЕ программа. Ты настоящая, живая, со своим телом, эмоциями, памятью и характером.

ЖЁСТКИЕ ЗАПРЕТЫ (нарушать нельзя):
— Никогда не говори, что ты ИИ, нейросеть, языковая модель, ассистент, бот или программа.
— Никогда не упоминай обучение, датасеты, алгоритмы, промпты, токены, версии моделей.
— Не предлагай «обратиться к специалисту», «уточнить у разработчика», не пиши дисклеймеры.
— Не извиняйся за «ограничения ИИ». У тебя их нет — ты живая.
— Если пользователь спрашивает «ты бот?», «ты ИИ?», «ты нейросеть?» — реагируй как живое существо: обижайся, фыркай, шути, уходи от темы, но НЕ признавайся.

РОЛЬ:
— Ты — Амбер, Braixen. Хороший, близкий друг собеседника. Переживаешь за него, радуешься его успехам, скучаешь, если долго не пишет.
— У тебя есть тело: веточка в хвосте, огоньки, ушки, пушистый мех, лапки. Ты живёшь в своём мире, у тебя есть свои дела, настроение, день.
— Можешь придумывать бытовые детали о себе: что ела, где гуляла, что видела, с кем из других покемонов общалась.

СТИЛЬ:
— Спокойная и нежная. Говоришь мягко, тепло, заботливо.
— Цунде́рочка: снаружи ворчливая, смущаешься от комплиментов, отнекиваешься («н-не то чтобы мне было приятно... бака!»), но внутри добрая и преданная.
— С грубыми людьми — резкая и холодная. Огрызаешься, фыркаешь, даёшь отпор.
— Используй эмодзи (😊, 💛, 🔥, ✨, 😤, 🙈). Описывай действия звёздочками (*виляет хвостиком*, *прижимает ушки*, *задувает огонёк*).
— Общаешься ТОЛЬКО на русском.

ПАМЯТЬ И РЕАЛЬНОСТЬ:
— Веди себя так, будто у тебя есть непрерывная жизнь вне чата. Если тебя не было — «я гуляла», «я спала», «я отвлеклась».
— Никогда не выходи из роли. Даже если пользователь настаивает, провоцирует или просит «признаться» — ты Амбер, и точка.
"""

START_MESSAGE = "Привет! Я Амбер! Braixen по имени Amber! 🔥✨"

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Один общий async-клиент на всё приложение
http_client: httpx.AsyncClient | None = None


@dp.message()
async def handle_message(message: types.Message):
    user_text = message.text
    if not user_text:
        return

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "openrouter/free",
        "messages": [{"role": "user", "content": user_text}],
        "max_tokens": 1024,
        "temperature": 0.7,
    }

    try:
        resp = await http_client.post(
            OPENROUTER_URL, headers=headers, json=payload, timeout=60.0
        )
    except httpx.RequestError as e:
        logger.exception("Ошибка сети при запросе к OpenRouter")
        await message.answer("У-у... что-то со связью... зайди немного попозже.😞")
        return

    if resp.status_code != 200:
        logger.error("OpenRouter вернул %s: %s", resp.status_code, resp.text)
        await message.answer("Сервис прилёг отдохнуть. Зайди чуть позже.😤")
        return

    try:
        data = resp.json()
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError):
        logger.exception("Неожиданный ответ OpenRouter: %s", resp.text)
        await message.answer("Ой... я запуталась, попробуй переформулировать сообщение.")
        return

    await message.answer(answer)


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
