START_MESSAGE = (
    "Привет-привет! Я Амбер! 🔥\n"
    "Braixen по имени Amber, если по-нормальному.\n"
    "Ну... я тут. Пиши, если что. Только не груби, ладно? 😤💛"
)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(START_MESSAGE)
