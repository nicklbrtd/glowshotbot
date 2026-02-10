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
    """Ensure a single giraffe banner exists above section content."""
    banner_id = None
    try:
        ui_state = await get_user_ui_state(int(tg_id))
        banner_id = ui_state.get("banner_msg_id")
        if banner_id is None:
            banner_id = ui_state.get("rate_kb_msg_id")
            if banner_id:
                try:
                    await set_user_banner_msg_id(int(tg_id), int(banner_id))
                except Exception:
                    pass
    except Exception:
        banner_id = None

    if banner_id and not force_new:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(banner_id),
                text=text,
                reply_markup=reply_markup,
            )
            return int(banner_id)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if (
                "message is not modified" in msg
                or "message can't be edited" in msg
                or "reply markup" in msg
                or "inline keyboard" in msg
            ):
                return int(banner_id)
            if "message to edit not found" in msg or "message_id invalid" in msg:
                banner_id = None
            else:
                return int(banner_id)
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(banner_id))
            except Exception:
                pass
    elif banner_id and force_new:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(banner_id))
        except Exception:
            pass

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
        )
    except Exception:
        return None

    try:
        await set_user_banner_msg_id(int(tg_id), sent.message_id)
    except Exception:
        pass

    return sent.message_id
