from __future__ import annotations

from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import get_user_by_tg_id


async def require_user_name(event: Message | CallbackQuery) -> bool:
    """
    Ensure user has a non-empty name.
    Returns True if name exists; otherwise prompts to set name and returns False.
    """
    tg_id = event.from_user.id if event.from_user else None
    user = await get_user_by_tg_id(int(tg_id)) if tg_id else None
    if user and (user.get("name") or "").strip():
        return True

    kb = InlineKeyboardBuilder()
    kb.button(text="Указать имя", callback_data="auth:start")
    kb.adjust(1)

    text = (
        "Чтобы пользоваться ботом, нужно указать имя.\n"
        "Нажми кнопку ниже и введи свой ник."
    )

    if isinstance(event, CallbackQuery) and event.message:
        try:
            await event.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            try:
                await event.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
            except Exception:
                pass
        try:
            await event.answer()
        except Exception:
            pass
        return False

    if isinstance(event, Message):
        try:
            await event.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            pass
        try:
            await event.delete()
        except Exception:
            pass
        return False

    return False
