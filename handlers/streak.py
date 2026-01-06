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
    streak_toggle_visibility_by_tg_id,
    streak_use_freeze_today,
)
from keyboards.common import build_back_kb
from utils.i18n import t

router = Router(name="streak")

DAILY_GOAL_RATE_COUNT = int(os.getenv("STREAK_DAILY_RATINGS", "3"))
DAILY_GOAL_COMMENT_COUNT = int(os.getenv("STREAK_DAILY_COMMENTS", "1"))
DAILY_GOAL_UPLOAD_COUNT = int(os.getenv("STREAK_DAILY_UPLOADS", "1"))
GRACE_HOURS = int(os.getenv("STREAK_GRACE_HOURS", "6"))


def _get_lang(user: dict | None) -> str:
    """Return "ru" or "en".

    Be defensive: DB/handlers might store language as:
    - lang: "ru" | "en" | "en-US" | "ru-RU"
    - language / language_code
    """
    if not user:
        return "ru"

    try:
        raw = (
            user.get("lang")
            or user.get("language")
            or user.get("language_code")
            or user.get("locale")
        )
        if not raw:
            return "ru"

        s = str(raw).strip().lower()
        # normalize like "en-us" -> "en"
        s = s.split("-")[0]
        if s in ("ru", "en"):
            return s
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

    rated_today = int(d.get("rated_today") or 0)
    commented_today = int(d.get("commented_today") or 0)
    uploaded_today = int(d.get("uploaded_today") or 0)

    need_rate = max(0, DAILY_GOAL_RATE_COUNT - rated_today)
    need_comm = max(0, DAILY_GOAL_COMMENT_COUNT - commented_today)
    need_upl = max(0, DAILY_GOAL_UPLOAD_COUNT - uploaded_today)

    notify_enabled = bool(d.get("notify_enabled"))
    nh = int(d.get("notify_hour") or 21)
    nm = int(d.get("notify_minute") or 0)

    lines: list[str] = []
    lines.append("🔥 <b>GlowShot Streak</b>")
    lines.append("")

    # header stats
    streak_state = "горит" if streak > 0 else "не горит"
    lines.append(f"Текущая серия: <b>{streak}</b> ({streak_state})")
    lines.append(f"Лучшая серия: <b>{best}</b>")
    lines.append(f"Заморозки: <b>{freeze}</b>")
    lines.append("")

    # tasks line
    def _mark(done: bool) -> str:
        return "✅" if done else "❌"

    rate_mark = _mark(rated_today >= DAILY_GOAL_RATE_COUNT)
    comment_mark = _mark(commented_today >= DAILY_GOAL_COMMENT_COUNT)
    upload_mark = _mark(uploaded_today >= DAILY_GOAL_UPLOAD_COUNT)

    lines.append(
        f"Задания: ⭐️ {rate_mark} {rated_today}/{DAILY_GOAL_RATE_COUNT} | "
        f"💬 {comment_mark} {commented_today}/{DAILY_GOAL_COMMENT_COUNT} | "
        f"📸 {upload_mark} {uploaded_today}/{DAILY_GOAL_UPLOAD_COUNT}"
    )
    lines.append("")

    lines.append("Это Streak. Чтобы не терять огонек тебе нужно каждый день выполнять задания. Одного выполненного задания достаточно, чтобы зажечь его.")
    lines.append("Твой Streak виден людям при оценивании, но его можно скрыть кнопкой «Скрыть Streak».")
    lines.append(f"Если ты не успел до полуночи, у тебя есть ещё {GRACE_HOURS} часов, чтобы закрыть день.")
    lines.append("За 111 дней Streak ты получишь награду: 11 дней GlowShot Premium.")

    state = "включены" if notify_enabled else "выключены"
    lines.append("")
    lines.append(f"Уведомления: {state} (время: {nh:02d}:{nm:02d})")

    return "\n".join(lines)


def build_streak_kb_from_dict(
    d: dict,
    lang: str,
    *,
    refresh_cb: str,
    toggle_notify_cb: str,
    toggle_visibility_cb: str,
    freeze_cb: str,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🙈 Скрыть Streak" if d.get("visible", True) else "👀 Показать Streak", callback_data=toggle_visibility_cb)
    kb.button(text="🧊 Заморозка", callback_data=freeze_cb)
    kb.button(text="🔄 Обновить", callback_data=refresh_cb)
    kb.button(
        text=(t("streak.kb.notify.on", lang) if bool(d.get("notify_enabled")) else t("streak.kb.notify.off", lang)),
        callback_data=toggle_notify_cb,
    )
    if back_cb:
        kb.button(text=t("common.back", lang), callback_data=back_cb)
    kb.adjust(2, 2, 1)
    return kb.as_markup()


async def load_streak_status_dict(tg_id: int) -> dict:
    await streak_rollover_if_needed_by_tg_id(int(tg_id))
    return await streak_get_status_by_tg_id(int(tg_id))


async def get_profile_streak_badge_and_line(tg_id: int) -> tuple[str, str]:
    try:
        s = await load_streak_status_dict(int(tg_id))
        if not bool(s.get("visible", True)):
            return "", ""
        user = await get_user_by_tg_id(int(tg_id))
        lang = _get_lang(user)
        cur_streak = int(s.get("streak") or 0)
        best_streak = int(s.get("best_streak") or 0)
        badge = f" 🔥{cur_streak}" if cur_streak > 0 else ""
        # Keep it simple: English for EN, Russian for RU
        if lang == "en":
            line = f"🔥 Streak: {cur_streak} (best {best_streak})"
        else:
            line = f"🔥 Streak: {cur_streak} (лучший {best_streak})"
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
        fallback_lang = "ru" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("ru") else "en"
        await callback.answer(t("streak.user_missing", fallback_lang), show_alert=True)
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
            toggle_visibility_cb="profile:streak:toggle_visibility",
            freeze_cb="profile:streak:freeze",
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
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    try:
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            toggle_visibility_cb="profile:streak:toggle_visibility",
            freeze_cb="profile:streak:freeze",
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
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    try:
        await streak_toggle_notify_by_tg_id(int(tg_id))
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            toggle_visibility_cb="profile:streak:toggle_visibility",
            freeze_cb="profile:streak:freeze",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    finally:
        await callback.answer(t("streak.toast.ok", lang))


@router.callback_query(F.data == "profile:streak:toggle_visibility")
async def profile_streak_toggle_visibility(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    try:
        await streak_toggle_visibility_by_tg_id(int(tg_id))
        status = await load_streak_status_dict(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            toggle_visibility_cb="profile:streak:toggle_visibility",
            freeze_cb="profile:streak:freeze",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    finally:
        await callback.answer()


@router.callback_query(F.data == "profile:streak:freeze")
async def profile_streak_freeze(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    try:
        status = await streak_use_freeze_today(int(tg_id))
        text = render_streak_text_from_dict(status, lang)
        kb = build_streak_kb_from_dict(
            status,
            lang,
            refresh_cb="profile:streak:refresh",
            toggle_notify_cb="profile:streak:toggle_notify",
            toggle_visibility_cb="profile:streak:toggle_visibility",
            freeze_cb="profile:streak:freeze",
            back_cb="menu:profile",
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

        used = bool(status.get("goal_done_today"))
        if used:
            await callback.answer("Заморозка применена.")
        else:
            await callback.answer("Сегодня уже закрыто или нет заморозок.", show_alert=True)
    except Exception:
        try:
            await callback.answer("Не удалось применить заморозку.", show_alert=True)
        except Exception:
            pass
