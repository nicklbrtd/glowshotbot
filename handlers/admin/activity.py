from __future__ import annotations

from datetime import datetime, timedelta
import time
import traceback
from typing import Any, Awaitable, Callable

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now
from utils.charts import render_activity_chart
from database import (
    get_activity_counts_by_hour,
    get_activity_counts_by_day,
    get_activity_overview,
    get_top_users_activity,
    get_top_sections,
    get_spam_suspects,
    get_errors_summary,
    log_bot_error,
)
from .common import _ensure_admin


router = Router(name="admin_activity")

_ACTIVITY_PREFIX = "admin_activity"
_PERIODS = {"day", "week", "month"}
_TABS = {"overview", "users", "sections", "spam", "errors"}
_CACHE_TTL_SEC = 45.0
_ACTIVITY_CACHE: dict[str, tuple[float, Any]] = {}


def _period_title(period: str) -> str:
    return {
        "day": "День",
        "week": "Неделя",
        "month": "Месяц",
    }.get(period, "День")


def _tab_title(tab: str) -> str:
    return {
        "overview": "📈 Overview",
        "users": "👥 Users",
        "sections": "🧭 Sections",
        "spam": "🧨 Spam/Abuse",
        "errors": "🧯 Errors",
    }.get(tab, "📈 Overview")


def _btn(label: str, selected: bool) -> str:
    return f"✅ {label}" if selected else label


def _kb_activity_menu(tab: str, period: str):
    kb = InlineKeyboardBuilder()
    kb.button(
        text=_btn("📈 Overview", tab == "overview"),
        callback_data="admin:activity:tab:overview",
    )
    kb.button(
        text=_btn("👥 Users", tab == "users"),
        callback_data="admin:activity:tab:users",
    )
    kb.button(
        text=_btn("🧭 Sections", tab == "sections"),
        callback_data="admin:activity:tab:sections",
    )
    kb.button(
        text=_btn("🧨 Spam", tab == "spam"),
        callback_data="admin:activity:tab:spam",
    )
    kb.button(
        text=_btn("🧯 Errors", tab == "errors"),
        callback_data="admin:activity:tab:errors",
    )
    kb.button(
        text=f"📅 Период: {_period_title(period)}",
        callback_data="admin:activity:period",
    )
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def _kb_period_picker(current_period: str):
    kb = InlineKeyboardBuilder()
    kb.button(
        text=_btn("📅 День", current_period == "day"),
        callback_data="admin:activity:set_period:day",
    )
    kb.button(
        text=_btn("🗓 Неделя", current_period == "week"),
        callback_data="admin:activity:set_period:week",
    )
    kb.button(
        text=_btn("🗓 Месяц", current_period == "month"),
        callback_data="admin:activity:set_period:month",
    )
    kb.button(text="⬅️ Назад", callback_data="admin:activity:period_back")
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


async def _get_activity_msg_ids(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get(f"{_ACTIVITY_PREFIX}_chat_id"), data.get(f"{_ACTIVITY_PREFIX}_msg_id")


async def _set_activity_msg_ids(state: FSMContext, chat_id: int, msg_id: int) -> None:
    await state.update_data(**{f"{_ACTIVITY_PREFIX}_chat_id": chat_id, f"{_ACTIVITY_PREFIX}_msg_id": msg_id})


async def _get_tab_period(state: FSMContext) -> tuple[str, str]:
    data = await state.get_data()
    tab = str(data.get(f"{_ACTIVITY_PREFIX}_tab") or "overview")
    period = str(data.get(f"{_ACTIVITY_PREFIX}_period") or "day")
    if tab not in _TABS:
        tab = "overview"
    if period not in _PERIODS:
        period = "day"
    return tab, period


async def _set_tab_period(state: FSMContext, *, tab: str | None = None, period: str | None = None) -> tuple[str, str]:
    current_tab, current_period = await _get_tab_period(state)
    new_tab = tab if tab in _TABS else current_tab
    new_period = period if period in _PERIODS else current_period
    await state.update_data(
        **{
            f"{_ACTIVITY_PREFIX}_tab": new_tab,
            f"{_ACTIVITY_PREFIX}_period": new_period,
        }
    )
    return new_tab, new_period


def _period_window(period: str) -> tuple[datetime, datetime, str]:
    now = get_moscow_now().replace(tzinfo=None)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if period == "week":
        start = end - timedelta(days=7)
        label = f"{start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    elif period == "month":
        start = end - timedelta(days=30)
        label = f"{start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    else:
        start = end - timedelta(days=1)
        label = start.strftime("%d.%m.%Y")
    return start, end, label


def _cache_key(name: str, start: datetime, end: datetime, extra: str = "") -> str:
    return f"{name}|{start.isoformat()}|{end.isoformat()}|{extra}"


async def _cached(key: str, loader: Callable[[], Awaitable[Any]]) -> Any:
    now = time.monotonic()
    cached = _ACTIVITY_CACHE.get(key)
    if cached and (now - cached[0]) <= _CACHE_TTL_SEC:
        return cached[1]
    value = await loader()
    _ACTIVITY_CACHE[key] = (now, value)
    return value


async def _cleanup_legacy_chart_msg(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    legacy_id = data.get("activity_chart_msg_id")
    if legacy_id:
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=int(legacy_id),
            )
        except Exception:
            pass
        try:
            await state.update_data(activity_chart_msg_id=None)
        except Exception:
            pass


async def _upsert_activity_text(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    text: str,
    reply_markup,
) -> None:
    if callback.message is None:
        return
    bot = callback.message.bot
    chat_id, msg_id = await _get_activity_msg_ids(state)
    if chat_id and msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    sent = await bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_notification=True,
    )
    await _set_activity_msg_ids(state, sent.chat.id, sent.message_id)


async def _upsert_activity_photo(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    chart_file: BufferedInputFile,
    caption: str,
    reply_markup,
) -> None:
    if callback.message is None:
        return
    bot = callback.message.bot
    chat_id, msg_id = await _get_activity_msg_ids(state)
    media = InputMediaPhoto(media=chart_file, caption=caption, parse_mode="HTML")

    if chat_id and msg_id:
        try:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=msg_id,
                media=media,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    sent = await bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=chart_file,
        caption=caption,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_notification=True,
    )
    await _set_activity_msg_ids(state, sent.chat.id, sent.message_id)


def _normalize_bucket(value: object, *, by: str) -> datetime | None:
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        if by == "hour":
            return dt.replace(minute=0, second=0, microsecond=0)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def _format_peak_lines(counts: list[int], labels: list[str], *, kind: str) -> list[str]:
    if not counts or not labels:
        return []

    max_val = max(counts)
    if max_val <= 0:
        return ["Пиков нет: активности не было."]

    pairs = [(i, counts[i]) for i in range(min(len(counts), len(labels))) if counts[i] > 0]
    if not pairs:
        return ["Пиков нет: активности не было."]

    pairs.sort(key=lambda x: (-x[1], x[0]))
    top = pairs[:3]

    def _label(i: int) -> str:
        if kind == "hour":
            try:
                h = int(labels[i])
                return f"{h:02d}:00–{(h + 1):02d}:00"
            except Exception:
                return str(labels[i])
        return str(labels[i])

    peak_label = _label(top[0][0])
    peak_count = top[0][1]
    lines = [f"Пик: {peak_label} — {peak_count}"]

    if len(top) > 1:
        top_str = ", ".join(f"{_label(i)} ({cnt})" for i, cnt in top)
        lines.append(f"Топ-3: {top_str}")
    return lines


def _user_label(row: dict | None) -> str:
    if not row:
        return "—"
    name = (row.get("name") or "").strip()
    username = (row.get("username") or "").strip()
    tg_id = row.get("tg_id")
    code = (row.get("author_code") or "").strip()
    if name:
        base = name
    elif username:
        base = f"@{username}"
    elif tg_id:
        base = f"tg_id {tg_id}"
    else:
        base = "—"
    return f"{base} ({code})" if code else base


def _map_handler_to_section(handler: str | None) -> str:
    h = (handler or "").lower()
    if "rate" in h or "vote" in h:
        return "rate"
    if "upload" in h or "photo" in h:
        return "upload"
    if "profile" in h:
        return "profile"
    if "result" in h:
        return "results"
    if "support" in h or "feedback" in h:
        return "support"
    if "admin" in h:
        return "admin"
    if "start" in h or "menu" in h:
        return "menu"
    return "other"


async def _render_overview(callback: CallbackQuery, state: FSMContext, *, period: str) -> None:
    start, end, label = _period_window(period)
    bucket_kind = "hour" if period == "day" else "day"

    if bucket_kind == "hour":
        rows = await _cached(
            _cache_key("counts_hour", start, end),
            lambda: get_activity_counts_by_hour(start, end),
        )
        by_bucket: dict[datetime, int] = {}
        for r in rows:
            dt = _normalize_bucket(r.get("bucket"), by="hour")
            if dt is not None:
                by_bucket[dt] = int(r.get("cnt") or 0)
        counts: list[int] = []
        labels: list[str] = []
        for h in range(24):
            dt = start + timedelta(hours=h)
            counts.append(by_bucket.get(dt, 0))
            labels.append(f"{h:02d}")
    else:
        rows = await _cached(
            _cache_key("counts_day", start, end),
            lambda: get_activity_counts_by_day(start, end),
        )
        days = 7 if period == "week" else 30
        by_bucket = {}
        for r in rows:
            dt = _normalize_bucket(r.get("bucket"), by="day")
            if dt is not None:
                by_bucket[dt] = int(r.get("cnt") or 0)
        counts = []
        labels = []
        for i in range(days):
            dt = start + timedelta(days=i)
            counts.append(by_bucket.get(dt, 0))
            labels.append(dt.strftime("%d.%m"))

    overview = await _cached(
        _cache_key("overview", start, end),
        lambda: get_activity_overview(start, end),
    )
    peaks = _format_peak_lines(counts, labels, kind=bucket_kind)
    top_user = overview.get("top_user") if isinstance(overview, dict) else None
    top_user_label = _user_label(top_user)
    top_user_cnt = int(overview.get("top_user_cnt") or 0)
    top_section = str(overview.get("top_section") or "—")
    top_section_cnt = int(overview.get("top_section_cnt") or 0)

    caption_lines = [
        "📈 <b>Активность · Overview</b>",
        f"📅 Период: <b>{_period_title(period)}</b> ({label})",
        "",
        f"👤 Unique users: <b>{int(overview.get('unique_users') or 0)}</b>",
        f"🧾 Events total: <b>{int(overview.get('total_events') or 0)}</b>",
        f"🧯 Errors: <b>{int(overview.get('errors_total') or 0)}</b>",
        f"🔥 Топ-раздел: <b>{top_section}</b> — {top_section_cnt}",
        f"👑 Лидер дня: <b>{top_user_label}</b> — {top_user_cnt} действий",
    ]
    if peaks:
        caption_lines.append("")
        caption_lines.extend(peaks)

    chart = render_activity_chart(counts, labels)
    chart_file = BufferedInputFile(chart.getvalue(), filename="activity_overview.png")
    tab, current_period = await _get_tab_period(state)
    await _upsert_activity_photo(
        callback,
        state,
        chart_file=chart_file,
        caption="\n".join(caption_lines),
        reply_markup=_kb_activity_menu(tab, current_period),
    )


async def _render_users(callback: CallbackQuery, state: FSMContext, *, period: str) -> None:
    start, end, label = _period_window(period)
    top_events = await _cached(
        _cache_key("users_events", start, end),
        lambda: get_top_users_activity(start, end, limit=10, kind="events"),
    )
    top_votes = await _cached(
        _cache_key("users_votes", start, end),
        lambda: get_top_users_activity(start, end, limit=5, kind="votes"),
    )
    top_uploads = await _cached(
        _cache_key("users_uploads", start, end),
        lambda: get_top_users_activity(start, end, limit=5, kind="uploads"),
    )
    top_reports = await _cached(
        _cache_key("users_reports", start, end),
        lambda: get_top_users_activity(start, end, limit=5, kind="reports"),
    )

    lines: list[str] = [
        "👥 <b>Users</b>",
        f"📅 Период: <b>{_period_title(period)}</b> ({label})",
        "",
        "Топ активных:",
    ]
    if top_events:
        for i, row in enumerate(top_events, start=1):
            lines.append(
                f"{i}) <b>{_user_label(row)}</b> — {int(row.get('metric_count') or 0)} действий "
                f"(🗳 {int(row.get('votes_count') or 0)} · 📤 {int(row.get('uploads_count') or 0)} · 🚨 {int(row.get('reports_count') or 0)})"
            )
    else:
        lines.append("— Нет событий за период.")

    lines.extend(["", "🗳 Топ голосующих:"])
    if top_votes:
        for i, row in enumerate(top_votes, start=1):
            lines.append(f"{i}) {_user_label(row)} — {int(row.get('metric_count') or 0)}")
    else:
        lines.append("— В логах нет детальных vote-событий.")

    lines.extend(["", "📤 Топ публикующих:"])
    if top_uploads:
        for i, row in enumerate(top_uploads, start=1):
            lines.append(f"{i}) {_user_label(row)} — {int(row.get('metric_count') or 0)}")
    else:
        lines.append("— В логах нет детальных upload-событий.")

    lines.extend(["", "🚨 Топ жалобщиков:"])
    if top_reports:
        for i, row in enumerate(top_reports, start=1):
            lines.append(f"{i}) {_user_label(row)} — {int(row.get('metric_count') or 0)}")
    else:
        lines.append("— В логах нет детальных report-событий.")

    tab, current_period = await _get_tab_period(state)
    await _upsert_activity_text(
        callback,
        state,
        text="\n".join(lines),
        reply_markup=_kb_activity_menu(tab, current_period),
    )


async def _render_sections(callback: CallbackQuery, state: FSMContext, *, period: str) -> None:
    start, end, label = _period_window(period)
    sections = await _cached(
        _cache_key("sections", start, end),
        lambda: get_top_sections(start, end, limit=10),
    )
    errors = await _cached(
        _cache_key("errors_summary_sections", start, end),
        lambda: get_errors_summary(start, end, limit=10),
    )
    section_map = {str(r.get("section") or "other"): int(r.get("cnt") or 0) for r in sections}
    start_cnt = section_map.get("messages", 0)
    menu_cnt = section_map.get("menu", 0)
    rate_cnt = section_map.get("rate", 0)
    vote_cnt = rate_cnt

    lines: list[str] = [
        "🧭 <b>Sections</b>",
        f"📅 Период: <b>{_period_title(period)}</b> ({label})",
        "",
        "Топ разделов:",
    ]
    if sections:
        for i, row in enumerate(sections, start=1):
            lines.append(f"{i}) <b>{row.get('section') or 'other'}</b> — {int(row.get('cnt') or 0)}")
    else:
        lines.append("— Нет событий за период.")

    lines.extend(["", "Топ хендлеров (по ошибкам):"])
    by_handler = list(errors.get("by_handler") or [])
    if by_handler:
        for i, row in enumerate(by_handler[:10], start=1):
            handler = str(row.get("handler") or "unknown")
            section = _map_handler_to_section(handler)
            lines.append(f"{i}) <code>{handler}</code> · {section} — {int(row.get('cnt') or 0)}")
    else:
        lines.append("— Ошибок по хендлерам нет.")

    lines.extend(
        [
            "",
            "Мини-воронка:",
            f"start({start_cnt}) → menu({menu_cnt}) → rate({rate_cnt}) → vote({vote_cnt})",
        ]
    )

    tab, current_period = await _get_tab_period(state)
    await _upsert_activity_text(
        callback,
        state,
        text="\n".join(lines),
        reply_markup=_kb_activity_menu(tab, current_period),
    )


async def _render_spam(callback: CallbackQuery, state: FSMContext, *, period: str) -> None:
    start, end, label = _period_window(period)
    suspects = await _cached(
        _cache_key("spam_suspects", start, end),
        lambda: get_spam_suspects(start, end, limit=10),
    )
    lines: list[str] = [
        "🧨 <b>Spam/Abuse</b>",
        f"📅 Период: <b>{_period_title(period)}</b> ({label})",
        "",
        "Подозрительные пользователи:",
    ]
    if suspects:
        for i, row in enumerate(suspects, start=1):
            lines.append(
                f"⚠️ {i}) <b>{_user_label(row)}</b> — score {int(row.get('score') or 0)} "
                f"(events {int(row.get('total_events') or 0)}, peak/min {int(row.get('peak_per_minute') or 0)}, "
                f"errors {int(row.get('errors_total') or 0)})"
            )
    else:
        lines.append("— Подозрительных пиков не найдено.")

    lines.extend(
        [
            "",
            "<i>Правило: высокий score = много событий за короткое время + ошибки (BadRequest/Flood).</i>",
        ]
    )

    tab, current_period = await _get_tab_period(state)
    await _upsert_activity_text(
        callback,
        state,
        text="\n".join(lines),
        reply_markup=_kb_activity_menu(tab, current_period),
    )


async def _render_errors(callback: CallbackQuery, state: FSMContext, *, period: str) -> None:
    start, end, label = _period_window(period)
    errors = await _cached(
        _cache_key("errors_summary", start, end),
        lambda: get_errors_summary(start, end, limit=10),
    )
    lines: list[str] = [
        "🧯 <b>Errors</b>",
        f"📅 Период: <b>{_period_title(period)}</b> ({label})",
        "",
        f"Всего ошибок: <b>{int(errors.get('total_errors') or 0)}</b>",
        f"Error-rate: <b>{float(errors.get('error_rate') or 0.0):.2f}%</b>",
        "",
        "Top error types:",
    ]
    by_type = list(errors.get("by_type") or [])
    if by_type:
        for i, row in enumerate(by_type[:10], start=1):
            lines.append(f"{i}) <b>{row.get('error_type') or 'Error'}</b> — {int(row.get('cnt') or 0)}")
    else:
        lines.append("— Нет ошибок.")

    lines.extend(["", "Top handlers by errors:"])
    by_handler = list(errors.get("by_handler") or [])
    if by_handler:
        for i, row in enumerate(by_handler[:10], start=1):
            lines.append(f"{i}) <code>{row.get('handler') or 'unknown'}</code> — {int(row.get('cnt') or 0)}")
    else:
        lines.append("— Нет проблемных хендлеров.")

    tab, current_period = await _get_tab_period(state)
    await _upsert_activity_text(
        callback,
        state,
        text="\n".join(lines),
        reply_markup=_kb_activity_menu(tab, current_period),
    )


async def _render_dashboard(callback: CallbackQuery, state: FSMContext) -> None:
    tab, period = await _get_tab_period(state)
    try:
        if tab == "users":
            await _render_users(callback, state, period=period)
        elif tab == "sections":
            await _render_sections(callback, state, period=period)
        elif tab == "spam":
            await _render_spam(callback, state, period=period)
        elif tab == "errors":
            await _render_errors(callback, state, period=period)
        else:
            await _render_overview(callback, state, period=period)
    except Exception as e:
        try:
            await log_bot_error(
                chat_id=callback.message.chat.id if callback.message else None,
                tg_user_id=callback.from_user.id if callback.from_user else None,
                handler=f"admin_activity:render:{tab}",
                update_type="callback",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass
        tab, period = await _get_tab_period(state)
        await _upsert_activity_text(
            callback,
            state,
            text=(
                "📈 <b>Активность</b>\n\n"
                "Не удалось построить выбранный экран.\n"
                "Попробуй переключить вкладку или период."
            ),
            reply_markup=_kb_activity_menu(tab, period),
        )


@router.callback_query(F.data == "admin:activity")
async def admin_activity_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    # Убираем предыдущее сообщение админ-меню, чтобы не плодить хвосты.
    try:
        data = await state.get_data()
        admin_chat_id = data.get("admin_chat_id")
        admin_msg_id = data.get("admin_msg_id")
        if admin_chat_id and admin_msg_id and callback.message:
            try:
                await callback.message.bot.delete_message(
                    chat_id=int(admin_chat_id),
                    message_id=int(admin_msg_id),
                )
            except Exception:
                pass
            try:
                await state.update_data(admin_chat_id=None, admin_msg_id=None)
            except Exception:
                pass
    except Exception:
        pass

    await _cleanup_legacy_chart_msg(callback, state)
    await _set_tab_period(state, tab="overview", period=(await _get_tab_period(state))[1])
    await _render_dashboard(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:activity:tab:"))
async def admin_activity_switch_tab(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    tab = (callback.data or "").split(":")[-1]
    await _set_tab_period(state, tab=tab)
    await _render_dashboard(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin:activity:period")
async def admin_activity_period_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    tab, period = await _get_tab_period(state)
    await _upsert_activity_text(
        callback,
        state,
        text=(
            "📅 <b>Выбор периода</b>\n\n"
            f"Текущий период: <b>{_period_title(period)}</b>\n"
            f"Текущая вкладка: <b>{_tab_title(tab)}</b>"
        ),
        reply_markup=_kb_period_picker(period),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:activity:period_back")
async def admin_activity_period_back(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    await _render_dashboard(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:activity:set_period:"))
async def admin_activity_set_period(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    period = (callback.data or "").split(":")[-1]
    await _set_tab_period(state, period=period)
    await _render_dashboard(callback, state)
    await callback.answer()


# Legacy callbacks for compatibility with old keyboard/buttons.
@router.callback_query(F.data == "admin:activity:day")
async def admin_activity_day(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    await _set_tab_period(state, period="day")
    await _render_dashboard(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin:activity:week")
async def admin_activity_week(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    await _set_tab_period(state, period="week")
    await _render_dashboard(callback, state)
    await callback.answer()


@router.callback_query(F.data == "admin:activity:month")
async def admin_activity_month(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    await _set_tab_period(state, period="month")
    await _render_dashboard(callback, state)
    await callback.answer()
