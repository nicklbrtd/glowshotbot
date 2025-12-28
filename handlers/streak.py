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
from utils.i18n import t

router = Router(name="streak")

DAILY_GOAL_RATE_COUNT = int(os.getenv("STREAK_DAILY_RATINGS", "3"))
DAILY_GOAL_COMMENT_COUNT = int(os.getenv("STREAK_DAILY_COMMENTS", "1"))
DAILY_GOAL_UPLOAD_COUNT = int(os.getenv("STREAK_DAILY_UPLOADS", "1"))
GRACE_HOURS = int(os.getenv("STREAK_GRACE_HOURS", "6"))


def _get_lang(user: dict | None) -> str:
    try:
        if user and user.get("lang") in ("ru", "en"):
            return str(user.get("lang"))
    except Exception:
        pass
    return "ru"


# -------------------- Reusable helpers (used by Profile UI too) --------------------

def render_streak_text_from_dict(d: dict, lang: str) -> str:
    streak = int(d.get("streak") or 0)
    best = int(d.get("best_streak") or 0)
    freeze = int(d.get("freeze_tokens") or 0)
    last = d.get("last_completed_day") or "—"

    goal_done = bool(d.get("goal_done_today"))
    goal_line = t("streak.goal.done", lang) if goal_done else t("streak.goal.not_done", lang)

    rated_today = int(d.get("rated_today") or 0)
    commented_today = int(d.get("commented_today") or 0)
    uploaded_today = int(d.get("uploaded_today") or 0)

    need_rate = max(0, DAILY_GOAL_RATE_COUNT - rated_today)
    need_comm = max(0, DAILY_GOAL_COMMENT_COUNT - commented_today)
    need_upl = max(0, DAILY_GOAL_UPLOAD_COUNT - uploaded_today)

    how = (
        t("streak.how.header", lang) + "\n"
        + t("streak.how.upload", lang, value=need_upl) + "\n"
        + t("streak.how.rate", lang, value=need_rate) + "\n"
        + t("streak.how.comment", lang, value=need_comm) + "\n"
    )

    notify_enabled = bool(d.get("notify_enabled"))
    nh = int(d.get("notify_hour") or 21)
    nm = int(d.get("notify_minute") or 0)

    state = t("streak.notify.on", lang) if notify_enabled else t("streak.notify.off", lang)

    return (
        t("streak.title", lang) + "\n\n"
        + t("streak.current", lang, value=streak) + "\n"
        + t("streak.best", lang, value=best) + "\n"
        + t("streak.freeze", lang, value=freeze) + "\n"
        + t("streak.last_day", lang, value=last) + "\n\n"
        + goal_line + "\n\n"
        + t(
            "streak.today",
            lang,
            rated=rated_today,
            rated_goal=DAILY_GOAL_RATE_COUNT,
            commented=commented_today,
            comment_goal=DAILY_GOAL_COMMENT_COUNT,
            uploaded=uploaded_today,
            upload_goal=DAILY_GOAL_UPLOAD_COUNT,
        )
        + "\n\n"
        + how
        + t("streak.grace", lang, value=GRACE_HOURS) + "\n"
        + t("streak.notify", lang, state=state, hh=f"{nh:02d}", mm=f"{nm:02d}")
    )


def build_streak_kb_from_dict(
    d: dict,
    lang: str,
    *,
    refresh_cb: str,
    toggle_notify_cb: str,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("streak.kb.refresh", lang), callback_data=refresh_cb)
    kb.button(
        text=(t("streak.kb.notify.on", lang) if bool(d.get("notify_enabled")) else t("streak.kb.notify.off", lang)),
        callback_data=toggle_notify_cb,
    )
    if back_cb:
        kb.button(text=t("common.back", lang), callback_data=back_cb)
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
        await callback.answer(t("streak.user_missing", "ru"), show_alert=True)
        return

    tg_id = int(user.get("tg_id") or callback.from_user.id)
    lang = _get_lang(user)

    try:
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
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
            f"{t('streak.error.title', lang)}\n\n"
            f"{t('streak.error.text', lang)}\n"
            f"{t('streak.error.err', lang, value=html.escape(err_name + ': ' + err_text))}\n\n"
            f"{t('streak.error.hint', lang)}",
            reply_markup=build_back_kb(callback_data="menu:profile", text=t("streak.back_profile", lang)),
            parse_mode="HTML",
        )

    await callback.answer()


@router.callback_query(F.data == "profile:streak:refresh")
async def profile_streak_refresh(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    try:
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
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
        await callback.answer(t("streak.toast.refreshed", lang))


@router.callback_query(F.data == "profile:streak:toggle_notify")
async def profile_streak_toggle_notify(callback: CallbackQuery):
    tg_id = callback.from_user.id
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    try:
        await streak_toggle_notify_by_tg_id(int(tg_id))
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
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
        await callback.answer(t("streak.toast.ok", lang))
