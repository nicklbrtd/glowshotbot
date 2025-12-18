from aiogram import Router, F
from aiogram.types import CallbackQuery, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import PAYMENT_PROVIDER_TOKEN

import time

from database import (
    create_invoice,
    get_invoice,
    get_user_premium_status,
    is_user_premium_active,
    mark_invoice_as_paid,
    mark_premium_as_paid,
)
from keyboards.common import build_back_kb

router = Router(name="payments")

TARIFFS = {
    "7d": {
        "title": "Премиум на неделю",
        "price_stars": 70,
        "price_rub": 79,
        "label": "premium_7d",
    },
    "30d": {
        "title": "Премиум на месяц",
        "price_stars": 230,
        "price_rub": 239,
        "label": "premium_30d",
    },
    "90d": {
        "title": "Премиум на 3 месяца",
        "price_stars": 500,
        "price_rub": 569,
        "label": "premium_90d",
    },
}

# --- Manual RUB payments (temporary, no Robokassa) ---
MANUAL_RUB_ENABLED = True
MANUAL_CARD_NUMBER = "XXXX XXXX XXXX XXXX"  # TODO: укажи номер карты
MANUAL_RECIPIENT = "ФИО получателя"        # TODO: укажи получателя
MANUAL_BANK_HINT = "Любой банк"            # можно оставить
MANUAL_CONTACT = "@your_username"          # TODO: твой юзернейм


@router.callback_query(F.data.startswith("premium:plan:"))
async def premium_choose_method(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    period_code = parts[2]
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

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
        f"Выбери способ оплаты для тарифа:\n<b>{tariff['title']}</b>\n\n"
        f"{stars_price} ⭐️ — оплата через Telegram Stars\n"
        f"{rub_price} ₽ — оплата переводом на карту\n"
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
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return

    _, _, method, period_code = parts
    if method not in ("stars",):
        await callback.answer("Неверный метод оплаты.", show_alert=True)
        return

    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    amount = int(tariff["price_stars"])  # количество звёзд
    currency = "XTR"
    provider_token = ""  # Для Stars внешний провайдер не нужен
    label = tariff["label"]

    user_id = callback.from_user.id

    invoice = await create_invoice(
        user_id=user_id,
        label=label,
        amount=amount,
        currency=currency,
        pay_method=method,
    )

    prices = [LabeledPrice(label=tariff["title"], amount=amount * 100)]

    await callback.message.answer_invoice(
        title=tariff["title"],
        description=f"Оплата {tariff['title']} через Telegram Stars",
        payload=invoice.invoice_id,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency=currency,
        prices=prices,
        start_parameter="premium",
        need_name=True,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )
    await callback.answer()


async def process_successful_payment(user_id: int, invoice_id: str):
    invoice = await get_invoice(invoice_id)
    if not invoice:
        return

    if invoice.is_paid:
        return

    await mark_invoice_as_paid(invoice_id)
    await mark_premium_as_paid(user_id, invoice.label)

    method = invoice.pay_method
    pay_method_line = "Способ оплаты: ⭐ Telegram Stars."

    # Here you can send a confirmation message or update user status


# Note: Removed Robokassa-related handlers and code as per instructions.
