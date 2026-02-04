from __future__ import annotations

from datetime import timedelta, datetime
import traceback

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now
from utils.charts import render_activity_chart
from database import get_activity_counts_by_hour, get_activity_counts_by_day, log_bot_error
from .common import _ensure_admin, edit_or_answer, ensure_primary_bot


router = Router(name="admin_activity")


def _kb_activity_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 День", callback_data="admin:activity:day")
    kb.button(text="🗓 Неделя", callback_data="admin:activity:week")
    kb.button(text="🗓 Месяц", callback_data="admin:activity:month")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()

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
    await edit_or_answer(callback.message, state, prefix="admin_activity", text=text, reply_markup=_kb_activity_menu())
    await callback.answer()


async def _send_activity_chart(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    title: str,
    counts: list[int],
    labels: list[str],
) -> None:
    try:
        primary_bot = ensure_primary_bot(callback.message.bot)
        send_bot = primary_bot or callback.message.bot
        chart = render_activity_chart(counts, labels)
        chart_file = BufferedInputFile(chart.getvalue(), filename="activity.png")
        caption = f"📈 <b>Активность</b>\n{title}"

        data = await state.get_data()
        prev_id = data.get("activity_chart_msg_id")
        prev_bot = data.get("activity_chart_bot") or "current"
        if prev_id:
            try:
                if prev_bot == "primary":
                    await primary_bot.delete_message(chat_id=callback.message.chat.id, message_id=int(prev_id))
                else:
                    await callback.message.bot.delete_message(chat_id=callback.message.chat.id, message_id=int(prev_id))
            except Exception:
                pass

        sent = None
        primary_err: Exception | None = None
        try:
            sent = await send_bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=chart_file,
                caption=caption,
                reply_markup=_kb_activity_menu(),
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception as e:
            primary_err = e
            try:
                sent = await send_bot.send_document(
                    chat_id=callback.message.chat.id,
                    document=chart_file,
                    caption=caption,
                    reply_markup=_kb_activity_menu(),
                    parse_mode="HTML",
                    disable_notification=True,
                )
            except Exception as e2:
                primary_err = primary_err or e2
                sent = None

        # Если основному боту нельзя написать — пробуем отправить через текущего
        if sent is None and send_bot is not callback.message.bot:
            try:
                sent = await callback.message.bot.send_photo(
                    chat_id=callback.message.chat.id,
                    photo=chart_file,
                    caption=caption,
                    reply_markup=_kb_activity_menu(),
                    parse_mode="HTML",
                    disable_notification=True,
                )
            except Exception:
                sent = None

        if sent is not None:
            bot_flag = "primary" if send_bot is primary_bot else "current"
            await state.update_data(activity_chart_msg_id=sent.message_id, activity_chart_bot=bot_flag)
            # если открыт support-бот, но график ушёл основному — сообщим коротко
            if send_bot is primary_bot and send_bot is not callback.message.bot:
                try:
                    await callback.answer("График отправлен в основной бот.", show_alert=False)
                except Exception:
                    pass
        elif primary_err and send_bot is primary_bot and send_bot is not callback.message.bot:
            err_txt = str(primary_err).lower()
            if "forbidden" in err_txt or "blocked" in err_txt or "chat not found" in err_txt:
                try:
                    await callback.message.answer(
                        "Основной бот не может написать вам.\n"
                        "Откройте основной бот и нажмите /start, затем попробуйте снова.",
                    )
                except Exception:
                    pass
        else:
            raise RuntimeError("send_photo/send_document failed for activity chart")
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

        rows = await get_activity_counts_by_hour(start.isoformat(), end.isoformat())
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
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
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

        rows = await get_activity_counts_by_day(start.isoformat(), end.isoformat())
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
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
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

        rows = await get_activity_counts_by_day(start.isoformat(), end.isoformat())
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
        await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
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
