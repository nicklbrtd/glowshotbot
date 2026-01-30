from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CommandObject

router = Router()


@router.message(CommandStart(deep_link=True))
async def handle_app_start(message: Message, command: CommandObject):
    args = (command.args or "").strip().lower()
    if not args.startswith("ios_app"):
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть GlowShot", url="https://glowshot.app/ios")],
        ]
    )

    await message.answer(
        "Регистрация в боте подтверждена. Вернись в приложение GlowShot и нажми «I’ve registered».",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
