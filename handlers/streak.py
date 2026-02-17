"""GlowShot — Streak (🔥)

UI-only handler.
Persistence lives in database.py (PostgreSQL via asyncpg).

Access:
- Only via inline buttons from Profile (no slash commands).

Integration points for other handlers:
- call `await streak_record_action_by_tg_id(tg_id, 'rate'|'comment'|'upload')`.
"""

from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import (
    get_user_by_tg_id,
    get_user_stats_overview,
    is_user_premium_active,
    streak_get_status_by_tg_id,
    streak_rollover_if_needed_by_tg_id,
    streak_toggle_notify_by_tg_id,
)
from utils.i18n import t
from config import (
    STREAK_DAILY_RATINGS,
    STREAK_DAILY_COMMENTS,
    STREAK_DAILY_UPLOADS,
    STREAK_GRACE_HOURS,
)

router = Router(name="streak")

DAILY_GOAL_RATE_COUNT = int(STREAK_DAILY_RATINGS)
DAILY_GOAL_COMMENT_COUNT = int(STREAK_DAILY_COMMENTS)
DAILY_GOAL_UPLOAD_COUNT = int(STREAK_DAILY_UPLOADS)
GRACE_HOURS = int(STREAK_GRACE_HOURS)


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
    all_done = (
        rated_today >= DAILY_GOAL_RATE_COUNT
        or commented_today >= DAILY_GOAL_COMMENT_COUNT
        or uploaded_today >= DAILY_GOAL_UPLOAD_COUNT
    )
    prefix_mark = "✅" if all_done else "❌"

    lines.append(
        f"{prefix_mark} Задания: "
        f"⭐️ {rated_today}/{DAILY_GOAL_RATE_COUNT} | "
        f"💬 {commented_today}/{DAILY_GOAL_COMMENT_COUNT} | "
        f"📸 {uploaded_today}/{DAILY_GOAL_UPLOAD_COUNT}"
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
    await _open_profile_stats(callback)


def _stats_screen_kb(lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    back_text = "⬅️ Back" if lang == "en" else "⬅️ Назад"
    home_text = "🏠 Home" if lang == "en" else "🏠 В меню"
    kb.row(
        InlineKeyboardButton(text=back_text, callback_data="menu:profile"),
        InlineKeyboardButton(text=home_text, callback_data="menu:back"),
    )
    return kb.as_markup()


def _fmt_avg(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _status_label(*, premium_active: bool, is_author: bool, lang: str) -> str:
    if lang == "en":
        if premium_active and is_author:
            return "💎 Premium + Author"
        if premium_active:
            return "💎 Premium"
        if is_author:
            return "✍️ Author"
        return "🟢 Member"

    if premium_active and is_author:
        return "💎 Premium + Автор"
    if premium_active:
        return "💎 Premium"
    if is_author:
        return "✍️ Автор"
    return "🟢 Участник"


def _render_profile_stats_text(stats: dict, *, premium_active: bool, is_author: bool, lang: str) -> str:
    votes_given = int(stats.get("votes_given") or 0)
    photos_uploaded = int(stats.get("photos_uploaded") or 0)
    my_avg_score = stats.get("my_avg_score")
    best_rank = stats.get("best_rank")
    my_votes_total = int(stats.get("my_votes_total") or 0)
    my_views_total = int(stats.get("my_views_total") or 0)
    credits = int(stats.get("credits") or 0)

    lines: list[str] = []
    if lang == "en":
        lines.append("👤 <b>Your Stats</b>")
        lines.append("")
        lines.append(f"🗳 Votes given: <b>{votes_given}</b>")
        lines.append(f"📸 Photos published: <b>{photos_uploaded}</b>")
        lines.append(f"⭐ Avg score of your photos: <b>{_fmt_avg(my_avg_score)}</b>")
        lines.append("")
        lines.append(f"🏆 Best place: <b>{('#' + str(int(best_rank))) if best_rank is not None else '—'}</b>")
        if my_votes_total > 0:
            lines.append(f"📊 Votes on your photos: <b>{my_votes_total}</b>")
        if my_views_total > 0:
            lines.append(f"👁 Views of your photos: <b>{my_views_total}</b>")
        lines.append("")
        lines.append(f"💳 Credits now: <b>{credits}</b>")
        lines.append(f"📌 Status: <b>{_status_label(premium_active=premium_active, is_author=is_author, lang=lang)}</b>")
    else:
        lines.append("👤 <b>Твоя статистика</b>")
        lines.append("")
        lines.append(f"🗳 Оценок поставлено: <b>{votes_given}</b>")
        lines.append(f"📸 Фото опубликовано: <b>{photos_uploaded}</b>")
        lines.append(f"⭐ Средняя оценка твоих фото: <b>{_fmt_avg(my_avg_score)}</b>")
        lines.append("")
        lines.append(f"🏆 Лучшее место: <b>{('#' + str(int(best_rank))) if best_rank is not None else '—'}</b>")
        if my_votes_total > 0:
            lines.append(f"📊 Голосов на твои фото: <b>{my_votes_total}</b>")
        if my_views_total > 0:
            lines.append(f"👁 Показов твоих фото: <b>{my_views_total}</b>")
        lines.append("")
        lines.append(f"💳 Кредиты сейчас: <b>{credits}</b>")
        lines.append(f"📌 Статус: <b>{_status_label(premium_active=premium_active, is_author=is_author, lang=lang)}</b>")

    if premium_active:
        votes_7d = int(stats.get("votes_7d") or 0)
        active_days_7d = int(stats.get("active_days_7d") or 0)
        lines.append("")
        if lang == "en":
            lines.append(f"📈 Votes in 7 days: <b>{votes_7d}</b>")
            lines.append(f"🎯 Active days in 7: <b>{active_days_7d}/7</b>")
        else:
            lines.append(f"📈 Оценок за 7 дней: <b>{votes_7d}</b>")
            lines.append(f"🎯 Активных дней за 7: <b>{active_days_7d}/7</b>")

    if is_author:
        avg_rank = stats.get("avg_rank")
        top10_count = int(stats.get("top10_count") or 0)
        positive_percent = stats.get("positive_percent")
        avg_rank_text = f"#{_fmt_avg(avg_rank)}" if avg_rank is not None else "—"
        top10_text = str(top10_count) if top10_count > 0 else "—"
        positive_text = f"{int(positive_percent)}%" if positive_percent is not None else "—"

        lines.append("")
        if lang == "en":
            lines.append(f"📌 Avg place in finals: <b>{avg_rank_text}</b>")
            lines.append(f"🏆 Top-10 entries: <b>{top10_text}</b>")
            lines.append(f"🎯 Positive ratings: <b>{positive_text}</b>")
        else:
            lines.append(f"📌 Среднее место в итогах: <b>{avg_rank_text}</b>")
            lines.append(f"🏆 Попаданий в топ-10: <b>{top10_text}</b>")
            lines.append(f"🎯 Положительных оценок: <b>{positive_text}</b>")

    return "\n".join(lines)


async def _open_profile_stats(callback: CallbackQuery, *, toast: str | None = None) -> None:
    user = await get_user_by_tg_id(callback.from_user.id)
    fallback_lang = "ru" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("ru") else "en"
    if user is None:
        await callback.answer(t("streak.user_missing", fallback_lang), show_alert=True)
        return

    lang = _get_lang(user)
    tg_id = int(user.get("tg_id") or callback.from_user.id)
    is_author = bool(user.get("is_author"))
    premium_active = bool(user.get("is_premium"))
    try:
        premium_active = premium_active or bool(await is_user_premium_active(int(tg_id)))
    except Exception:
        pass

    try:
        stats = await get_user_stats_overview(
            int(user["id"]),
            include_premium_metrics=premium_active,
            include_author_metrics=is_author,
        )
        text = _render_profile_stats_text(stats, premium_active=premium_active, is_author=is_author, lang=lang)
        kb = _stats_screen_kb(lang)
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    except Exception:
        err_text = "Не удалось загрузить статистику." if lang != "en" else "Failed to load stats."
        kb = _stats_screen_kb(lang)
        try:
            await callback.message.edit_text(err_text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    finally:
        if toast:
            await callback.answer(toast)
        else:
            await callback.answer()


@router.callback_query(F.data == "profile:streak:refresh")
async def profile_streak_refresh(callback: CallbackQuery):
    toast = "Updated" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("en") else "Обновлено"
    await _open_profile_stats(callback, toast=toast)


@router.callback_query(F.data == "profile:streak:toggle_notify")
async def profile_streak_toggle_notify(callback: CallbackQuery):
    # Streak screen is hidden in UI; keep callback for backward compatibility.
    toast = "Stats opened" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("en") else "Открыта статистика"
    await _open_profile_stats(callback, toast=toast)


@router.callback_query(F.data == "profile:streak:toggle_visibility")
async def profile_streak_toggle_visibility(callback: CallbackQuery):
    # Streak screen is hidden in UI; keep callback for backward compatibility.
    toast = "Stats opened" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("en") else "Открыта статистика"
    await _open_profile_stats(callback, toast=toast)


@router.callback_query(F.data == "profile:streak:freeze")
async def profile_streak_freeze(callback: CallbackQuery):
    # Streak screen is hidden in UI; keep callback for backward compatibility.
    toast = "Stats opened" if (getattr(callback.from_user, "language_code", "") or "").lower().startswith("en") else "Открыта статистика"
    await _open_profile_stats(callback, toast=toast)
