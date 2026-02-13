from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now
from database import (
    get_tech_mode_state,
    set_tech_mode_state,
    get_update_mode_state,
    set_update_mode_state,
)

from .common import _ensure_admin, edit_or_answer


router = Router(name="admin_settings")


def _tech_countdown_minutes(state: dict) -> int | None:
    if not bool(state.get("tech_enabled")):
        return None
    start_at_raw = state.get("tech_start_at")
    if not start_at_raw:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start_at_raw))
    except Exception:
        return None
    now = get_moscow_now()
    if now >= start_dt:
        return 0
    return max(1, int((start_dt - now).total_seconds() // 60))


def _fmt_tech_state(state: dict) -> str:
    enabled = bool(state.get("tech_enabled"))
    start_at_raw = state.get("tech_start_at")
    if not enabled:
        return "выключен"
    if start_at_raw:
        try:
            start_dt = datetime.fromisoformat(str(start_at_raw))
        except Exception:
            start_dt = None
        if start_dt:
            now = get_moscow_now()
            if now < start_dt:
                mins = max(1, int((start_dt - now).total_seconds() // 60))
                return f"запланирован через {mins} мин (с {start_dt.strftime('%H:%M')})"
            return f"включен (с {start_dt.strftime('%H:%M')})"
    return "включен"


def _kb_settings(state: dict, update_state: dict):
    kb = InlineKeyboardBuilder()
    enabled = bool(state.get("tech_enabled"))
    if enabled:
        kb.button(text="🔴 Выключить тех.режим", callback_data="admin:settings:tech:off")
    else:
        kb.button(text="🟢 Включить тех.режим", callback_data="admin:settings:tech:on")
    upd_enabled = bool(update_state.get("update_enabled"))
    if upd_enabled:
        kb.button(text="🟠 Выключить обновление", callback_data="admin:settings:update:off")
    else:
        kb.button(text="🟢 Включить обновление", callback_data="admin:settings:update:on")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


@router.callback_query(F.data == "admin:settings")
async def admin_settings_open(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    tech_state = await get_tech_mode_state()
    update_state = await get_update_mode_state()
    countdown = _tech_countdown_minutes(tech_state)
    extra = ""
    if countdown is not None and countdown > 0:
        extra = f"До старта: <b>{countdown} мин</b>\n"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Тех.режим: <b>{_fmt_tech_state(tech_state)}</b>\n"
        f"{extra}"
        f"Обновление: <b>{'включено' if update_state.get('update_enabled') else 'выключено'}</b>\n"
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_settings",
        text=text,
        reply_markup=_kb_settings(tech_state, update_state),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:settings:tech:on")
async def admin_settings_tech_on(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    start_at = (get_moscow_now() + timedelta(minutes=5)).isoformat()
    await set_tech_mode_state(enabled=True, start_at=start_at)

    tech_state = await get_tech_mode_state()
    countdown = _tech_countdown_minutes(tech_state)
    extra = ""
    if countdown is not None and countdown > 0:
        extra = f"До старта: <b>{countdown} мин</b>\n"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Тех.режим: <b>{_fmt_tech_state(tech_state)}</b>\n"
        f"{extra}"
    )
    upd_state = await get_update_mode_state()
    text += f"Обновление: <b>{'включено' if upd_state.get('update_enabled') else 'выключено'}</b>\n"
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_settings",
        text=text,
        reply_markup=_kb_settings(tech_state, upd_state),
    )
    await callback.answer("Через 5 минут начнутся тех работы!", show_alert=True)


@router.callback_query(F.data == "admin:settings:tech:off")
async def admin_settings_tech_off(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_tech_mode_state(enabled=False, start_at=None)
    tech_state = await get_tech_mode_state()
    countdown = _tech_countdown_minutes(tech_state)
    extra = ""
    if countdown is not None and countdown > 0:
        extra = f"До старта: <b>{countdown} мин</b>\n"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Тех.режим: <b>{_fmt_tech_state(tech_state)}</b>\n"
        f"{extra}"
    )
    upd_state = await get_update_mode_state()
    text += f"Обновление: <b>{'включено' if upd_state.get('update_enabled') else 'выключено'}</b>\n"
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_settings",
        text=text,
        reply_markup=_kb_settings(tech_state, upd_state),
    )
    await callback.answer("Тех.режим выключен")


@router.callback_query(F.data == "admin:settings:update:on")
async def admin_settings_update_on(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_update_mode_state(enabled=True, notice_text=None, bump_version=True)
    tech_state = await get_tech_mode_state()
    update_state = await get_update_mode_state()
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Тех.режим: <b>{_fmt_tech_state(tech_state)}</b>\n"
        f"Обновление: <b>включено</b>\n"
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_settings",
        text=text,
        reply_markup=_kb_settings(tech_state, update_state),
    )
    await callback.answer("Режим обновления включён. Пользователи увидят уведомление один раз.", show_alert=True)


@router.callback_query(F.data == "admin:settings:update:off")
async def admin_settings_update_off(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await set_update_mode_state(enabled=False, notice_text=None, bump_version=False)
    tech_state = await get_tech_mode_state()
    update_state = await get_update_mode_state()
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Тех.режим: <b>{_fmt_tech_state(tech_state)}</b>\n"
        f"Обновление: <b>выключено</b>\n"
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_settings",
        text=text,
        reply_markup=_kb_settings(tech_state, update_state),
    )
    await callback.answer("Режим обновления выключен", show_alert=False)
