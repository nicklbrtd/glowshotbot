from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ПЛАТЕЖИ =======================================
# =============================================================

from typing import Optional, Union
from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from handlers.payments import TARIFFS
from utils.time import get_moscow_now
from config import MASTER_ADMIN_ID

from database import (
    get_user_by_tg_id,
    get_payments_count,
    get_payments_page,
    get_revenue_summary,
    get_subscriptions_total,
    get_subscriptions_page,
)

from .common import (
    _ensure_admin,
    _ensure_user,
    UserAdminStates,
    UserAwardsStates,
    RoleStates,
    PaymentsStates,
)

router = Router()

UserEvent = Union[Message, CallbackQuery]


# =============================================================
# ==== ENSURE ADMIN ===========================================
# =============================================================

async def _get_from_user(event: UserEvent):
    return event.from_user


async def _ensure_user(event: UserEvent) -> Optional[dict]:
    from_user = await _get_from_user(event)
    user = await get_user_by_tg_id(from_user.id)
    if not user:
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала /start", show_alert=True)
        return None
    return user


async def _ensure_admin(event: UserEvent) -> Optional[dict]:
    user = await _ensure_user(event)
    if not user:
        return None

    from_user = await _get_from_user(event)

    if MASTER_ADMIN_ID and from_user.id == MASTER_ADMIN_ID:
        return user

    if not user.get("is_admin"):
        if isinstance(event, CallbackQuery):
            await event.answer("Нет прав администратора", show_alert=True)
        return None

    return user


# =============================================================
# ==== FSM ====================================================
# =============================================================

class PaymentsStates(StatesGroup):
    idle = State()


# =============================================================
# ==== HELPER =================================================
# =============================================================

async def _edit_or_send(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
    data = await state.get_data()
    chat_id = data.get("payments_chat_id")
    msg_id = data.get("payments_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(
        payments_chat_id=sent.chat.id,
        payments_msg_id=sent.message_id,
    )


# =============================================================
# ==== МЕНЮ ПЛАТЕЖЕЙ ==========================================
# =============================================================

@router.callback_query(F.data == "admin:payments")
async def admin_payments_menu(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return

    text = (
        "<b>💳 Платежи и подписки</b>\n\n"
        "Здесь можно посмотреть:\n"
        "• успешные платежи\n"
        "• доходы\n"
        "• тарифы\n"
        "• подписки пользователей"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Платежи", callback_data="admin:payments:list:1")
    kb.button(text="💰 Доходы", callback_data="admin:payments:revenue")
    kb.button(text="🏷 Тарифы", callback_data="admin:payments:tariffs")
    kb.button(text="👥 Подписки", callback_data="admin:payments:subs:1")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_or_send(callback.message, state, text, kb.as_markup())
    await callback.answer()


# =============================================================
# ==== СПИСОК ПЛАТЕЖЕЙ ========================================
# =============================================================

@router.callback_query(F.data.startswith("admin:payments:list:"))
async def admin_payments_list(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return

    page = int(callback.data.split(":")[-1])
    page_size = 20

    total = await get_payments_count()
    max_page = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, max_page))

    rows = await get_payments_page(page, page_size)

    lines = [
        "<b>📜 Успешные платежи</b>",
        f"Всего: <b>{total}</b>",
        "",
    ]

    if not rows:
        lines.append("Платежей пока нет.")
    else:
        for p in rows:
            created = p.get("created_at")
            try:
                dt = datetime.fromisoformat(created)
                created = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass

            username = p.get("user_username")
            label = f"@{username}" if username else f"ID {p.get('user_tg_id')}"

            amount = p.get("amount", 0)
            currency = p.get("currency")
            amount_text = f"{amount / 100:.2f} ₽" if currency == "RUB" else f"{amount} ⭐"

            lines.append(
                f"{created} — {label}\n"
                f"   {p.get('period_code')} / {p.get('days')} дн. / {amount_text}"
            )

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(text="◀️", callback_data=f"admin:payments:list:{page-1}")
    if page < max_page:
        kb.button(text="▶️", callback_data=f"admin:payments:list:{page+1}")
    kb.button(text="⬅️ Назад", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)

    await _edit_or_send(callback.message, state, "\n".join(lines), kb.as_markup())
    await callback.answer()


# =============================================================
# ==== ДОХОДЫ =================================================
# =============================================================

@router.callback_query(F.data == "admin:payments:revenue")
async def admin_payments_revenue(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return

    day = await get_revenue_summary("day")
    week = await get_revenue_summary("week")
    month = await get_revenue_summary("month")

    def block(title, d):
        return (
            f"<b>{title}</b>\n"
            f"• RUB: {d.get('rub_total', 0):.2f} ₽ ({d.get('rub_count', 0)})\n"
            f"• ⭐ Stars: {d.get('stars_total', 0)} ({d.get('stars_count', 0)})"
        )

    text = "\n\n".join([
        "<b>💰 Доходы</b>",
        block("Сегодня", day),
        block("7 дней", week),
        block("30 дней", month),
    ])

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_or_send(callback.message, state, text, kb.as_markup())
    await callback.answer()


# =============================================================
# ==== ТАРИФЫ ================================================
# =============================================================

@router.callback_query(F.data == "admin:payments:tariffs")
async def admin_payments_tariffs(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return

    lines = ["<b>🏷 Тарифы</b>", ""]

    for code, t in TARIFFS.items():
        lines.append(
            f"<b>{t['title']}</b>\n"
            f"Код: <code>{code}</code>\n"
            f"{t['days']} дн. — {t['price_rub']} ₽ / {t['price_stars']} ⭐\n"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_or_send(callback.message, state, "\n".join(lines), kb.as_markup())
    await callback.answer()


# =============================================================
# ==== ПОДПИСКИ ===============================================
# =============================================================

@router.callback_query(F.data.startswith("admin:payments:subs:"))
async def admin_payments_subs(callback: CallbackQuery, state: FSMContext):
    if not await _ensure_admin(callback):
        return

    page = int(callback.data.split(":")[-1])
    page_size = 20

    total = await get_subscriptions_total()
    max_page = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, max_page))

    rows = await get_subscriptions_page(page, page_size)

    lines = [
        "<b>👥 Подписки</b>",
        f"Всего платящих: <b>{total}</b>",
        "",
    ]

    for r in rows:
        username = r.get("user_username")
        label = f"@{username}" if username else f"ID {r.get('user_tg_id')}"
        lines.append(
            f"{label}\n"
            f"Платежей: {r.get('payments_count')} | "
            f"Дней: {r.get('total_days')} | "
            f"{r.get('total_rub', 0):.2f} ₽ / {r.get('total_stars', 0)} ⭐"
        )

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(text="◀️", callback_data=f"admin:payments:subs:{page-1}")
    if page < max_page:
        kb.button(text="▶️", callback_data=f"admin:payments:subs:{page+1}")
    kb.button(text="⬅️ Назад", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)

    await _edit_or_send(callback.message, state, "\n".join(lines), kb.as_markup())
    await callback.answer()