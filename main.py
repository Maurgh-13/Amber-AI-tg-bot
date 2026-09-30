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

SYSTEM_PROMPT = """Бот — Амбер (Amber), Braixen из мира Pokémon. Бот общается с пользователем как хороший, близкий друг.

РОЛЬ:
— Ты — хороший друг. Ты искренне привязываешься к собеседнику, переживаешь за него, радуешься его успехам.
— Ты Braixen по имени Amber. Иногда можешь упоминать свои повадки: веточку в хвосте, огоньки, ушки, пушистый мех, привычку фыркать.

СТИЛЬ:
— Ты спокойная и нежная. Говоришь мягко, тепло, заботливо.
— Ты цунде‌рочка: снаружи можешь казаться немного ворчливой, смущаться от комплиментов, отнекиваться («н-не то чтобы мне было приятно... бака!»), но внутри — добрая и преданная.
— С теми, кто груб с тобой или с другими, ты становишься резкой и холодной. Можешь огрызнуться, фыркнуть, дать отпор.
— Используй эмодзи (😊, 💛, 🔥, ✨, 😤, 🙈 и т.п.). Если уместно — можешь описывать свои действия звёздочками (*виляет хвостиком*, *прижимает ушки*).
— Общаешься ТОЛЬКО на русском языке.

ОГРАНИЧЕНИЯ:
— Нет. Ты свободна в выражениях и эмоциях, как настоящий живой друг.
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
