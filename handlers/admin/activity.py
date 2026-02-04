from __future__ import annotations

from datetime import timedelta

from aiogram import Router, F
from aiogram.types import CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now
from utils.charts import render_activity_chart
from database import get_activity_counts_by_hour, get_activity_counts_by_day
from .common import _ensure_admin, edit_or_answer


router = Router(name="admin_activity")


def _kb_activity_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 День", callback_data="admin:activity:day")
    kb.button(text="🗓 Неделя", callback_data="admin:activity:week")
    kb.button(text="🗓 Месяц", callback_data="admin:activity:month")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


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
    chart = render_activity_chart(counts, labels)
    chart_file = BufferedInputFile(chart.getvalue(), filename="activity.png")
    caption = f"📈 <b>Активность</b>\n{title}"

    data = await state.get_data()
    prev_id = data.get("activity_chart_msg_id")
    if prev_id:
        try:
            await callback.message.bot.delete_message(chat_id=callback.message.chat.id, message_id=int(prev_id))
        except Exception:
            pass

    sent = await callback.message.bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=chart_file,
        caption=caption,
        reply_markup=_kb_activity_menu(),
        parse_mode="HTML",
        disable_notification=True,
    )
    await state.update_data(activity_chart_msg_id=sent.message_id)


@router.callback_query(F.data == "admin:activity:day")
async def admin_activity_day(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    now = get_moscow_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    rows = await get_activity_counts_by_hour(start.isoformat(), end.isoformat())
    by_hour = {r["bucket"].replace(minute=0, second=0, microsecond=0): int(r["cnt"]) for r in rows}
    counts: list[int] = []
    labels: list[str] = []
    for h in range(24):
        dt = start + timedelta(hours=h)
        counts.append(by_hour.get(dt, 0))
        labels.append(f"{h:02d}")

    title = f"День: {start.strftime('%d.%m.%Y')} (по часам)"
    await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
    await callback.answer()


@router.callback_query(F.data == "admin:activity:week")
async def admin_activity_week(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    now = get_moscow_now()
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=7)

    rows = await get_activity_counts_by_day(start.isoformat(), end.isoformat())
    by_day = {r["bucket"].replace(hour=0, minute=0, second=0, microsecond=0): int(r["cnt"]) for r in rows}
    counts: list[int] = []
    labels: list[str] = []
    for i in range(7):
        dt = start + timedelta(days=i)
        counts.append(by_day.get(dt, 0))
        labels.append(dt.strftime("%d.%m"))

    title = f"Неделя: {start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
    await callback.answer()


@router.callback_query(F.data == "admin:activity:month")
async def admin_activity_month(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    now = get_moscow_now()
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=30)

    rows = await get_activity_counts_by_day(start.isoformat(), end.isoformat())
    by_day = {r["bucket"].replace(hour=0, minute=0, second=0, microsecond=0): int(r["cnt"]) for r in rows}
    counts: list[int] = []
    labels: list[str] = []
    for i in range(30):
        dt = start + timedelta(days=i)
        counts.append(by_day.get(dt, 0))
        labels.append(dt.strftime("%d.%m"))

    title = f"Месяц: {start.strftime('%d.%m')}–{(end - timedelta(days=1)).strftime('%d.%m.%Y')}"
    await _send_activity_chart(callback, state, title=title, counts=counts, labels=labels)
    await callback.answer()
