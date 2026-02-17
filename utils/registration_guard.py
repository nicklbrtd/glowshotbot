from __future__ import annotations

from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import get_user_by_tg_id, get_user_ui_state


REGISTRATION_INTRO_TEXT = (
    "Добро пожаловать в глоушот.\n\n"
    "Это место, где фотографии живут.\n"
    "Где каждый кадр оценивают.\n"
    "Где ты можешь вырасти как фотограф.\n\n"
    "Здесь всё просто:\n"
    "• Публикуешь фото\n"
    "• Оцениваешь других\n"
    "• Получаешь оценки на свои\n"
    "• Попадаешь в итоги дня\n\n"
    "Чем активнее ты — тем больше тебя видят.\n\n"
    "Начнём?"
)


def _build_add_name_kb(text: str = "Начнём!"):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data="auth:start")
    kb.adjust(1)
    return kb


async def require_user_name(
    event: Message | CallbackQuery,
    *,
    prompt_text: str | None = None,
    button_text: str = "Начать регистрацию 📸",
) -> bool:
    """
    Ensure user has a non-empty name.
    Returns True if name exists; otherwise prompts to set name and returns False.
    """
    tg_id = event.from_user.id if event.from_user else None
    user = await get_user_by_tg_id(int(tg_id)) if tg_id else None
    if user and (user.get("name") or "").strip():
        return True

    text = prompt_text or REGISTRATION_INTRO_TEXT
    kb = _build_add_name_kb(button_text)

    if isinstance(event, CallbackQuery) and event.message:
        try:
            await event.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            try:
                await event.message.edit_caption(
                    caption=text,
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await event.message.delete()
                except Exception:
                    pass
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
            ui_state = await get_user_ui_state(int(tg_id)) if tg_id else None
        except Exception:
            ui_state = None
        target_msg_id = None
        if ui_state:
            target_msg_id = ui_state.get("screen_msg_id") or ui_state.get("menu_msg_id")
        if target_msg_id:
            try:
                await event.bot.edit_message_text(
                    chat_id=event.chat.id,
                    message_id=int(target_msg_id),
                    text=text,
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await event.bot.edit_message_caption(
                        chat_id=event.chat.id,
                        message_id=int(target_msg_id),
                        caption=text,
                        reply_markup=kb.as_markup(),
                        parse_mode="HTML",
                    )
                except Exception:
                    try:
                        await event.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
                    except Exception:
                        pass
        else:
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
