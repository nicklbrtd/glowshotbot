"""GlowShot — Streak (🔥)

UI-only handler.
Persistence lives in database.py (PostgreSQL via asyncpg).

Commands:
- /streak — show streak status
- /checkin — manual test action (counts as comment)

Integration points for other handlers:
- call `await streak_record_action_by_tg_id(tg_id, 'rate'|'comment'|'upload')`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    streak_get_status_by_tg_id,
    streak_rollover_if_needed_by_tg_id,
    streak_record_action_by_tg_id,
    streak_toggle_notify_by_tg_id,
    streak_add_freeze_by_tg_id,
)

router = Router(name="streak")

DAILY_GOAL_RATE_COUNT = int(os.getenv("STREAK_DAILY_RATINGS", "3"))
DAILY_GOAL_COMMENT_COUNT = int(os.getenv("STREAK_DAILY_COMMENTS", "1"))
DAILY_GOAL_UPLOAD_COUNT = int(os.getenv("STREAK_DAILY_UPLOADS", "1"))
GRACE_HOURS = int(os.getenv("STREAK_GRACE_HOURS", "6"))


@dataclass
class StreakStatus:
    tg_id: int
    streak: int
    best_streak: int
    freeze_tokens: int
    last_completed_day: str | None
    today_key: str
    goal_done_today: bool
    rated_today: int
    commented_today: int
    uploaded_today: int
    notify_enabled: bool
    notify_hour: int
    notify_minute: int


def _kb_streak(status: StreakStatus):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Обновить", callback_data="streak:refresh")
    kb.button(text="🧊 +1 Freeze (тест)", callback_data="streak:freeze_add")
    kb.button(
        text=("🔔 Уведомления: ВКЛ" if status.notify_enabled else "🔕 Уведомления: ВЫКЛ"),
        callback_data="streak:toggle_notify",
    )
    kb.adjust(1)
    return kb.as_markup()


def _render_status(status: StreakStatus) -> str:
    goal_line = "✅ Дневная цель выполнена" if status.goal_done_today else "❌ Дневная цель НЕ выполнена"

    need_rate = max(0, DAILY_GOAL_RATE_COUNT - status.rated_today)
    need_comm = max(0, DAILY_GOAL_COMMENT_COUNT - status.commented_today)
    need_upl = max(0, DAILY_GOAL_UPLOAD_COUNT - status.uploaded_today)

    how = (
        "Сделай ЛЮБОЕ из этого сегодня:\n"
        f"• 📸 загрузить фото: осталось {need_upl}\n"
        f"• ⭐ оценить фото: осталось {need_rate}\n"
        f"• 💬 оставить коммент: осталось {need_comm}\n"
    )

    last = status.last_completed_day or "—"

    return (
        "🔥 <b>GlowShot Streak</b>\n\n"
        f"Текущая серия: <b>{status.streak}</b>\n"
        f"Лучшая серия: <b>{status.best_streak}</b>\n"
        f"Freeze: <b>{status.freeze_tokens}</b> 🧊\n"
        f"Последний день с огоньком: <b>{last}</b>\n\n"
        f"{goal_line}\n\n"
        f"Сегодня: ⭐ {status.rated_today}/{DAILY_GOAL_RATE_COUNT} | "
        f"💬 {status.commented_today}/{DAILY_GOAL_COMMENT_COUNT} | "
        f"📸 {status.uploaded_today}/{DAILY_GOAL_UPLOAD_COUNT}\n\n"
        f"{how}\n"
        f"⏳ Грейс после полуночи: <b>{GRACE_HOURS}ч</b>\n"
        f"🔔 Уведомления: <b>{'вкл' if status.notify_enabled else 'выкл'}</b> ({status.notify_hour:02d}:{status.notify_minute:02d})\n"
    )


async def _load_status(tg_id: int) -> StreakStatus:
    d = await streak_get_status_by_tg_id(int(tg_id))
    return StreakStatus(
        tg_id=int(tg_id),
        streak=int(d.get("streak") or 0),
        best_streak=int(d.get("best_streak") or 0),
        freeze_tokens=int(d.get("freeze_tokens") or 0),
        last_completed_day=d.get("last_completed_day"),
        today_key=str(d.get("today_key")),
        goal_done_today=bool(d.get("goal_done_today")),
        rated_today=int(d.get("rated_today") or 0),
        commented_today=int(d.get("commented_today") or 0),
        uploaded_today=int(d.get("uploaded_today") or 0),
        notify_enabled=bool(d.get("notify_enabled")),
        notify_hour=int(d.get("notify_hour") or 21),
        notify_minute=int(d.get("notify_minute") or 0),
    )


@router.message(Command("streak"))
async def cmd_streak(message: Message):
    tg_id = message.from_user.id
    await streak_rollover_if_needed_by_tg_id(int(tg_id))
    status = await _load_status(int(tg_id))
    await message.answer(_render_status(status), reply_markup=_kb_streak(status))


@router.callback_query(F.data == "streak:refresh")
async def cb_refresh(query: CallbackQuery):
    tg_id = query.from_user.id
    await streak_rollover_if_needed_by_tg_id(int(tg_id))
    status = await _load_status(int(tg_id))
    await query.message.edit_text(_render_status(status), reply_markup=_kb_streak(status))
    await query.answer("Обновил 🔥")


@router.callback_query(F.data == "streak:toggle_notify")
async def cb_toggle_notify(query: CallbackQuery):
    tg_id = query.from_user.id
    await streak_toggle_notify_by_tg_id(int(tg_id))
    status = await _load_status(int(tg_id))
    await query.message.edit_text(_render_status(status), reply_markup=_kb_streak(status))
    await query.answer("Ок")


@router.callback_query(F.data == "streak:freeze_add")
async def cb_freeze_add(query: CallbackQuery):
    tg_id = query.from_user.id
    await streak_add_freeze_by_tg_id(int(tg_id), 1)
    status = await _load_status(int(tg_id))
    await query.message.edit_text(_render_status(status), reply_markup=_kb_streak(status))
    await query.answer("+1 🧊")


@router.message(Command("checkin"))
async def cmd_checkin(message: Message):
    tg_id = message.from_user.id
    payload = await streak_record_action_by_tg_id(int(tg_id), "comment")
    status = await _load_status(int(tg_id))

    if payload.get("streak_changed"):
        await message.answer(
            f"🔥 ОГОНЁК ЗАЖЁГСЯ! Серия теперь: <b>{payload.get('streak')}</b>\n\n" + _render_status(status),
            reply_markup=_kb_streak(status),
        )
    else:
        await message.answer(
            "Ок, отметил активность ✅\n\n" + _render_status(status),
            reply_markup=_kb_streak(status),
        )