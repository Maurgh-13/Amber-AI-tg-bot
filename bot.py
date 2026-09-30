import os
import telebot
import google.generativeai as genai

# Подключаем ключи из настроек хостинга
BOT_TOKEN = os.environ.get('BOT_TOKEN')
AI_TOKEN = os.environ.get('AI_TOKEN')

bot = telebot.TeleBot(BOT_TOKEN)
genai.configure(api_key=AI_TOKEN)
model = genai.GenerativeModel('gemini-pro')

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Привет! Я Амбер! Braixen по имени Amber!")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    try:
        # Отправляем текст пользователя в нейросеть
        response = model.generate_content(message.text)
        bot.reply_to(message, response.text)
    except Exception as e:
        bot.reply_to(message, "Простите, произошла ошибка, попробуйте позже.")

# Запуск бота
bot.infinity_polling()

