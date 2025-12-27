
from __future__ import annotations

from aiogram import Router, F
from aiogram.types import CallbackQuery
from datetime import datetime, timedelta

from utils.time import get_moscow_now
from database import get_premium_news_since
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import get_user_premium_status, is_user_premium_active


router = Router(name="premium")



PREMIUM_BENEFITS_TEXT = (
    "💎 <b>GlowShot Premium</b>\n\n"
    "<b>Вот что даёт премиум:</b>\n"
    "• 📷 <b>Две активные фотографии вместо одной</b>\n"
    "  Больше оценок — больше шансов попасть в итоги.\n"
    "• 🔗 <b>Ссылка в профиле</b>\n"
    "  Можно добавить ссылку на свой аккаунт или Telegram‑канал. Другие увидят её при оценивании.\n"
    "• 👀 <b>Ты на виду</b>\n"
    "  При оценке твоих фото другие пользователи будут видеть твоё имя.\n"
    "• 💬 <b>Приоритетная поддержка</b>\n"
    "  В поддержке тебя замечают быстрее.\n\n"
    "Список будет дополняться!"
)


def _format_until_and_days_left(until_iso: str | None) -> tuple[str, str]:
    """Returns (human_until, days_left_text)."""
    if not until_iso:
        return ("—", "")
    try:
        dt = datetime.fromisoformat(until_iso)
        human = dt.strftime("%d.%m.%Y")
        now = get_moscow_now()
        days_left = (dt.date() - now.date()).days
        if days_left < 0:
            days_left = 0
        return (human, f"({days_left} дней)")
    except Exception:
        return (str(until_iso), "")


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

    if is_active:
        # --- Active premium scenario ---
        human_until, days_left_text = _format_until_and_days_left(until)
        status_block = (
            "💎 <b>GlowShot Premium</b>\n"
            f"<b>Статус:</b> активно до <b>{human_until}</b> {days_left_text}\n"
        )

        # News for last 7 days
        since = (get_moscow_now() - timedelta(days=7)).isoformat()
        news_items = await get_premium_news_since(since, limit=10)
        if news_items:
            news_lines = ["<b>Новое в премиум за последнюю неделю:</b>"]
            for i, it in enumerate(news_items, start=1):
                news_lines.append(f"{i}. {it}")
            news_block = "\n".join(news_lines)
        else:
            news_block = "<b>Новое в премиум за последнюю неделю:</b>\n— пока ничего не добавляли"

        text = status_block + "\n" + news_block

        kb.button(text="✨ Преимущества", callback_data="premium:benefits")
        kb.button(text="🔁 Продлить подписку", callback_data="premium:plans")
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(1)

    else:
        # --- Inactive premium scenario ---
        text = PREMIUM_BENEFITS_TEXT

        kb.button(text="Неделя — 70 ⭐️ / 79 ₽", callback_data="premium:plan:7d")
        kb.button(text="Месяц — 230 ⭐️ / 239 ₽", callback_data="premium:plan:30d")
        kb.button(text="3 месяца — 500 ⭐️ / 569 ₽", callback_data="premium:plan:90d")
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


# New handler for "Преимущества"
@router.callback_query(F.data == "premium:benefits")
async def premium_benefits(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    await callback.message.edit_text(PREMIUM_BENEFITS_TEXT, reply_markup=kb.as_markup())
    await callback.answer()