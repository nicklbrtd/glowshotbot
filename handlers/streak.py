"""GlowShot — Streak (🔥)

UI-only handler.
Persistence lives in database.py (PostgreSQL via asyncpg).

Access:
- Only via inline buttons from Profile (no slash commands).

Integration points for other handlers:
- call `await streak_record_action_by_tg_id(tg_id, 'rate'|'comment'|'upload')`.
"""

from __future__ import annotations

import os
import html
import traceback

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest

from database import (
    get_user_by_tg_id,
    streak_get_status_by_tg_id,
    streak_rollover_if_needed_by_tg_id,
    streak_record_action_by_tg_id,
    streak_toggle_notify_by_tg_id,
)
from keyboards.common import build_back_kb

router = Router(name="streak")

DAILY_GOAL_RATE_COUNT = int(os.getenv("STREAK_DAILY_RATINGS", "3"))
DAILY_GOAL_COMMENT_COUNT = int(os.getenv("STREAK_DAILY_COMMENTS", "1"))
DAILY_GOAL_UPLOAD_COUNT = int(os.getenv("STREAK_DAILY_UPLOADS", "1"))
GRACE_HOURS = int(os.getenv("STREAK_GRACE_HOURS", "6"))


# -------------------- Reusable helpers (used by Profile UI too) --------------------

def render_streak_text_from_dict(d: dict) -> str:
    streak = int(d.get("streak") or 0)
    best = int(d.get("best_streak") or 0)
    freeze = int(d.get("freeze_tokens") or 0)
    last = d.get("last_completed_day") or "—"

    goal_done = bool(d.get("goal_done_today"))
    goal_line = "✅ Дневная цель выполнена" if goal_done else "❌ Дневная цель НЕ выполнена"

    rated_today = int(d.get("rated_today") or 0)
    commented_today = int(d.get("commented_today") or 0)
    uploaded_today = int(d.get("uploaded_today") or 0)

    need_rate = max(0, DAILY_GOAL_RATE_COUNT - rated_today)
    need_comm = max(0, DAILY_GOAL_COMMENT_COUNT - commented_today)
    need_upl = max(0, DAILY_GOAL_UPLOAD_COUNT - uploaded_today)

    how = (
        "Сделай ЛЮБОЕ из этого сегодня:\n"
        f"• 📸 загрузить фото: осталось {need_upl}\n"
        f"• ⭐ оценить фото: осталось {need_rate}\n"
        f"• 💬 оставить коммент: осталось {need_comm}\n"
    )

    notify_enabled = bool(d.get("notify_enabled"))
    nh = int(d.get("notify_hour") or 21)
    nm = int(d.get("notify_minute") or 0)

    return (
        "🔥 <b>GlowShot Streak</b>\n\n"
        f"Текущая серия: <b>{streak}</b>\n"
        f"Лучшая серия: <b>{best}</b>\n"
        f"Freeze: <b>{freeze}</b> 🧊\n"
        f"Последний день с огоньком: <b>{last}</b>\n\n"
        f"{goal_line}\n\n"
        f"Сегодня: ⭐ {rated_today}/{DAILY_GOAL_RATE_COUNT} | "
        f"💬 {commented_today}/{DAILY_GOAL_COMMENT_COUNT} | "
        f"📸 {uploaded_today}/{DAILY_GOAL_UPLOAD_COUNT}\n\n"
        f"{how}\n"
        f"⏳ Грейс после полуночи: <b>{GRACE_HOURS}ч</b>\n"
        f"🔔 Уведомления: <b>{'вкл' if notify_enabled else 'выкл'}</b> ({nh:02d}:{nm:02d})\n"
    )


def build_streak_kb_from_dict(
    d: dict,
    *,
    refresh_cb: str,
    toggle_notify_cb: str,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Обновить", callback_data=refresh_cb)
    kb.button(
        text=("🔔 Уведомления: ВКЛ" if bool(d.get("notify_enabled")) else "🔕 Уведомления: ВЫКЛ"),
        callback_data=toggle_notify_cb,
    )
    if back_cb:
        kb.button(text="⬅️ Назад", callback_data=back_cb)
    kb.adjust(1)
    return kb.as_markup()


async def load_streak_status_dict(tg_id: int) -> dict:
    await streak_rollover_if_needed_by_tg_id(int(tg_id))
    return await streak_get_status_by_tg_id(int(tg_id))


async def get_profile_streak_badge_and_line(tg_id: int) -> tuple[str, str]:
    try:
        s = await load_streak_status_dict(int(tg_id))
        cur_streak = int(s.get("streak") or 0)
        best_streak = int(s.get("best_streak") or 0)
        badge = f" 🔥{cur_streak}" if cur_streak > 0 else ""
        line = f"🔥 Streak: {cur_streak} (best {best_streak})"
        return badge, line
    except Exception:
        return "", ""


async def get_profile_streak_status(tg_id: int) -> dict | None:
    try:
        return await load_streak_status_dict(int(tg_id))
    except Exception:
        return None


async def toggle_profile_streak_notify_and_status(tg_id: int) -> dict | None:
    await streak_toggle_notify_by_tg_id(int(tg_id))
    return await get_profile_streak_status(int(tg_id))


@router.callback_query(F.data == "profile:streak")
async def profile_streak_open(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    tg_id = int(user.get("tg_id") or callback.from_user.id)

    try:
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status)
        kb = build_streak_kb_from_dict(
            status,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    except Exception as e:
        err_name = type(e).__name__
        err_text = str(e)[:180]
        print("[PROFILE_STREAK_ERROR]", err_name, err_text)
        print(traceback.format_exc())

        await callback.message.edit_text(
            "🔥 <b>Streak</b>\n\n"
            "Не получилось загрузить статус streak 😭\n"
            f"Ошибка: <code>{html.escape(err_name)}: {html.escape(err_text)}</code>\n\n"
            "Обычно это либо косяк в БД/миграции streak, либо таймаут соединения. "
            "Скинь этот код из ошибки в логи — и я починю.",
            reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ В профиль"),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data == "profile:streak:refresh")
async def profile_streak_refresh(callback: CallbackQuery):
    tg_id = callback.from_user.id

    try:
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status)
        kb = build_streak_kb_from_dict(
            status,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    finally:
        await callback.answer("Обновил 🔥")


@router.callback_query(F.data == "profile:streak:toggle_notify")
async def profile_streak_toggle_notify(callback: CallbackQuery):
    tg_id = callback.from_user.id

    try:
        await streak_toggle_notify_by_tg_id(int(tg_id))
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status)
        kb = build_streak_kb_from_dict(
            status,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    finally:
        await callback.answer("Ок")
