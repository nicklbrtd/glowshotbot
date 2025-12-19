

from __future__ import annotations

# =============================================================
# ==== ОБЩЕЕ ДЛЯ АДМИНКИ (helpers / types) ====================
# =============================================================

from typing import Optional, Union

from aiogram.types import Message, CallbackQuery

from config import MASTER_ADMIN_ID
from database import get_user_by_tg_id


UserEvent = Union[Message, CallbackQuery]


def _safe_int(v) -> int:
    """Безопасно привести значение к int."""
    try:
        return int(v or 0)
    except Exception:
        return 0


async def _get_from_user(event: UserEvent):
    """Вернуть Telegram user из Message/CallbackQuery."""
    if isinstance(event, CallbackQuery):
        return event.from_user
    return event.from_user


async def _ensure_user(event: UserEvent) -> Optional[dict]:
    """Проверить, что пользователь существует в БД (зарегистрирован)."""
    from_user = await _get_from_user(event)
    user = await get_user_by_tg_id(from_user.id)

    if user is None:
        text = "Сначала нужно зарегистрироваться через /start."
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
            except Exception:
                pass
            try:
                await event.message.answer(text)
            except Exception:
                pass
        else:
            try:
                await event.answer(text)
            except Exception:
                pass
        return None

    return user


async def _ensure_admin(event: UserEvent) -> Optional[dict]:
    """Проверить права администратора (включая MASTER_ADMIN_ID)."""
    user = await _ensure_user(event)
    if user is None:
        return None

    from_user = await _get_from_user(event)

    # MASTER_ADMIN_ID имеет доступ всегда
    if MASTER_ADMIN_ID and from_user.id == MASTER_ADMIN_ID:
        return user

    if not user.get("is_admin"):
        text = "У тебя нет прав администратора."
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
            except Exception:
                pass
        else:
            try:
                await event.answer(text)
            except Exception:
                pass
        return None

    return user


__all__ = [
    "UserEvent",
    "_safe_int",
    "_get_from_user",
    "_ensure_user",
    "_ensure_admin",
]