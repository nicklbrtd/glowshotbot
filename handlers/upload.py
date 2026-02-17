import io
import random
from PIL import Image  # type: ignore
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from datetime import datetime, timedelta
from asyncpg.exceptions import UniqueViolationError

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.i18n import t
from utils.banner import ensure_giraffe_banner
from utils.registration_guard import require_user_name
from utils.antispam import should_throttle
from keyboards.common import HOME

from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler

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
)

from database_results import (
    PERIOD_DAY,
    SCOPE_GLOBAL,
    KIND_TOP_PHOTOS,
    get_results_items,
)

from utils.time import get_moscow_now
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
    file_id_to_save = None
    converted = False
    if source == "photo":
        file_id_to_save = message.photo[-1].file_id
    else:
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
                converted = True
            except Exception:
                # если не смогли конвертировать — используем как есть, может пройти
                photo_bytes = raw_bytes
        except Exception:
            photo_bytes = None

    # Если пришёл документ — пересылаем как фото для единообразия
    sent_photo = None
    try:
        if photo_bytes is not None:
            sent_photo = await message.bot.send_photo(
                chat_id=upload_chat_id,
                photo=BufferedInputFile(photo_bytes, filename="upload.jpg"),
                caption="Фотография получена ✅\n\nТеперь напиши название этой работы.\n<b>Поменять название после загрузки нельзя.</b>\n\n",
                reply_markup=build_upload_wizard_kb(back_to="photo"),
                disable_notification=True,
            )
            file_id_to_save = sent_photo.photo[-1].file_id if sent_photo and sent_photo.photo else file_id_to_save
        else:
            # Заменяем старое служебное сообщение на новое с фотографией
            try:
                await message.bot.delete_message(chat_id=upload_chat_id, message_id=upload_msg_id)
            except Exception:
                pass
            sent_photo = await message.bot.send_photo(
                chat_id=upload_chat_id,
                photo=file_id_to_save,
                caption="Фотография получена ✅\n\nТеперь напиши название этой работы.\n<b>Поменять название после загрузки нельзя.</b>\n\n",
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
        file_id=file_id_to_save,
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
    second: bool = False,
    publish_notice: str | None = None,
) -> str:
    ready = _ready_wording(user)
    selfie = _selfie_wording(user)
    title_line = "📸 <b>Загрузить фотографию!</b>"
    if second:
        title_line = "📸 <b>Загрузить вторую фотографию!</b>"
    lines: list[str] = [
        title_line,
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
    lines.append(
        ("Это загрузка второй активной фотографии.\n\n" if second else "") + f"Когда будешь {ready} — жми «Загрузить».",
    )
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


async def _render_upload_intro_screen(
    callback: CallbackQuery,
    state: FSMContext,
    user: dict,
    *,
    second: bool = False,
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
        second=second,
        publish_notice=(
            denied_reason or "Публикация сегодня недоступна. Завтра можно."
            if not can_upload_today
            else None
        ),
    )
    kb = build_upload_intro_kb(
        remaining=remaining,
        limit=limit,
        idea_cb="myphoto:idea:extra" if second else "myphoto:idea",
        upload_cb="myphoto:add:extra" if second else "myphoto:add",
    )
    sent_id = await _edit_or_replace_text(callback, text, kb)
    if sent_id is not None:
        await remember_screen(callback.from_user.id, sent_id, state=state)


def _photo_ratings_enabled(photo: dict) -> bool:
    return bool(photo.get("ratings_enabled", True))


async def _is_photo_locked_for_user(photo_id: int, state: FSMContext) -> bool:
    data = await state.get_data()
    locked_ids = set(data.get("myphoto_locked_ids") or [])
    return photo_id in locked_ids


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


def build_my_photo_caption(photo: dict, *, locked: bool = False) -> str:
    """Собрать подпись к фотографии в разделе «Моя фотография».

    Здесь нет статистики — только базовая информация о работе.
    Остальные тексты (статистика, комментарии) формируются в отдельных хендлерах.
    """

    # Информация об устройстве
    device_type_raw = (photo.get("device_type") or "").lower()

    # Подбираем эмодзи под тип устройства
    if "смартфон" in device_type_raw or "phone" in device_type_raw:
        device_emoji = "📱"
    elif "фотокамера" in device_type_raw or "camera" in device_type_raw:
        device_emoji = "📷"
    else:
        device_emoji = "📸"

    title = photo.get("title") or "Без названия"

    # Формируем хвост с устройством для заголовка (модель не используем)
    if device_type_raw:
        device_suffix = f" ({device_emoji})"
    else:
        device_suffix = ""

    title_line = f"\"{title}\"{device_suffix}"

    description = photo.get("description")

    caption_lines: list[str] = [
        f"<b>{title_line}</b>",
    ]

    if locked:
        caption_lines.append("💎 Вторая фотография доступна с GlowShot Premium.")

    if description:
        caption_lines.append("")
        caption_lines.append(f"📝 {description}")

    return "\n".join(caption_lines)


def build_my_photo_keyboard(
    photo_id: int,
    *,
    ratings_enabled: bool | None = None,
    can_add_more: bool = False,
    is_premium_user: bool = False,
    nav_prev: bool = False,
    nav_next: bool = False,
    locked: bool = False,
    show_premium_cta: bool = False,
    premium_back_cb: str | None = None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if not locked or is_premium_user:
        rows.append([
            InlineKeyboardButton(text=t("myphoto.btn.share", lang), callback_data=f"myphoto:share:{photo_id}")
        ])

    if not locked:
        rows.append([
            InlineKeyboardButton(text=t("myphoto.btn.edit", lang), callback_data=f"myphoto:edit:{photo_id}"),
            InlineKeyboardButton(text=t("myphoto.btn.stats", lang), callback_data=f"myphoto:stats:{photo_id}"),
        ])

    nav_row: list[InlineKeyboardButton] = []
    if can_add_more:
        nav_row.append(InlineKeyboardButton(text=t("myphoto.btn.add", lang), callback_data="myphoto:add_intro:extra"))
    elif nav_prev or nav_next:
        if nav_prev:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data="myphoto:nav:prev"))
        if nav_next:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data="myphoto:nav:next"))
    if nav_row:
        rows.append(nav_row)

    if locked and show_premium_cta:
        back_cb = premium_back_cb or "menu:back"
        rows.append([InlineKeyboardButton(text=t("myphoto.btn.premium", lang), callback_data=f"premium:open:{back_cb}")])

    rows.append(
        [
            InlineKeyboardButton(text=HOME, callback_data="menu:back"),
            InlineKeyboardButton(text=t("myphoto.btn.delete", lang), callback_data=f"myphoto:delete:{photo_id}"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


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

def build_edit_cancel_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✏️ К редактированию", callback_data=f"myphoto:editmenu:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()


def build_edit_desc_kb(photo_id: int, has_description: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_description:
        kb.row(InlineKeyboardButton(text="🗑 Удалить описание", callback_data=f"myphoto:edit:desc_clear:{photo_id}"))
    kb.row(
        InlineKeyboardButton(text="✏️ К редактированию", callback_data=f"myphoto:editmenu:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()

def build_device_type_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📱 Смартфон", callback_data=f"myphoto:device:set:{photo_id}:phone"),
        InlineKeyboardButton(text="📸 Камера", callback_data=f"myphoto:device:set:{photo_id}:camera"),
    )
    kb.row(
        InlineKeyboardButton(text="✏️ К редактированию", callback_data=f"myphoto:editmenu:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()

def build_tag_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tag_key, label in EDIT_TAGS:
        kb.row(InlineKeyboardButton(text=label, callback_data=f"myphoto:tag:set:{photo_id}:{tag_key}"))
    kb.row(
        InlineKeyboardButton(text="✏️ К редактированию", callback_data=f"myphoto:editmenu:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
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


def _compute_photo_status(*, rank: int | None, votes_count: int, avg_score: float) -> str:
    if rank is not None and rank <= 10:
        return "🔥 В зоне топа"
    if rank is not None and rank <= 15:
        return "📌 Близко к топу"
    if votes_count < 10:
        return "🌱 Набирает оценки"
    if avg_score < 6:
        return "📉 Нужны оценки"
    return "✅ Стабильная позиция"

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


# Backward-compat alias for old typo name.
buxild_upload_wizard_kb = build_upload_wizard_kb


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


async def build_my_photo_main_text(photo: dict, *, locked: bool = False) -> str:
    ratings_enabled = _photo_ratings_enabled(photo) and (not locked)

    device_type_raw = str(photo.get("device_type") or "")
    emoji = _device_emoji(device_type_raw)

    title = (photo.get("title") or "Без названия").strip()
    title_safe = _esc_html(title)

    tag_key = str(photo.get("tag") or "")
    tag_text = _tag_label(tag_key)

    # дата публикации (day_key)
    day_key = (photo.get("day_key") or "").strip()
    pub_str = "—"
    if day_key:
        try:
            pub_dt = datetime.fromisoformat(day_key)
            pub_str = pub_dt.strftime("%d.%m.%Y")
        except Exception:
            pub_str = day_key

    stats = await get_photo_stats(photo["id"])
    ratings_count = int(stats.get("ratings_count") or 0)
    score = stats.get("bayes_score")

    score_str = "—"
    if score is not None:
        try:
            f = float(score)
            score_str = f"{f:.2f}".rstrip("0").rstrip(".")
        except Exception:
            score_str = "—"

    if emoji:
        header = f"<code>\"{title_safe}\"</code> ({emoji})"
    else:
        header = f"<code>\"{title_safe}\"</code> (устройство не указано)"

    lines: list[str] = []
    lines.append(f"<b>{header}</b>")
    lines.append(f"🏷️ Тег: <b>{_esc_html(tag_text)}</b>")
    lines.append("")
    lines.append(f"📅 Опубликовано: {pub_str}")
    def _strike(text: str) -> str:
        return f"<s>{text}</s>" if not ratings_enabled else text

    lines.append(_strike(f"💖 Оценок: {ratings_count}"))
    lines.append(_strike(f"📊 Рейтинг: <b>{score_str}</b>"))
    if not ratings_enabled:
        lines.append("🚫 Оценки для этой фотографии выключены.")
        if locked:
            lines.append("💎 Вторая активная фотография доступна только с Premium.")

    return "\n".join(lines)


async def _show_my_photo_section(
    *,
    chat_id: int,
    service_message: Message,
    state: FSMContext,
    photo: dict,
    nav_prev: bool = False,
    nav_next: bool = False,
    can_add_more: bool = False,
    is_premium_user: bool = False,
    locked: bool = False,
    user: dict | None = None,
) -> None:
    """Показ раздела «Моя фотография» одним сообщением с фото, подписью и кнопками.

    Логика:
    1) Пытаемся удалить старое служебное сообщение (меню / шаг мастера).
    2) Отправляем НОВОЕ сообщение с фотографией, caption и inline‑клавиатурой.
    3) Сохраняем id этого сообщения в FSM, чтобы потом можно было его удалить при выходе в меню.
    """

    caption = await build_my_photo_main_text(photo, locked=locked)
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(
        photo["id"],
        ratings_enabled=_photo_ratings_enabled(photo),
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        nav_prev=nav_prev,
        nav_next=nav_next,
        locked=locked,
        lang=lang,
    )

    # 1. Отправляем новое сообщение с фото, подписью и кнопками
    sent_photo = await service_message.bot.send_photo(
        chat_id=chat_id,
        photo=_photo_public_id(photo),
        caption=caption,
        reply_markup=kb,
        disable_notification=True,
    )

    # 2. Сохраняем id сообщения с фотографией и id самой фотографии в FSM
    await _store_photo_message_id(state, sent_photo.message_id, photo_id=photo["id"])

    # 3. После успешной отправки удаляем старое служебное сообщение (меню/шаг мастера)
    try:
        await service_message.delete()
    except Exception:
        # Если удаление не удалось (например, сообщение уже удалено) — просто игнорируем
        pass


async def _edit_or_replace_my_photo_message(
    callback: CallbackQuery,
    state: FSMContext,
    photo: dict,
    *,
    nav_prev: bool | None = None,
    nav_next: bool | None = None,
    can_add_more: bool | None = None,
    is_premium_user: bool | None = None,
    locked: bool | None = None,
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
                    # оставляем остальные поля как есть, чтобы не затирать is_premium/locked
                )
            except Exception:
                ids = []

    current_idx = 0
    if photo.get("id") in ids:
        current_idx = ids.index(photo["id"])
    if nav_prev is None:
        nav_prev = current_idx > 0
    if nav_next is None:
        nav_next = current_idx < len(ids) - 1
    if can_add_more is None:
        can_upload_today = True
        if user and not is_unlimited_upload_user(user):
            can_upload_today, _ = await check_can_upload_today(int(user["id"]))
        can_add_more = len(ids) < 2 and can_upload_today
    if is_premium_user is None:
        is_premium_user = bool(data.get("myphoto_is_premium"))
    if locked is None:
        locked_ids = set(data.get("myphoto_locked_ids") or [])
        locked = photo.get("id") in locked_ids

    caption = await build_my_photo_main_text(photo, locked=bool(locked))
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(
        photo["id"],
        ratings_enabled=_photo_ratings_enabled(photo),
        can_add_more=bool(can_add_more),
        is_premium_user=bool(is_premium_user),
        nav_prev=bool(nav_prev),
        nav_next=bool(nav_next),
        locked=bool(locked),
        show_premium_cta=bool(locked and not is_premium_user and len(ids) > 1),
        premium_back_cb=f"myphoto:open",
        lang=lang,
    )

    # 1) Пробуем edit_media (идеально для перелистывания 2 фото)
    try:
        if msg.photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=_photo_public_id(photo), caption=caption),
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
        disable_notification=True,
    )
    return sent.message_id


# ========= ВХОД В РАЗДЕЛ "МОЯ ФОТОГРАФИЯ" =========


@router.callback_query(F.data == "myphoto:open")
async def my_photo_menu(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "myphoto:open", 1.0):
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
    try:
        await ensure_giraffe_banner(
            callback.message.bot,
            callback.message.chat.id,
            callback.from_user.id,
            force_new=False,
        )
    except Exception:
        pass
    data = await state.get_data()
    menu_msg_id = data.get("menu_msg_id")
    opened_from_menu = menu_msg_id and callback.message and callback.message.message_id == menu_msg_id

    is_admin = is_admin_user(user)
    is_unlimited = is_unlimited_upload_user(user)
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
        photos = sorted(photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
    except Exception:
        pass

    # Ограничиваем набор до 2 фото (премиум максимум две активные)
    photos = photos[:2]

    photo: dict | None = None
    current_idx = 0
    if photos:
        data = await state.get_data()
        last_pid = data.get("myphoto_last_id")
        photo_ids = [p["id"] for p in photos]
        if last_pid and last_pid in photo_ids:
            current_idx = photo_ids.index(last_pid)
            photo = photos[current_idx]
        else:
            photo = photos[0]
    locked_ids: list[int] = []

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
        can_upload_today = True
        denied_reason = None
        if not is_unlimited:
            can_upload_today, denied_reason = await check_can_upload_today(int(user_id))
        limit, current, remaining = await _idea_counters(user, is_premium_user)
        kb = build_upload_intro_kb(remaining=remaining, limit=limit)

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
        await state.update_data(
            myphoto_ids=[],
            myphoto_current_idx=0,
            myphoto_last_id=None,
            myphoto_is_premium=is_premium_user,
            myphoto_locked_ids=[],
        )

        sent_id = None
        if opened_from_menu:
            sent = await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb,
                disable_notification=True,
            )
            sent_id = sent.message_id
        else:
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=text, reply_markup=kb)
                else:
                    await callback.message.edit_text(text, reply_markup=kb)
                sent_id = callback.message.message_id
            except Exception:
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
                sent_id = sent.message_id

        if sent_id is not None:
            await remember_screen(callback.from_user.id, sent_id, state=state)

        await callback.answer()
        return

    if photo.get("is_deleted"):
        limit, current, remaining = await _idea_counters(user, is_premium_user)
        can_upload_today = True
        denied_reason = None
        if not is_unlimited:
            can_upload_today, denied_reason = await check_can_upload_today(int(user_id))
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
        sent_id = await _edit_or_replace_text(
            callback,
            text,
            build_upload_intro_kb(remaining=remaining, limit=limit),
        )
        if sent_id is not None:
            await remember_screen(callback.from_user.id, sent_id, state=state)
        await callback.answer()
        return

    # Сохраняем список фото и текущий индекс в state для навигации
    photo_ids = [p["id"] for p in photos]
    await state.update_data(
        myphoto_ids=photo_ids,
        myphoto_current_idx=current_idx,
        myphoto_last_id=photo["id"],
        myphoto_is_premium=is_premium_user,
        myphoto_locked_ids=locked_ids,
    )

    nav_prev = current_idx > 0
    nav_next = current_idx < len(photo_ids) - 1
    can_upload_today = True
    if not is_unlimited:
        can_upload_today, _ = await check_can_upload_today(int(user_id))
    can_add_more = len(photo_ids) < 2 and can_upload_today
    locked = False

    await _show_my_photo_section(
        chat_id=callback.message.chat.id,
        service_message=callback.message,
        state=state,
        photo=photo,
        nav_prev=nav_prev,
        nav_next=nav_next,
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        locked=locked,
        user=user,
    )

    await callback.answer()


@router.callback_query(F.data == "myphoto:idea")
async def myphoto_generate_idea(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_user(callback)
    if user is None:
        return

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    limit_per_week, current_used, remaining_before = await _idea_counters(user, is_premium_user)
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
    """
    Навигация по своим фотографиям: вперёд / назад.
    Работает на основе списка активных работ пользователя.
    """
    user = await _ensure_user(callback)
    if user is None:
        return

    direction = (callback.data or "").split(":")[-1]
    user_id = int(user["id"])
    is_unlimited = is_unlimited_upload_user(user)

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    photos = await get_active_photos_for_user(user_id, limit=2)
    try:
        photos = sorted(photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
    except Exception:
        pass
    photos = photos[:2]

    if not photos:
        await my_photo_menu(callback, state)
        return

    photo_ids = [p["id"] for p in photos]
    data = await state.get_data()
    current_idx = int(data.get("myphoto_current_idx") or 0)
    last_pid = data.get("myphoto_last_id")
    if last_pid in photo_ids:
        current_idx = photo_ids.index(last_pid)
    current_idx = max(0, min(current_idx, len(photo_ids) - 1))

    if direction == "next" and current_idx < len(photo_ids) - 1:
        current_idx += 1
    elif direction == "prev" and current_idx > 0:
        current_idx -= 1

    photo = photos[current_idx]
    nav_prev = current_idx > 0
    nav_next = current_idx < len(photo_ids) - 1
    can_upload_today = True
    if not is_unlimited:
        can_upload_today, _ = await check_can_upload_today(int(user_id))
    can_add_more = len(photo_ids) < 2 and can_upload_today
    locked = False

    await state.update_data(
        myphoto_ids=photo_ids,
        myphoto_current_idx=current_idx,
        myphoto_last_id=photo["id"],
        myphoto_is_premium=is_premium_user,
        myphoto_locked_ids=[],
    )

    await _edit_or_replace_my_photo_message(
        callback,
        state,
        photo,
        nav_prev=nav_prev,
        nav_next=nav_next,
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        locked=False,
    )
    await callback.answer()


_MY_ARCHIVE_PAGE_SIZE = 5


def _build_my_archive_kb(page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    nav_row: list[InlineKeyboardButton] = []
    if has_prev:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"myphoto:archive:{page-1}"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"myphoto:archive:{page+1}"))
    if nav_row:
        kb.row(*nav_row)
    kb.row(
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="profile:open"),
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

    offset = page * _MY_ARCHIVE_PAGE_SIZE
    rows = await get_archived_photos_for_user(
        int(user["id"]),
        limit=_MY_ARCHIVE_PAGE_SIZE + 1,
        offset=offset,
    )
    has_next = len(rows) > _MY_ARCHIVE_PAGE_SIZE
    items = rows[:_MY_ARCHIVE_PAGE_SIZE]
    has_prev = page > 0

    lines: list[str] = ["📚 <b>Мой архив</b>", ""]
    if not items:
        lines.append("Архив пока пуст.")
    else:
        for idx, photo in enumerate(items, start=1 + offset):
            title = _esc_html(str(photo.get("title") or "Без названия"))
            submit_day = str(photo.get("submit_day") or photo.get("day_key") or "—")
            try:
                avg = f"{float(photo.get('avg_score') or 0):.2f}".rstrip("0").rstrip(".")
            except Exception:
                avg = "0"
            votes = int(photo.get("votes_count") or 0)
            lines.append(f"{idx}. <b>{title}</b>")
            lines.append(f"   📅 {submit_day} · 📊 {avg} · 💖 {votes}")
        lines.append("")
        lines.append(f"Страница {page + 1}")

    kb = _build_my_archive_kb(page=page, has_prev=has_prev, has_next=has_next)
    sent_id = await _edit_or_replace_text(callback, "\n".join(lines), kb)
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

    data = await state.get_data()
    locked_ids = set(data.get("myphoto_locked_ids") or [])
    if photo_id in locked_ids:
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
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

    lines: list[str] = []
    if status_raw == "archived":
        lines.append("📊 <b>Итоги фото</b>")
        lines.append("")
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
        lines.append(f"📌 Статус: <b>{_esc_html(computed_status)}</b>")
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
    """Показывает экран правил/идей для загрузки второй фотографии."""
    user = await _ensure_user(callback)
    if user is None:
        return

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    limit, current, remaining = await _idea_counters(user, is_premium_user)
    idea_title, idea_hint = _get_daily_idea()
    text = _build_upload_intro_text(
        user,
        idea_label="Идея дня",
        idea_title=idea_title,
        idea_hint=idea_hint,
        second=True,
    )
    kb = build_upload_intro_kb(
        remaining=remaining,
        limit=limit,
        idea_cb="myphoto:idea:extra",
        upload_cb="myphoto:add:extra",
    )

    sent = await callback.message.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=kb,
        disable_notification=True,
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "myphoto:idea:extra")
async def myphoto_generate_idea_extra(callback: CallbackQuery, state: FSMContext):
    """Сгенерировать идею для второй фотографии (те же лимиты, другая навигация)."""
    user = await _ensure_user(callback)
    if user is None:
        return

    is_premium_user = False
    try:
        if user.get("tg_id"):
            is_premium_user = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium_user = False

    limit_per_week, current_used, remaining_before = await _idea_counters(user, is_premium_user)
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
        second=True,
    )
    remaining_after = max(limit_per_week - new_count, 0)
    kb = build_upload_intro_kb(
        remaining=remaining_after,
        limit=limit_per_week,
        idea_cb="myphoto:idea:extra",
        upload_cb="myphoto:add:extra",
    )

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

    await callback.answer()


@router.callback_query(F.data.startswith("myphoto:ratings:"))
async def myphoto_toggle_ratings(callback: CallbackQuery, state: FSMContext):
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
    data = await state.get_data()
    locked_ids = set(data.get("myphoto_locked_ids") or [])
    if photo_id in locked_ids:
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
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
    user = await _ensure_user(callback)
    if user is None:
        return
    data = await state.get_data()
    is_extra = bool(data.get("upload_is_extra"))
    try:
        await state.clear()
    except Exception:
        pass

    await _render_upload_intro_screen(callback, state, user, second=is_extra)
    await callback.answer()


@router.callback_query(F.data == "myphoto:upload_back")
async def myphoto_upload_back(callback: CallbackQuery, state: FSMContext):
    """Go back inside upload wizard (from title step back to photo step)."""
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
    GlowShot 2.1: 1 публикация в день для всех ролей.
    Активных одновременно — до 2 (день загрузки + следующий день).
    Админ: технический bypass.
    """
    if is_unlimited:
        return 2, 10**9
    return 2, 1


async def _can_user_upload_now(user: dict, is_premium_user: bool, is_unlimited: bool) -> tuple[bool, str | None]:
    stats = {}
    try:
        stats = await get_user_stats(int(user["id"]))
    except Exception:
        stats = {}
    max_active, _daily_limit = _user_photo_limits(user, stats, is_unlimited=is_unlimited)
    user_id = int(user["id"])

    if not is_unlimited:
        can_upload, reason = await check_can_upload_today(user_id)
        if not can_upload:
            return False, reason or "Сегодня новая публикация недоступна."

    active_count = 0
    try:
        active_count = len(await get_active_photos_for_user(user_id))
    except Exception:
        active_count = 0

    if active_count >= max_active:
        # Нужен ручной делит перед новой загрузкой
        if is_premium_user and max_active > 1:
            return False, "Удалите одну из текущих фотографий, чтобы загрузить новую."
        return False, "Сначала удалите текущую фотографию."

    return True, None


# ========= ДОБАВЛЕНИЕ ФОТО =========


@router.callback_query(F.data.regexp(r"^myphoto:add(?::extra)?$"))
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

    await state.set_state(MyPhotoStates.waiting_photo)
    is_extra = (callback.data or "") == "myphoto:add:extra"
    await state.update_data(
        upload_msg_id=callback.message.message_id,
        upload_chat_id=callback.message.chat.id,
        upload_is_photo=bool(getattr(callback.message, "photo", None)),
        upload_user_id=user_id,
        file_id=None,
        title=None,
        upload_is_extra=is_extra,
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
        InlineKeyboardButton(text="Удалить", callback_data=f"myphoto:delete_confirm:{photo_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"myphoto:delete_cancel:{photo_id}"),
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

    await callback.answer("Фотография удалена.")
    # Обновляем раздел «Моя фотография» после удаления
    await my_photo_menu(callback, state)
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
    # show_premium_cta: если фото залочено и у пользователя нет премиума, но есть 2 фото
    data = await state.get_data()
    ids = data.get("myphoto_ids") or []
    is_premium_user = await is_user_premium_active(user["tg_id"])
    current_idx = ids.index(photo["id"]) if photo["id"] in ids else 0
    nav_prev = current_idx > 0
    nav_next = current_idx < len(ids) - 1
    can_upload_today = True
    if not is_unlimited_upload_user(user):
        can_upload_today, _ = await check_can_upload_today(int(user["id"]))
    can_add_more = len(ids) < 2 and can_upload_today
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(
        photo["id"],
        ratings_enabled=_photo_ratings_enabled(photo),
        can_add_more=can_add_more,
        nav_prev=nav_prev,
        nav_next=nav_next,
        show_premium_cta=bool(photo.get("locked") or (len(ids) > 1 and not is_premium_user)),
        lang=lang,
    )
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
    kb.row(
        InlineKeyboardButton(text="📸 К фото", callback_data=f"myphoto:back:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
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

    kb.row(
        InlineKeyboardButton(text="📸 К фото", callback_data=f"myphoto:back:{photo_id}"),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
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

    data = await state.get_data()
    ids: list[int] = data.get("myphoto_ids") or []
    try:
        current_idx = ids.index(photo_id) if photo_id in ids else 0
    except Exception:
        current_idx = 0
    nav_prev = current_idx > 0
    nav_next = current_idx < len(ids) - 1
    can_upload_today = True
    if not is_unlimited_upload_user(user):
        can_upload_today, _ = await check_can_upload_today(int(user["id"]))
    can_add_more = len(ids) < 2 and can_upload_today
    is_premium_user = bool(data.get("myphoto_is_premium"))
    locked_ids = set(data.get("myphoto_locked_ids") or [])

    caption = await build_my_photo_main_text(photo, locked=photo_id in locked_ids)
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(
        photo_id,
        ratings_enabled=_photo_ratings_enabled(photo),
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        nav_prev=nav_prev,
        nav_next=nav_next,
        locked=photo_id in locked_ids,
        lang=lang,
    )

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
            photo=_photo_public_id(photo),
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

    data = await state.get_data()
    locked_ids = set(data.get("myphoto_locked_ids") or [])

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
    if photo_id in locked_ids:
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
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

    kb.row(InlineKeyboardButton(text="📸 К фото", callback_data=f"myphoto:back:{photo_id}"))
    kb.row(InlineKeyboardButton(text=HOME, callback_data="menu:back"))

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
    if await _is_photo_locked_for_user(photo_id, state):
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
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
    if await _is_photo_locked_for_user(photo_id, state):
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
        return

    await state.set_state(EditPhotoStates.waiting_device_type)
    await state.update_data(edit_photo_id=photo_id)

    text = "📷 <b>Устройство</b>\n\nВыбери тип устройства:"
    await callback.message.edit_caption(caption=text, reply_markup=build_device_type_kb(photo_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:device:set:(\d+):(phone|camera|other)$"))
async def myphoto_device_set(callback: CallbackQuery, state: FSMContext):
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
    if await _is_photo_locked_for_user(photo_id, state):
        await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
        return

    text = "🏷 <b>Тег</b>\n\nВыбери из списка:"
    await callback.message.edit_caption(caption=text, reply_markup=build_tag_kb(photo_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^myphoto:tag:set:(\d+):(.*)$"))
async def myphoto_tag_set(callback: CallbackQuery, state: FSMContext):
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

    # Basic guards (do not crash on broken state)
    if not user_id or not file_id or not title:
        await bot.send_message(
            chat_id=upload_chat_id or fallback_chat_id,
            text="Сессия загрузки сбилась. Открой «Моя фотография» и попробуй загрузить заново.",
            disable_notification=True,
        )
        try:
            await state.clear()
        except Exception:
            pass
        return

    chat_id = upload_chat_id or fallback_chat_id

    # Готовим авторский код
    try:
        author_code = await ensure_user_author_code(int(event.from_user.id))
    except Exception:
        author_code = "GS-UNKNOWN"

    # Временный быстрый путь: не обрабатываем фото, сразу используем оригинальный file_id
    # TODO: вернуть водяной знак и проверку качества (GlowShot • {author_code}, 2026 All rights Reserved)
    file_id_public = file_id
    sent_msg_id: int | None = upload_msg_id or None
    is_unlimited_actor = False
    try:
        actor = await get_user_by_tg_id(int(event.from_user.id))
        is_unlimited_actor = bool(actor and is_unlimited_upload_user(actor))
    except Exception:
        is_unlimited_actor = False

    if not is_unlimited_actor:
        can_upload_now, denied_reason = await check_can_upload_today(int(user_id))
        if not can_upload_now:
            deny_text = denied_reason or "Сегодня новая публикация недоступна. Завтра можно."
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=HOME, callback_data="menu:back")]
                ]
            )
            if sent_msg_id:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=sent_msg_id,
                        text=deny_text,
                        reply_markup=kb,
                    )
                except Exception:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=deny_text,
                        reply_markup=kb,
                        disable_notification=True,
                    )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=deny_text,
                    reply_markup=kb,
                    disable_notification=True,
                )
            await state.clear()
            return

    # Сохраняем фото в БД, handle unique violation
    try:
        photo_id = await create_today_photo(
            user_id=user_id,
            file_id=file_id_public or file_id,
            file_id_public=file_id_public or file_id,
            file_id_original=file_id,
            title=title,
        )

        # За каждую публикацию: +2 credits.
        try:
            await add_credits(int(user_id), 2)
        except Exception:
            pass

        # 🔥 streak: successful upload counts as activity
        try:
            tg_id = int(event.from_user.id)
            await streak_record_action_by_tg_id(tg_id, "upload")
        except Exception:
            # Never break upload flow because of streak
            pass

    except UniqueViolationError:
        text = "Ты уже загружал(а) фотографию сегодня. Новая публикация будет доступна завтра."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=HOME, callback_data="menu:back")]
            ]
        )
        if sent_msg_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_msg_id,
                    text=text,
                    reply_markup=kb,
                )
            except Exception:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=kb,
                    disable_notification=True,
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=kb,
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

    # Финальная карточка: стараемся НЕ плодить сообщения.
    caption = await build_my_photo_main_text(photo)

    # Контекст навигации и лимитов
    try:
        active_photos_after = await get_active_photos_for_user(int(user_id), limit=2)
        active_photos_after = sorted(active_photos_after, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
    except Exception:
        active_photos_after = [photo]
    photo_ids_after = [p["id"] for p in active_photos_after]
    try:
        current_idx = photo_ids_after.index(photo["id"])
    except ValueError:
        current_idx = 0
    nav_prev = current_idx > 0
    nav_next = current_idx < len(photo_ids_after) - 1

    actor = None
    is_premium_user = False
    try:
        if hasattr(event, "from_user") and getattr(event.from_user, "id", None):
            is_premium_user = await is_user_premium_active(int(event.from_user.id))
    except Exception:
        is_premium_user = False
    lang = "ru"
    try:
        if hasattr(event, "from_user") and getattr(event.from_user, "id", None):
            actor = await get_user_by_tg_id(int(event.from_user.id))
            if actor:
                lang = (actor.get("lang") or "ru").split("-")[0]
    except Exception:
        lang = "ru"
        actor = None

    can_upload_today = True
    if actor and not is_unlimited_upload_user(actor):
        can_upload_today, _ = await check_can_upload_today(int(user_id))
    can_add_more = len(photo_ids_after) < 2 and can_upload_today

    kb = build_my_photo_keyboard(
        photo["id"],
        ratings_enabled=_photo_ratings_enabled(photo),
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        nav_prev=nav_prev,
        nav_next=nav_next,
        lang=lang,
    )

    # Обновляем отправленную ватермаркнутую карточку
    final_msg_id: int | None = None

    try:
        if sent_msg_id:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=sent_msg_id,
                caption=caption,
                reply_markup=kb,
            )
            await _store_photo_message_id(state, sent_msg_id, photo_id=photo["id"])
            final_msg_id = sent_msg_id
            final_msg_obj = None  # edit_message_caption не возвращает Message, будем использовать event
        else:
            raise ValueError("no message to edit")
    except Exception:
        sent_photo = await bot.send_photo(
            chat_id=chat_id,
            photo=_photo_public_id(photo),
            caption=caption,
            reply_markup=kb,
            disable_notification=True,
        )
        await _store_photo_message_id(state, sent_photo.message_id, photo_id=photo["id"])
        final_msg_id = sent_photo.message_id
        final_msg_obj = sent_photo
    await state.clear()
    # Сохраняем контекст навигации после очистки мастера
    await state.update_data(
        myphoto_ids=photo_ids_after,
        myphoto_current_idx=current_idx,
        myphoto_last_id=photo["id"],
        myphoto_is_premium=is_premium_user,
        myphoto_photo_msg_id=final_msg_id,
        myphoto_locked_ids=[],
    )
    # После успешной загрузки сразу открываем раздел "Моя фотография"
    try:
        if isinstance(event, CallbackQuery):
            await my_photo_menu(event, state)
        else:
            # Сообщение в роли callback, чтобы переиспользовать существующий хендлер
            msg_obj = final_msg_obj or event

            class _MsgAsCallback:
                def __init__(self, message):
                    self.message = message
                    self.from_user = message.from_user
                    self.bot = message.bot
                    self.data = "myphoto:open"

                async def answer(self, *args, **kwargs):
                    return None

            await my_photo_menu(_MsgAsCallback(msg_obj), state)
    except Exception:
        pass
    return
