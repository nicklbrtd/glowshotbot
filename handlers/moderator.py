from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from database import (
    get_user_by_tg_id,
    is_moderator_by_tg_id,
    get_photo_by_id,
    mark_photo_deleted,
    set_photo_moderation_status,
    get_next_photo_for_moderation,
    get_user_by_id,
    get_photo_stats,
    get_photo_report_stats,
    add_moderator_review,
    get_next_photo_for_self_moderation,
    get_next_photo_for_detailed_moderation,
)

# Роутер раздела модерации
router = Router()


class ModeratorStates(StatesGroup):
    """Состояния FSM для модератора."""
    # Ввод причины удаления/бана
    waiting_ban_reason = State()
    # Поиск пользователя
    waiting_user_search_query = State()
    # Поиск пользователя для бана / разбана
    waiting_user_block_query = State()


def build_moderator_menu() -> InlineKeyboardMarkup:
    """
    Клавиатура раздела модерации.

    Здесь:
    - очередь жалоб (фото со статусом under_review),
    - детальная проверка (фото со статусом under_detailed_review),
    - самостоятельная проверка (любой активный контент),
    - раздел пользователей,
    - выход обратно в главное меню.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Модерация жалоб", callback_data="mod:queue")
    kb.button(text="🧪 Детальная проверка", callback_data="mod:deep")
    kb.button(text="🧾 Проверять самостоятельно", callback_data="mod:self")
    kb.button(text="👥 Пользователи", callback_data="mod:users")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()


def build_moderator_users_menu() -> InlineKeyboardMarkup:
    """
    Подменю работы с пользователями.

    Здесь:
    - поиск пользователя;
    - блок / разбан по ID или username (интерфейс);
    - список заблокированных (интерфейсная заглушка, пока не реализована в БД).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти пользователя", callback_data="mod:users_search")
    kb.button(text="🚫 Блок / разбан", callback_data="mod:users_block")
    kb.button(text="🧾 Список заблокированных", callback_data="mod:users_blocked")
    kb.button(text="⬅️ Назад", callback_data="mod:menu")
    kb.adjust(1)
    return kb.as_markup()


def build_moderation_photo_keyboard(photo_id: int, source: str) -> InlineKeyboardMarkup:
    """
    Клавиатура для карточки модерации конкретной фотографии.

    source:
      - "queue"  — фото из очереди жалоб;
      - "self"   — фото из самостоятельной проверки.
    """
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Всё в порядке",
        callback_data=f"mod:photo_ok:{source}:{photo_id}",
    )
    kb.button(
        text="⏭ Пропустить",
        callback_data=f"mod:photo_skip:{source}:{photo_id}",
    )
    if source == "queue":
        kb.button(
            text="🔍 На детальную проверку",
            callback_data=f"mod:photo_deep:{photo_id}",
        )
        kb.button(
            text="🗑 Удалить фотографию",
            callback_data=f"mod:photo_delete:{source}:{photo_id}",
        )
        kb.button(
            text="⛔ Удалить + бан",
            callback_data=f"mod:photo_delete_ban:{source}:{photo_id}",
        )
    else:
        kb.button(
            text="👤 Профиль автора",
            callback_data=f"mod:photo_profile:{photo_id}",
        )
        kb.button(
            text="🗑 Удалить фотографию",
            callback_data=f"mod:photo_delete:{source}:{photo_id}",
        )
        kb.button(
            text="⛔ Удалить + бан",
            callback_data=f"mod:photo_delete_ban:{source}:{photo_id}",
        )
    kb.button(
        text="⬅️ Меню модерации",
        callback_data="mod:menu",
    )
    kb.adjust(1)
    return kb.as_markup()


async def _build_moderation_caption(
    photo: dict,
    *,
    show_reports: bool = False,
    show_stats: bool = False,
) -> str:
    """
    Собирает подпись к фото для модерации.

    show_reports — показать статистику жалоб;
    show_stats   — показать базовую статистику оценок.
    """
    caption_lines: list[str] = [
        "📷 <b>Фотография на модерации</b>",
        "",
        f"ID работы: <code>{photo['id']}</code>",
    ]

    # Базовая информация о работе
    title = photo.get("title")
    if title:
        caption_lines.append(f"Название: <b>«{title}»</b>")

    category = photo.get("category") or "photo"
    caption_lines.append(f"Категория: <code>{category}</code>")

    device_type = photo.get("device_type")
    device_info = photo.get("device_info")
    if device_type or device_info:
        device_parts: list[str] = []
        if device_type:
            device_parts.append(device_type)
        if device_info:
            device_parts.append(device_info)
        caption_lines.append(f"Устройство: {' — '.join(device_parts)}")

    day_key = photo.get("day_key")
    if day_key:
        caption_lines.append(f"День участия: <code>{day_key}</code>")

    moderation_status = photo.get("moderation_status")
    if moderation_status:
        caption_lines.append(f"Статус модерации: <code>{moderation_status}</code>")

    # Автор
    author_name = None
    try:
        author = await get_user_by_id(photo["user_id"])
    except Exception:
        author = None

    if author is not None:
        username = author.get("username")
        display_name = author.get("name") or author.get("display_name")
        if username:
            author_name = f"@{username}"
        elif display_name:
            author_name = display_name

    if author_name:
        caption_lines.append(f"Автор: {author_name}")

    # Описание работы (если есть)
    description = photo.get("description")
    if description:
        caption_lines.append("")
        caption_lines.append(f"Описание:\n{description}")

    # Статистика жалоб (для очереди жалоб)
    if show_reports:
        try:
            report_stats = await get_photo_report_stats(photo["id"])
        except Exception:
            report_stats = None

        if report_stats is not None:
            pending = report_stats.get("total_pending", 0)
            total = report_stats.get("total_all", 0)
            caption_lines.append("")
            caption_lines.append(
                f"🚨 Жалобы: {pending} в работе / {total} всего"
            )

    # Статистика оценок (для самостоятельной проверки и / или очереди)
    if show_stats:
        try:
            stats = await get_photo_stats(photo["id"])
        except Exception:
            stats = None

        if stats is not None:
            ratings_count = stats.get("ratings_count", 0)
            avg_rating = stats.get("avg_rating")
            skips_count = stats.get("skips_count", 0)

            caption_lines.append("")
            caption_lines.append("📊 Статистика оценок:")
            if avg_rating is not None:
                caption_lines.append(
                    f"• Средний рейтинг: <b>{avg_rating:.2f}</b>"
                )
            else:
                caption_lines.append("• Средний рейтинг: нет оценок")
            caption_lines.append(f"• Кол-во оценок: {ratings_count}")
            if skips_count:
                caption_lines.append(f"• Пропусков: {skips_count}")

    return "\n".join(caption_lines)


async def show_next_photo_for_moderation(callback: CallbackQuery) -> None:
    """
    Отправляет модератору следующую фотографию, которая ожидает проверки по жалобам.
    Берётся фото со статусом 'under_review'.
    """
    photo = await get_next_photo_for_moderation()

    if not photo:
        try:
            await callback.message.edit_text(
                "Сейчас нет фотографий, ожидающих модерации по жалобам.",
                reply_markup=build_moderator_menu(),
            )
        except TelegramBadRequest:
            # Если сообщение нельзя отредактировать (например, это уже карточка с фото),
            # просто отправляем новое.
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text="Сейчас нет фотографий, ожидающих модерации по жалобам.",
                reply_markup=build_moderator_menu(),
            )
        return

    chat_id = callback.message.chat.id
    caption = await _build_moderation_caption(
        photo,
        show_reports=True,
        show_stats=True,
    )

    # Отправляем карточку с фото для модерации
    try:
        await callback.message.bot.send_photo(
            chat_id=chat_id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="queue"),
        )
    except TelegramBadRequest:
        # На случай, если file_id по какой-то причине не валиден
        await callback.message.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\n⚠️ Не удалось загрузить превью фотографии.",
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="queue"),
        )


async def show_next_photo_for_self_check(callback: CallbackQuery) -> None:
    """
    Отправляет модератору фотографию для самостоятельной проверки.

    Логика:
    - берём фото из общей базы (active + не удалённые);
    - не показываем свои работы модератора;
    - не показываем фото, уже просмотренные этим модератором в self-режиме.
    """
    tg_id = callback.from_user.id
    user = await get_user_by_tg_id(tg_id)

    if user is None:
        await callback.answer("Сначала зарегистрируйся в боте через /start.", show_alert=True)
        return

    # Берём фото по специальной логике для самостоятельной модерации
    photo = await get_next_photo_for_self_moderation(user["id"])

    if not photo:
        try:
            await callback.message.edit_text(
                "Сейчас нет фотографий для самостоятельной проверки.",
                reply_markup=build_moderator_menu(),
            )
        except TelegramBadRequest:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text="Сейчас нет фотографий для самостоятельной проверки.",
                reply_markup=build_moderator_menu(),
            )
        return

    # Отправляем карточку с фото для самостоятельной проверки (source="self")
    chat_id = callback.message.chat.id
    caption = await _build_moderation_caption(
        photo,
        show_reports=True,
        show_stats=True,
    )

    try:
        await callback.message.bot.send_photo(
            chat_id=chat_id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="self"),
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\n⚠️ Не удалось загрузить превью фотографии.",
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="self"),
        )

async def show_next_photo_for_deep_check(callback: CallbackQuery) -> None:
    """
    Отправляет модератору следующую фотографию, которая ожидает детальной проверки.

    Берутся фото со статусом 'under_detailed_review' (логика в get_next_photo_for_detailed_moderation).
    """
    photo = await get_next_photo_for_detailed_moderation()

    if not photo:
        try:
            await callback.message.edit_text(
                "Сейчас нет фотографий на детальной проверке.",
                reply_markup=build_moderator_menu(),
            )
        except TelegramBadRequest:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text="Сейчас нет фотографий на детальной проверке.",
                reply_markup=build_moderator_menu(),
            )
        return

    chat_id = callback.message.chat.id
    caption = await _build_moderation_caption(
        photo,
        show_reports=True,
        show_stats=True,
    )

    try:
        await callback.message.bot.send_photo(
            chat_id=chat_id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="deep"),
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=chat_id,
            text=caption + "\n\n⚠️ Не удалось загрузить превью фотографии.",
            reply_markup=build_moderation_photo_keyboard(photo["id"], source="deep"),
        )


# ================== Удаление фото и удаление+бан ==================

@router.callback_query(F.data.startswith("mod:photo_delete:"))
async def moderator_photo_delete(callback: CallbackQuery) -> None:
    """
    Удаление фотографии модератором.

    Логика:
    - помечаем фото как удалённое в БД (is_deleted = 1);
    - выставляем статус модерации, чтобы оно не попадало в очереди;
    - показываем следующую работу в том же режиме (очередь / детальная / самостоятельная).
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")
    # ['mod', 'photo_delete', '<source>', '<photo_id>']
    if len(parts) != 4:
        await callback.answer("Не удалось разобрать команду удаления.", show_alert=True)
        return

    _, _, source, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Некорректный идентификатор фотографии.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if not photo:
        await callback.answer("Эта фотография уже недоступна.", show_alert=True)
    else:
        # Помечаем фото как удалённое «из всех разделов»
        try:
            await mark_photo_deleted(photo_id)
        except Exception:
            # Если что-то пошло не так — всё равно пытаемся убрать её из модерации
            pass

        try:
            await set_photo_moderation_status(photo_id, "deleted_by_moderator")
        except Exception:
            # Если поле статуса по какой-то причине не обновилось — не критично,
            # выборка по is_deleted всё равно не будет её показывать.
            pass

        await callback.answer("Фотография удалена и больше не участвует в оценивании.", show_alert=True)

    # Переходим к следующей работе в зависимости от источника
    try:
        if source == "queue":
            await show_next_photo_for_moderation(callback)
        elif source == "deep":
            await show_next_photo_for_deep_check(callback)
        else:
            # Для всего остального считаем, что это самостоятельная проверка
            await show_next_photo_for_self_check(callback)
    except Exception:
        # Если не получилось показать следующую карточку, просто выходим в меню модерации
        try:
            await callback.message.edit_text(
                "Раздел модерации.",
                reply_markup=build_moderator_menu(),
            )
        except TelegramBadRequest:
            try:
                await callback.message.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text="Раздел модерации.",
                    reply_markup=build_moderator_menu(),
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("mod:photo_delete_ban:"))
async def moderator_photo_delete_and_ban(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Удалить фотографию и запустить процесс бана пользователя.

    Пока реализуем только удаление фото «отовсюду», а механику бана
    (с выбором срока и причины) докручиваем отдельно.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")
    # ['mod', 'photo_delete_ban', '<source>', '<photo_id>']
    if len(parts) != 4:
        await callback.answer("Не удалось разобрать команду удаления.", show_alert=True)
        return

    _, _, source, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Некорректный идентификатор фотографии.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if not photo:
        await callback.answer("Эта фотография уже недоступна.", show_alert=True)
    else:
        try:
            await mark_photo_deleted(photo_id)
        except Exception:
            pass

        try:
            await set_photo_moderation_status(photo_id, "deleted_by_moderator")
        except Exception:
            pass

        # TODO: здесь можно запомнить пользователя и перевести модератора
        # в состояние ввода причины/срока бана (используя FSM).
        await callback.answer(
            "Фотография удалена. Логику бана пользователя дополним позднее.",
            show_alert=True,
        )

    # Пытаемся продолжить модерацию в том же режиме
    try:
        if source == "queue":
            await show_next_photo_for_moderation(callback)
        elif source == "deep":
            await show_next_photo_for_deep_check(callback)
        else:
            await show_next_photo_for_self_check(callback)
    except Exception:
        try:
            await callback.message.edit_text(
                "Раздел модерации.",
                reply_markup=build_moderator_menu(),
            )
        except TelegramBadRequest:
            try:
                await callback.message.bot.send_message(
                    chat_id=callback.message.chat.id,
                    text="Раздел модерации.",
                    reply_markup=build_moderator_menu(),
                )
            except Exception:
                pass


@router.message(Command("moderator"))
async def moderator_entry(message: Message, state: FSMContext) -> None:
    """
    Вход в режим модератора по команде /moderator.

    Логика:
    - если пользователь не зарегистрирован в базе — просим сначала пройти обычную регистрацию;
    - если пользователь уже модератор — сразу показываем меню модерации;
    - если пользователь не модератор — говорим, что доступ выдаёт админ.
    """
    try:
        await message.delete()
    except Exception:
        # Например, если нет прав на удаление сообщения
        pass
    tg_id = message.from_user.id
    user = await get_user_by_tg_id(tg_id)

    if user is None:
        await message.answer(
            "Сначала зарегистрируйся в боте через /start, "
            "а потом повтори команду /moderator."
        )
        return

    if await is_moderator_by_tg_id(tg_id):
        # Уже модератор — сразу показываем меню
        await message.answer(
            "Ты уже модератор.\n\nВот твой раздел модерации:",
            reply_markup=build_moderator_menu(),
        )
        return

    await message.answer(
        "Режим модератора доступен только по назначению администратора.\n\n"
        "Если ты хочешь стать модератором, напиши админу бота."
    )


# Новый обработчик: вход в модераторскую панель по callback из главного меню
@router.callback_query(F.data == "moderator:menu")
async def moderator_menu_from_main(callback: CallbackQuery) -> None:
    """
    Вход в модераторскую панель по кнопке из главного меню.

    Используется callback_data="moderator:menu" из build_main_menu.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    try:
        await callback.message.edit_text(
            "Раздел модерации.",
            reply_markup=build_moderator_menu(),
        )
    except TelegramBadRequest:
        # Если это было не обычное текстовое сообщение, пробуем отправить новое
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Раздел модерации.",
            reply_markup=build_moderator_menu(),
        )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:menu")
async def moderator_menu_open(callback: CallbackQuery) -> None:
    """
    Открытие меню модератора по callback 'mod:menu'.

    Можно вызывать, например, из админского раздела или других мест.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        "Раздел модерации.",
        reply_markup=build_moderator_menu(),
    )


@router.callback_query(F.data == "mod:queue")
async def moderator_queue(callback: CallbackQuery) -> None:
    """
    Запуск очереди модерации.

    Показывает модератору следующую фотографию,
    которая находится в статусе 'under_review'.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_moderation(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:self")
async def moderator_self_check(callback: CallbackQuery) -> None:
    """
    Самостоятельная проверка фотографий модератором.

    Интерфейс как при обычной оценке, но только две кнопки:
    - «в порядке»
    - «забанить»
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_self_check(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:deep")
async def moderator_deep_check(callback: CallbackQuery) -> None:
    """
    Режим детальной проверки фотографий.

    Показывает только те работы, которые модераторы явно отправили
    в статус 'under_detailed_review'.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    await show_next_photo_for_deep_check(callback)

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users")
async def moderator_users_menu(callback: CallbackQuery) -> None:
    """
    Вход в раздел работы с пользователями.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    try:
        await callback.message.edit_text(
            "Раздел пользователей.\n\nВыбери нужное действие:",
            reply_markup=build_moderator_users_menu(),
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Раздел пользователей.\n\nВыбери нужное действие:",
            reply_markup=build_moderator_users_menu(),
        )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users_search")
async def moderator_users_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Запуск поиска пользователя: просим модератора ввести ID или username.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    # Сохраняем, к какому сообщению привязано подменю
    await state.update_data(
        user_menu_msg_id=callback.message.message_id,
        user_menu_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_user_search_query)

    text = (
        "🔍 Поиск пользователя.\n\n"
        "Отправь одним сообщением:\n"
        "• @username, или\n"
        "• ID Telegram, или\n"
        "• внутренний ID пользователя из базы (если знаешь)."
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=None,
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
        )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("mod:photo_ok:"))
async def moderator_photo_ok(callback: CallbackQuery) -> None:
    """
    Модератор помечает фотографию как «всё хорошо».

    Логика:
    - проверяем, что нажал модератор;
    - ставим статус "active";
    - логируем просмотр в moderator_reviews;
    - удаляем карточку из чата модератора;
    - показываем следующую фотографию в соответствующем режиме.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат:
    #   mod:photo_ok:<source>:<photo_id>
    # На всякий случай поддержим старый вариант mod:photo_ok:<photo_id>
    source = "queue"
    photo_id_str: str | None = None

    if len(parts) == 4:
        source = parts[2]
        photo_id_str = parts[3]
    elif len(parts) == 3:
        photo_id_str = parts[2]
    else:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    # Возвращаем фотографию в обычную ротацию
    try:
        await set_photo_moderation_status(photo_id, "active")
    except Exception:
        await callback.answer("Не удалось обновить статус фотографии.", show_alert=True)
        return

    # Фиксируем, что модератор посмотрел и принял решение по этой работе
    try:
        moderator = await get_user_by_tg_id(tg_id)
    except Exception:
        moderator = None

    if moderator is not None:
        try:
            if source == "queue":
                review_source = "report"
            elif source == "self":
                review_source = "self"
            elif source == "deep":
                review_source = "deep"
            else:
                review_source = source
            await add_moderator_review(
                moderator_id=moderator["id"],
                photo_id=photo_id,
                source=review_source,
            )
        except Exception:
            # Не валим обработчик, если статистику не удалось записать
            pass

    # Чистим карточку из чата модератора
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Показываем следующую фотографию в соответствующем режиме
    if source == "self":
        await show_next_photo_for_self_check(callback)
    else:
        await show_next_photo_for_moderation(callback)

    try:
        await callback.answer("Фотография возвращена в ленту.", show_alert=False)
    except TelegramBadRequest:
        pass


# Новый обработчик: отправить фото на детальную проверку
@router.callback_query(F.data.startswith("mod:photo_deep:"))
async def moderator_photo_deep(callback: CallbackQuery) -> None:
    """
    Отправить фотографию на детальную проверку по жалобам.

    Логика:
    - проверяем, что нажал модератор;
    - меняем статус фотографии на 'under_detailed_review';
    - логируем действие модератора;
    - уведомляем автора о детальной проверке;
    - показываем следующую фотографию из очереди жалоб.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат: mod:photo_deep:<photo_id>
    if len(parts) != 3:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    photo_id_str = parts[2]
    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    # Обновляем статус фотографии
    try:
        await set_photo_moderation_status(photo_id, "under_detailed_review")
    except Exception:
        await callback.answer("Не удалось обновить статус фотографии.", show_alert=True)
        return

    # Фиксируем действие модератора
    try:
        moderator = await get_user_by_tg_id(tg_id)
    except Exception:
        moderator = None

    if moderator is not None:
        try:
            await add_moderator_review(
                moderator_id=moderator["id"],
                photo_id=photo_id,
                source="report",
            )
        except Exception:
            # Не валим обработчик, если статистику не удалось записать
            pass

    # Уведомляем автора фотографии
    try:
        photo = await get_photo_by_id(photo_id)
    except Exception:
        photo = None

    if photo is not None:
        author_user_id = photo.get("user_id")
        if author_user_id:
            try:
                author = await get_user_by_id(author_user_id)
            except Exception:
                author = None

            if author is not None:
                author_tg_id = author.get("tg_id")
                if author_tg_id:
                    notify_text = (
                        "ℹ️ Ваша фотография отправлена на детальную проверку модератором.\n\n"
                        "На время проверки она может быть временно скрыта из оценивания."
                    )
                    kb = InlineKeyboardBuilder()
                    kb.button(
                        text="✅ Просмотрено",
                        callback_data="user:notify_seen",
                    )
                    kb.adjust(1)
                    try:
                        await callback.message.bot.send_message(
                            chat_id=author_tg_id,
                            text=notify_text,
                            reply_markup=kb.as_markup(),
                        )
                    except Exception:
                        pass

    # Удаляем текущую карточку
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Показываем следующую фотографию из очереди жалоб
    await show_next_photo_for_moderation(callback)

    try:
        await callback.answer("Фотография отправлена на детальную проверку.", show_alert=False)
    except TelegramBadRequest:
        pass


# Новый обработчик: показать профиль автора фото в режиме самостоятельной проверки
@router.callback_query(F.data.startswith("mod:photo_profile:"))
async def moderator_photo_profile(callback: CallbackQuery) -> None:
    """
    Показать профиль автора фотографии в режиме самостоятельной проверки.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат: mod:photo_profile:<photo_id>
    if len(parts) != 3:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    photo_id_str = parts[2]
    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    try:
        photo = await get_photo_by_id(photo_id)
    except Exception:
        photo = None

    if not photo:
        await callback.answer("Не удалось найти фотографию.", show_alert=True)
        return

    author_user_id = photo.get("user_id")
    if not author_user_id:
        await callback.answer("Автор фотографии не найден.", show_alert=True)
        return

    try:
        author = await get_user_by_id(author_user_id)
    except Exception:
        author = None

    if not author:
        await callback.answer("Автор фотографии не найден.", show_alert=True)
        return

    # Формируем краткий профиль автора
    lines: list[str] = []
    lines.append("👤 <b>Профиль автора</b>")
    name = author.get("name") or author.get("display_name")
    username = author.get("username")
    if name:
        lines.append(f"Имя: <b>{name}</b>")
    if username:
        lines.append(f"Username: @{username}")
    age = author.get("age")
    if age:
        lines.append(f"Возраст: {age}")
    gender = author.get("gender")
    if gender:
        lines.append(f"Пол: {gender}")
    channel = author.get("channel_username") or author.get("channel_link")
    if channel:
        lines.append(f"Канал: {channel}")
    bio = author.get("bio")
    if bio:
        lines.append("")
        lines.append("Описание:")
        lines.append(bio)

    text = "\n".join(lines)

    try:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
        )
    except Exception:
        pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# Новый обработчик: удалить фото без бана
@router.callback_query(F.data.startswith("mod:photo_delete:"))
async def moderator_photo_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Удаление фотографии без бана автора.

    Логика:
    - проверяем, что нажал модератор;
    - сохраняем ID фото и источник (queue/self) в состоянии;
    - ставим действие delete;
    - просим модератора ввести причину удаления;
    - переводим в состояние waiting_ban_reason.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат:
    #   mod:photo_delete:<source>:<photo_id>
    #   или mod:photo_delete:<photo_id> (на всякий случай)
    source = "queue"
    photo_id_str: str | None = None

    if len(parts) == 4:
        source = parts[2]
        photo_id_str = parts[3]
    elif len(parts) == 3:
        photo_id_str = parts[2]
    else:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    await state.update_data(
        mod_ban_photo_id=photo_id,
        mod_ban_source=source,
        mod_ban_action="delete",
        mod_ban_prompt_msg_id=callback.message.message_id,
        mod_ban_prompt_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_ban_reason)

    text = (
        "Напиши причину удаления фотографии одним сообщением.\n\n"
        "Эта причина будет показана автору фотографии."
    )

    try:
        await callback.message.edit_caption(
            caption=text,
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=None,
            )
        except TelegramBadRequest:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
            )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# Новый обработчик: удалить фото + бан
@router.callback_query(F.data.startswith("mod:photo_delete_ban:"))
async def moderator_photo_delete_ban(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Удаление фотографии с одновременным баном автора на загрузку новых работ.

    Логика:
    - проверяем, что нажал модератор;
    - сохраняем ID фото и источник в состоянии;
    - ставим действие delete_and_ban;
    - просим модератора ввести причину удаления и бана;
    - переводим в состояние waiting_ban_reason.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат:
    #   mod:photo_delete_ban:<source>:<photo_id>
    #   или mod:photo_delete_ban:<photo_id>
    source = "queue"
    photo_id_str: str | None = None

    if len(parts) == 4:
        source = parts[2]
        photo_id_str = parts[3]
    elif len(parts) == 3:
        photo_id_str = parts[2]
    else:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    await state.update_data(
        mod_ban_photo_id=photo_id,
        mod_ban_source=source,
        mod_ban_action="delete_and_ban",
        mod_ban_prompt_msg_id=callback.message.message_id,
        mod_ban_prompt_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_ban_reason)

    text = (
        "Напиши причину удаления <b>и бана</b> пользователя одним сообщением.\n\n"
        "Эта причина будет показана автору фотографии."
    )

    try:
        await callback.message.edit_caption(
            caption=text,
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=None,
            )
        except TelegramBadRequest:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
            )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


# Новый обработчик: пропуск фотографии модератором
@router.callback_query(F.data.startswith("mod:photo_skip:"))
async def moderator_photo_skip(callback: CallbackQuery) -> None:
    """
    Модератор пропускает фотографию без изменения статуса.

    Логика:
    - проверяем, что нажал модератор;
    - логируем просмотр в moderator_reviews;
    - удаляем карточку из чата модератора;
    - показываем следующую фотографию в соответствующем режиме.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат:
    #   mod:photo_skip:<source>:<photo_id>
    # На всякий случай поддержим старый вариант mod:photo_skip:<photo_id>
    source = "queue"
    photo_id_str: str | None = None

    if len(parts) == 4:
        source = parts[2]
        photo_id_str = parts[3]
    elif len(parts) == 3:
        photo_id_str = parts[2]
    else:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    # Фиксируем, что модератор увидел эту работу в выбранном режиме
    try:
        moderator = await get_user_by_tg_id(tg_id)
    except Exception:
        moderator = None

    if moderator is not None:
        try:
            if source == "queue":
                review_source = "report"
            elif source == "self":
                review_source = "self"
            elif source == "deep":
                review_source = "deep"
            else:
                review_source = source
            await add_moderator_review(
                moderator_id=moderator["id"],
                photo_id=photo_id,
                source=review_source,
            )
        except Exception:
            # Не валим обработчик, если статистику не удалось записать
            pass

    # Чистим карточку из чата модератора
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Показываем следующую фотографию в соответствующем режиме
    if source == "self":
        await show_next_photo_for_self_check(callback)
    else:
        await show_next_photo_for_moderation(callback)

    try:
        await callback.answer("Фотография пропущена.", show_alert=False)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("mod:photo_block:"))
async def moderator_photo_block(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Модератор инициирует блокировку/удаление фотографии.

    Первый шаг:
    - проверяем, что нажал модератор;
    - сохраняем ID фото и источник (queue/self) в состоянии;
    - показываем две кнопки:
        • «Удалить фотографию»
        • «Удалить и забанить»
    Второй шаг (см. handler mod:block_action) — выбор варианта и ввод причины.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    # Ожидаемый формат:
    #   mod:photo_block:<source>:<photo_id>
    # На всякий случай поддержим старый вариант mod:photo_block:<photo_id>
    source = "queue"
    photo_id_str: str | None = None

    if len(parts) == 4:
        source = parts[2]
        photo_id_str = parts[3]
    elif len(parts) == 3:
        photo_id_str = parts[2]
    else:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    try:
        photo_id = int(photo_id_str)
    except (TypeError, ValueError):
        await callback.answer("Некорректный ID фотографии.", show_alert=True)
        return

    # Сохраняем данные для следующего шага (выбор варианта + ввод причины)
    await state.update_data(
        mod_ban_photo_id=photo_id,
        mod_ban_source=source,
        mod_ban_prompt_msg_id=callback.message.message_id,
        mod_ban_prompt_chat_id=callback.message.chat.id,
    )

    # Кнопки выбора варианта
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Удалить фотографию",
        callback_data="mod:block_action:delete",
    )
    kb.button(
        text="⛔ Удалить и забанить",
        callback_data="mod:block_action:delete_and_ban",
    )
    kb.adjust(1)
    markup = kb.as_markup()

    text = (
        "Ты выбрал(а) вариант «забанить».\n\n"
        "Выбери, что сделать:\n"
        "• <b>Удалить фотографию</b>\n"
        "• <b>Удалить и забанить пользователя</b>"
    )

    # Переиспользуем ту же карточку: просто меняем подпись и клавиатуру
    try:
        await callback.message.edit_caption(
            caption=text,
            reply_markup=markup,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=markup,
            )
        except TelegramBadRequest:
            # Если карточку отредактировать нельзя, отправляем новое сообщение
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=markup,
            )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("mod:block_action:"))
async def moderator_block_action(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Выбор конкретного действия после «забанить»:

    - mod:block_action:delete
    - mod:block_action:delete_and_ban

    На этом шаге:
    - сохраняем выбранное действие в состоянии;
    - просим модератора ввести причину (одно сообщение);
    - переводим в состояние waiting_ban_reason.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные для модерации.", show_alert=True)
        return

    action_key = parts[2]
    if action_key not in ("delete", "delete_and_ban"):
        await callback.answer("Некорректный тип действия.", show_alert=True)
        return

    # Обновляем состояние
    await state.update_data(
        mod_ban_action=action_key,
        mod_ban_prompt_msg_id=callback.message.message_id,
        mod_ban_prompt_chat_id=callback.message.chat.id,
    )
    await state.set_state(ModeratorStates.waiting_ban_reason)

    if action_key == "delete_and_ban":
        text = (
            "Напиши причину удаления <b>и бана</b> пользователя одним сообщением.\n\n"
            "Эта причина будет показана автору фотографии."
        )
    else:
        text = (
            "Напиши причину удаления фотографии одним сообщением.\n\n"
            "Эта причина будет показана автору фотографии."
        )

    try:
        await callback.message.edit_caption(
            caption=text,
            reply_markup=None,
        )
    except TelegramBadRequest:
        try:
            await callback.message.edit_text(
                text,
                reply_markup=None,
            )
        except TelegramBadRequest:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
            )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.message(ModeratorStates.waiting_user_search_query)
async def moderator_users_search_input(message: Message, state: FSMContext) -> None:
    """
    Обработка ввода поискового запроса модератором.

    Логика:
    - удаляем сообщение с вводом модератора (чистый чат);
    - пытаемся найти пользователя по ID Telegram или внутреннему ID;
    - username пока не поддерживаем напрямую (нет быстрого поиска в БД);
    - показываем краткую информацию о пользователе;
    - возвращаемся в подменю пользователей.
    """
    # Удаляем текст модератора
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    menu_msg_id = data.get("user_menu_msg_id")
    menu_chat_id = data.get("user_menu_chat_id", message.chat.id)

    query = (message.text or "").strip()
    if not query:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Пустой запрос. Попробуй ещё раз через раздел «Пользователи».",
        )
        await state.clear()
        return

    found_user = None

    # Поиск по username пока не реализован на уровне БД — заглушка
    if query.startswith("@"):
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=(
                "Поиск по username пока не реализован на уровне базы данных.\n\n"
                "Пока можно искать только по ID Telegram или внутреннему ID пользователя."
            ),
        )
    else:
        # Пробуем воспринимать как число: сначала как tg_id, потом как internal id
        try:
            num_id = int(query)
        except ValueError:
            num_id = None

        if num_id is not None:
            # Сначала пробуем как ID Telegram
            user = await get_user_by_tg_id(num_id)
            if user is None:
                # Если не нашли — пробуем как внутренний ID
                try:
                    user = await get_user_by_id(num_id)
                except Exception:
                    user = None
            found_user = user

    if found_user is None:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Пользователь не найден. Попробуй другой запрос.",
        )
    else:
        # Формируем краткую карточку пользователя
        lines: list[str] = []
        lines.append("👤 <b>Найден пользователь</b>")
        internal_id = found_user.get("id")
        tg_id_value = found_user.get("tg_id")
        username = found_user.get("username")
        name = found_user.get("name") or found_user.get("display_name")
        age = found_user.get("age")
        gender = found_user.get("gender")
        channel = found_user.get("channel_username") or found_user.get("channel_link")
        is_moderator_flag = found_user.get("is_moderator")
        is_admin_flag = found_user.get("is_admin")

        if internal_id is not None:
            lines.append(f"ID в базе: <code>{internal_id}</code>")
        if tg_id_value is not None:
            lines.append(f"ID Telegram: <code>{tg_id_value}</code>")
        if username:
            lines.append(f"Username: @{username}")
        if name:
            lines.append(f"Имя: <b>{name}</b>")
        if age:
            lines.append(f"Возраст: {age}")
        if gender:
            lines.append(f"Пол: {gender}")
        if channel:
            lines.append(f"Канал: {channel}")
        if is_admin_flag:
            lines.append("Роль: администратор")
        elif is_moderator_flag:
            lines.append("Роль: модератор")

        bio = found_user.get("bio")
        if bio:
            lines.append("")
            lines.append("Описание:")
            lines.append(bio)

        text = "\n".join(lines)

        await message.bot.send_message(
            chat_id=message.chat.id,
            text=text,
        )

    # Возвращаем подменю пользователей на том же сообщении, откуда стартовали
    if menu_msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=menu_chat_id,
                message_id=menu_msg_id,
                text="Раздел пользователей.\n\nВыбери нужное действие:",
                reply_markup=build_moderator_users_menu(),
            )
        except Exception:
            await message.bot.send_message(
                chat_id=menu_chat_id,
                text="Раздел пользователей.\n\nВыбери нужное действие:",
                reply_markup=build_moderator_users_menu(),
            )

    await state.clear()


@router.callback_query(F.data == "mod:users_block")
async def moderator_users_block_stub(callback: CallbackQuery) -> None:
    """
    Заглушка для блока / разбана пользователей.

    Реальную логику блокировок можно будет добавить отдельно в database.py,
    а здесь только интерфейс.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    text = (
        "Раздел «Блок / разбан» пока в разработке.\n\n"
        "Когда добавим поддержку хранения глобальных блокировок "
        "в базе данных, здесь можно будет:\n"
        "• блокировать пользователя по ID или username;\n"
        "• снимать блокировку;\n"
        "• указывать причину ограничения."
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_moderator_users_menu(),
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=build_moderator_users_menu(),
        )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "mod:users_blocked")
async def moderator_users_blocked_stub(callback: CallbackQuery) -> None:
    """
    Заглушка для списка заблокированных пользователей.

    Реальную выборку из базы можно будет добавить отдельно.
    """
    tg_id = callback.from_user.id

    if not await is_moderator_by_tg_id(tg_id):
        await callback.answer(
            "Этот раздел доступен только модераторам.",
            show_alert=True,
        )
        return

    text = (
        "Список заблокированных пользователей пока не реализован.\n\n"
        "Когда появится таблица/функции для хранения таких блокировок, "
        "здесь можно будет посмотреть их в удобном виде."
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_moderator_users_menu(),
        )
    except TelegramBadRequest:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=build_moderator_users_menu(),
        )

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@router.message(ModeratorStates.waiting_ban_reason)
async def moderator_ban_reason_input(message: Message, state: FSMContext) -> None:
    """
    Модератор ввёл причину удаления/бана.

    На этом шаге:
    - удаляем сообщение с текстом причины (чистый UX);
    - ставим статус фото "blocked";
    - логируем действие модератора;
    - отправляем автору уведомление с причиной;
    - (если выбрано delete_and_ban — в тексте говорим про бан на 3 дня,
      а техническую реализацию бана можно будет добавить в upload-логике);
    - завершаем состояние.
    """
    # Удаляем сообщение модератора с причиной — чтобы в чате не копился служебный мусор
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    photo_id = data.get("mod_ban_photo_id")
    action = data.get("mod_ban_action")
    source = data.get("mod_ban_source", "queue")
    prompt_msg_id = data.get("mod_ban_prompt_msg_id")
    prompt_chat_id = data.get("mod_ban_prompt_chat_id", message.chat.id)

    reason = (message.text or "").strip()
    if not reason:
        reason = "Причина не указана."

    if not photo_id or not action:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Не удалось обработать модерацию: отсутствуют данные. Попробуй ещё раз из меню модератора.",
        )
        await state.clear()
        return

    # Обновляем статус фотографии
    try:
        await set_photo_moderation_status(int(photo_id), "blocked")
    except Exception:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Не удалось обновить статус фотографии. Попробуй позже.",
        )
        await state.clear()
        return

    # Фиксируем модераторское действие в журнале
    try:
        moderator = await get_user_by_tg_id(message.from_user.id)
    except Exception:
        moderator = None

    if moderator is not None:
        try:
            await add_moderator_review(
                moderator_id=moderator["id"],
                photo_id=int(photo_id),
                source="report" if source == "queue" else "self",
            )
        except Exception:
            pass

    # Пытаемся уведомить автора фотографии
    try:
        photo = await get_photo_by_id(int(photo_id))
    except Exception:
        photo = None

    if photo is not None:
        author_user_id = photo.get("user_id")
        if author_user_id:
            try:
                author = await get_user_by_id(author_user_id)
            except Exception:
                author = None

            if author is not None:
                author_tg_id = author.get("tg_id")
                if author_tg_id:
                    if action == "delete_and_ban":
                        notify_text = (
                            "⚠️ Ваша фотография была удалена модератором, "
                            "а возможность публиковать новые работы временно "
                            "ограничена на 3 дня.\n\n"
                            f"Причина: {reason}"
                        )
                    else:
                        notify_text = (
                            "⚠️ Ваша фотография была удалена модератором "
                            "и больше не участвует в оценке.\n\n"
                            f"Причина: {reason}"
                        )
                    kb = InlineKeyboardBuilder()
                    kb.button(
                        text="✅ Просмотрено",
                        callback_data="user:notify_seen",
                    )
                    kb.adjust(1)
                    try:
                        await message.bot.send_message(
                            chat_id=author_tg_id,
                            text=notify_text,
                            reply_markup=kb.as_markup(),
                        )
                    except Exception:
                        pass

    # Пытаемся удалить карточку с фото, если её message_id известен
    if prompt_msg_id:
        try:
            await message.bot.delete_message(
                chat_id=prompt_chat_id,
                message_id=prompt_msg_id,
            )
        except Exception:
            pass

    # Сообщаем модератору итог
    if action == "delete_and_ban":
        summary_text = (
            "Фотография удалена, пользователю объявлен бан на 3 дня.\n\n"
            "Техническую реализацию ограничения на загрузку мы можем "
            "добавить отдельно в логике загрузки фото."
        )
    else:
        summary_text = "Фотография удалена и больше не участвует в оценке."

    await message.bot.send_message(
        chat_id=message.chat.id,
        text=summary_text + "\n\nЧтобы продолжить, открой раздел модерации и выбери нужный режим.",
    )

    await state.clear()


@router.callback_query(F.data == "user:notify_seen")
async def user_notify_seen(callback: CallbackQuery) -> None:
    """
    Пользователь нажал «Просмотрено» под служебным уведомлением.

    Логика:
    - удаляем сообщение с уведомлением;
    - не создаём новых сообщений.
    """
    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass