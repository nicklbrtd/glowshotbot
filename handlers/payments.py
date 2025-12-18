
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
from config import PAYMENT_PROVIDER_TOKEN, ROBOKASSA_LOGIN, ROBOKASSA_PASSWORD1, ROBOKASSA_IS_TEST

from database import (
    set_user_premium_status,
    log_successful_payment,
    get_user_premium_status,
)
from utils.time import get_moscow_now
from aiogram.utils.keyboard import InlineKeyboardBuilder
import hashlib
import random
import time
from urllib.parse import urlencode

router = Router(name="payments")

# Базовые тарифы премиума.
# Для каждого тарифа указываем цену и в рублях, и в Stars.
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


ROBOKASSA_ENABLED = bool(ROBOKASSA_LOGIN and ROBOKASSA_PASSWORD1)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _money_rub_str(amount_rub: int) -> str:
    # 79 -> "79.00"
    return f"{amount_rub:.2f}"


def build_robokassa_pay_url(tg_id: int, period_code: str) -> str:
    """Собираем ссылку на оплату Robokassa.

    В подпись (SignatureValue) обязательно входят все Shp_* параметры.
    Для тестового режима добавляем IsTest=1.
    """
    tariff = TARIFFS.get(period_code)
    if not tariff:
        raise ValueError("Unknown tariff")

    out_sum = _money_rub_str(int(tariff["price_rub"]))

    # InvId должен быть уникальным
    inv_id = int(time.time()) * 1000 + random.randint(0, 999)

    desc = f"GlowShot Premium {period_code}"

    # кастомные поля (пойдут в подпись и придут в ResultURL)
    shp = {
        "Shp_tg_id": str(tg_id),
        "Shp_period": str(period_code),
    }

    base = f"{ROBOKASSA_LOGIN}:{out_sum}:{inv_id}:{ROBOKASSA_PASSWORD1}"
    for k in sorted(shp.keys()):
        base += f":{k}={shp[k]}"

    sig = _md5_hex(base)

    params = {
        "MerchantLogin": ROBOKASSA_LOGIN,
        "OutSum": out_sum,
        "InvId": str(inv_id),
        "Description": desc,
        "SignatureValue": sig,
        **shp,
        "Culture": "ru",
    }

    if ROBOKASSA_IS_TEST:
        params["IsTest"] = "1"

    return "https://auth.robokassa.ru/Merchant/Index.aspx?" + urlencode(params)


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
    kb.button(text=f"{stars_price} ⭐️ — Telegram Stars", callback_data=f"premium:order:stars:{period_code}")

    if ROBOKASSA_ENABLED:
        kb.button(text=f"{rub_price} ₽ — Карта", callback_data=f"premium:rk:prepare:{period_code}")
    else:
        kb.button(text=f"{rub_price} ₽ — Карта", callback_data="premium:rk:not_ready")

    kb.button(text="❌ Отмена", callback_data="profile:premium")
    kb.adjust(1)

    text = (
        f"💎 <b>GlowShot Premium {period_title}</b>\n\n"
        "Выбери способ оплаты:"
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "premium:rk:not_ready")
async def premium_rk_not_ready(callback: CallbackQuery):
    await callback.answer("Оплата картой пока недоступна (Robokassa не настроена).", show_alert=True)


@router.callback_query(F.data.startswith("premium:rk:prepare:"))
async def premium_prepare_robokassa(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, _, period_code = parts
    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    if not ROBOKASSA_ENABLED:
        await callback.answer("Robokassa не настроена 😔", show_alert=True)
        return

    period_title = {
        "7d": "на неделю",
        "30d": "на месяц",
        "90d": "на 3 месяца",
    }.get(period_code, period_code)

    try:
        pay_url = build_robokassa_pay_url(callback.from_user.id, period_code)
    except Exception as e:
        await callback.answer(f"Не удалось собрать ссылку: {e}", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Оплатить 💳", url=pay_url)
    kb.button(text="⬅️ Назад", callback_data=f"premium:plan:{period_code}")
    kb.adjust(1)

    test_line = "\n\n🧪 <b>Robokassa: тестовый режим включен</b>" if ROBOKASSA_IS_TEST else ""

    text = (
        f"💎 <b>GlowShot Premium {period_title}</b>\n\n"
        "Ваш счёт готов:\n"
        "Нажми «Оплатить» — откроется страница Robokassa.\n"
        "После оплаты тебя вернёт обратно в бот."
        f"{test_line}"
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()




@router.callback_query(F.data.startswith("premium:order:"))
async def premium_create_invoice(callback: CallbackQuery):
    """
    Создание инвойса на оплату премиума.
    Поддерживаются способы:
    - RUB (через провайдера и PAYMENT_PROVIDER_TOKEN)
    - XTR (Telegram Stars)
    """
    parts = (callback.data or "").split(":")
    # ожидаем "premium:order:METHOD:PERIOD"
    if len(parts) != 4:
        await callback.answer("Некорректный тариф.", show_alert=True)
        return

    _, _, method, period_code = parts
    if method not in ("rub", "stars"):
        await callback.answer("Некорректный способ оплаты.", show_alert=True)
        return

    tariff = TARIFFS.get(period_code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    if method == "rub":
        if not PAYMENT_PROVIDER_TOKEN:
            await callback.answer(
                "Оплата картой временно недоступна 😔", show_alert=True
            )
            return

        amount = int(tariff["price_rub"] * 100)
        currency = "RUB"
        provider_token = PAYMENT_PROVIDER_TOKEN
        label = tariff["label"]
    else:
        # Stars — просто количество звёзд, без умножения
        amount = int(tariff["price_stars"])  # 5, 15, 40 и т.д.
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

    # Ожидаем payload формата 'premium:rub:7d' или 'premium:stars:7d'
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
    if method == "rub":
        pay_method_line = "Способ оплаты: 💳 карта (RUB)."
    else:
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