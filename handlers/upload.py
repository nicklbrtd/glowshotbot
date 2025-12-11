from utils.validation import has_links_or_usernames, has_promo_channel_invite
from datetime import datetime, timedelta
from sqlite3 import IntegrityError as SQLiteIntegrityError

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiogram.exceptions import TelegramBadRequest
from keyboards.common import build_back_to_menu_kb

from database import (
    get_user_by_tg_id,
    get_today_photo_for_user,
    create_today_photo,
    mark_photo_deleted,
    get_photo_by_id,
    get_photo_stats,
    get_comments_for_photo,
    get_daily_top_photos,
    add_weekly_candidate,
    is_photo_in_weekly,
    get_weekly_photos_for_user,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
)


router = Router()


class MyPhotoStates(StatesGroup):
    """Состояния мастера загрузки фотографии.

    Новый порядок:
    1) выбор категории работы;
    2) загрузка фото;
    3) название;
    4) выбор типа устройства;
    5) описание (необязательно).
    """

    waiting_category = State()
    waiting_photo = State()
    waiting_title = State()
    waiting_device_type = State()
    waiting_description = State()
def _build_draft_caption(*, category: str | None, title: str | None, device_type: str | None, description: str | None) -> str:
    """Собрать временную подпись к работе во время мастера загрузки.

    Используется, чтобы пользователь видел, что уже заполнено.
    """

    lines: list[str] = []

    if category:
        if category == "poster":
            cat_label = "Постер"
        else:
            cat_label = "Обычная фотография"
        lines.append(f"Категория: <b>{cat_label}</b>")

    if title:
        lines.append(f"Название: <b>{title}</b>")

    if device_type:
        lines.append(f"Устройство: <i>{device_type}</i>")

    if description is not None:
        if description == "":
            lines.append("📝 Текст ещё не добавлен")
        else:
            lines.append(f"📝 {description}")

    if not lines:
        lines.append("Подготовка работы…")

    return "\n".join(lines)


# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========


def get_moscow_now() -> datetime:
    """Текущее время по Москве.

    Вся логика по дню/итогам завязана именно на московское время.
    """
    return datetime.utcnow() + timedelta(hours=3)


# ====== Helpers for time formatting (таймер до следующей загрузки) ======
def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    """
    Простейшее склонение русских слов по числу.
    1 час, 2 часа, 5 часов и т.п.
    """
    v = abs(value) % 100
    if 11 <= v <= 19:
        return many
    v = v % 10
    if v == 1:
        return one
    if 2 <= v <= 4:
        return few
    return many


def _format_time_until_next_upload() -> str:
    """
    Вернуть человекочитаемую строку, через сколько можно будет загрузить новый кадр.
    Новая фотография доступна с полуночи следующего дня по Москве.
    Пример: 'через 3 часа 15 минут'.
    """
    now = get_moscow_now()

    # Время, когда открывается окно загрузки нового кадра: полночь следующего дня по Москве.
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_upload_start = today_midnight + timedelta(days=1)

    if now >= next_upload_start:
        # Формально уже наступило время новой загрузки — показываем, что ждать почти не нужно.
        return "совсем скоро"

    delta = next_upload_start - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 60:
        return "через минуту"

    total_minutes = total_seconds // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60

    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}")
    if minutes > 0:
        parts.append(f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}")

    if not parts:
        return "совсем скоро"

    return "через " + " ".join(parts)


def is_admin_user(user: dict) -> bool:
    """Признак админа по полю is_admin пользователя.

    Если флаг отсутствует или ложный — считаем пользователя обычным.
    """

    return bool(user.get("is_admin"))




def build_my_photo_caption(photo: dict) -> str:
    """Собрать подпись к фотографии в разделе «Моя фотография».

    Здесь нет статистики — только базовая информация о работе.
    Остальные тексты (статистика, комментарии) формируются в отдельных хендлерах.
    """

    # Информация об устройстве
    device_type_raw = (photo.get("device_type") or "").lower()
    device_info = photo.get("device_info") or ""

    # Подбираем эмодзи под тип устройства
    if "смартфон" in device_type_raw or "phone" in device_type_raw:
        device_emoji = "📱"
    elif "фотокамера" in device_type_raw or "camera" in device_type_raw:
        device_emoji = "📷"
    else:
        device_emoji = "📸"

    title = photo.get("title") or "Без названия"

    # Формируем хвост с устройством для заголовка
    if device_info:
        device_suffix = f" ({device_emoji} {device_info})"
    elif device_type_raw:
        device_suffix = f" ({device_emoji})"
    else:
        device_suffix = ""

    title_line = f"\"{title}\"{device_suffix}"

    # Категория работы
    category_code = photo.get("category") or "photo"
    if category_code == "poster":
        category_label = "Постер"
    else:
        category_label = "Обычная фотография"

    description = photo.get("description")

    caption_lines: list[str] = [
        f"<b>{title_line}</b>",
        f"Категория: <i>{category_label}</i>",
    ]

    if description:
        caption_lines.append("")
        caption_lines.append(f"📝 {description}")

    return "\n".join(caption_lines)


def build_my_photo_keyboard(photo_id: int, can_promote: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура под разделом «Моя фотография» для конкретного кадра.

    Кнопки:
    • Статистика;
    • Комментарии;
    • Мои итоги;
    • Продвигать (после итогов дня);
    • Повторить (после итогов дня);
    • Новая фотография (после итогов дня);
    • Удалить;
    • В меню.
    """

    kb = InlineKeyboardBuilder()

    # Основные действия и навигация по работе
    kb.button(text="📊 Статистика", callback_data=f"myphoto:stats:{photo_id}")
    kb.button(text="💬 Комментарии", callback_data=f"myphoto:comments:{photo_id}")

    kb.button(text="🏅 Мои итоги", callback_data=f"myphoto:myresults:{photo_id}")

    # Продвижение в итоги недели (если доступно)
    if can_promote:
        kb.button(text="🚀 Продвигать", callback_data=f"myphoto:promote:{photo_id}")

    # Кнопки для пост-итогов (пока заглушки, но уже есть в интерфейсе)
    kb.button(text="🔁 Повторить", callback_data=f"myphoto:repeat:{photo_id}")
    kb.button(text="🖼 Новая фотография", callback_data=f"myphoto:new:{photo_id}")

    # Удаление и выход в меню
    kb.button(text="🗑 Удалить", callback_data=f"myphoto:delete:{photo_id}")
    kb.button(text="⬅️ В меню", callback_data="menu:back")

    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


async def _ensure_user(callback: CallbackQuery | Message) -> dict | None:
    """Унифицированное получение пользователя.

    Если пользователя нет в базе — отправляет аккуратное уведомление и возвращает None.
    Также здесь проверяем глобальные ограничения (блокировки) пользователя.
    """

    from_user = callback.from_user if isinstance(callback, CallbackQuery) else callback.from_user
    user = await get_user_by_tg_id(from_user.id)
    if user is None:
        text = "Тебя нет в базе, попробуй /start."
        if isinstance(callback, CallbackQuery):
            await callback.answer(text, show_alert=True)
        else:
            await callback.answer(text)
        return None

    # Проверяем глобальную блокировку пользователя (используется модерацией).
    block = await get_user_block_status_by_tg_id(from_user.id)
    is_blocked = bool(block.get("is_blocked"))
    blocked_until_str = block.get("blocked_until")
    blocked_reason = block.get("blocked_reason")

    # Если есть срок блокировки, проверяем, не истёк ли он.
    if blocked_until_str:
        try:
            blocked_until_dt = datetime.fromisoformat(blocked_until_str)
        except Exception:
            blocked_until_dt = None
    else:
        blocked_until_dt = None

    # Если срок указан и уже прошёл — автоматически снимаем блокировку.
    if is_blocked and blocked_until_dt is not None and blocked_until_dt <= datetime.utcnow():
        try:
            await set_user_block_status_by_tg_id(
                from_user.id,
                is_blocked=False,
                blocked_until=None,
                reason=None,
            )
        except Exception:
            # Если не удалось обновить статус, не ломаем логику — просто считаем, что блок не активен.
            pass
        return user

    # Если блок активен без срока или срок ещё не истёк — не даём продолжать.
    if is_blocked and (blocked_until_dt is None or blocked_until_dt > datetime.utcnow()):
        # Собираем текст уведомления.
        lines: list[str] = [
            "Твой аккаунт временно ограничен модераторами.",
            "Сейчас ты не можешь выкладывать новые фотографии.",
        ]

        if blocked_until_dt is not None:
            # Показываем время в человекочитаемом формате (по Москве).
            blocked_until_msk = blocked_until_dt + timedelta(hours=3)
            lines.append("")
            lines.append(
                f"Ограничение действует до {blocked_until_msk.strftime('%d.%m.%Y %H:%M')} по Москве."
            )

        if blocked_reason:
            lines.append("")
            lines.append(f"Причина: {blocked_reason}")

        text = "\n".join(lines)

        if isinstance(callback, CallbackQuery):
            # Делаем алерт, чтобы не плодить новые сообщения в чате.
            await callback.answer(text, show_alert=True)
        else:
            # Для обычного Message просто отвечаем одним сообщением.
            await callback.answer(text)

        return None

    return user


async def _store_photo_message_id(state: FSMContext, message_id: int, photo_id: int | None = None) -> None:
    """Сохранить в FSM id сообщения с фотографией и, при необходимости, id самой фотографии.

    Это нужно, чтобы:
    • при выходе в меню можно было удалить лишнее сообщение с фоткой;
    • иметь под рукой последнюю выложенную работу пользователя (myphoto_last_id).
    """

    data = await state.get_data()
    data["myphoto_photo_msg_id"] = message_id
    if photo_id is not None:
        data["myphoto_last_id"] = photo_id
    await state.set_data(data)


async def _clear_photo_message_id(state: FSMContext) -> None:
    """Очистить в FSM сведения о сообщении с фотографией.

    Используется при удалении фото или выходе в меню, чтобы не держать мусор в состоянии.
    """

    data = await state.get_data()
    if "myphoto_photo_msg_id" in data:
        data["myphoto_photo_msg_id"] = None
    await state.set_data(data)


async def _compute_can_promote(photo: dict) -> bool:
    """Можно ли показывать кнопку «Продвигать» для этой фотографии.

    Условие:
    • текущая дата строго позже дня фотографии;
    • фотография входит в топ-5 дня;
    • её ещё нет в недельном отборе.
    """

    now = get_moscow_now()

    day_key = photo.get("day_key")
    if not day_key:
        # Фото ещё не из БД, неизвестно, к какому дню относится — не даём продвигать
        return False

    try:
        day = datetime.fromisoformat(day_key).date()
    except Exception:
        day = now.date()

    # Продвигать можно только, когда день фотографии уже полностью прошёл
    if now.date() <= day:
        return False

    top5 = await get_daily_top_photos(day_key, limit=5)
    in_top5 = any(p["id"] == photo["id"] for p in top5)
    if not in_top5:
        return False

    if await is_photo_in_weekly(photo["id"]):
        return False

    return True


async def build_my_photo_main_text(photo: dict) -> str:
    """Собрать основную подпись к работе в разделе «Моя фотография».

    Здесь показываем:
    • название и устройство;
    • категорию;
    • базовую статистику (средний рейтинг, количество оценок);
    • текущий статус итога дня по этой работе;
    • таймер до возможности загрузить новую фотографию;
    • описание (если есть).
    """

    # Информация об устройстве
    device_type_raw = (photo.get("device_type") or "").lower()
    device_info = photo.get("device_info") or ""

    if "смартфон" in device_type_raw or "phone" in device_type_raw:
        device_emoji = "📱"
    elif "фотокамера" in device_type_raw or "camera" in device_type_raw:
        device_emoji = "📷"
    else:
        device_emoji = "📸"

    title = photo.get("title") or "Без названия"

    # Формируем хвост с устройством для заголовка
    if device_info:
        device_suffix = f" ({device_emoji} {device_info})"
    elif device_type_raw:
        device_suffix = f" ({device_emoji})"
    else:
        device_suffix = ""

    title_line = f"\"{title}\"{device_suffix}"

    # Категория работы
    category_code = photo.get("category") or "photo"
    if category_code == "poster":
        category_label = "Постер"
    else:
        category_label = "Обычная фотография"

    lines: list[str] = [
        f"<b>{title_line}</b>",
        f"Категория: <i>{category_label}</i>",
        "",
        "<b>Статистика</b>",
    ]

    # Статистика по оценкам
    stats = await get_photo_stats(photo["id"])
    ratings_count = stats.get("ratings_count", 0)
    avg = stats.get("avg_rating")

    if ratings_count > 0 and avg is not None:
        lines.append(f"• Средний рейтинг: <b>{avg:.1f}</b>")
        lines.append(f"• Оценок: <b>{ratings_count}</b>")
    else:
        lines.append("• Эту фотографию ещё никто не оценил 😶")

    # Итог по этой работе
    day_key = photo.get("day_key")
    now = get_moscow_now()

    lines.append("")

    if day_key:
        try:
            day = datetime.fromisoformat(day_key).date()
        except Exception:
            day = now.date()

        # Итоги по этой работе считаем подведёнными, когда день фотографии полностью прошёл.
        if now.date() <= day:
            results_time_reached = False
        else:
            results_time_reached = True

        if not results_time_reached:
            lines.append(
                "Итог этой фотографии: пока не подведён.\n"
                "Итоги по этому дню появятся на следующий день."
            )
        else:
            top = await get_daily_top_photos(day_key, limit=50)
            place = None
            top_entry = None
            for idx, p in enumerate(top, start=1):
                if p["id"] == photo["id"]:
                    place = idx
                    top_entry = p
                    break

            if place is not None and top_entry is not None:
                best_count = top_entry.get("best_count") or 0
                avg_top = top_entry.get("avg_rating")
                avg_top_str = f"{avg_top:.1f}" if avg_top is not None else "—"
                lines.append(
                    f"Итог этой фотографии: место <b>{place}</b> в итогах дня.\n"
                    f"Лучшие оценки (≥9): <b>{best_count}</b>, средний рейтинг: <b>{avg_top_str}</b>."
                )
            else:
                lines.append(
                    "Итог этой фотографии: в топ дня не попала, но её всё ещё могут оценивать ✨"
                )
    else:
        lines.append("Итог этой фотографии: ещё не участвует в итогах дня.")

    # Таймер до новой загрузки
    remaining = _format_time_until_next_upload()
    lines.append("")
    lines.append(f"Новую фотографию можно выложить {remaining}.")

    # Описание — в конце
    description = photo.get("description")
    if description:
        lines.append("")
        lines.append(f"📝 {description}")

    return "\n".join(lines)


async def _show_my_photo_section(
    *,
    chat_id: int,
    service_message: Message,
    state: FSMContext,
    photo: dict,
) -> None:
    """Показ раздела «Моя фотография» одним сообщением с фото, подписью и кнопками.

    Логика:
    1) Пытаемся удалить старое служебное сообщение (меню / шаг мастера).
    2) Отправляем НОВОЕ сообщение с фотографией, caption и inline‑клавиатурой.
    3) Сохраняем id этого сообщения в FSM, чтобы потом можно было его удалить при выходе в меню.
    """

    can_promote = await _compute_can_promote(photo)
    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo["id"], can_promote=can_promote)

    # 1. Удаляем старое служебное сообщение, если оно ещё существует
    try:
        await service_message.delete()
    except Exception:
        # Если удаление не удалось (например, сообщение уже удалено) — просто игнорируем
        pass

    # 2. Отправляем новое сообщение с фото, подписью и кнопками
    sent_photo = await service_message.bot.send_photo(
        chat_id=chat_id,
        photo=photo["file_id"],
        caption=caption,
        reply_markup=kb,
        disable_notification=True,
    )

    # 3. Сохраняем id сообщения с фотографией и id самой фотографии в FSM
    await _store_photo_message_id(state, sent_photo.message_id, photo_id=photo["id"])


# ========= ВХОД В РАЗДЕЛ "МОЯ ФОТОГРАФИЯ" =========


@router.callback_query(F.data == "myphoto:open")
async def my_photo_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    is_admin = is_admin_user(user)
    user_id = user["id"]

    photo = await get_today_photo_for_user(user_id)

    if photo is None:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        if last_pid:
            candidate = await get_photo_by_id(last_pid)
            if candidate is not None:
                try:
                    today_key = get_moscow_now().date().isoformat()
                    if candidate.get("day_key") == today_key and not candidate.get("is_deleted"):
                        photo = candidate
                except Exception:
                    pass

    if photo is not None and photo.get("is_deleted") and is_admin:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        if last_pid:
            candidate = await get_photo_by_id(last_pid)
            if candidate is not None:
                try:
                    today_key = get_moscow_now().date().isoformat()
                    if candidate.get("day_key") == today_key and not candidate.get("is_deleted"):
                        photo = candidate
                except Exception:
                    pass

    if photo is None:
        kb = InlineKeyboardBuilder()
        kb.button(text="📤 Добавить фото", callback_data="myphoto:add")
        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.button(text="❓ Помощь", callback_data="myphoto:help")
        kb.adjust(1, 2)

        await callback.message.edit_text(
            "📸 <b>Загрузить фотографию!</b>\n\n"
            "Здесь оценивают кадры, а не твою внешность.\n\n"
            "<b>Правила загрузки:</b>\n"
            "• Один кадр в день на пользователя;\n"
            "• Без ссылок, @username и рекламы в названии и описании;\n"
            "• Только свои фотографии;\n"
            "• Без откровенного контента и насилия.\n\n"
            "Когда будешь готов — жми «Добавить фото» ниже.",
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    if photo["is_deleted"]:
        kb = InlineKeyboardBuilder()

        if is_admin:
            kb.button(text="➕ Добавить фото", callback_data="myphoto:add")
            text = (
                "Ты уже выкладывал(а) фото сегодня и удалил(а) его.\n\n"
                "Как админ ты можешь выложить новый кадр поверх старого."
            )
        else:
            remaining = _format_time_until_next_upload()
            text = (
                "Ты уже выкладывал(а) фото сегодня и удалил(а) его.\n\n"
                f"Новый кадр можно будет выложить {remaining}."
            )

        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.adjust(1)

        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await callback.answer()
        return

    await _show_my_photo_section(
        chat_id=callback.message.chat.id,
        service_message=callback.message,
        state=state,
        photo=photo,
    )

    await callback.answer()


# ========= ДОБАВЛЕНИЕ ФОТО =========


@router.callback_query(F.data == "myphoto:add")
async def myphoto_add(callback: CallbackQuery, state: FSMContext):
    """Старт мастера загрузки новой работы.

    Шаг 1 — выбор категории (постер / обычная фотография).
    """

    user = await _ensure_user(callback)
    if user is None:
        return

    user_id = user["id"]
    is_admin = is_admin_user(user)

    photo = await get_today_photo_for_user(user_id)

    # По‑прежнему один проект в день для обычных пользователей
    if photo is not None and not is_admin:
        remaining = _format_time_until_next_upload()
        if photo.get("is_deleted"):
            msg = (
                "Ты уже выкладывал(а) фото сегодня и удалил(а) его.\n\n"
                f"Новый кадр можно будет выложить {remaining}."
            )
        else:
            msg = (
                "Ты уже выложил(а) фото сегодня.\n\n"
                f"Новый кадр можно будет выложить {remaining}."
            )
        await callback.answer(msg, show_alert=True)
        return

    # Админу всё ещё позволяем перезаливать, помечая старый кадр удалённым
    if photo is not None and is_admin:
        if not photo.get("is_deleted"):
            await mark_photo_deleted(photo["id"])

    await state.set_state(MyPhotoStates.waiting_category)
    await state.update_data(
        upload_msg_id=callback.message.message_id,
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=False,
        upload_user_id=user_id,
        category=None,
        file_id=None,
        title=None,
        device_type=None,
        description=None,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Постер", callback_data="myphoto:category:poster")
    kb.button(text="📷 Обычная фотография", callback_data="myphoto:category:photo")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        "Выбери категорию работы:\n\n"
        "Это можно будет использовать для отдельных рейтингов постеров и обычных фотографий.",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(MyPhotoStates.waiting_category, F.data.startswith("myphoto:category:"))
async def myphoto_choose_category(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал категорию работы."""

    try:
        _, _, code = callback.data.split(":", 2)
    except ValueError:
        code = "photo"

    if code not in {"poster", "photo"}:
        code = "photo"

    data = await state.get_data()
    data["category"] = code
    await state.set_data(data)

    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")

    if not upload_msg_id or not upload_chat_id:
        await state.clear()
        await callback.message.answer(
            "Сессия загрузки фотографии сбилась. Зайди в раздел «Моя фотография» и попробуй ещё раз.",
            disable_notification=True,
        )
        await callback.answer()
        return

    # Показываем пользователю выбранную категорию и просим отправить фото
    draft_text = _build_draft_caption(
        category=code,
        title=None,
        device_type=None,
        description=None,
    )

    await state.set_state(MyPhotoStates.waiting_photo)

    await callback.message.bot.edit_message_text(
        chat_id=upload_chat_id,
        message_id=upload_msg_id,
        text=(
            f"{draft_text}\n\n"
            "Теперь отправь фотографию (1 шт.), которую хочешь выложить на сегодня."
        ),
    )
    await callback.answer()


@router.message(MyPhotoStates.waiting_photo, F.photo)
async def myphoto_got_photo(message: Message, state: FSMContext):
    """Получили фотографию от пользователя в мастере загрузки.

    На этом шаге начинаем показывать саму фотографию и собираем над ней текст. Дальше
    будем редактировать подпись (caption) этого сообщения.
    """

    data = await state.get_data()
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")
    category = data.get("category")

    if not upload_msg_id or not upload_chat_id or not category:
        await state.clear()
        await message.answer(
            "Сессия загрузки фотографии сбилась.\n\n"
            "Зайди в раздел «Моя фотография» и попробуй ещё раз.",
            disable_notification=True,
        )
        return

    file_id = message.photo[-1].file_id

    # Удаляем сообщение пользователя с фото, чтобы всё оставалось в одном диалоге от бота
    await message.delete()

    # Формируем первичный черновик подписи
    draft_text = _build_draft_caption(
        category=category,
        title=None,
        device_type=None,
        description=None,
    )

    # Заменяем старое служебное сообщение на новое с фотографией
    try:
        await message.bot.delete_message(chat_id=upload_chat_id, message_id=upload_msg_id)
    except Exception:
        pass

    sent_photo = await message.bot.send_photo(
        chat_id=upload_chat_id,
        photo=file_id,
        caption=(
            f"{draft_text}\n\n"
            "Теперь напиши название этой работы."
        ),
        disable_notification=True,
    )

    await state.update_data(
        file_id=file_id,
        upload_msg_id=sent_photo.message_id,
        upload_chat_id=upload_chat_id,
        upload_is_photo=True,
    )

    await state.set_state(MyPhotoStates.waiting_title)


@router.message(MyPhotoStates.waiting_photo)
async def myphoto_waiting_photo_wrong(message: Message):

    await message.delete()


@router.message(MyPhotoStates.waiting_title, F.text)
async def myphoto_got_title(message: Message, state: FSMContext):

    data = await state.get_data()
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")
    category = data.get("category")
    device_type = data.get("device_type")
    description = data.get("description")

    title = (message.text or "").strip()
    if not upload_msg_id or not upload_chat_id:
        await state.clear()
        await message.answer(
            "Сессия загрузки фотографии сбилась.\n\n"
            "Зайди в раздел «Моя фотография» и попробуй ещё раз.",
            disable_notification=True,
        )
        return

    if not title:
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=upload_chat_id,
                message_id=upload_msg_id,
                caption=(
                    _build_draft_caption(
                        category=category,
                        title=None,
                        device_type=device_type,
                        description=description,
                    )
                    + "\n\nНазвание не может быть пустым.\n\nКак назовём эту работу?"
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    if has_links_or_usernames(title) or has_promo_channel_invite(title):
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=upload_chat_id,
                message_id=upload_msg_id,
                caption=(
                    _build_draft_caption(
                        category=category,
                        title=None,
                        device_type=device_type,
                        description=description,
                    )
                    + "\n\nВ названии работы нельзя оставлять @username, ссылки или сайты.\n\n"
                      "Придумай название без контактов — только про саму фотографию."
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await state.update_data(title=title)
    await state.set_state(MyPhotoStates.waiting_device_type)
    await message.delete()

    draft_text = _build_draft_caption(
        category=category,
        title=title,
        device_type=device_type,
        description=description,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 смартфон", callback_data="myphoto:device:phone")
    kb.button(text="📷 фотокамера", callback_data="myphoto:device:camera")
    kb.adjust(2)

    try:
        await message.bot.edit_message_caption(
            chat_id=upload_chat_id,
            message_id=upload_msg_id,
            caption=(
                f"{draft_text}\n\n"
                "На какое устройство снята работа? Выбери тип устройства:"
            ),
            reply_markup=kb.as_markup(),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.message(MyPhotoStates.waiting_title)
async def myphoto_waiting_title_wrong(message: Message):

    await message.delete()


# === ОБРАБОТКА ВЫБОРА ТИПА УСТРОЙСТВА ===


@router.callback_query(MyPhotoStates.waiting_device_type, F.data.startswith("myphoto:device:"))
async def myphoto_device_type(callback: CallbackQuery, state: FSMContext):

    try:
        _, _, code = callback.data.split(":", 2)
    except ValueError:
        code = "phone"

    mapping = {
        "phone": "смартфон",
        "camera": "фотокамера",
    }
    device_type = mapping.get(code, "смартфон")

    data = await state.get_data()
    data["device_type"] = device_type
    await state.set_data(data)

    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")
    category = data.get("category")
    title = data.get("title")
    description = data.get("description")

    if not upload_msg_id or not upload_chat_id:
        await state.clear()
        await callback.message.answer(
            "Сессия загрузки фотографии сбилась. Зайди в раздел «Моя фотография» и попробуй ещё раз.",
            disable_notification=True,
        )
        await callback.answer()
        return

    draft_text = _build_draft_caption(
        category=category,
        title=title,
        device_type=device_type,
        description=description,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="myphoto:skip_description")
    kb.adjust(1)

    await state.set_state(MyPhotoStates.waiting_description)

    try:
        await callback.message.bot.edit_message_caption(
            chat_id=upload_chat_id,
            message_id=upload_msg_id,
            caption=(
                f"{draft_text}\n\n"
                "Хочешь добавить описание для этой фотографии?\n"
                "Напиши его текстом или нажми «Пропустить»."
            ),
            reply_markup=kb.as_markup(),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer()


@router.callback_query(MyPhotoStates.waiting_description, F.data == "myphoto:skip_description")
async def myphoto_skip_description(callback: CallbackQuery, state: FSMContext):
    """Пользователь решил не добавлять описание."""

    data = await state.get_data()
    data["description"] = ""
    await state.set_data(data)

    await _finalize_photo_creation(callback.message, state)
    await callback.answer()


@router.message(MyPhotoStates.waiting_description, F.text)
async def myphoto_got_description(message: Message, state: FSMContext):

    data = await state.get_data()
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")

    if not upload_msg_id or not upload_chat_id:
        await state.clear()
        await message.answer(
            "Сессия загрузки фотографии сбилась.\n\n"
            "Зайди в раздел «Моя фотография» и попробуй ещё раз.",
            disable_notification=True,
        )
        return

    description_raw = (message.text or "").strip()
    # Пользователь может написать «пропустить» текстом вместо нажатия кнопки.
    # В этом случае обрабатываем это как явный отказ от описания.
    if description_raw.lower() == "пропустить":
        await message.delete()
        data["description"] = ""
        await state.set_data(data)
        await _finalize_photo_creation(message, state)
        return

    if has_links_or_usernames(description_raw) or has_promo_channel_invite(description_raw):
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=upload_chat_id,
                message_id=upload_msg_id,
                caption=(
                    _build_draft_caption(
                        category=data.get("category"),
                        title=data.get("title"),
                        device_type=data.get("device_type"),
                        description="",
                    )
                    + "\n\nВ описании нельзя оставлять @username, ссылки или сайты.\n\n"
                      "Напиши просто описание работы без контактов или нажми «Пропустить»."
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await message.delete()

    data["description"] = description_raw
    await state.set_data(data)

    await _finalize_photo_creation(message, state)


async def _finalize_photo_creation(message_or_service: Message, state: FSMContext) -> None:
    """Финализация мастера: создаём запись в БД и открываем раздел «Моя фотография».

    `message_or_service` — либо сообщение пользователя (для текстового описания),
    либо текущее служебное сообщение с фото (для skip)."""

    data = await state.get_data()
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")
    file_id = data.get("file_id")
    title = data.get("title")
    category = data.get("category") or "photo"
    device_type = data.get("device_type") or "не указано"
    description = data.get("description")
    user_id = data.get("upload_user_id")

    if not all([upload_msg_id, upload_chat_id, file_id, title, user_id]):
        await state.clear()
        try:
            await message_or_service.bot.edit_message_caption(
                chat_id=upload_chat_id or message_or_service.chat.id,
                message_id=upload_msg_id or message_or_service.message_id,
                caption="Что-то пошло не так при сохранении фотографии. Попробуй ещё раз через раздел «Моя фотография».",
            )
        except Exception:
            try:
                await message_or_service.bot.send_message(
                    chat_id=upload_chat_id or message_or_service.chat.id,
                    text="Что-то пошло не так при сохранении фотографии. Попробуй ещё раз через раздел «Моя фотография».",
                    disable_notification=True,
                )
            except Exception:
                pass
        return

    try:
        photo_id = await create_today_photo(
            user_id=user_id,
            file_id=file_id,
            title=title,
            category=category,
            device_type=device_type,
            device_info=None,
            description=description,
        )
    except SQLiteIntegrityError:
        existing_photo = await get_today_photo_for_user(user_id)
        if existing_photo is not None:
            photo = existing_photo
        else:
            await state.clear()
            try:
                await message_or_service.bot.edit_message_caption(
                    chat_id=upload_chat_id,
                    message_id=upload_msg_id,
                    caption=(
                        "Похоже, на сегодня у тебя уже есть фотография.\n\n"
                        "Новый кадр нельзя сохранить до подведения итогов дня."
                    ),
                )
            except Exception:
                try:
                    await message_or_service.bot.send_message(
                        chat_id=upload_chat_id,
                        text=(
                            "Похоже, на сегодня у тебя уже есть фотография.\n\n"
                            "Новый кадр нельзя сохранить до подведения итогов дня."
                        ),
                        disable_notification=True,
                    )
                except Exception:
                    pass
            return
    else:
        photo = await get_photo_by_id(photo_id)

    if photo is None:
        await state.clear()
        try:
            await message_or_service.bot.edit_message_caption(
                chat_id=upload_chat_id,
                message_id=upload_msg_id,
                caption="Что-то пошло не так при сохранении фотографии. Попробуй ещё раз.",
            )
        except Exception:
            try:
                await message_or_service.bot.send_message(
                    chat_id=upload_chat_id,
                    text="Что-то пошло не так при сохранении фотографии. Попробуй ещё раз.",
                    disable_notification=True,
                )
            except Exception:
                pass
        return

    await state.set_state(None)

    # Используем текущий message как service_message, чтобы перейти к полноценному разделу «Моя фотография»
    try:
        service_message = await message_or_service.bot.edit_message_caption(
            chat_id=upload_chat_id,
            message_id=upload_msg_id,
            caption="Оформляем твою работу…",
        )
    except Exception:
        service_message = message_or_service

    await _show_my_photo_section(
        chat_id=upload_chat_id,
        service_message=service_message,
        state=state,
        photo=photo,
    )




# ========= КНОПКИ ПОД ФОТО =========


@router.callback_query(F.data.startswith("myphoto:delete:"))
async def myphoto_delete(callback: CallbackQuery, state: FSMContext):

    try:
        _, _, pid = callback.data.split(":", 2)
        photo_id = int(pid)
    except Exception:
        await callback.answer("Странная фотография, не могу удалить.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    user = await _ensure_user(callback)
    if user is None:
        return

    if photo["user_id"] != user["id"]:
        await callback.answer("Это не твоя фотография.", show_alert=True)
        return

    # === Блокируем удаление, если фото уже в итогах дня ===
    now = get_moscow_now()
    day_key = photo.get("day_key")
    try:
        day = datetime.fromisoformat(day_key).date() if day_key else now.date()
    except Exception:
        day = now.date()

    # Считаем, что итоги дня подводятся после 20:45 по московскому времени
    results_time_reached = (
        now.date() > day
        or (now.date() == day and (now.hour, now.minute) >= (20, 45))
    )

    if results_time_reached:
        # Фото уже участвует в итогах дня — не даём его удалять, чтобы не ломать статистику
        await callback.answer(
            "Эта фотография уже участвует в итогах дня, удалить её нельзя.\n\n"
            "Итоги — это история, их мы не переписываем.",
            show_alert=True,
        )
        return

    # === Обычное удаление (до итогов дня) ===
    await mark_photo_deleted(photo_id)

    data = await state.get_data()
    photo_msg_id = data.get("myphoto_photo_msg_id")
    if photo_msg_id:
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=photo_msg_id,
            )
        except Exception:
            pass
        await _clear_photo_message_id(state)

    kb = build_back_to_menu_kb()

    remaining = _format_time_until_next_upload()
    text = (
        "Фотография удалена.\n\n"
        f"Новый кадр можно будет выложить {remaining}."
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb,
            disable_notification=True,
        )

    await callback.answer("Фотография удалена.")


@router.callback_query(F.data.startswith("myphoto:comments:"))
async def myphoto_comments(callback: CallbackQuery):

    try:
        _, _, pid = callback.data.split(":", 2)
        photo_id = int(pid)
    except Exception:
        await callback.answer("Странная фотография, не могу показать комментарии.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    comments = await get_comments_for_photo(photo_id, limit=10)

    lines: list[str] = ["<b>Комментарии к этой фотографии:</b>"]
    if not comments:
        lines.append("Пока ни одного комментария 😶")
    else:
        for c in comments:
            is_public = bool(c.get("is_public", 1))
            author = "Аноним"
            if is_public:
                name = c.get("name") or ""
                username = c.get("username")
                if username:
                    if name:
                        author = f"{name} (@{username})"
                    else:
                        author = f"@{username}"
                elif name:
                    author = name

            text = c.get("text") or ""
            rating = c.get("rating_value")
            rating_str = f" ({rating})" if rating is not None else ""

            lines.append(f"• <b>{author}</b>: {text}{rating_str}")

    text = "\n".join(lines)

    kb = build_my_photo_keyboard(photo_id)

    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        except TelegramBadRequest:
            # Телега сказала "message is not modified" — значит, и так всё ок.
            # Просто игнорируем эту ошибку, чтобы не засирать лог.
            pass
    else:
        await callback.message.edit_text(text, reply_markup=kb)

    await callback.answer()



@router.callback_query(F.data.startswith("myphoto:stats:"))
async def myphoto_stats(callback: CallbackQuery):

    try:
        _, _, pid = callback.data.split(":", 2)
        photo_id = int(pid)
    except Exception:
        await callback.answer("Странная фотография, не могу показать статистику.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    stats = await get_photo_stats(photo_id)
    ratings_count = stats["ratings_count"]
    avg = stats["avg_rating"]
    skips = stats["skips_count"]

    lines: list[str] = ["<b>Статистика по этой фотографии:</b>"]

    if ratings_count > 0 and avg is not None:
        lines.append(f"• Средняя оценка: <b>{avg:.1f}</b>")
        lines.append(f"• Количество оценок: <b>{ratings_count}</b>")
    else:
        lines.append("• Эту фотографию ещё никто не оценил 😶")

    if skips > 0:
        lines.append(f"• Пропусков: <b>{skips}</b>")

    text = "\n".join(lines)
    kb = build_my_photo_keyboard(photo_id)

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=kb)
    else:
        await callback.message.edit_text(text, reply_markup=kb)

    await callback.answer()


# ====== HANDLERS FOR MYPHOTO RESULTS, NEW, REPEAT, EXTRA ======

@router.callback_query(F.data.startswith("myphoto:myresults:"))
async def myphoto_myresults(callback: CallbackQuery, state: FSMContext):

    try:
        _, _, pid = callback.data.split(":", 2)
        photo_id = int(pid)
    except Exception:
        await callback.answer("Странная фотография, не могу показать итоги.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    user = await _ensure_user(callback)
    if user is None:
        return

    if photo["user_id"] != user["id"]:
        await callback.answer("Это не твоя фотография.", show_alert=True)
        return

    day_key = photo.get("day_key")

    lines: list[str] = ["<b>Итоги для этой работы:</b>"]

    # Итоги дня по этой дате
    if day_key:
        top = await get_daily_top_photos(day_key, limit=50)
        place = None
        top_entry = None
        for idx, p in enumerate(top, start=1):
            if p["id"] == photo_id:
                place = idx
                top_entry = p
                break

        if place is not None:
            avg = top_entry.get("avg_rating")
            best_count = top_entry.get("best_count") or 0
            avg_str = f"{avg:.1f}" if avg is not None else "—"
            lines.append(
                f"• Итоги дня ({day_key}): место <b>{place}</b>, "
                f"лучшие оценки (≥9): <b>{best_count}</b>, средняя: <b>{avg_str}</b>"
            )
        else:
            lines.append(f"• В итогах дня за {day_key} эта работа не была в топе.")
    else:
        lines.append("• Для этой работы ещё нет привязки к дню съёмки.")

    # Итоги недели — участвует ли работа в недельном отборе
    weekly_photos = await get_weekly_photos_for_user(user["id"])
    in_weekly = any(p["id"] == photo_id for p in weekly_photos)
    if in_weekly:
        lines.append("• Работа участвует в отборе на итоги недели ✅")
    else:
        lines.append("• В отборе недели эта работа пока не участвует.")

    if len(lines) == 1:
        lines.append("Пока у этой работы нет побед — всё ещё впереди ✨")

    text = "\n".join(lines)

    # Пересобираем клавиатуру для фотографии (с учётом возможности продвижения)
    can_promote = await _compute_can_promote(photo)
    kb = build_my_photo_keyboard(photo_id, can_promote=can_promote)

    if callback.message.photo:
        try:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await callback.message.edit_text(text, reply_markup=kb)

    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:new:"))
async def myphoto_new_stub(callback: CallbackQuery):
    """
    Заглушка под будущую механику «Новой фотографии» после итогов дня.

    Сейчас просто информируем пользователя, чтобы кнопка не была мёртвой.
    Реальная логика будет завязана на переносе участия кадра на следующий день.
    """

    await callback.answer(
        "Загрузка новой фотографии после итогов дня пока в разработке.\n\n"
        "Скоро здесь можно будет выложить новый кадр, не ломая старые итоги.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("myphoto:repeat:"))
async def myphoto_repeat_stub(callback: CallbackQuery):
    """
    Заглушка под механику «Повторить» — повторное участие работы в следующем дне,
    если она не попала в топ‑5.
    """

    await callback.answer(
        "Функция «Повторить» пока в разработке.\n\n"
        "Идея: дать работе ещё один шанс в следующий конкурсный день.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("myphoto:extra:"))
async def myphoto_extra(callback: CallbackQuery):
    """
    Заглушка под премиум‑функцию «Загрузить ещё одну».
    """

    await callback.answer(
        "Возможность выкладывать несколько работ в день будет доступна "
        "с премиум‑подпиской.\n\nПока эта функция в разработке 💎",
        show_alert=True,
    )


# ====== WEEKLY PROMOTE & WEEKLY SECTION ======


@router.callback_query(F.data.startswith("myphoto:promote:"))
async def myphoto_promote(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, pid = callback.data.split(":", 2)
        photo_id = int(pid)
    except Exception:
        await callback.answer("Не могу понять, какую фотографию продвигать.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    user = await _ensure_user(callback)
    if user is None:
        return

    if photo["user_id"] != user["id"]:
        await callback.answer("Это не твоя фотография.", show_alert=True)
        return

    now = get_moscow_now()
    try:
        day = datetime.fromisoformat(photo["day_key"]).date()
    except Exception:
        day = now.date()

    if not (
        now.date() > day
        or (now.date() == day and (now.hour, now.minute) >= (20, 45))
    ):
        await callback.answer("Продвигать можно только после итогов дня.", show_alert=True)
        return

    top10 = await get_daily_top_photos(photo["day_key"], limit=10)
    place = None
    top_entry = None
    for idx, p in enumerate(top10, start=1):
        if p["id"] == photo_id:
            place = idx
            top_entry = p
            break

    if place is None or place > 5:
        await callback.answer(
            "Эта фотография не вошла в топ‑5 дня, её нельзя продвинуть.",
            show_alert=True,
        )
        return

    if await is_photo_in_weekly(photo_id):
        await callback.answer("Эта фотография уже участвует в итогах недели.", show_alert=True)
        return

    await add_weekly_candidate(photo_id)

    avg = top_entry.get("avg_rating")
    best_count = top_entry.get("best_count") or 0
    avg_str = f"{avg:.1f}" if avg is not None else "—"

    await callback.answer(
        f"Твоя фотография заняла {place} место в дне.\n"
        f"Лучшие оценки (≥9): {best_count}\n"
        f"Средняя оценка: {avg_str}\n\n"
        f"Она добавлена в кандидаты на итоги недели 🎉",
        show_alert=True,
    )

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo_id, can_promote=False)

    if callback.message.photo:
        await callback.message.edit_caption(caption=caption, reply_markup=kb)
    else:
        await callback.message.edit_text(caption, reply_markup=kb)


@router.callback_query(F.data == "myphoto:weekly")
async def myphoto_weekly(callback: CallbackQuery):

    user = await _ensure_user(callback)
    if user is None:
        return

    photos = await get_weekly_photos_for_user(user["id"])

    lines: list[str] = ["<b>Твои фотографии для итогов недели:</b>"]
    if not photos:
        lines.append("Пока ни одной фотографии 😶")
    else:
        for p in photos:
            day_key = p.get("day_key") or ""
            title = p.get("title") or "Без названия"
            lines.append(f"• {day_key} — <b>{title}</b>")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Моя фотография", callback_data="menu:my_photo")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(1, 1)

    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=kb.as_markup())
    else:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())

@router.callback_query(F.data == "myphoto:help")
async def myphoto_help(callback: CallbackQuery):
    """
    Заглушка для раздела «Помощь» в блоке загрузки фотографии.
    """
    await callback.answer(
        "Здесь скоро появится подробная помощь по загрузке фотографий.\n\n"
        "Главное: загружай свои кадры, без ссылок и рекламы, и соблюдай правила платформы.",
        show_alert=True,
    )