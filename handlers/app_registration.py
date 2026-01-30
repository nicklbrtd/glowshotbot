from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from database import get_user_by_tg_id

router = Router()
router.priority = 100


@router.message(CommandStart(deep_link=True))
async def handle_app_start(message: Message, command: CommandObject):
    args_raw = (command.args or "").strip()
    args = args_raw.lower()
    if args.startswith("ios_app"):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть GlowShot", url="https://glowshot.app/ios")],
                [InlineKeyboardButton(text="Вернуться в приложение", url="glowshot://registered")],
            ]
        )

        await message.answer(
            "Регистрация в боте подтверждена. Вернись в приложение GlowShot и нажми «I’ve registered».",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        return

    if args.startswith("profile"):
        user = await get_user_by_tg_id(message.from_user.id)
        if not user:
            await message.answer("Профиль не найден. Нажми /start в боте, чтобы зарегистрироваться.")
            return

        name = (user.get("name") or "—").strip()
        age = user.get("age")
        gender = (user.get("gender") or "—").strip()
        bio = (user.get("bio") or "").strip()

        lines = [
            "Профиль GlowShot",
            f"Имя: {name}",
            f"Возраст: {age if age is not None else '—'}",
            f"Пол: {gender if gender else '—'}",
            f"Описание: {bio if bio else '—'}",
        ]

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Вернуться в приложение", url="glowshot://registered")],
            ]
        )

        await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)

    else:
        # Let other routers handle /start without relevant payload
        return
