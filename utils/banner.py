from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

from database import get_user_ui_state, set_user_banner_msg_id


async def ensure_giraffe_banner(
    bot: Bot,
    chat_id: int,
    tg_id: int,
    *,
    text: str = "🦒",
    reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | InlineKeyboardMarkup | None = None,
    force_new: bool = False,
) -> int | None:
    """
    Send a fresh giraffe banner and delete the previous one to keep it on top.
    No in-place edits — always a new message.
    """
    old_banner = None
    try:
        ui_state = await get_user_ui_state(int(tg_id))
        old_banner = ui_state.get("banner_msg_id")
    except Exception:
        old_banner = None

    sent = None
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
        )
    except Exception:
        sent = None

    if sent:
        try:
            await set_user_banner_msg_id(int(tg_id), sent.message_id)
        except Exception:
            pass

    if old_banner:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(old_banner))
        except Exception:
            pass

    return sent.message_id if sent else None
