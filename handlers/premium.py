from datetime import datetime
from utils.time import get_moscow_now

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import get_user_premium_status, is_user_premium_active
from keyboards.common import build_back_kb

router = Router(name="premium")


def _format_premium_until(until: str | None) -> str | None:
    """
    Привести premium_until (ISO-строка) к человеку понятному виду dd.mm.yyyy.
    Если формат странный или None — вернуть None.
    """
    if not until:
        return None

    try:
        dt = datetime.fromisoformat(until)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        # На всякий случай не падаем, а возвращаем как есть
        return until
    



@router.callback_query(F.data == "profile:premium")
async def profile_premium_menu(callback: CallbackQuery):
    tg_id = callback.from_user.id

    is_active = False
    premium_until_human: str | None = None
    days_left: int | None = None

    try:
        raw_status = await get_user_premium_status(tg_id)
        is_active = await is_user_premium_active(tg_id)
        premium_until_raw = raw_status.get("premium_until")
        premium_until_human = _format_premium_until(premium_until_raw)

        if premium_until_raw:
            try:
                dt = datetime.fromisoformat(premium_until_raw)
                delta_days = (dt.date() - get_moscow_now().date()).days
                if delta_days >= 0:
                    days_left = delta_days
            except Exception:
                days_left = None
    except Exception:
        # если что-то сломалось — просто покажем базовый экран
        is_active = False
        premium_until_human = None
        days_left = None

    # Заготовка под список "что нового" — позже вынесем в админ-меню
    new_features_week = [
        "Можно это",
        "можно это и то",
    ]

    # Заготовка под список преимуществ — ты потом дополнишь
    premium_features = [
        "Две активные фотографии вместо одной",
        "Расширенная статистика по фото",
        "Дополнительные инструменты продвижения",
        "Приоритетная поддержка",
        "И ещё фичи (добавишь позже)",
    ]

    kb = InlineKeyboardBuilder()

    if is_active:
        # Кнопки: Продлить / Преимущества / Назад
        kb.button(text="🔁 Продлить", callback_data="premium:plans")
        kb.button(text="✨ Преимущества", callback_data="profile:premium_benefits")
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(1)

        status_line = "Статус: <b>активен</b>"
        if premium_until_human:
            status_line += f" до {premium_until_human}"

        if days_left is not None:
            if days_left >= 365:
                years = days_left // 365
                years_text = "год" if years == 1 else ("года" if 2 <= years <= 4 else "лет")
                status_line += f" (<b>{years} {years_text}</b>)"
            else:
                status_line += f" (<b>{days_left} дн.</b>)"

        whats_new = "\n".join([f"{i+1}. {t}" for i, t in enumerate(new_features_week)])

        text = (
            "💎 <b>GlowShot Premium</b>\n\n"
            f"{status_line}\n"
            "Ты можешь продлить подписку, нажав на кнопку <b>«Продлить»</b>.\n\n"
            "<b>Новые функции за последнюю неделю:</b>\n"
            f"{whats_new}"
        )
    else:
        # Кнопки тарифов + Назад
        kb.button(text="Неделя 70 ⭐️ / 79 ₽", callback_data="premium:plan:7d")
        kb.button(text="Месяц 230 ⭐️ / 239 ₽", callback_data="premium:plan:30d")
        kb.button(text="3 месяца 500 ⭐️ / 569 ₽", callback_data="premium:plan:90d")
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(1)

        feats = "\n".join([f"• {x}" for x in premium_features])
        text = (
            "💳 <b>GlowShot Premium</b>\n\n"
            "Вот что даёт премиум:\n\n"
            f"{feats}\n\n"
            "Выбери тариф ниже 👇"
        )

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:premium_benefits")
async def profile_premium_benefits(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    text = (
        "✨ <b>Преимущества GlowShot Premium</b>\n\n"
        "• Две активные фотографии вместо одной\n"
        "• Расширенная статистика по фотографиям\n"
        "• Дополнительные инструменты продвижения\n"
        "• Приоритетная поддержка\n"
        "• И другие фичи (ты добавишь позже)\n\n"
        "Хочешь оформить/продлить — вернись назад и выбери тариф."
    )

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
