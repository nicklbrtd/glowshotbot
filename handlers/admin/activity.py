from __future__ import annotations

from datetime import timedelta, datetime
import traceback

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now
from utils.charts import render_activity_chart
from database import get_activity_counts_by_hour, get_activity_counts_by_day, log_bot_error
from .common import _ensure_admin


router = Router(name="admin_activity")

_ACTIVITY_PREFIX = "admin_activity"


def _kb_activity_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 День", callback_data="admin:activity:day")
    kb.button(text="🗓 Неделя", callback_data="admin:activity:week")
    kb.button(text="🗓 Месяц", callback_data="admin:activity:month")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


async def _get_activity_msg_ids(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get(f"{_ACTIVITY_PREFIX}_chat_id"), data.get(f"{_ACTIVITY_PREFIX}_msg_id")


async def _set_activity_msg_ids(state: FSMContext, chat_id: int, msg_id: int) -> None:
    await state.update_data(**{f"{_ACTIVITY_PREFIX}_chat_id": chat_id, f"{_ACTIVITY_PREFIX}_msg_id": msg_id})


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


@router.callback_query(F.data == "admin:activity")
async def admin_activity_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "📈 <b>Активность</b>\n\n"
        "Выбери период:\n"
        "• День (по часам)\n"
        "• Неделя (по дням)\n"
        "• Месяц (по дням)\n"
    )
    await _cleanup_legacy_chart_msg(callback, state)
    await _upsert_activity_text(callback, state, text=text, reply_markup=_kb_activity_menu())
    await callback.answer()


async def _send_activity_chart(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    title: str,
    counts: list[int],
    labels: list[str],
    kind: str,
) -> None:
    try:
        chart = render_activity_chart(counts, labels)
        chart_file = BufferedInputFile(chart.getvalue(), filename="activity.png")
        extra_lines = _format_peak_lines(counts, labels, kind=kind)
        if extra_lines:
            caption = "📈 <b>Активность</b>\n" + title + "\n\n" + "\n".join(extra_lines)
        else:
            caption = f"📈 <b>Активность</b>\n{title}"

        await _upsert_activity_photo(
            callback,
            state,
            chart_file=chart_file,
            caption=caption,
            reply_markup=_kb_activity_menu(),
        )
    except Exception as e:
        try:
            await callback.answer("Не удалось отправить график.", show_alert=True)
        except Exception:
            pass
        try:
            await log_bot_error(
                chat_id=callback.message.chat.id if callback.message else None,
                tg_user_id=callback.from_user.id if callback.from_user else None,
                handler="admin_activity:send_chart",
                update_type="callback",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "admin:activity:day")
async def admin_activity_day(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    try:
        await callback.answer("Готовлю график…", show_alert=False)
    except Exception:
        pass

    try:
        now = get_moscow_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        rows = await get_activity_counts_by_hour(start, end)
        by_hour: dict[datetime, int] = {}
        for r in rows:
            dt = _normalize_bucket(r.get("bucket"), by="hour")
            if dt is not None:
                by_hour[dt] = int(r.get("cnt") or 0)
        counts: list[int] = []
        labels: list[str] = []
        for h in range(24):
            dt = start + timedelta(hours=h)
            counts.append(by_hour.get(dt, 0))
            labels.append(f"{h:02d}")

        title = f"День: {start.strftime('%d.%m.%Y')} (по часам)"
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels, kind="hour")
    except Exception as e:
        try:
            await log_bot_error(
                chat_id=callback.message.chat.id if callback.message else None,
                tg_user_id=callback.from_user.id if callback.from_user else None,
                handler="admin_activity:day",
                update_type="callback",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "admin:activity:week")
async def admin_activity_week(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    try:
        await callback.answer("Готовлю график…", show_alert=False)
    except Exception:
        pass

    try:
        now = get_moscow_now()
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=7)

        rows = await get_activity_counts_by_day(start, end)
        by_day: dict[datetime, int] = {}
        for r in rows:
            dt = _normalize_bucket(r.get("bucket"), by="day")
            if dt is not None:
                by_day[dt] = int(r.get("cnt") or 0)
        counts: list[int] = []
        labels: list[str] = []
        for i in range(7):
            dt = start + timedelta(days=i)
            counts.append(by_day.get(dt, 0))
            labels.append(dt.strftime("%d.%m"))

        title = f"Неделя: {start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels, kind="day")
    except Exception as e:
        try:
            await log_bot_error(
                chat_id=callback.message.chat.id if callback.message else None,
                tg_user_id=callback.from_user.id if callback.from_user else None,
                handler="admin_activity:week",
                update_type="callback",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "admin:activity:month")
async def admin_activity_month(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return
    try:
        await callback.answer("Готовлю график…", show_alert=False)
    except Exception:
        pass

    try:
        now = get_moscow_now()
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=30)

        rows = await get_activity_counts_by_day(start, end)
        by_day: dict[datetime, int] = {}
        for r in rows:
            dt = _normalize_bucket(r.get("bucket"), by="day")
            if dt is not None:
                by_day[dt] = int(r.get("cnt") or 0)
        counts: list[int] = []
        labels: list[str] = []
        for i in range(30):
            dt = start + timedelta(days=i)
            counts.append(by_day.get(dt, 0))
            labels.append(dt.strftime("%d.%m"))

        title = f"Месяц: {start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels, kind="day")
    except Exception as e:
        try:
            await log_bot_error(
                chat_id=callback.message.chat.id if callback.message else None,
                tg_user_id=callback.from_user.id if callback.from_user else None,
                handler="admin_activity:month",
                update_type="callback",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass
