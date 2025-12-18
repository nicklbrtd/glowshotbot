
from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import get_user_premium_status, is_user_premium_active

# Import shared tariffs from the single source of truth
from handlers.payments import TARIFFS

router = Router(name="premium")


PREMIUM_FEATURES_TEXT = (
    "💎 <b>GlowShot Premium</b>\n\n"
    "Премиум даёт больше свободы и приятных плюшек внутри бота.\n\n"
    "<b>Что открывается:</b>\n"
    "• больше возможностей в профиле и контенте\n"
    "• дополнительные удобства/ускорения\n"
    "• поддержка развития проекта\n\n"
    "<b>Тарифы:</b>\n"
    "• Неделя — 70 ⭐️ / 79 ₽\n"
    "• Месяц — 230 ⭐️ / 239 ₽\n"
    "• 3 месяца — 500 ⭐️ / 569 ₽\n"
)


@router.callback_query(F.data == "profile:premium")
async def profile_premium_menu(callback: CallbackQuery):
    """Premium screen (info + buy/extend entrypoint).

    Payment flow and invoices live in `handlers/payments.py`.
    Here we only show the UX and route users to the payment handlers.
    """

    tg_id = callback.from_user.id

    status = await get_user_premium_status(tg_id)
    is_active = await is_user_premium_active(tg_id)

    until = (status or {}).get("premium_until")
    if is_active:
        if until:
            status_line = f"✅ Premium активен до <b>{until}</b>"
        else:
            status_line = "✅ Premium активен"
    else:
        status_line = "❌ Premium не активен"

    kb = InlineKeyboardBuilder()

    # If active — offer extend screen (plans list)
    if is_active:
        kb.button(text="Продлить подписку", callback_data="premium:plans")
    else:
        # If not active — show direct plan buttons (handled by payments.py)
        kb.button(text="Неделя 70 ⭐️ / 79 ₽", callback_data="premium:plan:7d")
        kb.button(text="Месяц 230 ⭐️ / 239 ₽", callback_data="premium:plan:30d")
        kb.button(text="3 месяца 500 ⭐️ / 569 ₽", callback_data="premium:plan:90d")

    # Back button to profile (explicit callback)
    kb.button(text="⬅️ Назад", callback_data="menu:profile")
    kb.adjust(1)

    text = f"{status_line}\n\n{PREMIUM_FEATURES_TEXT}"

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "premium:plans")
async def premium_plans_shortcut(callback: CallbackQuery):
    """Safety shortcut.

    The real handler for `premium:plans` is in `handlers/payments.py`.
    If routing order changes and this one triggers, we simply re-send the same callback
    data so the user can proceed.
    """
    # Build a minimal plans screen using shared TARIFFS
    kb = InlineKeyboardBuilder()
    kb.button(text="Неделя 70 ⭐️ / 79 ₽", callback_data="premium:plan:7d")
    kb.button(text="Месяц 230 ⭐️ / 239 ₽", callback_data="premium:plan:30d")
    kb.button(text="3 месяца 500 ⭐️ / 569 ₽", callback_data="premium:plan:90d")
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    await callback.message.edit_text(
        "💎 <b>GlowShot Premium</b>\n\nВыбери период подписки:",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
