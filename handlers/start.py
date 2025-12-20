import os
import random
import html
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import InlineKeyboardMarkup

import database as db
from keyboards.common import build_main_menu

router = Router()

REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@nyqcreative")
# TODO: заполняй вручную — если пусто, премиум-блок не показывается
PREMIUM_WEEKLY_UPDATES: list[str] = [
    "🆕 Улучшена статистика в профиле (умный скор, больше метрик)",
]


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

async def build_menu_text(*, tg_id: int, user: dict | None, is_premium: bool) -> str:
    """Формирует текст главного меню (персональный)."""

    # Имя
    name = None
    if user:
        try:
            name = user.get("name") or user.get("first_name")
        except Exception:
            name = None
    if not name:
        name = "друг"

    safe_name = html.escape(str(name), quote=False)

    # Статы (внутри сворачиваемой цитаты)
    active_count_text = "—"
    can_change_text = "—"
    active_rating_text = "—"
    can_rate_text = "—"

    try:
        # Берём пул из database.py (он у тебя уже есть)
        p = db._assert_pool()  # type: ignore[attr-defined]

        # Внутренний id пользователя (если есть) + tg_id (на случай легаси)
        internal_id = None
        if user:
            try:
                internal_id = int(user.get("id"))
            except Exception:
                internal_id = None

        candidate_user_ids: list[int] = []
        if internal_id is not None:
            candidate_user_ids.append(int(internal_id))
        candidate_user_ids.append(int(tg_id))

        # Лимит активных фото
        limit_active = 2 if is_premium else 1

        async with p.acquire() as conn:
            # Активных фото
            try:
                active_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM photos
                    WHERE user_id = ANY($1::bigint[]) AND is_deleted = 0
                    """,
                    candidate_user_ids,
                )
                if active_count is None:
                    active_count = 0
                active_count_text = str(int(active_count))
                can_change_text = "можно изменить" if int(active_count) < int(limit_active) else "нельзя изменить"
            except Exception:
                pass

            # Рейтинг активной фотки (берём самую свежую активную по id)
            try:
                row = await conn.fetchrow(
                    """
                    SELECT
                        ph.id,
                        COUNT(r.id)::int AS ratings_count,
                        AVG(r.value)::float AS avg_rating
                    FROM photos ph
                    LEFT JOIN ratings r ON r.photo_id = ph.id
                    WHERE ph.user_id = ANY($1::bigint[]) AND ph.is_deleted = 0
                    GROUP BY ph.id
                    ORDER BY ph.id DESC
                    LIMIT 1
                    """,
                    candidate_user_ids,
                )
                if row:
                    cnt = int(row.get("ratings_count") or 0)
                    avg = row.get("avg_rating")
                    if cnt > 0 and avg is not None:
                        avg_f = float(avg)
                        avg_s = f"{avg_f:.2f}".rstrip("0").rstrip(".")
                        active_rating_text = avg_s
                    elif cnt > 0:
                        active_rating_text = str(cnt)
                    else:
                        active_rating_text = "—"
            except Exception:
                pass

            # Можно оценить: фотки, которые пользователь ещё не оценивал
            try:
                rater_ids: list[int] = []
                if internal_id is not None:
                    rater_ids.append(int(internal_id))
                rater_ids.append(int(tg_id))

                unrated = await conn.fetchval(
                    """
                    SELECT COUNT(*)::int
                    FROM photos ph
                    WHERE ph.is_deleted = 0
                      AND ph.user_id <> ALL($1::bigint[])
                      AND NOT EXISTS (
                        SELECT 1
                        FROM ratings r
                        WHERE r.photo_id = ph.id
                          AND r.user_id = ANY($2::bigint[])
                      )
                    """,
                    candidate_user_ids,
                    rater_ids,
                )
                if unrated is None:
                    unrated = 0
                can_rate_text = str(int(unrated))
            except Exception:
                pass


    except Exception:
        # Если что-то пошло не так — просто оставляем дефолтные "—"
        pass

    stats_lines = [
        f"Фото: {active_count_text} активная ({can_change_text})",
        f"Рейтинг: {active_rating_text}",
        f"Можно оценить: {can_rate_text}",
    ]

    stats_block = "\n".join(stats_lines)

    lines: list[str] = []
    greet_prefix = "💎 " if is_premium else ""
    lines.append(f"{greet_prefix}Привет, {safe_name}")
    lines.append(stats_block)

    # Рекламный блок — только не премиум, и всегда показываем
    if not is_premium:
        promos = [
            "🆕 Новость: скоро появятся новые итоги и больше топов 🏁",
            "💎 Хочешь больше возможностей? Премиум скоро станет ещё круче.",
            "💎 Премиум даёт 2 активные фотки и расширенную статистику.",
            "💎 Поддержи проект — получи удобные фичи и меньше ограничений.",
            "💎 Премиум: больше слотов, больше топов, больше кайфа.",
        ]
        lines.append("")
        lines.append("<b>Рекламный блок:</b>")
        lines.append(random.choice(promos))

    # Премиум блок — только премиум, и только если есть обновления
    if is_premium and PREMIUM_WEEKLY_UPDATES:
        lines.append("")
        lines.append("<b>Премиум блок:</b>")
        for upd in PREMIUM_WEEKLY_UPDATES:
            lines.append(f"• {html.escape(str(upd), quote=False)}")

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
        user = await db.get_user_by_tg_id(message.from_user.id)

        is_premium = await db.is_user_premium_active(message.from_user.id)

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

        menu_text = (await build_menu_text(tg_id=message.from_user.id, user=user, is_premium=is_premium)) + "\n\n" + payment_note
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

    user = await db.get_user_by_tg_id(message.from_user.id)

    if user is None:
        # Если человек зашёл по реферальной ссылке вида /start ref_CODE — сохраняем pending
        if payload and payload.startswith("ref_"):
            ref_code = payload[4:].strip()
            if ref_code:
                try:
                    await db.save_pending_referral(message.from_user.id, ref_code)
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
        is_premium = await db.is_user_premium_active(message.from_user.id)

        chat_id = message.chat.id
        data = await state.get_data()
        menu_msg_id = data.get("menu_msg_id")

        sent_message = None
        menu_text = await build_menu_text(tg_id=message.from_user.id, user=user, is_premium=is_premium)

        if menu_msg_id:
            # Пытаемся отредактировать уже существующее сообщение меню
            try:
                await message.bot.edit_message_text(
                    menu_text,
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
                    menu_text,
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
                menu_text,
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
        user = await db.get_user_by_tg_id(user_id)
        is_admin = _get_flag(user, "is_admin")
        is_moderator = _get_flag(user, "is_moderator")
        is_premium = await db.is_user_premium_active(user_id)
        menu_text = await build_menu_text(tg_id=user_id, user=user, is_premium=is_premium)
        try:
            await callback.message.edit_text(
                menu_text,
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
                    text=menu_text,
                    reply_markup=build_main_menu(
                        is_admin=is_admin,
                        is_moderator=is_moderator,
                        is_premium=is_premium,
                    ),
                    disable_notification=True,
                )
            except Exception:
                await callback.message.answer(
                    menu_text,
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

    user = await db.get_user_by_tg_id(callback.from_user.id)
    is_admin = _get_flag(user, "is_admin")
    is_moderator = _get_flag(user, "is_moderator")
    is_premium = await db.is_user_premium_active(callback.from_user.id)

    menu_msg_id = None

    menu_text = await build_menu_text(tg_id=callback.from_user.id, user=user, is_premium=is_premium)
    # 1. Пытаемся превратить текущее сообщение в меню
    try:
        await callback.message.edit_text(
            menu_text,
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
                text=menu_text,
                reply_markup=build_main_menu(
                    is_admin=is_admin,
                    is_moderator=is_moderator,
                    is_premium=is_premium,
                ),
                disable_notification=True,
            )
        except Exception:
            sent = await callback.message.answer(
                menu_text,
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