import os
from datetime import datetime, timedelta, timezone
import random
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from database import get_user_by_tg_id, is_user_premium_active
from keyboards.common import build_main_menu

router = Router()

REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@nyqcreative")


def _get_flag(user, key: str) -> bool:
    """
    Аккуратно достаём булевый флаг из объекта пользователя, не зная точно,
    dict он, sqlite.Row или dataclass.
    """
    if user is None:
        return False

    # Попытка как у словаря / sqlite.Row
    try:
        value = user[key]  # type: ignore[index]
    except Exception:
        # Попытка как у объекта с атрибутами
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
    # Текущее время по Москве
    now = datetime.now(timezone(timedelta(hours=3)))

    # Итоги дня теперь подводятся каждый день в 05:00 по Москве
    results_hour = 5
    results_minute = 0

    today_results_time = now.replace(
        hour=results_hour, minute=results_minute, second=0, microsecond=0
    )

    lines: list[str] = []

    lines.append("<b>GlowShot</b> — бот для любителей фотографии.")
    lines.append("")

    if now < today_results_time:
        # До ближайших итогов — в 05:00 по Москве
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
            "Итоги дня подводятся каждый день в <b>05:00 по Москве</b>."
        )
        lines.append(f"До ближайших итогов осталось: <b>{left_str}</b>.")
    else:
        # Итоги за прошлый день уже есть — подталкиваем посмотреть
        lines.append("Итоги дня уже подведены — загляни в раздел «Итоги дня» 👇")
        lines.append(
            "Следующие итоги будут завтра в <b>05:00 по Москве</b>."
        )

    lines.append("")
    lines.append(
        "Выложи свою фотографию, оцени чужие и следи за результатами.\n"
        "Стань легендой!"
    )

    # Рекламные блоки
    non_premium_promos = [
        "Представьте, что это реклама премиума",
        "Представьте, что тут любая другая реклама",
        "Хотите больше функций? Узнайте про GlowShot Премиум!",
        "С премиумом вы получите больше возможностей для творчества!",
        "Поддержите проект — оформите подписку на GlowShot Премиум!",
        "С премиумом можно будет добавить свой телеграм‑канал в профиль!",
        "Премиум уже доступен — посмотрите, что он даёт в разделе профиля.",
    ]

    premium_promos = [
        "У тебя активен GlowShot Премиум — используй дополнительные возможности по максимуму.",
        "Премиум даёт больше свободы: приватные комментарии, ссылка на канал и комфортный рейтинг.",
        "Ты в GlowShot Премиум: можно добавить ссылку на канал в профиль и настроить профиль под себя.",
        "Статус Премиум активен — экспериментируй с фотографиями, а бот аккуратно всё подсчитает.",
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
    user = await get_user_by_tg_id(message.from_user.id)

    if user is None:
        text = (
            "Привет! Это <b>GlowShot</b> — бот для тех, кто любит фотографировать.\n\n"
            "Здесь мы оцениваем <b>кадры</b>.\n"
            "Один день — одна работа, анонимные оценки и итоги.\n\n"
            "Для начала нужно зарегистрироваться:"
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