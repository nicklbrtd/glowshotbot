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
    Keep a single giraffe banner per user:
    - try to update the existing one when possible;
    - if it is too old or missing, send a new banner and delete the stale one;
    - always remember the latest banner id.
    Editing Telegram messages is only allowed with inline keyboards, so for
    reply keyboards we always send a fresh banner.
    """
    try:
        ui_state = await get_user_ui_state(int(tg_id))
        old_banner = ui_state.get("banner_msg_id")
    except Exception:
        old_banner = None

    sent_id: int | None = None
    can_edit_inline = isinstance(reply_markup, InlineKeyboardMarkup) and not force_new

    if can_edit_inline and old_banner:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(old_banner),
                text=text,
                reply_markup=reply_markup,
            )
            sent_id = int(old_banner)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                sent_id = int(old_banner)
            elif "message to edit not found" in msg or "message_id invalid" in msg:
                old_banner = None
            elif "message can't be edited" in msg:
                # Too old — create a fresh banner below.
                pass
            else:
                # Unexpected error: keep old banner id to avoid spamming.
                sent_id = int(old_banner)
        except Exception:
            sent_id = int(old_banner) if old_banner else None

    if sent_id is None:
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
            sent_id = int(sent.message_id)
            if old_banner and old_banner != sent_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(old_banner))
                except Exception:
                    pass
        else:
            # Sending failed — fall back to the previous banner if it exists.
            sent_id = int(old_banner) if old_banner is not None else None

    if sent_id is not None:
        try:
            await set_user_banner_msg_id(int(tg_id), int(sent_id))
        except Exception:
            pass

    return sent_id
