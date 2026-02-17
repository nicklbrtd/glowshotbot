import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

from database import get_user_ui_state, set_user_banner_msg_id
from keyboards.common import build_section_menu
from utils.time import get_moscow_today


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
    reason: str | None = None,
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
    original_banner_id = old_banner

    sent_id: int | None = None
    can_edit_inline = isinstance(reply_markup, InlineKeyboardMarkup) and not force_new
    can_apply_reply = isinstance(reply_markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove)) and not force_new
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

    # Для reply-клавиатуры не пересоздаём баннер без необходимости:
    # - держим/редактируем существующий баннер;
    # - применяем reply-markup скрытым служебным сообщением.
    if can_apply_reply and old_banner:
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
                if not send_if_missing:
                    sent_id = int(old_banner)
            else:
                sent_id = int(old_banner)
        except Exception:
            sent_id = int(old_banner)

        if sent_id is not None:
            try:
                tmp = await bot.send_message(
                    chat_id=chat_id,
                    text="\u2060",
                    reply_markup=reply_markup,
                    disable_notification=True,
                )
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(tmp.message_id))
                except Exception:
                    pass
            except Exception:
                # Баннер уже сохранён, поэтому просто не валим поток из-за keyboard apply.
                pass

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
                    "old_banner_id": original_banner_id,
                    "reason": reason,
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
            # Всегда пытаемся удалить предыдущий баннер, если уверены, что это именно он.
            if original_banner_id and original_banner_id != sent_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(original_banner_id))
                except Exception as e:
                    logger.warning(
                        "giraffe_banner.delete_old_failed",
                        extra={
                            "chat_id": chat_id,
                            "tg_id": tg_id,
                            "old_banner_id": original_banner_id,
                            "new_banner_id": sent_id,
                            "error": str(e),
                            "reason": reason,
                        },
                    )
        else:
            # Sending failed — fall back to the previous banner if it exists.
            sent_id = int(old_banner) if old_banner is not None else None

    if sent_id is not None:
        try:
            await set_user_banner_msg_id(int(tg_id), int(sent_id))
        except Exception:
            pass

    return sent_id


async def sync_giraffe_section_nav(
    bot: Bot,
    chat_id: int,
    tg_id: int,
    *,
    section: str,
    lang: str = "ru",
    force_new: bool = False,
) -> int | None:
    """
    Обновить навигационную reply-клавиатуру для текущего раздела и удержать один баннер «🦒».
    """
    # Если пользователь вернулся в другой день — обновляем баннер полностью.
    if not force_new:
        try:
            ui_state = await get_user_ui_state(int(tg_id))
            updated_at_raw = ui_state.get("updated_at")
            if updated_at_raw:
                updated_at = (
                    updated_at_raw
                    if isinstance(updated_at_raw, datetime)
                    else datetime.fromisoformat(str(updated_at_raw))
                )
                if str(updated_at.date()) != str(get_moscow_today()):
                    force_new = True
        except Exception:
            pass

    kb = build_section_menu(section=section, lang=lang)
    return await ensure_giraffe_banner(
        bot=bot,
        chat_id=chat_id,
        tg_id=tg_id,
        text="🦒",
        reply_markup=kb,
        force_new=force_new,
        send_if_missing=True,
        reason=f"section_nav:{section}",
    )
