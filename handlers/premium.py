from datetime import datetime

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
    

@router.callback_query(F.data == "premium:menu")
async def premium_main_menu(callback: CallbackQuery):
    """
    Отдельная премиум-панель из главного меню.
    Здесь собираются все премиум-функции, а экран profile:premium
    остаётся экраном про подписку/статус.
    """
    tg_id = callback.from_user.id

    is_active = False
    try:
        is_active = await is_user_premium_active(tg_id)
    except Exception:
        is_active = False

    kb = InlineKeyboardBuilder()
    if is_active:
        # У пользователя уже есть премиум
        kb.button(text="💳 Подписка", callback_data="profile:premium")
        kb.button(text="✨ Преимущества", callback_data="profile:premium_benefits")
    else:
        # Пока нет премиума — ведём на экран подписки и даём почитать преимущества
        kb.button(text="💳 Оформить премиум", callback_data="profile:premium")
        kb.button(text="✨ Преимущества", callback_data="profile:premium_benefits")

    kb.button(text="🏠 В меню", callback_data="menu:back")
    kb.adjust(1)

    if is_active:
        text = (
            "✨ <b>Премиум-панель GlowShot</b>\n\n"
            "У тебя уже активен премиум-аккаунт.\n\n"
            "Здесь будут собраны все дополнительные функции и настройки премиума: "
            "управление подпиской, дополнительные инструменты, новые фичи.\n\n"
            "Пока доступно управление подпиской и список преимуществ.\n"
            "Новые возможности будут появляться постепенно 👀"
        )
    else:
        text = (
            "✨ <b>Премиум-панель GlowShot</b>\n\n"
            "У тебя пока нет активной премиум-подписки.\n\n"
            "Через эту панель ты сможешь управлять премиум-функциями и видеть новые фичи.\n\n"
            "Нажми «💳 Оформить премиум», чтобы перейти к экрану подписки."
        )

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:premium")
async def profile_premium_menu(callback: CallbackQuery):
    tg_id = callback.from_user.id

    is_active = False
    premium_until_human: str | None = None
    raw_status = {}

    try:
        raw_status = await get_user_premium_status(tg_id)
        is_active = await is_user_premium_active(tg_id)
        premium_until_human = _format_premium_until(raw_status.get("premium_until"))
    except Exception:
        # В случае ошибок просто покажем базовый текст без статуса
        pass

        kb = InlineKeyboardBuilder()

        # Отдельная премиум-панель, чтобы из профиля можно было зайти в общий премиум-центр
        kb.button(text="✨ Премиум-панель", callback_data="premium:menu")

        if is_active:
            kb.button(text="✨ Преимущества", callback_data="profile:premium_benefits")
            kb.button(text="💳 Управление подпиской", callback_data="profile:premium_buy")
        else:
            kb.button(text="💳 Оплатить подписку", callback_data="profile:premium_buy")
            kb.button(text="✨ Преимущества", callback_data="profile:premium_benefits")

        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(1)

    if is_active:
        if premium_until_human:
            status_line = f"Статус: <b>активен</b> до {premium_until_human}."
        else:
            # Бессрочный премиум
            status_line = "Статус: <b>активен</b> (бессрочно)."

        text = (
            "💎 <b>GlowShot Premium</b>\n\n"
            "У тебя уже активен премиум-аккаунт.\n"
            f"{status_line}\n\n"
            "Ты можешь продлить подписку или изменить тариф через кнопку "
            "<b>«Управление подпиской»</b> ниже.\n\n"
            "Оплата премиума происходит через <b>Telegram Stars</b> (⭐)."
        )
    else:
        # Если флаг is_premium стоит, но срок истёк — покажем понятный статус
        if raw_status.get("is_premium") and raw_status.get("premium_until"):
            expired_line = "Статус: <b>срок действия истёк</b>.\n\n"
        else:
            expired_line = ""

        text = (
            "💎 <b>GlowShot Premium</b>\n\n"
            f"{expired_line}"
            "GlowShot Premium — это расширенные возможности для тех, кто серьёзно относится к своим кадрам.\n\n"
            "Оформить подписку можно прямо в боте через <b>Telegram Stars</b> (⭐).\n"
            "Нажми кнопку <b>«Оплатить подписку»</b> ниже, чтобы выбрать тариф."
        )

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:premium_benefits")
async def profile_premium_benefits(callback: CallbackQuery):
    """
    Экран с преимуществами премиума (пока статичный текст-заглушка).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    text = (
        "✨ <b>Преимущества GlowShot Premium</b>\n\n"
        "Планируемые возможности для премиум-пользователей:\n\n"
        "• Расширенная статистика по фотографиям\n"
        "• Дополнительные инструменты продвижения\n"
        "• Повтор участия в итогах для понравившихся работ\n"
        "• Возможность указать свой телеграм-канал в профиле\n"
        "• Приоритетная поддержка\n\n"
        "Список возможностей будет пополняться. "
        "Следи за обновлениями в боте 👀"
    )

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
