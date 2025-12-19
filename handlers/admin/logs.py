from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ЛОГИ / ОШИБКИ =================================
# =============================================================

from datetime import datetime
from typing import Optional, Union

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import MASTER_ADMIN_ID
from database import (
    get_user_by_tg_id,
    get_bot_error_logs_page,
    get_bot_error_logs_count,
    clear_bot_error_logs,
)

from .common import (
    _ensure_admin,
    _ensure_user,
)

router = Router()

UserEvent = Union[Message, CallbackQuery]


# =============================================================
# ==== ДОСТУП (ensure_admin) ==================================
# =============================================================

async def _get_from_user(event: UserEvent):
    if isinstance(event, CallbackQuery):
        return event.from_user
    return event.from_user


async def _ensure_user(event: UserEvent) -> Optional[dict]:
    from_user = await _get_from_user(event)
    user = await get_user_by_tg_id(from_user.id)
    if user is None:
        text = "Сначала нужно зарегистрироваться через /start."
        if isinstance(event, CallbackQuery):
            await event.message.answer(text)
        else:
            await event.answer(text)
        return None
    return user


async def _ensure_admin(event: UserEvent) -> Optional[dict]:
    user = await _ensure_user(event)
    if user is None:
        return None

    from_user = await _get_from_user(event)

    # Мастер-админ всегда имеет доступ
    if MASTER_ADMIN_ID and from_user.id == MASTER_ADMIN_ID:
        return user

    if not user.get("is_admin"):
        text = "У тебя нет прав администратора."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None

    return user


# =============================================================
# ==== РЕНДЕР / ХЕЛПЕРЫ =======================================
# =============================================================

_LOGS_PAGE_LIMIT = 10
_MAX_TG_TEXT = 3900  # safe margin for Telegram 4096


def _cut_text(s: str | None, limit: int = _MAX_TG_TEXT) -> str:
    if not s:
        return "—"
    s = str(s)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _fmt_dt_safe(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(dt_str)


async def _render_logs_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    page = max(1, int(page))

    total = await get_bot_error_logs_count()
    total_pages = max(1, (total + _LOGS_PAGE_LIMIT - 1) // _LOGS_PAGE_LIMIT)
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * _LOGS_PAGE_LIMIT
    rows = await get_bot_error_logs_page(offset=offset, limit=_LOGS_PAGE_LIMIT)

    lines: list[str] = [
        "🧾 <b>Логи / ошибки</b>",
        f"Всего записей: <b>{total}</b>",
        f"Страница: <b>{page}</b> / <b>{total_pages}</b>",
        "",
    ]

    if not rows:
        lines.append("Пока нет ошибок. Красота ✨")
    else:
        for r in rows:
            rid = r.get("id")
            created_at = _fmt_dt_safe(r.get("created_at"))
            error_type = r.get("error_type") or "Error"
            handler = r.get("handler") or "—"
            tg_user_id = r.get("tg_user_id")
            update_type = r.get("update_type") or "—"

            lines.append(
                f"<b>#{rid}</b> · {created_at}\n"
                f"• <b>{error_type}</b> в <code>{handler}</code> · {update_type}\n"
                f"• user: <code>{tg_user_id if tg_user_id is not None else '—'}</code>"
            )
            lines.append("")

    text = "\n".join(lines).strip()

    kb = InlineKeyboardBuilder()

    # Кнопки "Подробнее" по каждой записи (до 5, чтобы не раздувать клаву)
    if rows:
        for r in rows[:5]:
            rid = r.get("id")
            if rid is not None:
                kb.button(text=f"🔎 #{rid}", callback_data=f"admin:logs:view:{rid}:{page}")
        kb.adjust(5)

    # пагинация
    prev_cb = f"admin:logs:page:{page-1}" if page > 1 else None
    next_cb = f"admin:logs:page:{page+1}" if page < total_pages else None

    if prev_cb or next_cb:
        if prev_cb:
            kb.button(text="⬅️", callback_data=prev_cb)
        if next_cb:
            kb.button(text="➡️", callback_data=next_cb)

    # действия
    kb.button(text="🧹 Очистить логи", callback_data=f"admin:logs:clear:confirm:{page}")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    # раскладка: (подробности до 5) / (стрелки 2) / (очистить) / (в меню)
    kb.adjust(5, 2, 1, 1)

    return text, kb.as_markup()


# =============================================================
# ==== ВХОД В РАЗДЕЛ ==========================================
# =============================================================

@router.callback_query(F.data == "admin:logs")
async def admin_logs_open(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    text, markup = await _render_logs_page(1)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# =============================================================
# ==== ПАГИНАЦИЯ ==============================================
# =============================================================

@router.callback_query(F.data.startswith("admin:logs:page:"))
async def admin_logs_page(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    page = 1
    if len(parts) >= 4 and parts[3].isdigit():
        page = int(parts[3])

    text, markup = await _render_logs_page(page)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# =============================================================
# ==== ДЕТАЛИ ОШИБКИ ==========================================
# =============================================================

@router.callback_query(F.data.startswith("admin:logs:view:"))
async def admin_logs_view(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    # формат: admin:logs:view:<log_id>:<back_page>
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Не удалось открыть запись.", show_alert=True)
        return

    try:
        log_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный id записи.", show_alert=True)
        return

    back_page = 1
    try:
        back_page = int(parts[4])
    except Exception:
        back_page = 1

    # Без отдельной функции get_by_id: ищем в свежих 200
    row = None
    try:
        recent = await get_bot_error_logs_page(offset=0, limit=200)
        for r in recent:
            if int(r.get("id", -1)) == log_id:
                row = r
                break
    except Exception:
        row = None

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к списку", callback_data=f"admin:logs:page:{back_page}")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    if not row:
        text = "Запись не найдена (возможно, слишком старая)."
        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())
        await callback.answer()
        return

    created_at = _fmt_dt_safe(row.get("created_at"))
    error_type = row.get("error_type") or "Error"
    handler = row.get("handler") or "—"
    update_type = row.get("update_type") or "—"
    chat_id = row.get("chat_id")
    tg_user_id = row.get("tg_user_id")

    error_text = _cut_text(row.get("error_text"), 1200)
    tb = _cut_text(row.get("traceback_text"), _MAX_TG_TEXT)

    text = (
        "🧾 <b>Детали ошибки</b>\n\n"
        f"ID: <code>{row.get('id')}</code>\n"
        f"Когда: <b>{created_at}</b>\n"
        f"Тип: <b>{error_type}</b>\n"
        f"Хендлер: <code>{handler}</code>\n"
        f"Update: <code>{update_type}</code>\n"
        f"chat_id: <code>{chat_id if chat_id is not None else '—'}</code>\n"
        f"user_id: <code>{tg_user_id if tg_user_id is not None else '—'}</code>\n\n"
        f"<b>Сообщение</b>\n<code>{error_text}</code>\n\n"
        f"<b>Traceback</b>\n<code>{tb}</code>"
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except TelegramBadRequest:
        # Иногда Telegram ругается на слишком длинный текст даже после обрезки
        safe_text = _cut_text(text, _MAX_TG_TEXT)
        try:
            await callback.message.edit_text(safe_text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(safe_text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(_cut_text(text, _MAX_TG_TEXT), reply_markup=kb.as_markup())

    await callback.answer()


# =============================================================
# ==== ОЧИСТКА ================================================
# =============================================================

@router.callback_query(F.data.startswith("admin:logs:clear:confirm:"))
async def admin_logs_clear_confirm(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    back_page = 1
    try:
        back_page = int(parts[4])
    except Exception:
        back_page = 1

    text = (
        "🧹 <b>Очистить логи?</b>\n\n"
        "Это удалит <b>все</b> записи ошибок из базы.\n"
        "Если хочешь сохранить историю — лучше сначала скопируй нужные записи."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, очистить", callback_data=f"admin:logs:clear:do:{back_page}")
    kb.button(text="❌ Отмена", callback_data=f"admin:logs:page:{back_page}")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin:logs:clear:do:"))
async def admin_logs_clear_do(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    await clear_bot_error_logs()

    text, markup = await _render_logs_page(1)
    text = "✅ Логи очищены.\n\n" + text

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()