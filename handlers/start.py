import os
import random
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import InlineKeyboardMarkup
from utils.time import get_moscow_now

from database import(
get_user_by_tg_id, 
is_user_premium_active, 
save_pending_referral
)
from keyboards.common import build_main_menu

router = Router()

REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@nyqcreative")


def _get_flag(user, key: str) -> bool:
    if user is None:
        return False

    try:
        value = user[key]  # type: ignore[index]
    except Exception:
        try:
            value = getattr(user, key)
        except Exception:
            return False

    return bool(value)


async def is_user_subscribed(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
    except TelegramBadRequest:
        return False

    return member.status in ("member", "administrator", "creator")


def build_subscribe_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура для экрана обязательной подписки:
    «Подписаться» + «Готово», сразу в виде InlineKeyboardMarkup.
    """
    kb = InlineKeyboardBuilder()

    if isinstance(REQUIRED_CHANNEL_ID, str) and REQUIRED_CHANNEL_ID.startswith("@"):
        channel_link = f"https://t.me/{REQUIRED_CHANNEL_ID.lstrip('@')}"
    else:
        channel_link = "https://t.me/nyqcreative"

    kb.button(
        text="🔔 Подписаться",
        url=channel_link,
    )
    kb.button(
        text="✅ Готово",
        callback_data="sub:check",
    )
    kb.adjust(1)
    return kb.as_markup()

def build_menu_text(is_premium: bool) -> str:
    """Формирует текст главного меню с таймером до итогов
    и небольшим рандомным сообщением.

    Для обычных пользователей — базовая реклама премиума.
    Для премиум‑пользователей — напоминание о доступных возможностях.
    """
    now = get_moscow_now()

    results_hour = 7
    results_minute = 0

    today_results_time = now.replace(
        hour=results_hour, minute=results_minute, second=0, microsecond=0
    )

    lines: list[str] = []

    lines.append("<b>GlowShot</b> — бот для любителей фотографии.")
    lines.append("")

    if now < today_results_time:
        # До ближайших итогов — в 07:00 по МСК
        delta = today_results_time - now
        total_seconds = int(delta.total_seconds())
        hours_left = total_seconds // 3600
        minutes_left = (total_seconds % 3600) // 60

        parts: list[str] = []
        if hours_left > 0:
            parts.append(f"{hours_left} ч")
        if minutes_left > 0:
            parts.append(f"{minutes_left} мин")

        if parts:
            left_str = " ".join(parts)
        else:
            left_str = "меньше минуты"

        lines.append(
            "Итоги дня подводятся каждый день в <b>07:00 по МСК</b>."
        )
        lines.append(f"До ближайших итогов осталось: <b>{left_str}</b>.")
    else:
        # Итоги за прошлый день уже есть — подталкиваем посмотреть
        lines.append("Итоги дня уже подведены — загляни в раздел «Итоги дня» 👇")
        lines.append(
            "Следующие итоги будут завтра в <b>07:00 по МСК</b>."
        )

    lines.append("")
    lines.append(
        "Выкладывай, Оценивай и Побеждай.\n"
        "<b>Группа:</b> @groupofglowshot"
    )

    # Рекламные блоки
    non_premium_promos = [
        "Премиум пока на стадии <b>разработки</b>, но скоро будет доступен!",
        "С премиум будет можно <b>добавить ссылку на свой телеграм‑канал в профиль!</b>",
        "С премиум будет можно <b>добавлять две фотографии, а не одну!</b>",
        "С премиум будет можно <b>оставлять приватные комментарии к фотографиям!</b>",
        "С премиум будет можно <b>видеть расширенную статистику</b> по своим фотографиям!",
        "С премиум у вас будет показываться 💎 другим людям!",
        "Премиум уже доступен, но пока в <b>тестовом режиме:(</b>",
    ]

    premium_promos = [
        "У тебя активен <b>GlowShot Премиум</b> - ты крут!",
        "Ты в GlowShot Премиум: можно добавить ссылку на канал в профиль.",
    ]

    promos_for_user = premium_promos if is_premium else non_premium_promos

    # Примерно в 1/3 случаев показываем промо
    if promos_for_user and random.random() < 0.33:
        lines.append("")
        lines.append(random.choice(promos_for_user))

    lines.append("")

    return "\n".join(lines)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    payload = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
    # Deep-link "Оценки по ссылке" (/start rate_CODE) is handled in handlers/linklike.py.
    # If we handle it here, non-registered users won't be able to rate.
    if payload and payload.startswith("rate_"):
        raise SkipHandler
    if payload in ("payment_success", "payment_fail"):
        user = await get_user_by_tg_id(message.from_user.id)

        is_premium = await is_user_premium_active(message.from_user.id)

        if payload == "payment_success":
            if is_premium:
                payment_note = (
                    "✅ <b>Оплата прошла!</b> Премиум уже активен.\n"
                    "Спасибо за поддержку проекта! 🎉"
                )
            else:
                payment_note = (
                    "🧾 <b>Платёж принят</b>. Сейчас подтверждаем оплату…\n"
                    "Обычно это занимает до 1 минуты.\n"
                )
        else:
            payment_note = (
                "❌ <b>Оплата не завершена</b> (отмена/ошибка).\n"
                "Если это была ошибка — попробуй ещё раз в «Профиль → Премиум»."
            )

        # Пытаемся обновить уже существующее сообщение меню (не спамим чат)
        data = await state.get_data()
        menu_msg_id = data.get("menu_msg_id")

        if user:
            is_admin = _get_flag(user, "is_admin")
            is_moderator = _get_flag(user, "is_moderator")
        else:
            is_admin = False
            is_moderator = False

        menu_text = build_menu_text(is_premium=is_premium) + "\n\n" + payment_note
        reply_kb = build_main_menu(
            is_admin=is_admin,
            is_moderator=is_moderator,
            is_premium=is_premium,
        )

        edited = False
        if menu_msg_id:
            try:
                await message.bot.edit_message_text(
                    menu_text,
                    chat_id=message.chat.id,
                    message_id=menu_msg_id,
                    reply_markup=reply_kb,
                )
                edited = True
            except Exception:
                edited = False

        if not edited:
            # Если меню ещё не было (или его нельзя отредактировать) — создаём новое меню
            sent = await message.answer(
                menu_text,
                reply_markup=reply_kb,
                disable_notification=True,
            )
            data["menu_msg_id"] = sent.message_id
            await state.set_data(data)

        # Убираем сам /start, чтобы не плодить сообщения
        try:
            await message.delete()
        except Exception:
            pass
        return

    user = await get_user_by_tg_id(message.from_user.id)

    if user is None:
        # Если человек зашёл по реферальной ссылке вида /start ref_CODE — сохраняем pending
        if payload and payload.startswith("ref_"):
            ref_code = payload[4:].strip()
            if ref_code:
                try:
                    await save_pending_referral(message.from_user.id, ref_code)
                except Exception:
                    pass

        text = (
            "🙃 Привет! Это <b>GlowShot</b> — бот для тех, кто любит фотографировать.\n\n"
            "Здесь мы оцениваем <b>кадры</b>.\n"
            "<b>Выкладывай</b> свои лучшие фотографии, <b>оценивай</b> работы других и <b>побеждай</b> в итогах.\n\n"
            "Но для начала нужно зарегистрироваться:"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🚀 Зарегистрироваться", callback_data="auth:start")
        kb.adjust(1)
        await message.answer(
            text,
            reply_markup=kb.as_markup(),
            disable_notification=True,
        )
        try:
            await message.delete()
        except Exception:
            pass
        return

    if await is_user_subscribed(message.bot, message.from_user.id):
        # флаги ролей
        is_admin = _get_flag(user, "is_admin")
        is_moderator = _get_flag(user, "is_moderator")
        is_premium = await is_user_premium_active(message.from_user.id)

        chat_id = message.chat.id
        data = await state.get_data()
        menu_msg_id = data.get("menu_msg_id")

        sent_message = None

        if menu_msg_id:
            # Пытаемся отредактировать уже существующее сообщение меню
            try:
                await message.bot.edit_message_text(
                    build_menu_text(is_premium=is_premium),
                    chat_id=chat_id,
                    message_id=menu_msg_id,
                    reply_markup=build_main_menu(
                        is_admin=is_admin,
                        is_moderator=is_moderator,
                        is_premium=is_premium,
                    ),
                )
            except TelegramBadRequest:
                # Если редактирование не удалось (сообщение удалено/устарело) — отправляем новое
                sent_message = await message.answer(
                    build_menu_text(is_premium=is_premium),
                    reply_markup=build_main_menu(
                        is_admin=is_admin,
                        is_moderator=is_moderator,
                        is_premium=is_premium,
                    ),
                    disable_notification=True,
                )
        else:
            # Меню ещё ни разу не показывалось — отправляем новое сообщение
            sent_message = await message.answer(
                build_menu_text(is_premium=is_premium),
                reply_markup=build_main_menu(
                    is_admin=is_admin,
                    is_moderator=is_moderator,
                    is_premium=is_premium,
                ),
                disable_notification=True,
            )

        # Если мы отправили новое меню — запоминаем его message_id в состоянии
        if sent_message is not None:
            data["menu_msg_id"] = sent_message.message_id
            await state.set_data(data)
    else:
        sub_kb = build_subscribe_keyboard()
        await message.answer(
            "Чтобы пользоваться ботом, подпишись на наш канал.\n\n"
            "1) Нажми «🔔 Подписаться»\n"
            "2) Вернись сюда и нажми «✅ Готово»",
            reply_markup=sub_kb,
            disable_notification=True,
        )
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data == "sub:check")
async def subscription_check(callback: CallbackQuery):
    user_id = callback.from_user.id

    if await is_user_subscribed(callback.bot, user_id):
        # достаём пользователя и флаги ролей
        user = await get_user_by_tg_id(user_id)
        is_admin = _get_flag(user, "is_admin")
        is_moderator = _get_flag(user, "is_moderator")
        is_premium = await is_user_premium_active(user_id)

        try:
            await callback.message.edit_text(
                build_menu_text(is_premium=is_premium),
                reply_markup=build_main_menu(
                    is_admin=is_admin,
                    is_moderator=is_moderator,
                    is_premium=is_premium,
                ),
            )
        except Exception:
            try:
                await callback.message.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text=build_menu_text(is_premium=is_premium),
                    reply_markup=build_main_menu(
                        is_admin=is_admin,
                        is_moderator=is_moderator,
                        is_premium=is_premium,
                    ),
                    disable_notification=True,
                )
            except Exception:
                await callback.message.answer(
                    build_menu_text(is_premium=is_premium),
                    reply_markup=build_main_menu(
                        is_admin=is_admin,
                        is_moderator=is_moderator,
                        is_premium=is_premium,
                    ),
                    disable_notification=True,
                )
        await callback.answer("Спасибо за подписку! 🎉", show_alert=False)
    else:
        await callback.answer(
            "Похоже, ты ещё не подписан на канал.\nПодпишись и попробуй снова 🙂",
            show_alert=True,
        )


@router.callback_query(F.data == "menu:back")
async def menu_back(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        # query уже протух — просто игнорируем, для UX это не критично
        pass
    chat_id = callback.message.chat.id
    data = await state.get_data()
    photo_msg_id = data.get("myphoto_photo_msg_id")

    user = await get_user_by_tg_id(callback.from_user.id)
    is_admin = _get_flag(user, "is_admin")
    is_moderator = _get_flag(user, "is_moderator")
    is_premium = await is_user_premium_active(callback.from_user.id)

    menu_msg_id = None

    # 1. Пытаемся превратить текущее сообщение в меню
    try:
        await callback.message.edit_text(
            build_menu_text(is_premium=is_premium),
            reply_markup=build_main_menu(
                is_admin=is_admin,
                is_moderator=is_moderator,
                is_premium=is_premium,
            ),
        )
        menu_msg_id = callback.message.message_id
    except Exception:
        # 2. Если редактировать нельзя (фото, удалено и т.д.) — сначала шлём НОВОЕ меню
        try:
            sent = await callback.message.bot.send_message(
                chat_id=chat_id,
                text=build_menu_text(is_premium=is_premium),
                reply_markup=build_main_menu(
                    is_admin=is_admin,
                    is_moderator=is_moderator,
                    is_premium=is_premium,
                ),
                disable_notification=True,
            )
        except Exception:
            sent = await callback.message.answer(
                build_menu_text(is_premium=is_premium),
                reply_markup=build_main_menu(
                    is_admin=is_admin,
                    is_moderator=is_moderator,
                    is_premium=is_premium,
                ),
                disable_notification=True,
            )
        menu_msg_id = sent.message_id

        # 3. И только ПОСЛЕ этого удаляем старое сообщение (с фоткой/разделом)
        try:
            await callback.message.delete()
        except Exception:
            pass

    # 4. После того, как меню появилось, можно спокойно удалить сообщение с фоткой
    if photo_msg_id:
        try:
            # на всякий случай не трогаем меню, если id совпали
            if photo_msg_id != menu_msg_id:
                await callback.message.bot.delete_message(
                    chat_id=chat_id,
                    message_id=photo_msg_id,
                )
        except Exception:
            pass
        data["myphoto_photo_msg_id"] = None

    # можно ещё сохранить menu_msg_id в state, если пользуешься этим выше
    if menu_msg_id:
        data["menu_msg_id"] = menu_msg_id
    await state.set_data(data)