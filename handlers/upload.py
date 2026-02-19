import io
import random
import asyncio
from PIL import Image  # type: ignore
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from datetime import date, datetime, timedelta
from asyncpg.exceptions import UniqueViolationError

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.i18n import t
from utils.banner import sync_giraffe_section_nav
from utils.registration_guard import require_user_name
from utils.antispam import should_throttle
from keyboards.common import HOME

from aiogram.exceptions import TelegramBadRequest

from database import (
    get_user_by_tg_id,
    create_today_photo,
    mark_photo_deleted_by_user,
    get_photo_by_id,
    update_photo_editable_fields,
    toggle_photo_ratings_enabled,
    get_photo_stats,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
    is_user_premium_active,
    get_active_photos_for_user,
    get_latest_photos_for_user,
    get_archived_photos_for_user,
    get_archived_photos_count,
    get_comment_counts_for_photo,
    get_photo_stats_snapshot,
    get_user_spend_today_stats,
    get_comments_for_photo_sorted,
    streak_record_action_by_tg_id,
    ensure_user_author_code,
    get_weekly_idea_requests,
    increment_weekly_idea_requests,
    get_user_stats,
    add_credits,
    check_can_upload_today,
    should_show_upload_rules,
    set_upload_rules_ack_at,
)

from database_results import (
    PERIOD_DAY,
    SCOPE_GLOBAL,
    KIND_TOP_PHOTOS,
    get_results_items,
)

from utils.time import get_moscow_now, format_party_id
from utils.watermark import apply_text_watermark
from utils.ui import cleanup_previous_screen, remember_screen


router = Router()


# =====================
# Results v2 helpers (daily only)
# =====================

def _shorten_alert(text: str, limit: int = 180) -> str:
    """Safely shrink text for callback alerts (Telegram limits ~200 chars)."""
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


async def _edit_or_replace_text(
    callback: CallbackQuery,
    text: str,
    kb: InlineKeyboardMarkup,
    *,
    parse_mode: str = "HTML",
) -> int | None:
    """Try edit current screen first; fallback to delete+send (single live screen)."""
    try:
        if getattr(callback.message, "photo", None):
            await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode=parse_mode)
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
        return int(callback.message.message_id)
    except Exception:
        pass
    try:
        await callback.message.delete()
    except Exception:
        pass
    try:
        sent = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=kb,
            parse_mode=parse_mode,
            disable_notification=True,
        )
        return int(sent.message_id)
    except Exception:
        return None


async def _tap_guard(callback: CallbackQuery, key: str, seconds: float = 0.7) -> bool:
    """Lightweight anti-spam guard for repeated callback taps."""
    if not should_throttle(callback.from_user.id, key, seconds):
        return False
    try:
        await callback.answer("Секунду…", show_alert=False)
    except Exception:
        pass
    return True


def _upload_processing_error_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="myphoto:open"),
    )
    return kb.as_markup()


async def _edit_or_replace_progress_message(
    *,
    bot,
    chat_id: int,
    message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    prefer_caption: bool = False,
    parse_mode: str | None = None,
) -> tuple[int | None, bool]:
    """
    Обновляет один progress-экран.
    prefer_caption=True: сначала пробуем edit_caption (для сообщения с фото).
    """
    if message_id:
        if prefer_caption:
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return int(message_id), True
            except Exception:
                pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return int(message_id), False
            except Exception:
                pass
        else:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return int(message_id), False
            except Exception:
                pass
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
                return int(message_id), True
            except Exception:
                pass
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        except Exception:
            pass

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=True,
        )
        return int(sent.message_id), False
    except Exception:
        return None, False


async def _replace_or_send_progress_photo(
    *,
    bot,
    chat_id: int,
    message_id: int | None,
    image_bytes: bytes,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[int | None, str | None, bool]:
    """
    Подменяет медиа в текущем progress-сообщении (или пересоздаёт одно сообщение при фоллбэке).
    Возвращает: (message_id, public_file_id, is_photo_message).
    """
    payload = BufferedInputFile(image_bytes, filename="watermarked.jpg")
    if message_id:
        try:
            edited = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=int(message_id),
                media=InputMediaPhoto(media=payload, caption=caption),
                reply_markup=reply_markup,
            )
            if isinstance(edited, Message) and edited.photo:
                return int(message_id), str(edited.photo[-1].file_id), True
        except Exception:
            pass
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        except Exception:
            pass

    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(image_bytes, filename="watermarked.jpg"),
            caption=caption,
            reply_markup=reply_markup,
            disable_notification=True,
        )
        file_id = str(sent.photo[-1].file_id) if sent.photo else None
        return int(sent.message_id), file_id, True
    except Exception:
        return None, None, False


async def _download_telegram_photo_bytes(bot, file_id: str) -> bytes:
    f = await bot.get_file(str(file_id))
    out = io.BytesIO()
    await bot.download_file(f.file_path, destination=out)
    return out.getvalue()


async def _set_upload_progress(
    *,
    state: FSMContext,
    bot,
    chat_id: int,
    message_id: int | None,
    text: str,
    is_photo_message: bool,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[int | None, bool]:
    msg_id, is_photo = await _edit_or_replace_progress_message(
        bot=bot,
        chat_id=int(chat_id),
        message_id=message_id,
        text=text,
        reply_markup=reply_markup,
        prefer_caption=bool(is_photo_message),
    )
    if msg_id is not None:
        await state.update_data(
            upload_msg_id=int(msg_id),
            upload_chat_id=int(chat_id),
            upload_is_photo=bool(is_photo),
            upload_progress_msg_id=int(msg_id),
            upload_progress_is_photo=bool(is_photo),
        )
    return msg_id, is_photo


async def _show_upload_processing_error(
    *,
    state: FSMContext,
    bot,
    chat_id: int,
    message_id: int | None,
    is_photo_message: bool,
    text: str = "❌ Не получилось загрузить фото. Попробуй ещё раз.",
) -> None:
    await _set_upload_progress(
        state=state,
        bot=bot,
        chat_id=int(chat_id),
        message_id=message_id,
        text=text,
        is_photo_message=bool(is_photo_message),
        reply_markup=_upload_processing_error_kb(),
    )
    try:
        await state.clear()
    except Exception:
        pass


async def _accept_image_for_upload(message: Message, state: FSMContext, source: str = "photo") -> None:
    """
    Унифицированная обработка входящего изображения (photo или document).
    Для document скачиваем и пересылаем как photo, чтобы итоговый file_id был фоткой.
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

    photo_bytes = None
    file_id_to_preview = None
    file_id_to_process = None
    if source == "photo":
        file_id_to_preview = message.photo[-1].file_id
        file_id_to_process = file_id_to_preview
    else:
        if message.document:
            file_id_to_process = str(message.document.file_id)
        try:
            buf = await message.bot.download(message.document)
            raw_bytes = buf.read()
            # Конвертируем документ в JPEG, чтобы Telegram принял как фото
            try:
                img = Image.open(io.BytesIO(raw_bytes))
                img = img.convert("RGB")
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=95, subsampling=0, optimize=True)
                photo_bytes = out.getvalue()
            except Exception:
                # если не смогли конвертировать — используем как есть, может пройти
                photo_bytes = raw_bytes
        except Exception:
            photo_bytes = None

    # Если пришёл документ — пересылаем как фото для единообразия
    sent_photo = None
    try:
        await message.bot.delete_message(chat_id=upload_chat_id, message_id=upload_msg_id)
    except Exception:
        pass
    try:
        if photo_bytes is not None:
            sent_photo = await message.bot.send_photo(
                chat_id=upload_chat_id,
                photo=BufferedInputFile(photo_bytes, filename="upload.jpg"),
                caption=(
                    "Фотография получена ✅\n\n"
                    "Теперь напиши название этой работы.\n"
                    "<b>Поменять название после загрузки нельзя.</b>\n\n"
                    "Для максимального качества можно отправлять фото как файл (JPEG/PNG)."
                ),
                reply_markup=build_upload_wizard_kb(back_to="photo"),
                disable_notification=True,
            )
            file_id_to_preview = sent_photo.photo[-1].file_id if sent_photo and sent_photo.photo else file_id_to_preview
        else:
            sent_photo = await message.bot.send_photo(
                chat_id=upload_chat_id,
                photo=file_id_to_preview,
                caption=(
                    "Фотография получена ✅\n\n"
                    "Теперь напиши название этой работы.\n"
                    "<b>Поменять название после загрузки нельзя.</b>\n\n"
                    "Для максимального качества можно отправлять фото как файл (JPEG/PNG)."
                ),
                reply_markup=build_upload_wizard_kb(back_to="photo"),
                disable_notification=True,
            )
    except Exception:
        # Сообщаем об ошибке конвертации/отправки и не трогаем состояние
        await message.answer(
            "Не удалось принять файл. Попробуй отправить как фото или другой формат (jpeg/png).",
            disable_notification=True,
        )
        return

    # Удаляем сообщение пользователя, чтобы чат оставался чистым
    try:
        await message.delete()
    except Exception:
        pass

    if sent_photo is None:
        await message.bot.send_message(
            chat_id=upload_chat_id,
            text="Не удалось принять изображение, попробуй ещё раз как фото.",
            disable_notification=True,
        )
        return

    await state.update_data(
        file_id=file_id_to_process or file_id_to_preview,
        upload_msg_id=sent_photo.message_id,
        upload_chat_id=upload_chat_id,
        upload_is_photo=True,
    )
    await state.set_state(MyPhotoStates.waiting_title)


async def _get_daily_top_photos_v2(day_key: str, limit: int = 10) -> list[dict]:
    """Read daily top photos from results_v2 cache and return full photo dicts."""
    try:
        items = await get_results_items(
            period=PERIOD_DAY,
            period_key=str(day_key),
            scope_type=SCOPE_GLOBAL,
            scope_key="",
            kind=KIND_TOP_PHOTOS,
            limit=int(limit),
        )
    except Exception:
        items = []

    photos: list[dict] = []
    for it in items:
        pid = it.get("photo_id")
        if pid is None:
            continue
        try:
            p = await get_photo_by_id(int(pid))
        except Exception:
            p = None
        if not p:
            continue
        if bool(p.get("is_deleted")):
            continue
        photos.append(p)
        if len(photos) >= int(limit):
            break

    return photos


class MyPhotoStates(StatesGroup):
    """Состояния мастера загрузки фотографии.

    порядок:
    1) загрузка фото;
    2) название.

    (Устройство/описание будут добавляться позже через редактирование.)
    """

    waiting_category = State()
    waiting_photo = State()
    waiting_title = State()
    waiting_device_type = State()
    waiting_description = State()


class EditPhotoStates(StatesGroup):
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


def is_unlimited_upload_user(user: dict) -> bool:
    """Админ и помощник могут бесконечно публиковать в течение дня (для тестов)."""
    return bool(user.get("is_admin") or user.get("is_helper"))


def _ready_wording(user: dict) -> str:
    g = (user.get("gender") or "").strip().lower()
    if g in {"м", "муж", "мужской", "male", "man", "парень"}:
        return "готов"
    if g in {"ж", "жен", "женский", "female", "woman", "девушка"}:
        return "готова"
    return "готов(а)"


def _selfie_wording(user: dict) -> str:
    g = (user.get("gender") or "").strip().lower()
    if g in {"м", "муж", "мужской", "male", "man", "парень"}:
        return "Селфи и кадры, где изображён ты сам"
    if g in {"ж", "жен", "женский", "female", "woman", "девушка"}:
        return "Селфи и кадры, где изображена ты сама"
    return "Селфи и кадры, где изображен(а) ты сам(а)"


IDEA_POOL: list[dict[str, str]] = [
    {"title": "Отражения", "hint": "Лужи, окна, зеркала, витрины"},
    {"title": "Тени и силуэты", "hint": "Низкое солнце, лестницы, велосипеды"},
    {"title": "Минимализм", "hint": "Один объект, пустой фон, чистые линии"},
    {"title": "Городская геометрия", "hint": "Лестницы, мосты, разметка на дорогах"},
    {"title": "Свет в тумане", "hint": "Фонари, пар, подсветка в дымке"},
    {"title": "Неон и вывески", "hint": "Мокрый асфальт, витрины, огни ночного города"},
    {"title": "Сверху вниз", "hint": "Эскалаторы, балконы, вид с лестницы"},
    {"title": "Макро деталей", "hint": "Текстуры ткани, листьев, ржавчины, дерева"},
    {"title": "Повторы и ритмы", "hint": "Окна, балконы, стулья, плитка"},
    {"title": "Цветовой контраст", "hint": "Красный на зелёном, синий на оранжевом"},
    {"title": "Движение", "hint": "Длинная выдержка, транспорт, метро, трассы"},
    {"title": "Домашний уют", "hint": "Лампа, книги, чай, тёплые пледы"},
    {"title": "Ночной город", "hint": "Гирлянды, фарлайты, отражения в окнах"},
    {"title": "Природные фактуры", "hint": "Мох, камни, кора, песок"},
    {"title": "Ретро настроение", "hint": "Старые вывески, плёночный стиль, винтажные предметы"},
    {"title": "Вода в кадре", "hint": "Брызги, дождь, фонтан, стекло с каплями"},
    {"title": "Спорт и динамика", "hint": "Бег, велосипед, мяч, размытие движения"},
    {"title": "Монохром", "hint": "Чёрно-белое, жёсткие тени, высокая контрастность"},
    {"title": "Симметрия", "hint": "Мосты, тоннели, отражения, арки"},
    {"title": "Сквозь что-то", "hint": "Дверные проёмы, решётки, листья на переднем плане"},
    {"title": "Тёплый vs холодный свет", "hint": "Лампы vs окно, вечернее и дневное освещение"},
    {"title": "Графика и шрифты", "hint": "Граффити, афиши, таблички, вывески"},
    {"title": "Микродетали города", "hint": "Кнопки лифта, домофоны, ручки дверей"},
    {"title": "Кухонные сцены", "hint": "Пар, специи, овощи, фактура посуды"},
    {"title": "Пространство и глубина", "hint": "Длинные коридоры, линии перспективы, туннели"},
]


def _current_week_key() -> str:
    now = get_moscow_now()
    monday = now.date() - timedelta(days=now.weekday())
    return monday.isoformat()


def _get_daily_idea() -> tuple[str, str]:
    if not IDEA_POOL:
        return "Свободная тема", "Придумай свой сюжет и покажи его в кадре"
    today = get_moscow_now().date()
    idx = today.toordinal() % len(IDEA_POOL)
    idea = IDEA_POOL[idx]
    return idea["title"], idea["hint"]


def _pick_random_idea(exclude_title: str | None = None) -> tuple[str, str]:
    if not IDEA_POOL:
        return _get_daily_idea()
    pool = [i for i in IDEA_POOL if (exclude_title is None or i["title"] != exclude_title)]
    if not pool:
        pool = IDEA_POOL
    idea = random.choice(pool)
    return idea["title"], idea["hint"]


async def _idea_counters(user: dict, is_premium_user: bool) -> tuple[int, int, int]:
    """Return (limit, current_used, remaining) for weekly idea requests."""
    limit = 7 if is_premium_user else 3
    current = 0
    try:
        current = await get_weekly_idea_requests(user["id"], _current_week_key())
    except Exception:
        current = 0
    remaining = max(limit - current, 0)
    return limit, current, remaining


def _build_upload_intro_text(
    user: dict,
    *,
    idea_label: str,
    idea_title: str,
    idea_hint: str,
    publish_notice: str | None = None,
) -> str:
    ready = _ready_wording(user)
    selfie = _selfie_wording(user)
    lines: list[str] = [
        "📸 <b>Загрузить фотографию!</b>",
        "",
        f"💡 <b>{idea_label}:</b> {idea_title}",
        f"🔍 <b>Попробуй:</b> {idea_hint}.",
        "",
        "🚫 <b>Что нельзя загружать:</b>",
        f"• {selfie};",
        "• Рекламные фотографии;",
        "• Чужие снимки;",
        "• Откровенный или триггерный контент.",
        "",
        "🛡 Модерация вправе удалить вашу фотографию и ограничить доступ к боту при нарушении правил.",
        "",
    ]
    if publish_notice:
        lines.extend([f"⚠️ {publish_notice}", ""])
    lines.append(f"Когда будешь {ready} — жми «Загрузить».")
    return "\n".join(lines)


def build_upload_intro_kb(
    *,
    remaining: int | None = None,
    limit: int | None = None,
    idea_cb: str = "myphoto:idea",
    upload_cb: str = "myphoto:add",
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    remaining_safe = None
    if remaining is not None:
        try:
            remaining_safe = max(int(remaining), 0)
        except Exception:
            remaining_safe = None
    idea_btn_text = "🎲 Сгенерировать идею"
    if remaining_safe is not None:
        idea_btn_text += f" ({remaining_safe})"
    kb.button(text=idea_btn_text, callback_data=idea_cb)
    kb.button(text="📤 Загрузить", callback_data=upload_cb)
    kb.button(text=HOME, callback_data="menu:back")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


UPLOAD_RULES_WAIT_SECONDS = 3


def _build_upload_rules_text(user: dict) -> str:
    selfie = _selfie_wording(user)
    return "\n".join(
        [
            "🛑 <b>Правила перед загрузкой</b>",
            "",
            f"🚫 <b>Селфи запрещены</b> — {selfie}.",
            "🚫 Реклама / промо / приглашения в каналы.",
            "🚫 Чужие фото (репосты/пинтерест) и чужие персонажи без права.",
            "🚫 NSFW/жесть/триггеры.",
            "",
            "✅ Загружай <b>свои</b> фото: стрит, природа, архитектура, детали, идеи.",
            "",
            "🛡 Модерация может удалить фото и ограничить доступ при нарушении.",
            "",
            "<i>Пожалуйста, прочитай. Кнопка появится через пару секунд.</i>",
        ]
    )


def build_upload_rules_wait_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⏳ Читаю…", callback_data="myphoto:rules:wait"))
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="myphoto:rules:back"),
    )
    return kb.as_markup()


def build_upload_rules_ack_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Ознакомился(ась) → дальше", callback_data="myphoto:rules:ack"))
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="myphoto:rules:back"),
    )
    return kb.as_markup()


async def _render_upload_intro_screen(
    callback: CallbackQuery,
    state: FSMContext,
    user: dict,
) -> None:
    """Render upload intro with ideas (used by upload cancel/back flows)."""
    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    limit, _current, remaining = await _idea_counters(user, is_premium_user)
    can_upload_today = True
    denied_reason = None
    if not is_unlimited_upload_user(user):
        can_upload_today, denied_reason = await check_can_upload_today(int(user["id"]))

    idea_title, idea_hint = _get_daily_idea()
    text = _build_upload_intro_text(
        user,
        idea_label="Идея дня",
        idea_title=idea_title,
        idea_hint=idea_hint,
        publish_notice=(
            denied_reason or "Публикация сегодня недоступна. Завтра можно."
            if not can_upload_today
            else None
        ),
    )
    kb = build_upload_intro_kb(remaining=remaining, limit=limit)
    sent_id = await _edit_or_replace_text(callback, text, kb)
    if sent_id is not None:
        await remember_screen(callback.from_user.id, sent_id, state=state)


def _photo_ratings_enabled(photo: dict) -> bool:
    return bool(photo.get("ratings_enabled", True))


def _photo_public_id(photo: dict) -> str:
    return str(photo.get("file_id_public") or photo.get("file_id"))


def _is_photo_quality_ok(image_bytes: bytes) -> tuple[bool, str | None]:
    """Проверяем базовое качество: разрешение не меньше 1200x800 (любая ориентация)."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
    except Exception:
        return False, "Не удалось прочитать изображение."

    min_side = min(w, h)
    max_side = max(w, h)
    if min_side < 800 or max_side < 1200:
        return False, f"Слишком низкое разрешение ({w}×{h}). Минимум: 1200×800."
    return True, None


def build_my_photo_keyboard(
    photo_id: int,
    *,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("myphoto.btn.stats", lang), callback_data=f"myphoto:stats:{photo_id}"),
                InlineKeyboardButton(text=t("myphoto.btn.edit", lang), callback_data=f"myphoto:edit:{photo_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=t("myphoto.btn.share", lang),
                    callback_data=f"myphoto:share:{photo_id}",
                ),
                InlineKeyboardButton(text="📨 Комментарии", callback_data=f"myphoto:comments:{photo_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=t("myphoto.btn.delete", lang),
                    callback_data=f"myphoto:delete:{photo_id}",
                    style="danger",
                ),
            ],
            [
                InlineKeyboardButton(text=HOME, callback_data="menu:back"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="myphoto:gallery"),
            ],
        ]
    )


EDIT_TAGS: list[tuple[str, str]] = [
    ("portrait", "👤 Портрет"),
    ("landscape", "🌄 Пейзаж"),
    ("street", "🏙 Стрит"),
    ("nature", "🌿 Природа"),
    ("architecture", "🏛 Архитектура"),
    ("travel", "🧳 Тревел"),
    ("macro", "🔎 Макро"),
    ("cosplay", "🧝 Косплей"),
    ("other", "✨ Другое"),
    ("", "🚫 Без тега"),
]

def build_edit_menu_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⭐️ Оценки", callback_data=f"myphoto:ratings:{photo_id}"))
    kb.row(
        InlineKeyboardButton(text="📷 Устройство", callback_data=f"myphoto:edit:device:{photo_id}"),
        InlineKeyboardButton(text="🏷 Тег", callback_data=f"myphoto:edit:tag:{photo_id}"),
    )
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"),
    )
    return kb.as_markup()

def build_device_type_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📱 Смартфон", callback_data=f"myphoto:device:set:{photo_id}:phone"),
        InlineKeyboardButton(text="📸 Камера", callback_data=f"myphoto:device:set:{photo_id}:camera"),
    )
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"),
    )
    return kb.as_markup()

def build_tag_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tag_key, label in EDIT_TAGS:
        kb.row(InlineKeyboardButton(text=label, callback_data=f"myphoto:tag:set:{photo_id}:{tag_key}"))
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"),
    )
    return kb.as_markup()


def _build_edit_menu_text(photo: dict) -> str:
    title = (photo.get("title") or "Без названия").strip()
    device_type = (photo.get("device_type") or "").strip()
    desc = (photo.get("description") or "").strip()
    tag = (photo.get("tag") or "").strip()
    ratings_enabled = bool(photo.get("ratings_enabled", True))

    tag_label = "🚫 Без тега" if tag == "" else tag
    for k, lbl in EDIT_TAGS:
        if k == tag:
            tag_label = lbl
            break

    title_safe = _esc_html(title)
    emoji = _device_emoji(device_type)

    if emoji:
        header = f"<code>\"{title_safe}\"</code> ({emoji})"
    else:
        header = f"<code>\"{title_safe}\"</code> (устройство не указано)"

    tag_line = _tag_label(tag)

    text = "✏️ <b>Редактирование</b>\n\n"
    text += f"<b>{header}</b>\n"
    text += f"Тег: <b>{_esc_html(tag_line)}</b>\n"
    text += f"Оценки: <b>{'включены' if ratings_enabled else 'выключены'}</b>"
    return text


async def _render_myphoto_edit_menu(
    *,
    bot,
    chat_id: int,
    message_id: int,
    photo: dict,
    had_photo: bool | None = None,
) -> tuple[int, bool]:
    """Показать меню редактирования для фото, возвращает (message_id, is_photo_message)."""
    text = _build_edit_menu_text(photo)
    kb = build_edit_menu_kb(int(photo["id"]))

    # 1) пробуем редактировать как caption, если знаем что там фото
    if had_photo:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
            return message_id, True
        except Exception:
            pass

    # 2) пробуем как текст
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        return message_id, False
    except Exception:
        pass

    # 3) фоллбек: удалить старое и отправить новое текстовое сообщение
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        disable_notification=True,
        parse_mode="HTML",
    )
    return sent.message_id, False


# ===== Stats keyboard and avg formatting helpers =====

def build_my_photo_stats_keyboard(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"),
    )
    return kb.as_markup()



def _fmt_avg(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return "—"


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        if " " in s and "T" not in s:
            return datetime.fromisoformat(s.replace(" ", "T"))
    except Exception:
        pass
    return None


def _fmt_num(v: float | int) -> str:
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _hours_alive(created_at: object) -> float:
    created_dt = _parse_dt(created_at)
    if not created_dt:
        return 0.0
    now = get_moscow_now()
    if created_dt.tzinfo is None and now.tzinfo is not None:
        created_dt = created_dt.replace(tzinfo=now.tzinfo)
    diff_hours = (now - created_dt).total_seconds() / 3600.0
    return max(diff_hours, 1 / 60)


def _format_time_left(expires_at: object) -> str:
    exp_dt = _parse_dt(expires_at)
    if not exp_dt:
        return "—"
    now = get_moscow_now()
    if exp_dt.tzinfo is None and now.tzinfo is not None:
        exp_dt = exp_dt.replace(tzinfo=now.tzinfo)
    delta = exp_dt - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "архивируется сейчас"

    minutes = seconds // 60
    days = minutes // (24 * 60)
    minutes %= (24 * 60)
    hours = minutes // 60
    minutes %= 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days} {_plural_ru(days, 'день', 'дня', 'дней')}")
    if hours > 0:
        parts.append(f"{hours} {_plural_ru(hours, 'час', 'часа', 'часов')}")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} {_plural_ru(minutes, 'минута', 'минуты', 'минут')}")
    return " ".join(parts) if parts else "меньше минуты"


def _photo_party_id(photo: dict) -> str:
    submit_day = (
        photo.get("submit_day")
        or photo.get("day_key")
        or str(photo.get("created_at") or "")[:10]
    )
    return format_party_id(submit_day, include_year_if_needed=True)


def _is_photo_active_for_myphoto(photo: dict | None) -> bool:
    if not photo:
        return False
    if bool(photo.get("is_deleted")):
        return False
    status = str(photo.get("status") or "active").strip().lower()
    if status != "active":
        return False
    exp_dt = _parse_dt(photo.get("expires_at"))
    if not exp_dt:
        return True
    now = get_moscow_now()
    if exp_dt.tzinfo is None and now.tzinfo is not None:
        exp_dt = exp_dt.replace(tzinfo=now.tzinfo)
    return exp_dt > now


def _short_title_for_button(title: object, *, limit: int = 28) -> str:
    s = str(title or "Без названия").strip().replace("\n", " ")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


async def _load_active_myphoto_gallery(user_id: int) -> list[dict]:
    photos = await get_active_photos_for_user(int(user_id), limit=10)
    photos = [p for p in photos if _is_photo_active_for_myphoto(p)]
    try:
        photos = sorted(
            photos,
            key=lambda p: (p.get("created_at") or "", p.get("id") or 0),
        )
    except Exception:
        pass
    return photos[:2]


VOTES_STABILITY_THRESHOLD = 10

DAILY_TIPS: list[str] = [
    "💡 Совет дня: главный объект + чистый фон = моментальный буст оценок",
    "💡 Совет дня: один сюжет в кадре работает лучше, чем перегруз деталями",
    "💡 Совет дня: мягкий боковой свет чаще даёт более высокий рейтинг",
    "💡 Совет дня: оставь немного воздуха вокруг объекта, кадр смотрится чище",
    "💡 Совет дня: вертикали держи ровно, архитектура это любит",
    "💡 Совет дня: убери лишнее с краёв кадра перед публикацией",
    "💡 Совет дня: контрастный акцент по цвету помогает зацепить взгляд",
    "💡 Совет дня: сначала композиция, потом фильтры",
    "💡 Совет дня: попробуй снимать на уровне глаз объекта, это усиливает фокус",
    "💡 Совет дня: один сильный кадр лучше серии похожих",
]

def _daily_tip() -> str:
    if not DAILY_TIPS:
        return "💡 Совет дня: чистая композиция почти всегда выигрывает."
    idx = get_moscow_now().date().toordinal() % len(DAILY_TIPS)
    return DAILY_TIPS[idx]


def _gallery_mission_text() -> str:
    return "🎯 Миссия дня: оцени 10 фото → +10 credits → твои работы покажут чаще"


def _build_myphoto_gallery_kb(
    photos: list[dict],
    *,
    can_add_more: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for photo in photos:
        tag_key = str(photo.get("tag") or "").strip()
        left_emoji = _tag_emoji(tag_key) if tag_key else "📸"
        kb.row(
            InlineKeyboardButton(
                text=f"{left_emoji} {_short_title_for_button(photo.get('title'))}",
                callback_data=f"myphoto:view:{int(photo['id'])}",
            )
        )
    if can_add_more:
        kb.row(InlineKeyboardButton(text="➕ Добавить фото", callback_data="myphoto:add"))
    kb.row(InlineKeyboardButton(text=HOME, callback_data="menu:back"))
    return kb.as_markup()


async def _build_myphoto_gallery_text(
    photos: list[dict],
    *,
    denied_reason: str | None = None,
) -> str:
    lines: list[str] = ["🖼 <b>Галерея активных фотографий</b>", ""]
    for idx, photo in enumerate(photos, start=1):
        title = _esc_html(str(photo.get("title") or "Без названия"))
        try:
            stats = await get_photo_stats(int(photo["id"]))
        except Exception:
            stats = {}
        bayes_raw = stats.get("bayes_score")
        if bayes_raw is None:
            bayes_str = "—"
        else:
            bayes_str = _fmt_avg(float(bayes_raw))
        votes_count = int(photo.get("votes_count") or stats.get("ratings_count") or 0)
        time_left = _esc_html(_format_time_left(photo.get("expires_at")))
        party_id = _esc_html(_photo_party_id(photo))
        lines.append(f"{idx}) <code>\"{title}\"</code> - <b>{party_id}</b>")
        meta = [f"⭐ {bayes_str}", f"🗳 {votes_count}"]
        if time_left != "—":
            meta.append(f"⏳ {time_left}")
        lines.append(" · ".join(meta))
        left_to_stable = max(0, int(VOTES_STABILITY_THRESHOLD) - votes_count)
        if left_to_stable > 0:
            lines.append(f"🚀 До “стабильного рейтинга”: ещё {left_to_stable} оценки")
        else:
            lines.append("🚀 До “стабильного рейтинга”: ещё 0 (уже ок)")
        lines.append("")

    if denied_reason:
        lines.append(f"⚠️ {_esc_html(denied_reason)}")
        lines.append("")
    lines.append(_gallery_mission_text())
    lines.append(_daily_tip())

    return "\n".join(lines).strip()


async def _render_myphoto_gallery(
    callback: CallbackQuery,
    state: FSMContext,
    user: dict,
    *,
    photos: list[dict] | None = None,
) -> bool:
    user_id = int(user["id"])
    if photos is None:
        photos = await _load_active_myphoto_gallery(user_id)
    if not photos:
        return False

    can_upload_today = True
    denied_reason: str | None = None
    if not is_unlimited_upload_user(user):
        can_upload_today, denied_reason = await check_can_upload_today(user_id)

    can_add_more = len(photos) < 2 and can_upload_today
    text = await _build_myphoto_gallery_text(
        photos,
        denied_reason=denied_reason if (len(photos) < 2 and not can_upload_today) else None,
    )
    kb = _build_myphoto_gallery_kb(photos, can_add_more=can_add_more)
    sent_id = await _edit_or_replace_text(callback, text, kb)
    if sent_id is not None:
        await remember_screen(callback.from_user.id, sent_id, state=state)
    await state.update_data(
        myphoto_ids=[int(p["id"]) for p in photos],
        myphoto_last_id=int(photos[-1]["id"]),
    )
    return True


def _compute_photo_status(*, rank: int | None, votes_count: int, avg_score: float) -> str:
    if votes_count <= 0:
        return "🆕 Новая работа"
    if votes_count < VOTES_STABILITY_THRESHOLD:
        return "🌱 Набирает оценки"
    if rank is not None and rank <= 10:
        return "🔥 В зоне топа"
    if rank is not None and rank <= 15:
        return "📌 Близко к топу"
    if avg_score < 6:
        return "📉 Нужны оценки"
    return "✅ Стабильная позиция"

def _format_status_line(status: str) -> str:
    raw = (status or "").strip()
    if not raw:
        return "📌 Статус: <b>—</b>"
    parts = raw.split(maxsplit=1)
    if len(parts) == 2:
        icon, text = parts[0], parts[1]
        return f"{_esc_html(icon)} Статус: <b>{_esc_html(text)}</b>"
    return f"📌 Статус: <b>{_esc_html(raw)}</b>"

def _esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _device_emoji(device_type_raw: str) -> str | None:
    dt = (device_type_raw or "").lower()
    if "смартфон" in dt or "phone" in dt:
        return "📱"
    if "фотокамера" in dt or "camera" in dt:
        return "📸"
    return None

def _tag_label(tag_key: str) -> str:
    t = (tag_key or "").strip()
    if t == "":
        return "не указан"
    for k, lbl in EDIT_TAGS:
        if k == t:
            # lbl может быть с эмодзи — это норм
            return lbl
    return t


def _tag_emoji(tag_key: str) -> str:
    t = (tag_key or "").strip()
    for k, lbl in EDIT_TAGS:
        if k != t:
            continue
        first = (lbl or "").strip().split(" ", 1)[0]
        if first:
            return first
        break
    return "🏷️"

def _shorten(text: str, limit: int = 220) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"

def _quote(text: str) -> str:
    # Telegram HTML поддерживает <blockquote>
    return f"<blockquote>{_esc_html(text)}</blockquote>"


# ===== Upload wizard navigation keyboard (Назад / В меню / Отмена) =====

def build_upload_wizard_kb(*, back_to: str = "menu") -> InlineKeyboardMarkup:
    """Inline keyboard for upload wizard.

    back_to:
      - "menu": photo step (buttons: "В меню" + "Отмена")
      - "photo": title step (buttons: "В меню" + "Назад")
    """
    kb = InlineKeyboardBuilder()
    if back_to == "photo":
        kb.row(
            InlineKeyboardButton(text=HOME, callback_data="menu:back"),
            InlineKeyboardButton(text="⬅️ Назад", callback_data="myphoto:upload_back"),
        )
    else:
        kb.row(
            InlineKeyboardButton(text=HOME, callback_data="menu:back"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="myphoto:upload_cancel"),
        )
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
    if not (user.get("name") or "").strip():
        try:
            if not await require_user_name(callback):
                return None
        except Exception:
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
        alert_text = _shorten_alert(text)

        if isinstance(callback, CallbackQuery):
            # Делаем алерт, чтобы не плодить новые сообщения в чате.
            await callback.answer(alert_text, show_alert=True)
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
            top10 = await _get_daily_top_photos_v2(day_key, limit=10)
            for i, p in enumerate(top10, start=1):
                if int(p.get("id") or 0) == int(photo.get("id") or 0):
                    return True, "daily_top10", i
        except Exception:
            pass
    return False, None, None


async def build_my_photo_main_text(photo: dict) -> str:
    ratings_enabled = _photo_ratings_enabled(photo)

    device_type_raw = str(photo.get("device_type") or "")
    emoji = _device_emoji(device_type_raw)

    title = (photo.get("title") or "Без названия").strip()
    title_safe = _esc_html(title)

    tag_key = str(photo.get("tag") or "")
    tag_text = _esc_html(_tag_label(tag_key))

    try:
        stats = await get_photo_stats(int(photo["id"]))
    except Exception:
        stats = {}
    try:
        snapshot = await get_photo_stats_snapshot(int(photo["id"]), include_author_metrics=False)
    except Exception:
        snapshot = {}

    votes_count = int(snapshot.get("votes_count") or photo.get("votes_count") or stats.get("ratings_count") or 0)
    bayes_raw = stats.get("bayes_score")
    if votes_count <= 0:
        bayes_str = "—"
    elif bayes_raw is None:
        bayes_str = "—"
    else:
        bayes_str = _fmt_avg(float(bayes_raw))
    views_total = int(snapshot.get("views_total") or photo.get("views_count") or 0)
    rank_raw = snapshot.get("rank")
    rank = int(rank_raw) if rank_raw is not None else None
    total_in_party_raw = snapshot.get("total_in_party")
    total_in_party = int(total_in_party_raw) if total_in_party_raw is not None else None
    if votes_count <= 0:
        rank = None
    if total_in_party is not None and total_in_party <= 0:
        rank = None
    rank_for_status = rank if votes_count >= VOTES_STABILITY_THRESHOLD else None
    avg_for_status = float(snapshot.get("avg_score") or 0.0)
    if avg_for_status <= 0 and bayes_raw is not None:
        try:
            avg_for_status = float(bayes_raw)
        except Exception:
            avg_for_status = 0.0
    computed_status = _compute_photo_status(rank=rank_for_status, votes_count=votes_count, avg_score=avg_for_status)
    time_left = _format_time_left(snapshot.get("expires_at") or photo.get("expires_at"))

    device_suffix = f" ({emoji})" if emoji else ""
    header = f"<b><code>\"{title_safe}\"</code>{device_suffix}</b>"

    lines: list[str] = [header, f"🏷️ Тег: <b>{tag_text}</b>", ""]
    metric_parts = [f"⭐: <b>{bayes_str}</b>", f"🗳: <b>{votes_count}</b>"]
    if views_total > 0:
        metric_parts.append(f"👁: <b>{views_total}</b>")
    lines.append(" · ".join(metric_parts))
    lines.append(_format_status_line(computed_status))
    if time_left != "—":
        lines.append(f"⏳ До архива: <b>{_esc_html(time_left)}</b>")

    if votes_count < VOTES_STABILITY_THRESHOLD:
        lines.append(f"🚀 До стабильного рейтинга: ещё <b>{VOTES_STABILITY_THRESHOLD - votes_count}</b> оценок")

    description = str(photo.get("description") or "").strip()
    if description and description.lower() not in {"нет", "none", "null"}:
        lines.extend(["", f"<blockquote>📝 {_esc_html(description)}</blockquote>"])

    if not ratings_enabled:
        lines.extend(["", "🚫 Оценки для этой фотографии выключены."])

    return "\n".join(lines)


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
    user = None
    try:
        user = await get_user_by_tg_id(int(callback.from_user.id))
    except Exception:
        user = None

    data = await state.get_data()
    ids: list[int] = data.get("myphoto_ids") or []

    # Если state потерял список фото (например, после долгого времени или выхода из FSM),
    # восстанавливаем его из БД, чтобы не терять навигацию и кнопку стрелок.
    if not ids:
        if user:
            try:
                fresh_photos = await get_latest_photos_for_user(int(user["id"]), limit=10)
                fresh_photos = sorted(fresh_photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
                fresh_photos = fresh_photos[:2]
                ids = [p["id"] for p in fresh_photos]
                # обновляем state, чтобы навигация и блокировки работали корректно
                await state.update_data(
                    myphoto_ids=ids,
                    myphoto_last_id=photo.get("id"),
                    # оставляем остальные поля как есть
                )
            except Exception:
                ids = []

    caption = await build_my_photo_main_text(photo)
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(photo["id"], lang=lang)

    # 1) Пробуем edit_media для текущего экрана карточки фото.
    try:
        if msg.photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=_photo_public_id(photo), caption=caption, parse_mode="HTML"),
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
        photo=_photo_public_id(photo),
        caption=caption,
        reply_markup=kb,
        parse_mode="HTML",
        disable_notification=True,
    )
    await _store_photo_message_id(state, sent.message_id, photo_id=photo["id"])


async def _edit_or_replace_caption_with_photo(
    *,
    bot,
    chat_id: int,
    message_id: int,
    file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> int:
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return message_id
    except Exception:
        pass

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

    sent = await bot.send_photo(
        chat_id=chat_id,
        photo=file_id,
        caption=caption,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_notification=True,
    )
    return sent.message_id


# ========= ВХОД В РАЗДЕЛ "МОЯ ФОТОГРАФИЯ" =========


@router.callback_query(F.data == "myphoto:open")
@router.callback_query(F.data == "myphoto:gallery")
async def my_photo_menu(callback: CallbackQuery, state: FSMContext):
    cb_data = callback.data or ""
    if cb_data in {"myphoto:open", "myphoto:gallery"}:
        throttle_key = "myphoto:gallery" if cb_data == "myphoto:gallery" else "myphoto:open"
        if should_throttle(callback.from_user.id, throttle_key, 1.0):
            try:
                await callback.answer("Секунду…", show_alert=False)
            except Exception:
                pass
            return

    await cleanup_previous_screen(
        callback.message.bot,
        callback.message.chat.id,
        callback.from_user.id,
        state=state,
        exclude_ids={callback.message.message_id},
    )
    user = await _ensure_user(callback)
    if user is None:
        return
    if not (user.get("name") or "").strip():
        if not await require_user_name(callback):
            return
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    try:
        await sync_giraffe_section_nav(
            callback.message.bot,
            callback.message.chat.id,
            callback.from_user.id,
            section="myphoto",
            lang=lang,
        )
    except Exception:
        pass

    photos = await _load_active_myphoto_gallery(int(user["id"]))
    if photos:
        await state.update_data(
            myphoto_ids=[int(p["id"]) for p in photos],
            myphoto_last_id=int(photos[-1]["id"]),
        )
        await _render_myphoto_gallery(callback, state, user, photos=photos)
        await callback.answer()
        return

    await state.update_data(
        myphoto_ids=[],
        myphoto_last_id=None,
    )
    await _render_upload_intro_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:view:(\d+)$"))
async def myphoto_view(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:view", 0.45):
        return
    user = await _ensure_user(callback)
    if user is None:
        return
    try:
        photo_id = int((callback.data or "").split(":")[2])
    except Exception:
        await callback.answer("Ошибка.")
        return

    photo = await get_photo_by_id(photo_id)
    if (
        photo is None
        or int(photo.get("user_id", 0)) != int(user.get("id", 0))
        or photo.get("is_deleted")
        or not _is_photo_active_for_myphoto(photo)
    ):
        await callback.answer("Фотография не найдена.", show_alert=True)
        photos = await _load_active_myphoto_gallery(int(user["id"]))
        if photos:
            await _render_myphoto_gallery(callback, state, user, photos=photos)
        else:
            await _render_upload_intro_screen(callback, state, user)
        return

    await state.update_data(
        myphoto_last_id=int(photo_id),
    )
    await _edit_or_replace_my_photo_message(
        callback,
        state,
        photo,
    )
    await callback.answer()


@router.callback_query(F.data == "myphoto:idea")
async def myphoto_generate_idea(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:idea", 0.9):
        return
    user = await _ensure_user(callback)
    if user is None:
        return

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    limit_per_week, current_used, _ = await _idea_counters(user, is_premium_user)
    week_key = _current_week_key()

    if current_used >= limit_per_week:
        await callback.answer(
            f"Лимит идей на неделю: {limit_per_week}. Попробуй после понедельника.",
            show_alert=True,
        )
        return

    try:
        new_count = await increment_weekly_idea_requests(user["id"], week_key)
    except Exception:
        new_count = current_used + 1

    daily_title, _ = _get_daily_idea()
    idea_title, idea_hint = _pick_random_idea(exclude_title=daily_title)
    text = _build_upload_intro_text(
        user,
        idea_label="Новая идея",
        idea_title=idea_title,
        idea_hint=idea_hint,
    )
    remaining_after = max(limit_per_week - new_count, 0)
    kb = build_upload_intro_kb(remaining=remaining_after, limit=limit_per_week)

    try:
        if callback.message and getattr(callback.message, "photo", None):
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

    if remaining_after > 0:
        await callback.answer(f"Новая идея готова! Осталось {remaining_after} на неделю.")
    else:
        await callback.answer("Новая идея готова! Лимит на неделю исчерпан.")


# ========= Навигация по своим фотографиям =========
@router.callback_query(F.data.startswith("myphoto:nav:"))
async def myphoto_nav(callback: CallbackQuery, state: FSMContext):
    # Legacy callback from old keyboards: now opens gallery.
    await my_photo_menu(callback, state)


_MY_ARCHIVE_PAGE_SIZE = 5


_MONTHS_RU_SHORT = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "мая",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}


def _format_archive_day(value: object) -> str:
    if value is None:
        return "—"
    try:
        if isinstance(value, date):
            dt = value
        else:
            raw = str(value).strip()
            if not raw:
                return "—"
            dt = date.fromisoformat(raw[:10])
        return f"{dt.day} {_MONTHS_RU_SHORT.get(dt.month, dt.month)} {dt.year}"
    except Exception:
        return str(value)


def _format_archive_rating(value: object) -> str:
    try:
        score = float(value or 0.0)
    except Exception:
        score = 0.0
    if score <= 0:
        return "—"
    return f"{score:.2f}".rstrip("0").rstrip(".")


def _format_archive_rank_line(photo: dict) -> str:
    rank = photo.get("final_rank")
    total = photo.get("total_in_party")
    if rank is None and total is None:
        return "—"
    if rank is None:
        return f"#—/{int(total)}"
    if total is None:
        return f"#{int(rank)}/—"
    return f"#{int(rank)}/{int(total)}"


def _format_archive_item(photo: dict) -> list[str]:
    submit_day = photo.get("submit_day") or photo.get("archived_at") or photo.get("day_key")
    party_id = _esc_html(format_party_id(submit_day, include_year_if_needed=True))
    title = _esc_html(str(photo.get("title") or "Без названия"))
    rating = _format_archive_rating(photo.get("avg_score"))
    rank_line = _format_archive_rank_line(photo)
    votes = int(photo.get("votes_count") or 0)

    return [
        f"{party_id} · <code>\"{title}\"</code>",
        f"⭐ {rating} · 🏆 {rank_line} · 🗳 {votes}",
    ]


def _build_my_archive_text(items: list[dict], page: int, pages_total: int) -> str:
    lines: list[str] = ["📚 <b>Мой архив</b>", ""]
    if not items:
        lines.append("Архив пока пуст.")
    else:
        for idx, photo in enumerate(items):
            if idx > 0:
                lines.append("")
            lines.extend(_format_archive_item(photo))
    lines.extend(["", f"Страница: {page + 1} / {pages_total}"])
    return "\n".join(lines)


def _build_my_archive_kb(page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="◀️", callback_data=f"myphoto:archive:{page-1}" if has_prev else "noop"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="profile:open"),
        InlineKeyboardButton(text="▶️", callback_data=f"myphoto:archive:{page+1}" if has_next else "noop"),
    )
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()


@router.callback_query(F.data.regexp(r"^myphoto:archive:(\d+)$"))
async def myphoto_archive(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    try:
        page = max(0, int((callback.data or "myphoto:archive:0").split(":")[2]))
    except Exception:
        page = 0

    total_count = await get_archived_photos_count(int(user["id"]))
    pages_total = max(1, (total_count + _MY_ARCHIVE_PAGE_SIZE - 1) // _MY_ARCHIVE_PAGE_SIZE)
    page = min(page, pages_total - 1)
    offset = page * _MY_ARCHIVE_PAGE_SIZE

    items = await get_archived_photos_for_user(
        int(user["id"]),
        limit=_MY_ARCHIVE_PAGE_SIZE,
        offset=offset,
    )
    has_prev = page > 0
    has_next = page < (pages_total - 1)

    kb = _build_my_archive_kb(page=page, has_prev=has_prev, has_next=has_next)
    sent_id = await _edit_or_replace_text(callback, _build_my_archive_text(items, page, pages_total), kb)
    if sent_id is not None:
        await remember_screen(callback.from_user.id, sent_id, state=state)
    await callback.answer()


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
    is_author_user = bool(user.get("is_author"))

    try:
        snapshot = await get_photo_stats_snapshot(photo_id, include_author_metrics=is_author_user)
    except Exception:
        snapshot = {}
    if not snapshot:
        await callback.answer("⚠️ Не удалось загрузить статистику. Попробуй ещё раз через пару секунд.", show_alert=True)
        return

    votes_count = int(snapshot.get("votes_count") or 0)
    avg_score = float(snapshot.get("avg_score") or 0.0)
    rank = snapshot.get("rank")
    total_in_party = snapshot.get("total_in_party")
    views_total = int(snapshot.get("views_total") or 0)
    votes_today = int(snapshot.get("votes_today") or 0)
    positive_votes = int(snapshot.get("positive_votes") or 0)
    positive_percent = int(round((positive_votes / votes_count) * 100)) if votes_count > 0 else 0

    life_hours = _hours_alive(snapshot.get("created_at") or photo.get("created_at"))
    votes_per_hour = (votes_count / life_hours) if life_hours > 0 else 0.0

    rank_str = "—"
    if rank is not None and total_in_party is not None:
        rank_str = f"{int(rank)} / {int(total_in_party)}"
    elif rank is not None:
        rank_str = f"{int(rank)} / —"
    elif total_in_party is not None:
        rank_str = f"— / {int(total_in_party)}"

    status_raw = str(snapshot.get("status") or photo.get("status") or "active").lower()
    computed_status = _compute_photo_status(rank=rank, votes_count=votes_count, avg_score=avg_score)
    time_left = _format_time_left(snapshot.get("expires_at") or photo.get("expires_at"))
    party_id = _esc_html(_photo_party_id(photo))

    lines: list[str] = []
    if status_raw == "archived":
        lines.append("📊 <b>Итоги фото</b>")
        lines.append("")
        lines.append(f"🧩 Партия: <b>{party_id}</b>")
        if avg_score > 0:
            lines.append(f"⭐ Финальный рейтинг: <b>{_fmt_avg(avg_score)}</b>")
        lines.append(f"🏆 Итоговое место: <b>{rank_str}</b>")
        lines.append(f"🗳 Голосов: <b>{votes_count}</b>")
        if positive_percent > 0:
            lines.append(f"🎯 Положительных: <b>{positive_percent}%</b>")
        lines.append("")
        lines.append("📦 Фото в архиве")
    else:
        lines.append("📊 <b>Статистика фото</b>")
        lines.append("")
        lines.append(f"🧩 Партия: <b>{party_id}</b>")
        if avg_score > 0:
            lines.append(f"⭐ Рейтинг: <b>{_fmt_avg(avg_score)}</b>")
        lines.append(f"🏆 Место: <b>{rank_str}</b>")
        lines.append(f"🗳 Голосов: <b>{votes_count}</b>")
        lines.append("")

        if positive_percent > 0:
            lines.append(f"🎯 Положительных: <b>{positive_percent}%</b>")
        if votes_per_hour > 0:
            lines.append(f"📈 <b>{_fmt_num(votes_per_hour)}</b> оценки в час")
        if views_total > 0:
            lines.append(f"👁 Показов: <b>{views_total}</b>")
        if votes_today > 0:
            lines.append(f"🔥 За сегодня: <b>{votes_today}</b>")

        lines.append("")
        lines.append(_format_status_line(computed_status))
        lines.append(f"⏳ До архивирования: <b>{_esc_html(time_left)}</b>")

        if is_premium_user:
            credits = 0
            try:
                user_stats = await get_user_stats(int(user["id"]))
                credits = int(user_stats.get("credits") or 0)
            except Exception:
                credits = 0
            spent_today = 0.0
            try:
                spend_stats = await get_user_spend_today_stats(int(user["id"]))
                spent_today = float(spend_stats.get("credits_spent_today") or 0.0)
            except Exception:
                spent_today = 0.0
            predicted_views = int(max(credits, 0) * 2)

            premium_lines: list[str] = []
            if credits > 0:
                premium_lines.append(f"💳 Кредиты: <b>{credits}</b>")
            if spent_today > 0:
                premium_lines.append(f"⚡ Потрачено сегодня: <b>{_fmt_num(spent_today)}</b>")
            if predicted_views > 0:
                premium_lines.append(f"🔮 Прогноз показов: <b>{predicted_views}</b>")
            if premium_lines:
                lines.append("")
                lines.extend(premium_lines)

        if is_author_user:
            saves = int(photo.get("saves_count") or photo.get("saves") or 0)
            shares = int(photo.get("shares_count") or photo.get("shares") or 0)
            comments = int(snapshot.get("comments_count") or 0)
            link_clicks = int(
                photo.get("link_clicks_count")
                or photo.get("link_clicks")
                or snapshot.get("link_clicks")
                or 0
            )

            author_lines: list[str] = []
            if saves > 0:
                author_lines.append(f"📥 Сохранений: <b>{saves}</b>")
            if comments > 0:
                author_lines.append(f"💬 Комментариев: <b>{comments}</b>")
            if shares > 0:
                author_lines.append(f"🔁 Репостов: <b>{shares}</b>")
            if link_clicks > 0:
                author_lines.append(f"📎 Переходов по ссылке: <b>{link_clicks}</b>")
            if author_lines:
                lines.append("")
                lines.extend(author_lines)

    text = "\n".join(lines)
    kb = build_my_photo_stats_keyboard(photo_id)
    sent_id = await _edit_or_replace_text(callback, text, kb)
    if sent_id is not None:
        await remember_screen(callback.from_user.id, sent_id, state=state)
    await callback.answer()


@router.callback_query(F.data == "myphoto:add_intro:extra")
async def myphoto_add_intro_extra(callback: CallbackQuery, state: FSMContext):
    """Legacy callback: route to the standard upload intro."""
    user = await _ensure_user(callback)
    if user is None:
        return
    await _render_upload_intro_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data == "myphoto:idea:extra")
async def myphoto_generate_idea_extra(callback: CallbackQuery, state: FSMContext):
    """Legacy callback: route to default idea generation."""
    await myphoto_generate_idea(callback, state)


@router.callback_query(F.data.startswith("myphoto:ratings:"))
async def myphoto_toggle_ratings(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:ratings_toggle", 0.8):
        return
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
    if int(photo.get("user_id", 0)) != int(user.get("id", 0)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    new_state = await toggle_photo_ratings_enabled(photo_id, int(user["id"]))
    if new_state is None:
        await callback.answer("Не удалось переключить.", show_alert=True)
        return

    try:
        photo = await get_photo_by_id(photo_id) or photo
    except Exception:
        pass

    await _edit_or_replace_my_photo_message(callback, state, photo)
    await callback.answer("Оценки включены" if new_state else "Оценки выключены")


# ========= ДОБАВЛЕНИЕ ФОТО =========


# --- Upload wizard: Cancel/Back handlers ---


@router.callback_query(F.data == "myphoto:upload_cancel")
async def myphoto_upload_cancel(callback: CallbackQuery, state: FSMContext):
    """Cancel upload wizard and return to upload intro with ideas."""
    if await _tap_guard(callback, "myphoto:upload_cancel", 0.8):
        return
    user = await _ensure_user(callback)
    if user is None:
        return
    try:
        await state.clear()
    except Exception:
        pass

    await _render_upload_intro_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data == "myphoto:upload_back")
async def myphoto_upload_back(callback: CallbackQuery, state: FSMContext):
    """Go back inside upload wizard (from title step back to photo step)."""
    if await _tap_guard(callback, "myphoto:upload_back", 0.8):
        return
    cur_state = await state.get_state()

    # If we are on title step — go back to photo step
    if cur_state == MyPhotoStates.waiting_title.state:
        await state.set_state(MyPhotoStates.waiting_photo)
        await state.update_data(file_id=None, title=None)

        text = "Отправь фотографию (1 шт.), которую хочешь добавить."
        kb = build_upload_wizard_kb(back_to="menu")
        new_msg_id = int(callback.message.message_id)
        chat_id = int(callback.message.chat.id)

        if callback.message.photo:
            try:
                await callback.message.delete()
            except Exception:
                pass
            sent = await callback.message.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=kb,
                disable_notification=True,
            )
            new_msg_id = int(sent.message_id)
        else:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                try:
                    await callback.message.delete()
                except Exception:
                    pass
                sent = await callback.message.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=kb,
                    disable_notification=True,
                )
                new_msg_id = int(sent.message_id)

        await state.update_data(
            upload_msg_id=new_msg_id,
            upload_chat_id=chat_id,
            upload_is_photo=False,
        )

        await callback.answer()
        return

    # From any other wizard state (or no state) — just return to My Photo section
    await my_photo_menu(callback, state)


# ---- upload limits helpers ----

def _user_photo_limits(user: dict, stats: dict, *, is_unlimited: bool) -> tuple[int, int]:
    """
    Возвращает (max_active, daily_limit).
    GlowShot: 1 публикация в день для всех ролей.
    Активных одновременно — до 2 (сегодняшняя и вчерашняя работа до архива).
    Админ: технический bypass.
    """
    if is_unlimited:
        return 2, 10**9
    return 2, 1


# ========= ДОБАВЛЕНИЕ ФОТО =========


async def _clear_rules_state(state: FSMContext) -> None:
    await state.update_data(
        rules_screen_active=False,
        rules_screen_msg_id=None,
        rules_gate_id=None,
    )


async def _start_upload_wizard(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    user_id: int,
) -> None:
    await state.set_state(MyPhotoStates.waiting_photo)
    await _clear_rules_state(state)
    await state.update_data(
        upload_msg_id=callback.message.message_id,
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=bool(getattr(callback.message, "photo", None)),
        upload_user_id=user_id,
        file_id=None,
        title=None,
    )

    text = "Отправь фотографию (1 шт.), которую хочешь добавить."
    kb = build_upload_wizard_kb(back_to="menu")
    sent_msg_id = await _edit_or_replace_text(callback, text, kb, parse_mode="HTML")
    if sent_msg_id is None:
        sent_msg_id = int(callback.message.message_id)
    await state.update_data(
        upload_msg_id=sent_msg_id,
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=False,
    )
    await remember_screen(callback.from_user.id, int(sent_msg_id), state=state)


async def _show_upload_rules_screen(
    callback: CallbackQuery,
    state: FSMContext,
    user: dict,
) -> None:
    data = await state.get_data()
    current_msg_id = int(callback.message.message_id)
    if bool(data.get("rules_screen_active")) and int(data.get("rules_screen_msg_id") or 0) == current_msg_id:
        await callback.answer()
        return

    gate_id = f"{int(user['id'])}:{int(get_moscow_now().timestamp() * 1000)}"
    await state.update_data(
        rules_screen_active=True,
        rules_screen_msg_id=current_msg_id,
        rules_gate_id=gate_id,
    )

    text = _build_upload_rules_text(user)
    sent_msg_id = await _edit_or_replace_text(callback, text, build_upload_rules_wait_kb(), parse_mode="HTML")
    if sent_msg_id is None:
        sent_msg_id = current_msg_id
    await state.update_data(
        rules_screen_active=True,
        rules_screen_msg_id=int(sent_msg_id),
        rules_gate_id=gate_id,
        upload_msg_id=int(sent_msg_id),
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=False,
    )
    await remember_screen(callback.from_user.id, int(sent_msg_id), state=state)
    await callback.answer()

    await asyncio.sleep(UPLOAD_RULES_WAIT_SECONDS)

    data_after = await state.get_data()
    if not bool(data_after.get("rules_screen_active")):
        return
    if str(data_after.get("rules_gate_id") or "") != gate_id:
        return

    rules_msg_id = int(data_after.get("rules_screen_msg_id") or sent_msg_id)
    updated_msg_id, _ = await _edit_or_replace_progress_message(
        bot=callback.bot,
        chat_id=int(callback.message.chat.id),
        message_id=rules_msg_id,
        text=text,
        reply_markup=build_upload_rules_ack_kb(),
        prefer_caption=False,
        parse_mode="HTML",
    )
    if updated_msg_id is None:
        return
    await state.update_data(
        rules_screen_active=True,
        rules_screen_msg_id=int(updated_msg_id),
        rules_gate_id=gate_id,
        upload_msg_id=int(updated_msg_id),
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=False,
    )
    if int(updated_msg_id) != int(rules_msg_id):
        await remember_screen(callback.from_user.id, int(updated_msg_id), state=state)


@router.callback_query(F.data == "myphoto:rules")
async def myphoto_rules(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return
    await _show_upload_rules_screen(callback, state, user)


@router.callback_query(F.data == "myphoto:rules:wait")
async def myphoto_rules_wait(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "myphoto:rules:back")
async def myphoto_rules_back(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:rules:back", 0.8):
        return
    user = await _ensure_user(callback)
    if user is None:
        return
    await _clear_rules_state(state)
    await _render_upload_intro_screen(callback, state, user)
    await callback.answer()


@router.callback_query(F.data == "myphoto:rules:ack")
async def myphoto_rules_ack(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:rules:ack", 1.0):
        return
    user = await _ensure_user(callback)
    if user is None:
        return
    await set_upload_rules_ack_at(int(user["id"]))
    await _start_upload_wizard(callback, state, user_id=int(user["id"]))
    await callback.answer()


@router.callback_query(F.data == "myphoto:add")
@router.callback_query(F.data == "myphoto:add:extra")
async def myphoto_add(callback: CallbackQuery, state: FSMContext):
    """Старт мастера загрузки новой работы.

    Шаг 1 — загрузка фотографии.
    """

    if await _tap_guard(callback, "myphoto:add", 1.0):
        return
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

    is_unlimited = is_unlimited_upload_user(user)
    max_active, _daily_limit = _user_photo_limits(user, {}, is_unlimited=is_unlimited)
    active_count = len(active_photos)

    if not is_unlimited:
        can_upload, deny_reason = await check_can_upload_today(int(user_id))
        if not can_upload:
            limit, _current, remaining = await _idea_counters(user, is_premium_user)
            idea_title, idea_hint = _get_daily_idea()
            text = _build_upload_intro_text(
                user,
                idea_label="Идея дня",
                idea_title=idea_title,
                idea_hint=idea_hint,
                publish_notice=deny_reason or "Публикация сегодня недоступна. Завтра можно.",
            )
            await _edit_or_replace_text(
                callback,
                text,
                build_upload_intro_kb(remaining=remaining, limit=limit),
            )
            await callback.answer()
            return

    # Проверка лимита активных фото
    if active_count >= max_active:
        await _edit_or_replace_text(
            callback,
            "Лимит активных фото достигнут. Подожди, пока фото уйдут в архив.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=HOME, callback_data="menu:back")]
                ]
            ),
        )
        await callback.answer()
        return

    if await should_show_upload_rules(int(user_id)):
        await _show_upload_rules_screen(callback, state, user)
        return

    await _start_upload_wizard(callback, state, user_id=int(user_id))
    await callback.answer()


@router.callback_query(MyPhotoStates.waiting_category, F.data.startswith("myphoto:category:"))
async def myphoto_choose_category(callback: CallbackQuery, state: FSMContext):
    # Категории временно отключены — мастер загрузки теперь сразу ждёт фото.
    await callback.answer("Категории сейчас не используются. Просто отправь фотографию.")


@router.message(MyPhotoStates.waiting_photo, F.photo)
async def myphoto_got_photo(message: Message, state: FSMContext):
    await _accept_image_for_upload(message, state, source="photo")


@router.message(MyPhotoStates.waiting_photo, F.document)
async def myphoto_got_document(message: Message, state: FSMContext):
    # Принимаем документы с изображениями (jpeg/png/heic/tiff и т.д.) и сразу конвертируем в фото
    mime = (message.document.mime_type or "").lower() if message.document else ""
    filename = (message.document.file_name or "").lower() if message.document else ""
    if not (mime.startswith("image/") or filename.endswith((".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"))):
        await myphoto_waiting_photo_wrong(message, state)
        return
    await _accept_image_for_upload(message, state, source="document")


@router.message(MyPhotoStates.waiting_photo)
async def myphoto_waiting_photo_wrong(message: Message, state: FSMContext):
    data = await state.get_data()
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")

    # Если прислали как файл (document) — подскажем, что нужно фото
    if message.document and (message.document.mime_type or "").startswith("image/"):
        hint = "Отправь фотографию как <b>фото</b>, не как файл."
    else:
        hint = "Отправь фотографию, чтобы продолжить загрузку."

    try:
        await message.delete()
    except Exception:
        pass

    if upload_msg_id and upload_chat_id:
        try:
            await message.bot.edit_message_text(
                chat_id=upload_chat_id,
                message_id=upload_msg_id,
                text=hint,
                reply_markup=build_upload_wizard_kb(back_to="menu"),
                parse_mode="HTML",
            )
            return
        except Exception:
            pass
    # если не получилось отредактировать — просто игнорируем, чтобы не плодить сообщения


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


# Patch the delete handler to show correct post-delete UI

@router.callback_query(F.data.regexp(r"^myphoto:delete:(\d+)$"))
async def myphoto_delete(callback: CallbackQuery, state: FSMContext):
    """
    Показывает подтверждение удаления своей фотографии (с премиум-предупреждением).
    """
    if await _tap_guard(callback, "myphoto:delete", 0.9):
        return
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

    confirm_text = (
        "🗑 <b>Удалить фотографию?</b>\n\n"
        "Фотография будет удалена полностью и не будет участвовать в оценках. "
        "Сегодня у вас больше не будет возможности загрузить фотографию еще раз!\n\n"
        ""
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Удалить", callback_data=f"myphoto:delete_confirm:{photo_id}", style="danger"),
        InlineKeyboardButton(text="Отмена", callback_data=f"myphoto:delete_cancel:{photo_id}", style="success"),
    )

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
    if await _tap_guard(callback, "myphoto:delete_confirm", 1.1):
        return
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

    await mark_photo_deleted_by_user(photo_id, int(user["id"]))
    await _clear_photo_message_id(state)

    # Удаляем старое сообщение с фотографией, чтобы не мелькала старая картинка
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Обновляем раздел «Моя фотография» после удаления
    await my_photo_menu(callback, state)
    return

# --- Cancel delete handler ---
@router.callback_query(F.data.regexp(r"^myphoto:delete_cancel:(\d+)$"))
async def myphoto_delete_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Отмена удаления — восстановить карточку фото.
    """
    if await _tap_guard(callback, "myphoto:delete_cancel", 0.8):
        return
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
    await _edit_or_replace_my_photo_message(callback, state, photo)
    await callback.answer("Отменено")


# ====== MY PHOTO CALLBACK HANDLERS FOR COMMENTS/STATS/REPEAT/PROMOTE/EDIT ======

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

    await _edit_or_replace_my_photo_message(callback, state, photo)
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

    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"),
    )

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

# --- Repeat disabled ---
@router.callback_query(F.data.regexp(r"^myphoto:repeat:(\d+)$"))
async def myphoto_repeat(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Повтор временно отключён.", show_alert=True)


@router.callback_query(F.data.regexp(r"^myphoto:edit:(\d+)$"))
async def myphoto_edit(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:edit", 0.7):
        return
    user = await _ensure_user(callback)
    if user is None:
        return

    photo_id = int((callback.data or "").split(":")[2])
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена.", show_alert=True)
        return
    if int(photo.get("user_id", 0)) != int(user.get("id", 0)):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    # Remember which message we should update after text edits
    try:
        await state.update_data(
            edit_target_chat_id=callback.message.chat.id,
            edit_target_msg_id=callback.message.message_id,
            edit_target_is_photo=bool(callback.message.photo),
            edit_photo_desc_exists=bool(photo.get("description")),
        )
    except Exception:
        pass

    new_msg_id, is_photo_msg = await _render_myphoto_edit_menu(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        photo=photo,
        had_photo=bool(callback.message.photo),
    )

    await state.update_data(
        edit_target_chat_id=callback.message.chat.id,
        edit_target_msg_id=new_msg_id,
        edit_target_is_photo=is_photo_msg,
    )

    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:editmenu:(\d+)$"))
async def myphoto_editmenu(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:editmenu", 0.7):
        return
    try:
        await state.clear()
    except Exception:
        pass

    user = await _ensure_user(callback)
    if user is None:
        return

    photo_id = int((callback.data or "").split(":")[2])
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or int(photo.get("user_id", 0)) != int(user.get("id", 0)):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        await state.update_data(
            edit_target_chat_id=callback.message.chat.id,
            edit_target_msg_id=callback.message.message_id,
            edit_target_is_photo=bool(callback.message.photo),
        )
    except Exception:
        pass

    new_msg_id, is_photo_msg = await _render_myphoto_edit_menu(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        photo=photo,
        had_photo=bool(callback.message.photo),
    )
    await state.update_data(
        edit_target_chat_id=callback.message.chat.id,
        edit_target_msg_id=new_msg_id,
        edit_target_is_photo=is_photo_msg,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:edit:title:(\d+)$"))
async def myphoto_edit_title(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Название теперь нельзя менять.", show_alert=True)


@router.callback_query(F.data.regexp(r"^myphoto:edit:device:(\d+)$"))
async def myphoto_edit_device(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return
    photo_id = int((callback.data or "").split(":")[3])

    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(EditPhotoStates.waiting_device_type)
    await state.update_data(edit_photo_id=photo_id)

    text = "📷 <b>Устройство</b>\n\nВыбери тип устройства:"
    await callback.message.edit_caption(caption=text, reply_markup=build_device_type_kb(photo_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:device:set:(\d+):(phone|camera|other)$"))
async def myphoto_device_set(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:device:set", 0.8):
        return
    user = await _ensure_user(callback)
    if user is None:
        return

    parts = (callback.data or "").split(":")
    photo_id = int(parts[3])
    dev_type = parts[4]

    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    # Save immediately: only device_type, and clear device_info
    try:
        await update_photo_editable_fields(photo_id, int(user["id"]), device_type=dev_type, device_info="")
    except Exception:
        pass

    await callback.answer("Сохранено ✅")

    # Refresh edit menu in the same message
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted"):
        return

    new_msg_id, is_photo_msg = await _render_myphoto_edit_menu(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        photo=photo,
        had_photo=bool(callback.message.photo),
    )
    await state.update_data(
        edit_target_chat_id=callback.message.chat.id,
        edit_target_msg_id=new_msg_id,
        edit_target_is_photo=is_photo_msg,
    )


@router.callback_query(F.data.regexp(r"^myphoto:edit:desc:(\d+)$"))
async def myphoto_edit_desc(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Описание фотографий отключено.", show_alert=True)


@router.callback_query(F.data.regexp(r"^myphoto:edit:tag:(\d+)$"))
async def myphoto_edit_tag(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return
    photo_id = int((callback.data or "").split(":")[3])

    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    text = "🏷 <b>Тег</b>\n\nВыбери из списка:"
    await callback.message.edit_caption(caption=text, reply_markup=build_tag_kb(photo_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:tag:set:(\d+):(.*)$"))
async def myphoto_tag_set(callback: CallbackQuery, state: FSMContext):
    if await _tap_guard(callback, "myphoto:tag:set", 0.8):
        return
    user = await _ensure_user(callback)
    if user is None:
        return
    parts = (callback.data or "").split(":")
    photo_id = int(parts[3])
    tag_key = ":".join(parts[4:])  # может быть пустым

    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or int(photo.get("user_id", 0)) != int(user["id"]):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await update_photo_editable_fields(photo_id, int(user["id"]), tag=tag_key)

    await callback.answer("Сохранено ✅")

    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted"):
        return

    new_msg_id, is_photo_msg = await _render_myphoto_edit_menu(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        photo=photo,
        had_photo=bool(callback.message.photo),
    )
    await state.update_data(
        edit_target_chat_id=callback.message.chat.id,
        edit_target_msg_id=new_msg_id,
        edit_target_is_photo=is_photo_msg,
    )


@router.message(EditPhotoStates.waiting_title, F.text)
async def myphoto_edit_title_text(message: Message, state: FSMContext):
    await message.delete()
    await state.clear()




@router.message(EditPhotoStates.waiting_description, F.text)
async def myphoto_edit_desc_text(message: Message, state: FSMContext):
    await message.delete()
    try:
        await message.answer("Описание фотографий отключено.")
    except Exception:
        pass
    await state.clear()


@router.callback_query(F.data.regexp(r"^myphoto:edit:desc_clear:(\d+)$"))
async def myphoto_edit_desc_clear(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Описание фотографий отключено.", show_alert=True)
    await state.clear()




# ====== FINALIZE PHOTO CREATION ======

async def _finalize_photo_creation(event: Message | CallbackQuery, state: FSMContext) -> None:
    """
    Завершить процесс создания фото: сохранить в БД и показать карточку пользователю.
    event: Message или CallbackQuery
    """
    if isinstance(event, CallbackQuery):
        bot = event.message.bot
        fallback_chat_id = int(event.message.chat.id)
    else:
        bot = event.bot
        fallback_chat_id = int(event.chat.id)

    data = await state.get_data()
    user_id = data.get("upload_user_id")
    file_id = data.get("file_id")
    title = data.get("title")
    upload_msg_id = data.get("upload_msg_id")
    upload_chat_id = data.get("upload_chat_id")

    sent_msg_id: int | None = int(upload_msg_id) if upload_msg_id else None
    chat_id = int(upload_chat_id or fallback_chat_id)
    progress_is_photo = bool(data.get("upload_is_photo"))

    if not user_id or not file_id or not title:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
            text="❌ Сессия загрузки сбилась. Открой «Моя фотография» и попробуй загрузить заново.",
        )
        return

    sent_msg_id, progress_is_photo = await _set_upload_progress(
        state=state,
        bot=bot,
        chat_id=chat_id,
        message_id=sent_msg_id,
        text="🖊 Название принято",
        is_photo_message=progress_is_photo,
    )
    if sent_msg_id is None:
        return

    tg_id = int(getattr(event.from_user, "id", 0) or 0)
    actor = await get_user_by_tg_id(tg_id) if tg_id else None
    is_unlimited_actor = bool(actor and is_unlimited_upload_user(actor))

    if not is_unlimited_actor:
        can_upload_now, denied_reason = await check_can_upload_today(int(user_id))
        if not can_upload_now:
            await _show_upload_processing_error(
                state=state,
                bot=bot,
                chat_id=chat_id,
                message_id=sent_msg_id,
                is_photo_message=progress_is_photo,
                text=denied_reason or "❌ Сегодня новая публикация недоступна. Завтра можно.",
            )
            return

    try:
        author_code = await ensure_user_author_code(int(tg_id))
    except Exception:
        author_code = "GS-UNKNOWN"

    is_author_user = bool(actor and actor.get("is_author"))
    try:
        is_premium_user = await is_user_premium_active(int(tg_id)) if tg_id else False
    except Exception:
        is_premium_user = bool(actor and actor.get("is_premium"))

    author_name = ((actor or {}).get("name") or "").strip() or author_code
    wm_highlight: str | None = None
    if is_author_user:
        year_text = str(get_moscow_now().year)
        watermark_text = f"Ⓒ {year_text} {author_name}. ALL RIGHTS RESERVED"
        wm_highlight = year_text
    elif is_premium_user:
        watermark_text = f"Ⓒ {author_code} · GlowShot™"
    else:
        watermark_text = f"GlowShot™ · {author_code}"

    sent_msg_id, progress_is_photo = await _set_upload_progress(
        state=state,
        bot=bot,
        chat_id=chat_id,
        message_id=sent_msg_id,
        text="🎨 Рисуем водяной знак…",
        is_photo_message=progress_is_photo,
    )
    if sent_msg_id is None:
        return

    try:
        original_bytes = await _download_telegram_photo_bytes(bot, str(file_id))
    except Exception:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
        )
        return

    watermarked_bytes = apply_text_watermark(
        original_bytes,
        watermark_text,
        highlight_text=wm_highlight,
        max_side=4096,
    )
    if not watermarked_bytes:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
        )
        return

    sent_msg_id, file_id_public, progress_is_photo = await _replace_or_send_progress_photo(
        bot=bot,
        chat_id=chat_id,
        message_id=sent_msg_id,
        image_bytes=watermarked_bytes,
        caption="☁️ Загружаем фото…",
    )
    if sent_msg_id is None or not file_id_public:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
        )
        return
    await state.update_data(
        upload_msg_id=int(sent_msg_id),
        upload_chat_id=int(chat_id),
        upload_is_photo=True,
        upload_progress_msg_id=int(sent_msg_id),
        upload_progress_is_photo=True,
    )

    sent_msg_id, progress_is_photo = await _set_upload_progress(
        state=state,
        bot=bot,
        chat_id=chat_id,
        message_id=sent_msg_id,
        text="📦 Сохраняем и добавляем в ленту…",
        is_photo_message=True,
    )
    if sent_msg_id is None:
        return

    try:
        photo_id = await create_today_photo(
            user_id=user_id,
            file_id=file_id_public,
            file_id_public=file_id_public,
            file_id_original=file_id,
            title=title,
        )
        try:
            await add_credits(int(user_id), 2)
        except Exception:
            pass
        try:
            if tg_id:
                await streak_record_action_by_tg_id(tg_id, "upload")
        except Exception:
            pass
    except UniqueViolationError:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
            text="❌ Ты уже загружал(а) фотографию сегодня. Новая публикация будет доступна завтра.",
        )
        return
    except Exception:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
        )
        return

    photo = await get_photo_by_id(photo_id)
    if not photo:
        await _show_upload_processing_error(
            state=state,
            bot=bot,
            chat_id=chat_id,
            message_id=sent_msg_id,
            is_photo_message=progress_is_photo,
            text="❌ Ошибка при сохранении фотографии. Попробуй ещё раз.",
        )
        return

    sent_msg_id, progress_is_photo = await _set_upload_progress(
        state=state,
        bot=bot,
        chat_id=chat_id,
        message_id=sent_msg_id,
        text="🏁 Готово! Фото участвует в оценивании",
        is_photo_message=True,
    )

    caption = await build_my_photo_main_text(photo)
    try:
        active_photos_after = await get_active_photos_for_user(int(user_id), limit=2)
        active_photos_after = sorted(active_photos_after, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
    except Exception:
        active_photos_after = [photo]
    photo_ids_after = [p["id"] for p in active_photos_after]

    lang = "ru"
    if actor:
        lang = (actor.get("lang") or "ru").split("-")[0]

    kb = build_my_photo_keyboard(photo["id"], lang=lang)

    final_msg_id: int | None = None
    if sent_msg_id:
        final_msg_id = await _edit_or_replace_caption_with_photo(
            bot=bot,
            chat_id=chat_id,
            message_id=int(sent_msg_id),
            file_id=_photo_public_id(photo),
            caption=caption,
            reply_markup=kb,
        )
    else:
        sent_photo = await bot.send_photo(
            chat_id=chat_id,
            photo=_photo_public_id(photo),
            caption=caption,
            reply_markup=kb,
            disable_notification=True,
        )
        final_msg_id = int(sent_photo.message_id)

    await _store_photo_message_id(state, int(final_msg_id), photo_id=photo["id"])

    await state.clear()
    await state.update_data(
        myphoto_ids=photo_ids_after,
        myphoto_last_id=photo["id"],
        myphoto_photo_msg_id=final_msg_id,
    )
    try:
        if final_msg_id and tg_id:
            await remember_screen(int(tg_id), int(final_msg_id), state=state)
    except Exception:
        pass
