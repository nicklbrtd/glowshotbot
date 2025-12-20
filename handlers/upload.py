from utils.validation import has_links_or_usernames, has_promo_channel_invite
from datetime import datetime, timedelta
from asyncpg.exceptions import UniqueViolationError

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiogram.exceptions import TelegramBadRequest, SkipHandler

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
    is_user_premium_active,
    get_active_photos_for_user,
    get_latest_photos_for_user,
    count_today_photos_for_user,
    get_comment_counts_for_photo,
    get_photo_ratings_stats,
	count_super_ratings_for_photo,
    count_comments_for_photo,
	count_active_users,
	count_photo_reports_for_photo,
	get_photo_rank_in_day,
	get_link_ratings_count_for_photo,
	get_photo_skip_count_for_photo,
    get_comments_for_photo_sorted,
)
from utils.time import get_moscow_now


router = Router()


class MyPhotoStates(StatesGroup):
    """Состояния мастера загрузки фотографии.

    Новый порядок:
    1) загрузка фото;
    2) название.

    (Устройство/описание будут добавляться позже через редактирование.)
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


def _ready_wording(user: dict) -> str:
    g = (user.get("gender") or "").strip().lower()
    if g in {"м", "муж", "мужской", "male", "man", "парень"}:
        return "готов"
    if g in {"ж", "жен", "женский", "female", "woman", "девушка"}:
        return "готова"
    return "готов(а)"


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


def build_my_photo_keyboard(photo_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton(text="🔗 Поделиться", callback_data=f"myphoto:share:{photo_id}"),
    ])

    rows.append([
        InlineKeyboardButton(text="💬 Комментарии", callback_data=f"myphoto:comments:{photo_id}:0"),
        InlineKeyboardButton(text="📊 Статистика", callback_data=f"myphoto:stats:{photo_id}"),
    ])

    rows.append([
        InlineKeyboardButton(text="🔁 Повторить", callback_data=f"myphoto:repeat:{photo_id}"),
        InlineKeyboardButton(text="🚀 Продвигать", callback_data=f"myphoto:promote:{photo_id}"),
    ])

    rows.append([
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"myphoto:edit:{photo_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"myphoto:delete:{photo_id}"),
    ])

    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ===== Stats keyboard and avg formatting helpers =====

def build_my_photo_stats_keyboard(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"),
    )
    kb.row(
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"),
    )
    return kb.as_markup()



def _fmt_avg(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


# ===== Upload wizard navigation keyboard (Назад / Отмена) =====

def build_upload_wizard_kb(*, back_to: str = "menu") -> InlineKeyboardMarkup:
    """Inline keyboard for upload wizard.

    back_to:
      - "menu": go back to "Моя фотография" section
      - "photo": go back to photo step (re-upload)
    """
    kb = InlineKeyboardBuilder()

    if back_to == "photo":
        kb.button(text="⬅️ Назад", callback_data="myphoto:upload_back")
    else:
        kb.button(text="⬅️ Назад", callback_data="myphoto:open")

    kb.button(text="❌ Отмена", callback_data="myphoto:upload_cancel")
    kb.adjust(2)
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
    blocked_until_str = block.get("block_until")
    blocked_reason = block.get("block_reason")

    # Если есть срок блокировки, проверяем, не истёк ли он.
    if blocked_until_str:
        try:
            blocked_until_dt = datetime.fromisoformat(blocked_until_str)
        except Exception:
            blocked_until_dt = None
    else:
        blocked_until_dt = None

    # Если срок указан и уже прошёл — автоматически снимаем блокировку.
    if is_blocked and blocked_until_dt is not None and blocked_until_dt <= get_moscow_now():
        try:
            await set_user_block_status_by_tg_id(
                from_user.id,
                is_blocked=False,
                reason=None,
                until_iso=None,
            )
        except Exception:
            # Если не удалось обновить статус, не ломаем логику — просто считаем, что блок не активен.
            pass
        return user

    # Если блок активен без срока или срок ещё не истёк — не даём продолжать.
    if is_blocked and (blocked_until_dt is None or blocked_until_dt > get_moscow_now()):
        # Собираем текст уведомления.
        lines: list[str] = [
            "Твой аккаунт временно ограничен модераторами.",
            "Сейчас ты не можешь выкладывать новые фотографии.",
        ]

        if blocked_until_dt is not None:
            # Показываем время в человекочитаемом формате (по Москве).
            blocked_until_msk = blocked_until_dt
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
            pub_str = "—"

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
) -> None:
    """Показ раздела «Моя фотография» одним сообщением с фото, подписью и кнопками.

    Логика:
    1) Пытаемся удалить старое служебное сообщение (меню / шаг мастера).
    2) Отправляем НОВОЕ сообщение с фотографией, caption и inline‑клавиатурой.
    3) Сохраняем id этого сообщения в FSM, чтобы потом можно было его удалить при выходе в меню.
    """

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo["id"])

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


async def _edit_or_replace_my_photo_message(
    callback: CallbackQuery,
    state: FSMContext,
    photo: dict,
) -> None:
    """
    UX:
    1) если текущее сообщение с фото — делаем edit_media;
    2) если не получилось — удаляем и отправляем новое.
    """
    msg = callback.message
    chat_id = msg.chat.id

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo["id"])

    # 1) Пробуем edit_media (идеально для перелистывания 2 фото)
    try:
        if msg.photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=photo["file_id"], caption=caption),
                reply_markup=kb,
            )
            await _store_photo_message_id(state, msg.message_id, photo_id=photo["id"])
            return
    except Exception:
        pass

    # 2) Фоллбек: удалить и отправить заново
    try:
        await msg.delete()
    except Exception:
        pass

    sent = await msg.bot.send_photo(
        chat_id=chat_id,
        photo=photo["file_id"],
        caption=caption,
        reply_markup=kb,
        disable_notification=True,
    )
    await _store_photo_message_id(state, sent.message_id, photo_id=photo["id"])


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

    photos = await get_latest_photos_for_user(user_id, limit=10)
    # сортируем новые сверху
    try:
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
    except Exception:
        pass

    # теперь у всех только 1 активная фотография
    photos = photos[:1]

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
        kb.button(text="📤 Загрузить", callback_data="myphoto:add")
        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.adjust(1)

        ready = _ready_wording(user)
        text = (
            "📸 <b>Загрузить фотографию!</b>\n\n"
            "Здесь оценивают кадры, а не твою внешность.\n\n"
            "<b>Правила загрузки:</b>\n"
            "• Можно загрузить только один кадр в день;\n"
            "• Селфи / фотографии, где изображён(а) ты сам(а) — нельзя;\n"
            "• Без рекламы: названия, ссылки и прочее;\n"
            "• Только свои фотографии;\n"
            "• Без откровенного контента и насилия.\n\n"
            "Администрация вправе удалить контент и ограничить доступ к боту при нарушении правил.\n\n"
            f"Когда будешь {ready} — жми «Загрузить»."
        )

        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=kb.as_markup())
            else:
                await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass

            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
            )

        await callback.answer()
        return

    if photo.get("is_deleted"):
        kb = InlineKeyboardBuilder()

        if is_admin:
            kb.button(text="➕ Добавить фото", callback_data="myphoto:add")
            text = (
                "Ты уже выкладывал(а) фото сегодня и удалил(а) его.\n\n"
                "Как админ ты можешь выложить новый кадр поверх старого."
            )
        else:
            # Premium может удалять/перезаливать без дневного ограничения
            if is_premium_user:
                kb.button(text="➕ Добавить фото", callback_data="myphoto:add")
                text = (
                    "Ты удалил(а) свою фотографию.\n\n"
                    "Как Premium пользователь ты можешь загрузить новую сразу."
                )
            else:
                remaining = _format_time_until_next_upload()
                text = (
                    "Ты уже выкладывал(а) фото сегодня и удалил(а) его.\n\n"
                    f"Новый кадр можно будет выложить {remaining}."
                )

        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.adjust(1)

        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=kb.as_markup())
            else:
                await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass

            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
            )

        await callback.answer()
        return

    await _show_my_photo_section(
        chat_id=callback.message.chat.id,
        service_message=callback.message,
        state=state,
        photo=photo,
    )

    await callback.answer()


# ========= Навигация по своим фотографиям =========
@router.callback_query(F.data.startswith("myphoto:nav:"))
async def myphoto_nav(callback: CallbackQuery, state: FSMContext):
    """
    Навигация по своим фотографиям: вперёд / назад.
    Работает на основе списка активных работ пользователя.
    """
    await callback.answer("Сейчас доступна только одна активная фотография.")
    return


# ====== 📊 Моя фотография: статистика ======
@router.callback_query(F.data.startswith("myphoto:stats:"))
async def myphoto_stats(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка.")
        return

    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Ошибка.")
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    # Only owner can view "my photo" stats
    if int(photo.get("user_id", 0)) != int(user.get("id", 0)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    # Premium flag (active)
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(int(user["tg_id"]))
    except Exception:
        is_premium_user = False

    # Base stats
    try:
        r = await get_photo_ratings_stats(photo_id)
    except Exception:
        await callback.answer("⚠️ Не удалось загрузить статистику. Попробуй ещё раз через пару секунд.", show_alert=True)
        return
    ratings_count = int(r.get("ratings_count") or 0)
    last_rating = r.get("last_rating")
    avg_rating = r.get("avg_rating")

    super_count = 0
    try:
        super_count = await count_super_ratings_for_photo(photo_id)
    except Exception:
        super_count = 0

    comments_count = 0
    try:
        comments_count = await count_comments_for_photo(photo_id)
    except Exception:
        comments_count = 0

    link_ratings = 0
    try:
        link_ratings = await get_link_ratings_count_for_photo(photo_id)
    except Exception:
        link_ratings = 0

    lines: list[str] = []
    lines.append("📊 <b>Статистика твоей фотографии:</b>")
    lines.append("")
    lines.append(f"⭐️ Оценок всего: <b>{ratings_count}</b>")
    lines.append(f"🕒 Последняя оценка: <b>{last_rating if last_rating is not None else '—'}</b>")
    lines.append(f"📈 Средняя оценка: <b>{_fmt_avg(avg_rating)}</b>")
    lines.append(f"🔥 Супер-оценок: <b>{super_count}</b>")
    lines.append(f"💬 Комментариев: <b>{comments_count}</b>")
    lines.append(f"🔗⭐️ Оценки по ссылке: <b>{link_ratings}</b>")

    lines.append("")

    if is_premium_user:
        place_now = None
        try:
            place_now = await get_photo_rank_in_day(photo_id, str(photo.get("day_key") or ""))
        except Exception:
            place_now = None

        total_users = 0
        try:
            total_users = await count_active_users()
        except Exception:
            total_users = 0

        rated_users = int(r.get("rated_users") or 0)
        not_rated = max(total_users - rated_users - 1, 0)

        good_cnt = int(r.get("good_count") or 0)  # >= 6
        bad_cnt = int(r.get("bad_count") or 0)    # <= 5

        skip_cnt = 0
        try:
            skip_cnt = await get_photo_skip_count_for_photo(photo_id)
        except Exception:
            skip_cnt = 0

        reports_cnt = 0
        try:
            reports_cnt = await count_photo_reports_for_photo(photo_id)
        except Exception:
            reports_cnt = 0

        # Activity days based on day_key (Moscow date)
        activity_days = "—"
        try:
            dk = (photo.get("day_key") or "").strip()
            if dk:
                d = datetime.fromisoformat(dk).date()
                days = (get_moscow_now().date() - d).days + 1
                if days < 1:
                    days = 1
                activity_days = str(days)
        except Exception:
            activity_days = "—"

        lines.append(f"🏆 Место в топ (сейчас): <b>{place_now if place_now is not None else '—'}</b>")
        lines.append(f"🙈 Не оценившие: <b>{not_rated}</b>")
        lines.append(f"✅ Хорошие (6–10): <b>{good_cnt}</b>")
        lines.append(f"⚠️ Плохие (1–5): <b>{bad_cnt}</b>")
        lines.append(f"⏭ Скип: <b>{skip_cnt}</b>")
        lines.append(f"🚨 Жалобы: <b>{reports_cnt}</b>")
        if str(activity_days).isdigit():
            d_int = int(activity_days)
            lines.append(f"📅 Активность: <b>{d_int}</b> {_plural_ru(d_int, 'день', 'дня', 'дней')}")
        else:
            lines.append(f"📅 Активность: <b>{activity_days}</b>")
    else:
        lines.append("🏆 Место в топ (сейчас): 💎 <b>Премиум</b>")
        lines.append("🙈 Не оценившие: 💎 <b>Премиум</b>")
        lines.append("✅ Хорошие (6–10): 💎 <b>Премиум</b>")
        lines.append("⚠️ Плохие (1–5): 💎 <b>Премиум</b>")
        lines.append("⏭ Скип: 💎 <b>Премиум</b>")
        lines.append("🚨 Жалобы: 💎 <b>Премиум</b>")
        lines.append("📅 Активность: 💎 <b>Премиум</b>")

    text = "\n".join(lines)
    kb = build_my_photo_stats_keyboard(photo_id)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb,
            disable_notification=True,
        )

    await callback.answer()

# ========= ДОБАВЛЕНИЕ ФОТО =========


# --- Upload wizard: Cancel/Back handlers ---


@router.callback_query(F.data == "myphoto:upload_cancel")
async def myphoto_upload_cancel(callback: CallbackQuery, state: FSMContext):
    """Cancel upload wizard and return to My Photo section."""
    try:
        await state.clear()
    except Exception:
        pass

    # Reuse existing My Photo entry handler to render proper UI.
    callback.data = "myphoto:open"
    await my_photo_menu(callback, state)


@router.callback_query(F.data == "myphoto:upload_back")
async def myphoto_upload_back(callback: CallbackQuery, state: FSMContext):
    """Go back inside upload wizard (from title step back to photo step)."""
    cur_state = await state.get_state()

    # If we are on title step — go back to photo step
    if cur_state == MyPhotoStates.waiting_title.state:
        await state.set_state(MyPhotoStates.waiting_photo)
        await state.update_data(file_id=None, title=None)

        text = "Окей, вернёмся назад. Теперь отправь фотографию (1 шт.), которую хочешь выложить."
        kb = build_upload_wizard_kb(back_to="menu")

        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=kb)
            else:
                await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb,
                disable_notification=True,
            )

        await callback.answer()
        return

    # From any other wizard state (or no state) — just return to My Photo section
    callback.data = "myphoto:open"
    await my_photo_menu(callback, state)


# ---- helper for upload limit after delete ----
async def _can_user_upload_now(user: dict, is_premium_user: bool, is_admin: bool) -> tuple[bool, str | None]:
    if is_admin or is_premium_user:
        return True, None
    try:
        today_count = await count_today_photos_for_user(int(user["id"]), include_deleted=True)
    except Exception:
        today_count = 0
    if today_count >= 1:
        return False, _format_time_until_next_upload()
    return True, None


# ========= ДОБАВЛЕНИЕ ФОТО =========


@router.callback_query(F.data == "myphoto:add")
async def myphoto_add(callback: CallbackQuery, state: FSMContext):
    """Старт мастера загрузки новой работы.

    Шаг 1 — загрузка фотографии.
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

    is_admin = is_admin_user(user)

    # Теперь загрузка новой фотографии возможна только после ручного удаления текущей активной.
    # (Исключение: админ может перезаливать.)
    if (not is_admin) and active_photos:
        await callback.answer(
            "Сначала удали текущую фотографию (🗑 Удалить) в разделе «Моя фотография»,\nа потом загружай новую.",
            show_alert=True,
        )
        return

    photo = await get_today_photo_for_user(user_id)

    # Ограничение: обычные — 1 раз в день, premium — без лимита
    if (not is_premium_user) and (not is_admin):
        today_count = await count_today_photos_for_user(user["id"], include_deleted=True)
        if today_count >= 1:
            remaining = _format_time_until_next_upload()
            await callback.answer(
                f"Ты уже выложил(а) фото сегодня.\n\n"
                f"Новый кадр можно будет выложить {remaining}.",
                show_alert=True,
            )
            return

    # Админу позволяем перезаливать: если сегодня уже есть активный кадр — мягко удаляем его
    if is_admin and photo is not None and not photo.get("is_deleted"):
        await mark_photo_deleted(photo["id"])

    await state.set_state(MyPhotoStates.waiting_photo)
    await state.update_data(
        upload_msg_id=callback.message.message_id,
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=bool(getattr(callback.message, "photo", None)),
        upload_user_id=user_id,
        file_id=None,
        title=None,
    )

    text = "Теперь отправь фотографию (1 шт.), которую хочешь выложить."
    kb = build_upload_wizard_kb(back_to="menu")

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass
        sent = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb,
            disable_notification=True,
        )
        # важно: обновим upload_msg_id, иначе дальнейшие шаги будут ссылаться на удалённое сообщение
        await state.update_data(upload_msg_id=sent.message_id, upload_chat_id=sent.chat.id, upload_is_photo=False)

    await callback.answer()


@router.callback_query(MyPhotoStates.waiting_category, F.data.startswith("myphoto:category:"))
async def myphoto_choose_category(callback: CallbackQuery, state: FSMContext):
    # Категории временно отключены — мастер загрузки теперь сразу ждёт фото.
    await callback.answer("Категории сейчас не используются. Просто отправь фотографию.")


@router.message(MyPhotoStates.waiting_photo, F.photo)
async def myphoto_got_photo(message: Message, state: FSMContext):
    """Получили фотографию от пользователя в мастере загрузки.

    На этом шаге начинаем показывать саму фотографию и собираем над ней текст. Дальше
    будем редактировать подпись (caption) этого сообщения.
    """

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

    file_id = message.photo[-1].file_id

    # Удаляем сообщение пользователя с фото, чтобы всё оставалось в одном диалоге от бота
    await message.delete()

    # Формируем первичный черновик подписи
    draft_text = "Фотография получена ✅"

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
            "Теперь напиши название этой работы.\n\nМожешь нажать «Назад», чтобы заменить фото, или «Отмена»."
        ),
        reply_markup=build_upload_wizard_kb(back_to="photo"),
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
                    "Фотография получена ✅\n\n"
                    "Название не может быть пустым.\n\n"
                    "Как назовём эту работу?"
                ),
                reply_markup=build_upload_wizard_kb(back_to="photo"),
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
                    "Фотография получена ✅\n\n"
                    "В названии нельзя оставлять @username, ссылки или сайты.\n\n"
                    "Придумай название без контактов — только про саму фотографию."
                ),
                reply_markup=build_upload_wizard_kb(back_to="photo"),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await state.update_data(title=title)
    await message.delete()

    # Раньше здесь был выбор устройства/описания. Сейчас — сразу публикуем.
    await _finalize_photo_creation(message, state)
    return


@router.message(MyPhotoStates.waiting_title)
async def myphoto_waiting_title_wrong(message: Message):

    await message.delete()


# === ОБРАБОТКА ВЫБОРА ТИПА УСТРОЙСТВА ===


@router.callback_query(MyPhotoStates.waiting_device_type, F.data.startswith("myphoto:device:"))
async def myphoto_device_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Этот шаг больше не используется.")


@router.callback_query(MyPhotoStates.waiting_description, F.data == "myphoto:skip_description")
async def myphoto_skip_description(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Этот шаг больше не используется.")


@router.message(MyPhotoStates.waiting_description, F.text)
async def myphoto_got_description(message: Message, state: FSMContext):
    await message.delete()
    await message.answer("Описание сейчас добавляется позже — через карточку фотографии.")
# === ОБРАБОТКА ВЫБОРА ТИПА УСТРОЙСТВА ===


# ====== DELETE PHOTO HANDLER PATCH ======

# Patch the delete handler to show correct post-delete UI

@router.callback_query(F.data.regexp(r"^myphoto:delete:(\d+)$"))
async def myphoto_delete(callback: CallbackQuery, state: FSMContext):
    """
    Показывает подтверждение удаления своей фотографии (с премиум-предупреждением).
    """
    user = await _ensure_user(callback)
    if user is None:
        return

    photo_id_str = callback.data.split(":")[2]
    try:
        photo_id = int(photo_id_str)
    except Exception:
        await callback.answer("Ошибка удаления.", show_alert=True)
        return

    # Проверяем, что фото принадлежит пользователю
    photo = await get_photo_by_id(photo_id)
    if photo is None or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    # Already deleted?
    if photo.get("is_deleted"):
        await callback.answer("Фотография уже удалена.", show_alert=True)
        return

    # Compute is_premium_user
    is_admin = is_admin_user(user)
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    warning = ""
    if not is_admin and not is_premium_user:
        today_count = await count_today_photos_for_user(user["id"], include_deleted=True)
        if today_count >= 1:
            remaining = _format_time_until_next_upload()
            warning = (
                f"\n\n⚠️ После удаления ты <b>не сможешь</b> загрузить новую фотографию {remaining}.\n"
                "Хочешь продолжить?"
            )
        else:
            warning = "\n\nХочешь продолжить?"
    else:
        warning = "\n\nХочешь продолжить?"

    confirm_text = (
        "🗑 <b>Удалить фотографию?</b>\n\n"
        "Фотография будет удалена из профиля и больше не будет участвовать в оценках."
        f"{warning}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"myphoto:delete_confirm:{photo_id}")
    kb.button(text="❌ Нет", callback_data=f"myphoto:delete_cancel:{photo_id}")
    kb.adjust(1)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=confirm_text, reply_markup=kb.as_markup())
        else:
            await callback.message.edit_text(confirm_text, reply_markup=kb.as_markup())
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=confirm_text,
            reply_markup=kb.as_markup(),
            disable_notification=True,
        )
    await callback.answer()


# --- Confirm delete handler ---
@router.callback_query(F.data.regexp(r"^myphoto:delete_confirm:(\d+)$"))
async def myphoto_delete_confirm(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение удаления своей фотографии.
    """
    user = await _ensure_user(callback)
    if user is None:
        return
    photo_id_str = callback.data.split(":")[2]
    try:
        photo_id = int(photo_id_str)
    except Exception:
        await callback.answer("Ошибка удаления.", show_alert=True)
        return

    # Проверяем, что фото принадлежит пользователю
    photo = await get_photo_by_id(photo_id)
    if photo is None or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return
    if photo.get("is_deleted"):
        await callback.answer("Фотография уже удалена.", show_alert=True)
        return

    await mark_photo_deleted(photo_id)
    await _clear_photo_message_id(state)

    is_admin = is_admin_user(user)
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    can_upload, remaining = await _can_user_upload_now(user, is_premium_user, is_admin)
    if can_upload:
        kb = InlineKeyboardBuilder()
        kb.button(text="📤 Загрузить", callback_data="myphoto:add")
        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.adjust(1, 1)
        ready = _ready_wording(user)
        text = (
            "📸 <b>Загрузить фотографию!</b>\n\n"
            "Здесь оценивают кадры, а не твою внешность.\n\n"
            "<b>Правила загрузки:</b>\n"
            "• Можно загрузить только один кадр в день;\n"
            "• Селфи / фотографии, где изображён(а) ты сам(а) — нельзя;\n"
            "• Без рекламы: названия, ссылки и прочее;\n"
            "• Только свои фотографии;\n"
            "• Без откровенного контента и насилия.\n\n"
            "Администрация вправе удалить контент и ограничить доступ к боту при нарушении правил.\n\n"
            f"Когда будешь {ready} — жми «Загрузить»."
        )
        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=kb.as_markup())
            else:
                await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
            )
        await callback.answer("Фотография удалена.")
        return
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="💎 Premium", callback_data="premium:open")
        kb.button(text="⬅️ В меню", callback_data="menu:back")
        kb.adjust(1, 1)
        text = (
            f"Загрузить новую фотографию можно {remaining}.\n\n"
            "Подождите, либо купите подписку GlowShot Premium и забудьте про лимиты."
        )
        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=kb.as_markup())
            else:
                await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
            )
        await callback.answer("Фотография удалена.")
        return

# --- Cancel delete handler ---
@router.callback_query(F.data.regexp(r"^myphoto:delete_cancel:(\d+)$"))
async def myphoto_delete_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Отмена удаления — восстановить карточку фото.
    """
    user = await _ensure_user(callback)
    if user is None:
        await callback.answer("Ок")
        return
    photo_id_str = callback.data.split(":")[2]
    try:
        photo_id = int(photo_id_str)
    except Exception:
        await callback.answer("Ок")
        return
    photo = await get_photo_by_id(photo_id)
    if photo is None or int(photo.get("user_id", 0)) != int(user["id"]) or photo.get("is_deleted"):
        await callback.answer("Ок")
        return
    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo["id"])
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=caption, reply_markup=kb)
        else:
            await callback.message.edit_text(caption, reply_markup=kb)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=caption,
            reply_markup=kb,
            disable_notification=True,
        )
    await callback.answer("Отменено")


# ====== MY PHOTO CALLBACK HANDLERS FOR COMMENTS/STATS/REPEAT/PROMOTE/EDIT ======

# --- Helper keyboards ---
def _myphoto_back_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}")
    kb.button(text="🏠 В меню", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()


def _myphoto_comments_kb(
    photo_id: int,
    page: int,
    has_prev: bool,
    has_next: bool,
    *,
    sort_key: str = "date",
    sort_dir: str = "desc",
    show_sort: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    nav_row: list[InlineKeyboardButton] = []
    if has_prev:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"myphoto:comments:{photo_id}:{page-1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"myphoto:comments:{photo_id}:{page+1}"))
    if nav_row:
        kb.row(*nav_row)

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))
    return kb.as_markup()


# --- Back to main card handler ---
@router.callback_query(F.data.regexp(r"^myphoto:back:(\d+)$"))
async def myphoto_back(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    photo_id_str = callback.data.split(":")[2]
    try:
        photo_id = int(photo_id_str)
    except Exception:
        await callback.answer("Ошибка.")
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or int(photo.get("user_id", 0)) != int(user["id"]) or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo_id)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=caption, reply_markup=kb)
        else:
            await callback.message.edit_text(caption, reply_markup=kb)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=kb,
            disable_notification=True,
        )

    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:comments:"))
async def myphoto_comments(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка.")
        return

    try:
        photo_id = int(parts[2])
    except Exception:
        await callback.answer("Ошибка.")
        return

    # page optional
    try:
        page = int(parts[3]) if len(parts) >= 4 else 0
    except Exception:
        page = 0

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return
    if int(photo.get("user_id", 0)) != int(user.get("id", 0)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    per_page = 15
    if page < 0:
        page = 0
    offset = page * per_page

    # counts
    try:
        counts = await get_comment_counts_for_photo(photo_id)
    except Exception:
        counts = {"public": 0, "anonymous": 0}

    public_cnt = int(counts.get("public") or 0)
    anon_cnt = int(counts.get("anonymous") or 0)

    # list
    try:
        rows = await get_comments_for_photo_sorted(
            photo_id,
            limit=per_page + 1,
            offset=offset,
            sort_key="date",
            sort_dir="desc",
        )
    except Exception:
        rows = []

    has_next = len(rows) > per_page
    comments = rows[:per_page]
    has_prev = page > 0

    lines = []
    lines.append("💬 <b>Комментарии к твоей фотографии:</b>")
    lines.append(f"💎 Анонимные: <b>{anon_cnt}</b>")
    lines.append(f"Публичные: <b>{public_cnt}</b>")
    lines.append("")

    if not comments:
        if (public_cnt + anon_cnt) > 0:
            lines.append("Комментарии есть, но список не загрузился 😵‍💫")
            lines.append("Нажми ещё раз или попробуй позже.")
        else:
            lines.append("Пока комментариев нет.")
    else:
        for i, c in enumerate(comments, start=1 + offset):
            is_public = bool(c.get("is_public", 1))
            username = (c.get("username") or "").strip()
            author_name = (c.get("author_name") or "").strip()
            score = c.get("score")
            text = (c.get("text") or "").strip()

            if is_public and username:
                who = f"@{username}" + (f" ({author_name})" if author_name else "")
            elif is_public and author_name:
                who = author_name
            elif is_public:
                who = "Пользователь"
            else:
                who = "💎 Пользователь"

            score_str = "—"
            try:
                if score is not None:
                    score_str = str(int(score))
            except Exception:
                score_str = "—"

            lines.append(f"{i}. {who} — <b>{score_str}</b>")
            lines.append(f"Пишет: {text if text else '—'}")

    text_out = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"myphoto:comments:{photo_id}:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"myphoto:comments:{photo_id}:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text_out, reply_markup=kb.as_markup())
        else:
            await callback.message.edit_text(text_out, reply_markup=kb.as_markup())
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text_out,
            reply_markup=kb.as_markup(),
            disable_notification=True,
        )

    await callback.answer()


# --- Promote handler ---
@router.callback_query(F.data.regexp(r"^myphoto:promote:(\d+)$"))
async def myphoto_promote(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    photo_id_str = callback.data.split(":")[2]
    try:
        photo_id = int(photo_id_str)
    except Exception:
        await callback.answer("Ошибка.")
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or int(photo.get("user_id", 0)) != int(user["id"]) or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return

    can = await _compute_can_promote(photo)
    if not can:
        await callback.answer("Сейчас эту фотографию нельзя продвигать.", show_alert=True)
        return

    try:
        await add_weekly_candidate(photo_id)
    except Exception:
        await callback.answer("Не удалось продвинуть фотографию. Попробуй позже.", show_alert=True)
        return

    await callback.answer("Готово! Фотография добавлена в недельный отбор ✅", show_alert=True)


# --- Repeat (temporary) handler ---
@router.callback_query(F.data.regexp(r"^myphoto:repeat:(\d+)$"))
async def myphoto_repeat(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Скоро ✨", show_alert=True)


# --- Edit (temporary) handler ---
@router.callback_query(F.data.regexp(r"^myphoto:edit:(\d+)$"))
async def myphoto_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Скоро ✨", show_alert=True)
# ====== FINALIZE PHOTO CREATION ======

# Patch: _finalize_photo_creation supports both Message and CallbackQuery and does not rely on callback.message
async def _finalize_photo_creation(event: Message | CallbackQuery, state: FSMContext) -> None:
    """
    Завершить процесс создания фото: сохранить в БД и показать карточку пользователю.
    event: Message или CallbackQuery
    """
    # поддерживаем вызов как из callback, так и из message
    if isinstance(event, CallbackQuery):
        bot = event.message.bot
        fallback_chat_id = event.message.chat.id
    else:
        bot = event.bot
        fallback_chat_id = event.chat.id

    data = await state.get_data()
    user_id = data.get("upload_user_id")
    file_id = data.get("file_id")
    title = data.get("title")
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")

    # Save photo in DB, handle unique violation
    try:
        photo_id = await create_today_photo(
            user_id=user_id,
            file_id=file_id,
            title=title,
        )
    except UniqueViolationError:
        await bot.send_message(
            chat_id=upload_chat_id or fallback_chat_id,
            text=(
                "Ты уже загружал(а) фотографию сегодня.\n\n"
                "Если хочешь заменить — удали текущую в разделе «Моя фотография» и попробуй снова."
            ),
            disable_notification=True,
        )
        await state.clear()
        return

    # Get photo object from DB
    photo = await get_photo_by_id(photo_id)
    if not photo:
        # fallback error
        await bot.send_message(
            chat_id=upload_chat_id or fallback_chat_id,
            text="Ошибка при сохранении фотографии. Попробуйте ещё раз.",
            disable_notification=True,
        )
        await state.clear()
        return

    chat_id = upload_chat_id or fallback_chat_id

    # Финальная карточка: стараемся НЕ плодить сообщения.
    caption = await build_my_photo_main_text(photo)
    kb = build_my_photo_keyboard(photo["id"])

    # 1) Если у нас есть сообщение мастера (это фото-сообщение от бота) — просто редактируем caption+кнопки.
    if upload_msg_id and chat_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=upload_msg_id,
                caption=caption,
                reply_markup=kb,
            )
            await _store_photo_message_id(state, upload_msg_id, photo_id=photo["id"])
            await state.clear()
            return
        except Exception:
            pass

    # 2) Фоллбек: удаляем старое сообщение (если есть) и отправляем новое.
    if upload_msg_id and chat_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=upload_msg_id)
        except Exception:
            pass

    sent_photo = await bot.send_photo(
        chat_id=chat_id,
        photo=photo["file_id"],
        caption=caption,
        reply_markup=kb,
        disable_notification=True,
    )
    await _store_photo_message_id(state, sent_photo.message_id, photo_id=photo["id"])
    await state.clear()
    return