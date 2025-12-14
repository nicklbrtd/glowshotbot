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
    get_user_by_id,
    is_user_premium_active,
    get_active_photos_for_user,
    is_photo_repeat_used,
    mark_photo_repeat_used,
    archive_photo_to_my_results,
    hard_delete_photo,
)
from utils.time import get_moscow_now


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

    description = photo.get("description")

    caption_lines: list[str] = [
        f"<b>{title_line}</b>",
    ]

    if description:
        caption_lines.append("")
        caption_lines.append(f"📝 {description}")

    return "\n".join(caption_lines)


def build_my_photo_keyboard(
    photo_id: int,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """
    Новая клавиатура «Моя фотография».

    • Кнопки по 2 на строку.
    • Все кнопки всегда показываются (ограничения — внутри обработчиков).
    • Если фото одно — «В меню» одной строкой.
    • Если фото два — снизу строка: (Назад?) + В меню + (Вперёд?)
    """
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton(text="💬 Комментарии", callback_data=f"myphoto:comments:{photo_id}:0"),
        InlineKeyboardButton(text="📊 Статистика", callback_data=f"myphoto:stats:{photo_id}"),
    ])

    rows.append([
        InlineKeyboardButton(text="🔁 Повторить", callback_data=f"myphoto:repeat:{photo_id}"),
        InlineKeyboardButton(text="🚀 Продвигать", callback_data=f"myphoto:promote:{photo_id}"),
    ])

    rows.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"myphoto:delete:{photo_id}"),
        InlineKeyboardButton(text="📤 Добавить фотографию", callback_data="myphoto:add"),
    ])

    if has_prev or has_next:
        nav_row: list[InlineKeyboardButton] = []
        if has_prev:
            nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:nav:{photo_id}:prev"))
        nav_row.append(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))
        if has_next:
            nav_row.append(InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"myphoto:nav:{photo_id}:next"))
        rows.append(nav_row)
    else:
        rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    • фотография входит в топ-4 дня;
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

    # Берём только топ-4 работ дня — продвигать можно 1–4 место
    top4 = await get_daily_top_photos(day_key, limit=4)
    in_top4 = any(p["id"] == photo["id"] for p in top4)
    if not in_top4:
        return False

    # Если эта конкретная фотография уже в недельном отборе — больше не продвигаем
    if await is_photo_in_weekly(photo["id"]):
        return False

    # Ограничение: у пользователя может быть только одна активная работа в недельном отборе
    user_id = photo.get("user_id")
    if user_id:
        weekly_photos = await get_weekly_photos_for_user(user_id)
        if weekly_photos:
            # уже есть хотя бы одна работа, участвующая в итогах недели
            return False

    return True


async def _photo_result_status(photo: dict) -> tuple[bool, str | None, int | None]:
    """
    Определить, «светилась» ли фотография в итогах.

    Возвращает: (is_in_results, kind, place)
    kind:
      - 'daily_top10' если входила в топ-10 дня
      - 'weekly_candidate' если была продвинута в недельный отбор
    place:
      - место 1..10 если применимо
    """
    day_key = photo.get("day_key")

    # 1) Топ-10 дня
    if day_key:
        try:
            top10 = await get_daily_top_photos(day_key, limit=10)
            for i, p in enumerate(top10, start=1):
                if int(p.get("id")) == int(photo.get("id")):
                    return True, "daily_top10", i
        except Exception:
            pass

    # 2) Недельный отбор
    try:
        if await is_photo_in_weekly(int(photo.get("id"))):
            return True, "weekly_candidate", None
    except Exception:
        pass

    return False, None, None


async def build_my_photo_main_text(photo: dict) -> str:
    """
    Новый шаблон:
    "название" (📱)

    📅 Опубликовано: 12.12.2025г
    💖 Оценок: 99
    📉/📈 Рейтинг: 8.2

    📝Описание: ...
    """

    device_type_raw = (photo.get("device_type") or "").lower()
    if "смартфон" in device_type_raw or "phone" in device_type_raw:
        device_emoji = "📱"
    elif "фотокамера" in device_type_raw or "camera" in device_type_raw:
        device_emoji = "📷"
    else:
        device_emoji = "📸"

    title = (photo.get("title") or "Без названия").strip()

    # дата публикации берём по day_key (московская дата)
    day_key = (photo.get("day_key") or "").strip()
    pub_str = day_key
    if day_key:
        try:
            pub_dt = datetime.fromisoformat(day_key)
            pub_str = pub_dt.strftime("%d.%m.%Y")
        except Exception:
            pub_str = day_key or "—"

    # статистика
    stats = await get_photo_stats(photo["id"])
    ratings_count = int(stats.get("ratings_count") or 0)
    avg = stats.get("avg_rating")

    if avg is None:
        avg_str = "—"
        trend = "📉"
    else:
        try:
            avg_f = float(avg)
            avg_str = f"{avg_f:.2f}".rstrip("0").rstrip(".")
            trend = "📈" if avg_f >= 7 else "📉"
        except Exception:
            avg_str = "—"
            trend = "📉"

    description = (photo.get("description") or "").strip()

    lines: list[str] = []
    lines.append(f"<b>\"{title}\" ({device_emoji})</b>")
    lines.append("")
    lines.append(f"📅 Опубликовано: {pub_str}г")
    lines.append(f"💖 Оценок: {ratings_count}")
    lines.append(f"{trend} Рейтинг: <b>{avg_str}</b>")

    if description:
        lines.append("")
        lines.append(f"📝Описание: {description}")

    return "\n".join(lines)


async def _show_my_photo_section(
    *,
    chat_id: int,
    service_message: Message,
    state: FSMContext,
    photo: dict,
    has_prev: bool = False,
    has_next: bool = False,
) -> None:
    """Показ раздела «Моя фотография» одним сообщением с фото, подписью и кнопками.

    Логика:
    1) Пытаемся удалить старое служебное сообщение (меню / шаг мастера).
    2) Отправляем НОВОЕ сообщение с фотографией, caption и inline‑клавиатурой.
    3) Сохраняем id этого сообщения в FSM, чтобы потом можно было его удалить при выходе в меню.
    """

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(
        photo["id"],
        has_prev=has_prev,
        has_next=has_next,
    )

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
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    photos = await get_active_photos_for_user(user_id)
    # сортируем новые сверху
    try:
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
    except Exception:
        pass

    # применяем лимиты
    photos = photos[: (2 if is_premium_user else 1)]

    photo: dict | None = None
    if photos:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        if last_pid:
            for p in photos:
                if p["id"] == last_pid:
                    photo = p
                    break
        if photo is None:
            photo = photos[0]

    if photo is None:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        if last_pid:
            candidate = await get_photo_by_id(last_pid)
            if candidate is not None and not candidate.get("is_deleted"):
                photo = candidate

    # Если сегодняшняя работа помечена как удалённая, но ты админ —
    # пробуем вернуть последнюю живую работу (любого дня)
    if photo is not None and photo.get("is_deleted") and is_admin:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        if last_pid:
            candidate = await get_photo_by_id(last_pid)
            if candidate is not None and not candidate.get("is_deleted"):
                photo = candidate

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

    # Считаем, есть ли соседние работы для навигации
    has_prev = False
    has_next = False
    if len(photos) > 1:
        idx = 0
        for i, p in enumerate(photos):
            if p["id"] == photo["id"]:
                idx = i
                break
        has_prev = idx > 0
        has_next = idx < len(photos) - 1

    await _show_my_photo_section(
        chat_id=callback.message.chat.id,
        service_message=callback.message,
        state=state,
        photo=photo,
        has_prev=has_prev,
        has_next=has_next,
    )

    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:nav:"))
async def myphoto_nav(callback: CallbackQuery, state: FSMContext):
    """
    Навигация по своим фотографиям: вперёд / назад.
    Работает на основе списка активных работ пользователя.
    """
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    # ['myphoto', 'nav', '<photo_id>', '<prev|next>']
    if len(parts) != 4:
        await callback.answer()
        return

    _, _, pid, direction = parts
    try:
        current_photo_id = int(pid)
    except ValueError:
        await callback.answer()
        return

    user_id = user["id"]

    # Применяем те же правила, что и в my_photo_menu:
    # сортировка + лимит активных фото (1 без Premium, 2 с Premium)
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    photos = await get_active_photos_for_user(user_id)
    if not photos:
        await callback.answer("У тебя пока нет активных фотографий.", show_alert=True)
        return

    try:
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
    except Exception:
        pass

    photos = photos[: (2 if is_premium_user else 1)]
    if not photos:
        await callback.answer("У тебя пока нет активных фотографий.", show_alert=True)
        return

    # Ищем индекс текущего кадра
    idx = 0
    for i, p in enumerate(photos):
        if p["id"] == current_photo_id:
            idx = i
            break

    if direction == "prev" and idx > 0:
        new_idx = idx - 1
    elif direction == "next" and idx < len(photos) - 1:
        new_idx = idx + 1
    else:
        new_idx = idx

    photo = photos[new_idx]

    has_prev = new_idx > 0
    has_next = new_idx < len(photos) - 1

    await _show_my_photo_section(
        chat_id=callback.message.chat.id,
        service_message=callback.message,
        state=state,
        photo=photo,
        has_prev=has_prev,
        has_next=has_next,
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
    active_photos = await get_active_photos_for_user(user_id)

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    max_allowed = 2 if is_premium_user else 1
    if len(active_photos) >= max_allowed:
        if not is_premium_user:
            await callback.answer(
                "У тебя уже есть активная фотография. Удали её или купи GlowShot Premium 💎, чтобы добавить вторую.",
                show_alert=True,
            )
        else:
            await callback.answer(
                "У тебя уже загружено 2 активные фотографии — это максимум даже для Premium.",
                show_alert=True,
            )
        return
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
            remaining = _format_time_until_next_upload()
            try:
                await message_or_service.bot.edit_message_caption(
                    chat_id=upload_chat_id,
                    message_id=upload_msg_id,
                    caption=(
                        "Похоже, на сегодня у тебя уже есть фотография.\n\n"
                        f"Новый кадр можно будет выложить {remaining}."
                    ),
                )
            except Exception:
                try:
                    await message_or_service.bot.send_message(
                        chat_id=upload_chat_id,
                        text=(
                            "Похоже, на сегодня у тебя уже есть фотография.\n\n"
                            f"Новый кадр можно будет выложить {remaining}."
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
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    try:
        photo_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None:
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    if int(photo.get("user_id") or 0) != int(user.get("id") or 0):
        await callback.answer("Это не твоя фотография.", show_alert=True)
        return

    # Проверяем участие в итогах
    in_results, kind, place = await _photo_result_status(photo)

    if in_results:
        # Берём снапшот статистики (чтобы в «Мои итоги» было красиво)
        try:
            stats = await get_photo_stats(photo_id)
            avg = stats.get("avg_rating")
            cnt = int(stats.get("ratings_count") or 0)
        except Exception:
            avg = None
            cnt = None

        # Архивируем
        try:
            await archive_photo_to_my_results(
                user_id=user["id"],
                photo=photo,
                kind=kind or "daily_top10",
                place=place,
                avg_rating=float(avg) if avg is not None else None,
                ratings_count=cnt,
            )
        except Exception:
            await callback.answer(
                "Не получилось перенести фото в «Мои итоги». Попробуй позже.",
                show_alert=True,
            )
            return

        # Убираем из активных (чтобы освободить слот)
        try:
            await mark_photo_deleted(photo_id)
        except Exception:
            pass

        await callback.answer("Фото перенесено в «Мои итоги» и убрано из «Моя фотография».", show_alert=True)
        await my_photo_menu(callback, state)
        return

    # Фото нигде не участвовало → удаляем полностью
    try:
        await hard_delete_photo(photo_id)
    except Exception:
        await callback.answer("Не удалось удалить фото. Попробуй позже.", show_alert=True)
        return

    await callback.answer("Фото удалено.", show_alert=True)
    await my_photo_menu(callback, state)


@router.callback_query(F.data.startswith("myphoto:comments:"))
async def myphoto_comments(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    # myphoto:comments:<photo_id>:<page>
    if len(parts) < 3:
        await callback.answer()
        return

    try:
        photo_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    page = 0
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except Exception:
            page = 0

    comments = await get_comments_for_photo(photo_id) or []
    per_page = 5
    total = len(comments)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))

    start = page * per_page
    chunk = comments[start:start + per_page]

    lines: list[str] = ["💬 <b>Комментарии</b>", ""]

    if total == 0:
        lines.append("Пока нет ни одного комментария.\nБудь первым 😌")
    else:
        for c in chunk:
            text = (c.get("text") or "").strip()
            is_public = bool(c.get("is_public", 1))

            if is_public:
                name = (c.get("user_name") or "").strip()
                username = (c.get("user_username") or "").strip()
                if username:
                    who = f"<a href=\"https://t.me/{username}\">{name or '@' + username}</a>"
                else:
                    who = name or "Пользователь"
            else:
                who = "🕵 Аноним"

            lines.append(f"• <b>{who}</b>: {text}")

    kb = _build_comments_nav_kb(photo_id, page, pages)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption="\n".join(lines), reply_markup=kb)
        else:
            await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        pass

    await callback.answer()


def _build_comments_nav_kb(photo_id: int, page: int, pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"myphoto:comments:{photo_id}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"myphoto:comments:{photo_id}:{page+1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:back:"))
async def myphoto_back(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    try:
        photo_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    # лимит 1/2 в навигации
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    photos = await get_active_photos_for_user(user["id"])
    try:
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
    except Exception:
        pass
    photos = photos[: (2 if is_premium_user else 1)]

    has_prev = False
    has_next = False
    if len(photos) > 1:
        idx = 0
        for i, p in enumerate(photos):
            if p["id"] == photo_id:
                idx = i
                break
        has_prev = idx > 0
        has_next = idx < len(photos) - 1

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo_id, has_prev=has_prev, has_next=has_next)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=caption, reply_markup=kb)
        else:
            await callback.message.edit_text(caption, reply_markup=kb)
    except Exception:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:stats:"))
async def myphoto_stats(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    try:
        photo_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    stats = await get_photo_stats(photo_id)
    ratings_count = int(stats.get("ratings_count") or 0)
    avg = stats.get("avg_rating")
    avg_str = "—" if avg is None else f"{float(avg):.2f}".rstrip("0").rstrip(".")

    super_count = int(stats.get("super_ratings_count") or 0)
    comments_count = int(stats.get("comments_count") or 0)

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    lines: list[str] = ["📊 <b>Статистика фотографии</b>", ""]
    lines.append(f"💖 Оценок: <b>{ratings_count}</b>")
    lines.append(f"📈 Средняя оценка: <b>{avg_str}</b>")
    lines.append(f"💬 Комментариев: <b>{comments_count}</b>")
    lines.append(f"💥 Супер-оценок: <b>{super_count}</b>")

    lines.append("")
    if is_premium_user:
        lines.append("💎 <b>Premium</b>: расширенная статистика будет доступна тут (в разработке).")
    else:
        lines.append("💎 Хочешь больше статистики? Это будет доступно в GlowShot Premium.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}")]]
    )

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption="\n".join(lines), reply_markup=kb)
        else:
            await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        pass

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
async def myphoto_repeat(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    try:
        photo_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    if int(photo.get("user_id") or 0) != int(user["id"]):
        await callback.answer("Это не твоя фотография.", show_alert=True)
        return

    # можно повторить только если у пользователя одна активная фотка (по твоему правилу)
    active_photos = await get_active_photos_for_user(user["id"])
    try:
        active_photos = sorted(active_photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
    except Exception:
        pass

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    active_photos = active_photos[: (2 if is_premium_user else 1)]
    if len(active_photos) != 1:
        await callback.answer("🔁 Повторить можно только когда у тебя одна активная фотография.", show_alert=True)
        return

    if await is_photo_repeat_used(photo_id):
        await callback.answer("Ты уже использовал(а) «Повторить» для этой работы — третьего шанса нет 🙃", show_alert=True)
        return

    # фото НЕ должно попадать в топ-10 дня
    day_key = photo.get("day_key")
    if day_key:
        top10 = await get_daily_top_photos(day_key, limit=10)
        if any(int(p["id"]) == int(photo_id) for p in top10):
            await callback.answer("Эта работа уже попадала в топ дня — повторить можно только если она не была в топе.", show_alert=True)
            return

    # дневной лимит
    today_photo = await get_today_photo_for_user(user["id"])
    if today_photo is not None and not today_photo.get("is_deleted"):
        await callback.answer("Сегодня у тебя уже есть опубликованная работа. Повтори позже — на следующий день.", show_alert=True)
        return

    # не повторяем в тот же день
    now = get_moscow_now().date()
    try:
        photo_day = datetime.fromisoformat(day_key).date() if day_key else None
    except Exception:
        photo_day = None
    if photo_day is not None and photo_day == now:
        await callback.answer("Эту работу нельзя повторить в тот же день. Попробуй завтра.", show_alert=True)
        return

    # создаём новую запись на сегодня
    try:
        new_photo = await create_today_photo(
            user_id=user["id"],
            file_id=photo["file_id"],
            title=photo.get("title") or "Без названия",
            device_type=photo.get("device_type") or "",
            device_info=photo.get("device_info"),
            category=photo.get("category") or "photo",
            description=photo.get("description"),
        )
    except Exception:
        await callback.answer("Не получилось повторить работу. Попробуй позже.", show_alert=True)
        return

    try:
        await mark_photo_repeat_used(photo_id)
    except Exception:
        pass

    try:
        # если create_today_photo возвращает dict с id
        if isinstance(new_photo, dict) and new_photo.get("id"):
            await state.update_data(myphoto_last_id=new_photo["id"])
    except Exception:
        pass

    await callback.answer("Готово! Работа опубликована ещё раз ✨")
    await my_photo_menu(callback, state)


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

    # Продвигать можно только после окончания календарного дня по Москве
    if now.date() <= day:
        await callback.answer("Продвигать можно только после окончания дня.", show_alert=True)
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

    can_promote = await _compute_can_promote(photo)
    if not can_promote:
        await callback.answer(
            "🚀 Продвигать можно только если работа была в топ-4 дня и день уже завершён.\n"
            "Также можно продвинуть только одну фотографию в неделю.",
            show_alert=True,
        )
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