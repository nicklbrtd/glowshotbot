from datetime import datetime, timedelta
import os
import hashlib
import aiohttp

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from keyboards.common import build_viewed_kb
from config import (
    MANUAL_RUB_ENABLED,
    MANUAL_CARD_NUMBER,
    MANUAL_RECIPIENT,
    MANUAL_BANK_HINT,
    MANUAL_CONTACT,
)

from database import (
    set_user_premium_status,
    log_successful_payment,
    get_user_premium_status,
)
from utils.time import get_moscow_now
from aiogram.utils.keyboard import InlineKeyboardBuilder
import time

router = Router(name="payments")

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

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

# --- TBank (link payments) ---
TB_INIT_URL = os.getenv("TB_INIT_URL", "https://securepay.tinkoff.ru/v2/Init").strip()
TB_TERMINAL_KEY = os.getenv("TB_TERMINAL_KEY", "").strip()
TB_PASSWORD = os.getenv("TB_PASSWORD", "").strip()
TB_SUCCESS_URL = os.getenv("TB_SUCCESS_URL", "https://littlebrthood1.fvds.ru/pay/success").strip()
TB_FAIL_URL = os.getenv("TB_FAIL_URL", "https://littlebrthood1.fvds.ru/pay/fail").strip()
TB_NOTIFICATION_URL = os.getenv("TB_NOTIFICATION_URL", "https://littlebrthood1.fvds.ru/tbank/notify").strip()


# --- Premium Expiry Reminder keyboard ---

def build_premium_expiry_reminder_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Продлить подписку", callback_data="premium:plans")
    kb.button(text="✖️ Отмена", callback_data="premium:reminder:dismiss")
    kb.adjust(1)
    return kb.as_markup()


def _tbank_token(payload: dict, password: str) -> str:
    data = {}
    for k, v in payload.items():
        if k == "Token":
            continue
        if isinstance(v, (dict, list)):
            continue
        data[str(k)] = "" if v is None else str(v)
    data["Password"] = password
    s = "".join(data[k] for k in sorted(data.keys()))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _period_to_plan(period_code: str) -> str:
    # must match webhook parser: GS_<tgid>_<plan>_<ts>
    return {
        "7d": "w",
        "30d": "m",
        "90d": "q",
    }.get(period_code, "m")


async def tbank_create_payment_link(*, tg_id: int, period_code: str, amount_rub: int) -> str:
    """Create a hosted payment link in TBank and return PaymentURL."""
    if not TB_TERMINAL_KEY or not TB_PASSWORD:
        raise RuntimeError("TBank keys are not configured")

    plan = _period_to_plan(period_code)
    order_id = f"GS_{tg_id}_{plan}_{int(time.time())}"

    payload = {
        "TerminalKey": TB_TERMINAL_KEY,
        "Amount": int(amount_rub) * 100,
        "OrderId": order_id,
        "Description": f"GlowShot Premium ({period_code})",
        "SuccessURL": TB_SUCCESS_URL,
        "FailURL": TB_FAIL_URL,
        "NotificationURL": TB_NOTIFICATION_URL,
        "PayType": "O",
    }
    payload["Token"] = _tbank_token(payload, TB_PASSWORD)

    async with aiohttp.ClientSession() as session:
        async with session.post(TB_INIT_URL, json=payload, timeout=20) as resp:
            data = await resp.json(content_type=None)

    if not isinstance(data, dict):
        raise RuntimeError(f"Bad TBank response: {data}")

    if data.get("Success") is True and data.get("PaymentURL"):
        return str(data["PaymentURL"])

    # Sometimes error fields are: Message/Details/ErrorCode
    msg = data.get("Message") or data.get("Details") or str(data)
    raise RuntimeError(f"TBank Init failed: {msg}")

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

    # TBank link payment (карта/СБП на странице Т-Банка)
    if TB_TERMINAL_KEY and TB_PASSWORD:
        kb.button(
            text=f"{rub_price} ₽ — Оплатить картой/СБП",
            callback_data=f"premium:tbank:{period_code}",
        )
    elif MANUAL_RUB_ENABLED:
        kb.button(
            text=f"{rub_price} ₽ — Перевод на карту",
            callback_data=f"premium:manual_rub:{period_code}",
        )
    else:
        kb.button(
            text=f"{rub_price} ₽ — Оплата рублями",
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

@router.callback_query(F.data.startswith("premium:tbank:"))
async def premium_tbank_link(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, period_code = parts
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    rub_price = int(tariff["price_rub"])

    try:
        payment_url = await tbank_create_payment_link(
            tg_id=int(callback.from_user.id),
            period_code=str(period_code),
            amount_rub=rub_price,
        )
    except Exception as e:
        await callback.answer(f"Не удалось создать ссылку оплаты: {e}", show_alert=True)
        return

    text = (
        "💳 <b>Оплата через Т‑Банк</b>\n\n"
        f"Тариф: <b>{tariff['title']}</b>\n"
        f"Сумма: <b>{rub_price} ₽</b>\n\n"
        "Нажми кнопку ниже — откроется страница оплаты (карта/СБП).\n"
        "После успешной оплаты премиум <b>включится автоматически</b> в течение минуты."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть оплату", url=payment_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"premium:plan:{period_code}")],
        ]
    )

    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(
    F.data.startswith("premium:manual_rub:") & ~F.data.startswith("premium:manual_rub:paid:")
)
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

    if not MANUAL_CARD_NUMBER or not MANUAL_RECIPIENT:
        await callback.answer(
            "Оплата переводом временно недоступна: не настроены реквизиты.",
            show_alert=True,
        )
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
        "Пришли, пожалуйста, <b>скрин/чек</b>.\n\n"
        f"Код: <code>{comment}</code>\n"
        f"Тариф: <b>{tariff['title']}</b>\n\n"
        f"НАПИСАТЬ: {MANUAL_CONTACT}"
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
    """Обработка успешной оплаты.

    Поддерживает:
    - premium:stars:<period>
    """
    successful_payment = message.successful_payment
    payload = successful_payment.invoice_payload or ""
    parts = payload.split(":")

    # --- Normal premium flow ---
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

    # Один аккуратный ответ без попыток редактировать сервисное сообщение оплаты.
    # Плюс стараемся не плодить лишний мусор: удаляем сообщение об успешной оплате, если Telegram позволит.

    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Открыть Premium", callback_data="profile:premium")
    kb.button(text="✖️ Закрыть", callback_data="premium:success_read")
    kb.adjust(1)

    # Сначала пробуем удалить сервисное сообщение (оно часто и создаёт "лишний" спам)
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        success_text,
        reply_markup=kb.as_markup(),
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


# --- Premium expiry reminder dismiss handler ---

@router.callback_query(F.data == "premium:reminder:dismiss")
async def premium_reminder_dismiss(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer()