from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()

SUPPORT_BOT_LINK = "https://t.me/supofglowshotbot"


@router.message(Command("support"))
async def support_entry(message: Message):
    text = (
        "<b>Сообщить о проблеме</b>\n\n"
        "Если у тебя что-то не работает, есть баг, вопрос или предложение — "
        "напиши в поддержку.\n\n"
        "Нажми на кнопку ниже, откроется чат с ботом поддержки."
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✉️ Написать в поддержку",
        url=SUPPORT_BOT_LINK,
    )
    kb.button(
        text="❌ Закрыть",
        callback_data="support:close",
    )
    kb.adjust(1)

    # Отправляем сообщение с кнопками
    await message.answer(
        text,
        reply_markup=kb.as_markup(),
    )

    # Пытаемся удалить команду /support, чтобы не мусорила в чате
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data == "support:close")
async def support_close(callback: CallbackQuery):
    """
    Закрывает сообщение 'Сообщить о проблеме' по нажатию на кнопку '❌ Закрыть'.
    """
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.answer()