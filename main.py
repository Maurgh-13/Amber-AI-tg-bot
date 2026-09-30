import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
import requests

TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render автоматически подставит ваш URL

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

@dp.message()
for_ai = {}

@dp.message()
async def handle_message(message: types.Message):
    user_text = message.text
    if not user_text:
        return
        
    # Запрос к OpenRouter API (бесплатная модель DeepSeek/Llama)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "deepseek/deepseek-chat:free", 
        "messages": [{"role": "user", "content": user_text}]
    }
    response = requests.post("https://openrouter.ai", headers=headers, json=data)
    try:
        res_json = response.json()
        answer = res_json["choices"][0]["message"]["content"]
    except Exception as e:
        answer = "Простите, произошла ошибка, попробуйте позже: {str(e)}"
        
    await message.answer(answer)

@app.on_event("startup")
async def on_startup():
    webhook_path = f"/webhook/{TOKEN}"
    full_url = f"{WEBHOOK_URL}{webhook_path}"
    await bot.set_webhook(full_url)
    app.state.bot = bot
    app.state.dp = dp
    
@app.post(f"/webhook/{TOKEN}")
async def incoming_webhook(request: Request):
    update = await request.json()
    telegram_update = types.Update(**update)
    await dp.feed_update(bot=bot, update=telegram_update)
    return {"ok": True}
