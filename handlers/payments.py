from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    Message,
    InlineKeyboardMarkup,
)
from keyboards.common import build_viewed_kb, build_back_kb
from config import PAYMENT_PROVIDER_TOKEN

from database import (
    set_user_premium_status,
    log_successful_payment,
    get_user_premium_status,
)
from utils.time import get_moscow_now
from aiogram.utils.keyboard import InlineKeyboardBuilder
import time

router = Router(name="payments")

TARIFFS = {
    "7d": {
        "days": 7,
        "price_rub": 79,
        "price_stars": 70,
        "title": "GlowShot Premium — 7 дней",
        "label": "Премиум на 7 дней",
        "description": "Доступ ко всем премиум-функциям на 7 дней.",
    },
    "30d": {
        "days": 30,
        "price_rub": 239,
        "price_stars": 200,
        "title": "GlowShot Premium — 30 дней",
        "label": "Премиум на 30 дней",
        "description": "Доступ ко всем премиум-функциям на 30 дней.",
    },
    "90d": {
        "days": 90,
        "price_rub": 569,
        "price_stars": 500,
        "title": "GlowShot Premium — 90 дней",
        "label": "Премиум на 90 дней",
        "description": "Доступ ко всем премиум-функциям на 90 дней.",
    },
}


# --- Manual RUB payments (temporary, no Robokassa) ---
MANUAL_RUB_ENABLED = True
MANUAL_CARD_NUMBER = "XXXX XXXX XXXX XXXX"  # TODO: укажи номер карты
MANUAL_RECIPIENT = "ФИО получателя"        # TODO: укажи получателя
MANUAL_BANK_HINT = "Любой банк"            # можно оставить
MANUAL_CONTACT = "@your_username"          # TODO: твой юзернейм


# --- Новый flow для премиум-панели и тарифов ---

@router.callback_query(F.data == "premium:plans")
async def premium_plans_from_active(callback: CallbackQuery):
    """Экран выбора периода (для продления из активного премиума)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Неделя 70 ⭐️ / 79 ₽", callback_data="premium:plan:7d")
    kb.button(text="Месяц 230 ⭐️ / 239 ₽", callback_data="premium:plan:30d")
    kb.button(text="3 месяца 500 ⭐️ / 569 ₽", callback_data="premium:plan:90d")
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    await callback.message.edit_text(
        "💎 <b>Продление GlowShot Premium</b>\n\nВыбери период подписки:",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("premium:plan:"))
async def premium_choose_method(callback: CallbackQuery):
    """После выбора периода — предлагаем способ оплаты (Stars / Карта)."""
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, period_code = parts
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    period_title = {
        "7d": "на неделю",
        "30d": "на месяц",
        "90d": "на 3 месяца",
    }.get(period_code, period_code)

    stars_price = tariff["price_stars"]
    rub_price = tariff["price_rub"]

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"{stars_price} ⭐️ — Telegram Stars",
        callback_data=f"premium:order:stars:{period_code}",
    )

    if MANUAL_RUB_ENABLED:
        kb.button(
            text=f"{rub_price} ₽ — Перевод на карту",
            callback_data=f"premium:manual_rub:{period_code}",
        )
    else:
        kb.button(
            text=f"{rub_price} ₽ — Перевод на карту (скоро)",
            callback_data="premium:rub:disabled",
        )

    kb.button(text="❌ Отмена", callback_data="profile:premium")
    kb.adjust(1)

    text = (
        f"💎 <b>GlowShot Premium {period_title}</b>\n\n"
        "Выбери способ оплаты:"
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "premium:rub:disabled")
async def premium_rub_disabled(callback: CallbackQuery):
    await callback.answer(
        "Оплата рублями сейчас отключена. Пока доступна оплата Telegram Stars ⭐️",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("premium:manual_rub:"))
async def premium_manual_rub(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, period_code = parts
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    rub_price = tariff["price_rub"]
    comment = f"GS-{callback.from_user.id}-{period_code}-{int(time.time())}"

    text = (
        "💳 <b>Оплата переводом на карту</b>\n\n"
        f"Тариф: <b>{tariff['title']}</b>\n"
        f"Сумма: <b>{rub_price} ₽</b>\n\n"
        f"<b>Куда перевести:</b>\n"
        f"Карта: <code>{MANUAL_CARD_NUMBER}</code>\n"
        f"Получатель: <b>{MANUAL_RECIPIENT}</b>\n"
        f"Банк: {MANUAL_BANK_HINT}\n\n"
        "<b>Важно:</b> в комментарии к переводу (или в сообщении после оплаты) укажи код:\n"
        f"<code>{comment}</code>\n\n"
        "После перевода нажми кнопку «Я оплатил». Мы попросим прислать чек/скрин."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"premium:manual_rub:paid:{period_code}:{comment}")
    kb.button(text="⬅️ Назад", callback_data=f"premium:plan:{period_code}")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("premium:manual_rub:paid:"))
async def premium_manual_rub_paid(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    period_code = parts[3]
    comment = ":".join(parts[4:])

    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    text = (
        "✅ <b>Окей!</b>\n\n"
        "Пришли, пожалуйста, <b>скрин/чек</b> перевода одним сообщением в этот чат.\n\n"
        f"Код: <code>{comment}</code>\n"
        f"Тариф: <b>{tariff['title']}</b>\n\n"
        f"Если нужно быстрее — напиши: {MANUAL_CONTACT}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="profile:premium")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("premium:order:"))
async def premium_create_invoice(callback: CallbackQuery):
    """
    Создание инвойса на оплату премиума.
    Поддерживаются способы:
    - XTR (Telegram Stars)
    """
    parts = (callback.data or "").split(":")
    # ожидаем "premium:order:METHOD:PERIOD"
    if len(parts) != 4:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, method, period_code = parts
    if method not in ("stars",):
        await callback.answer("Некорректный способ оплаты.", show_alert=True)
        return

    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    amount = int(tariff["price_stars"])  # количество звёзд
    currency = "XTR"
    provider_token = ""  # Для Stars внешний провайдер не нужен
    label = tariff["label"]

    prices = [
        LabeledPrice(
            label=label,
            amount=amount,
        )
    ]

    try:
        await callback.bot.send_invoice(
            chat_id=callback.from_user.id,
            title=tariff["title"],
            description=tariff["description"],
            provider_token=provider_token,
            currency=currency,
            prices=prices,
            payload=f"premium:{method}:{period_code}",
            start_parameter="premium-subscription",
        )
    except Exception as e:
        await callback.answer(
            f"Не удалось создать счёт. Попробуй позже.\n\n{e}", show_alert=True
        )
        return

    await callback.answer("Отправил счёт в чат с ботом 💳")


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    """
    Подтверждаем pre-checkout запрос, чтобы Telegram продолжил оплату.
    """
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    """
    Обработка успешной оплаты: активируем премиум пользователю.
    """
    successful_payment = message.successful_payment
    payload = successful_payment.invoice_payload or ""

    # Ожидаем payload формата 'premium:stars:7d'
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "premium":
        await message.answer(
            "Оплата получена, но тариф не распознан.\n"
            "Напиши, пожалуйста, в поддержку, чтобы мы проверили вручную."
        )
        return

    _, method, period_code = parts
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await message.answer(
            "Оплата получена, но тариф не найден.\n"
            "Напиши, пожалуйста, в поддержку, чтобы мы проверили вручную."
        )
        return

    days = tariff["days"]
    now = get_moscow_now()

    # ✅ Продление премиума: если он уже активен и premium_until в будущем — добавляем дни к текущему сроку.
    base_dt = now
    try:
        current = await get_user_premium_status(message.from_user.id)
        current_until = (current or {}).get("premium_until")
        if current_until:
            try:
                cur_dt = datetime.fromisoformat(current_until)
                if cur_dt > base_dt:
                    base_dt = cur_dt
            except Exception:
                pass
    except Exception:
        pass

    until_dt = base_dt + timedelta(days=days)
    premium_until_iso = until_dt.isoformat(timespec="seconds")
    human_until = until_dt.strftime("%d.%m.%Y")

    await set_user_premium_status(
        message.from_user.id,
        True,
        premium_until=premium_until_iso,
    )


    # Логируем платёж в свою таблицу payments
    try:
        await log_successful_payment(
            tg_id=message.from_user.id,
            method=method,
            period_code=period_code,
            days=days,
            amount=successful_payment.total_amount,
            currency=successful_payment.currency,
            telegram_charge_id=getattr(successful_payment, "telegram_payment_charge_id", None),
            provider_charge_id=getattr(successful_payment, "provider_payment_charge_id", None),
        )
    except Exception:
        # Если что-то пойдёт не так, оплата всё равно считается успешной
        pass


    # Текст чуть-чуть различаем по способу оплаты чисто косметически
    pay_method_line = "Способ оплаты: ⭐ Telegram Stars."

    success_text = (
        "💎 <b>Оплата успешно получена!</b>\n\n"
        f"Твой GlowShot Premium активен до <b>{human_until}</b> "
        f"(на {days} дн.).\n"
        f"{pay_method_line}\n\n"
        "Спасибо, что поддерживаешь проект 💙"
    )

    kb = build_viewed_kb("premium:success_read")

    # Пытаемся по максимуму не плодить новые сообщения: сначала пробуем отредактировать
    # сам инвойс. Если Telegram не даст это сделать — тогда уже отправим новое сообщение.
    try:
        await message.edit_text(
            success_text,
            reply_markup=kb,
        )
    except Exception:
        await message.answer(
            success_text,
            reply_markup=kb,
        )


@router.callback_query(F.data == "premium:success_read")
async def premium_success_read(callback: CallbackQuery):
    """
    Пользователь отметил, что уведомление об оплате прочитано.
    По возможности удаляем сообщение, чтобы не засорять чат.
    """
    try:
        await callback.message.delete()
    except Exception:
        # Если удалить нельзя (например, в каком-то типе чата), просто убираем кнопки.
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer()