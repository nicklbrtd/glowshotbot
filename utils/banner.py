import logging

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

from database import get_user_ui_state, set_user_banner_msg_id


logger = logging.getLogger(__name__)


async def ensure_giraffe_banner(
    bot: Bot,
    chat_id: int,
    tg_id: int,
    *,
    text: str = "🦒",
    reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | InlineKeyboardMarkup | None = None,
    force_new: bool = False,
    send_if_missing: bool = True,
) -> int | None:
    """
    Keep a single giraffe banner per user:
    - try to update the existing one when possible;
    - if it is too old or missing, send a new banner and delete the stale one;
    - always remember the latest banner id.
    For InlineKeyboardMarkup we edit the existing banner when possible.
    For ReplyKeyboardMarkup/Remove we send a banner message to apply the keyboard.
    When reply_markup is None we prefer editing the existing banner to avoid deleting
    a previous banner that could be anchoring a reply keyboard.
    """
    try:
        ui_state = await get_user_ui_state(int(tg_id))
        old_banner = ui_state.get("banner_msg_id")
    except Exception:
        old_banner = None

    sent_id: int | None = None
    can_edit_inline = isinstance(reply_markup, InlineKeyboardMarkup) and not force_new
    # Если reply_markup не задан, не нужно спамить новыми баннерами.
    # Иначе можно случайно удалить предыдущий баннер, который держал ReplyKeyboard (оценки).
    can_edit_plain = reply_markup is None and not force_new
    touch_mode = reply_markup is None and not send_if_missing

    if can_edit_plain and old_banner:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(old_banner),
                text=text,
            )
            sent_id = int(old_banner)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                sent_id = int(old_banner)
            elif "message to edit not found" in msg or "message_id invalid" in msg:
                old_banner = None
            elif "message can't be edited" in msg:
                # Too old. В touch-режиме НЕ создаём новый баннер (иначе он уедет вниз под карточку).
                if not send_if_missing:
                    sent_id = int(old_banner)
                else:
                    # We'll send a new banner below.
                    pass
            else:
                sent_id = int(old_banner)
        except Exception:
            sent_id = int(old_banner)

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
                if not send_if_missing:
                    sent_id = int(old_banner)
                else:
                    # Too old — create a fresh banner below.
                    pass
            else:
                # Unexpected error: keep old banner id to avoid spamming.
                sent_id = int(old_banner)
        except Exception:
            sent_id = int(old_banner) if old_banner else None

    if sent_id is None:
        # В touch-режиме мы НЕ создаём новый баннер. Просто возвращаем старый id (если есть).
        if touch_mode:
            return int(old_banner) if old_banner is not None else None

        if not send_if_missing:
            return int(old_banner) if old_banner is not None else None

        sent = None
        try:
            logger.info(
                "giraffe_banner.send_message",
                extra={
                    "chat_id": chat_id,
                    "tg_id": tg_id,
                    "force_new": force_new,
                    "reply_markup": type(reply_markup).__name__ if reply_markup else None,
                    "send_if_missing": send_if_missing,
                },
            )
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
            )
        except Exception:
            sent = None

        if sent is not None:
            sent_id = int(sent.message_id)
            # Удаляем старый баннер только когда мы действительно отправили новый с reply_markup.
            # Если reply_markup=None, старый мог быть «якорем» ReplyKeyboard, и его удаление ломает кнопки.
            if (
                old_banner
                and old_banner != sent_id
                and reply_markup is not None
            ):
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
