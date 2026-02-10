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

from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler

from database import (
    get_user_by_tg_id,
    get_today_photo_for_user,
    create_today_photo,
    mark_photo_deleted,
    get_photo_by_id,
    update_photo_editable_fields,
    toggle_photo_ratings_enabled,
    get_photo_stats,
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
    get_link_ratings_count_for_photo,
    get_photo_skip_count_for_photo,
    get_comments_for_photo_sorted,
    streak_record_action_by_tg_id,
    ensure_user_author_code,
    get_weekly_idea_requests,
    increment_weekly_idea_requests,
    set_user_screen_msg_id,
)

from database_results import (
    PERIOD_DAY,
    SCOPE_GLOBAL,
    KIND_TOP_PHOTOS,
    get_results_items,
)

from utils.time import get_moscow_now
from utils.watermark import apply_text_watermark


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
        ("Это загрузка второй активной фотографии.\n\n" if second else "") + f"Когда будешь {ready} — жми «Загрузить».",
    ]
    return "\n".join(lines)


def build_upload_intro_kb(
    *,
    remaining: int | None = None,
    limit: int | None = None,
    idea_cb: str = "myphoto:idea",
    upload_cb: str = "myphoto:add",
    back_cb: str = "menu:back",
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
    kb.button(text="⬅️ Назад", callback_data=back_cb)
    kb.adjust(1)
    return kb.as_markup()


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

    # Верхний ряд — «Поделиться» (только если не залочено или есть премиум)
    if not locked or is_premium_user:
        rows.append([
            InlineKeyboardButton(text=t("myphoto.btn.share", lang), callback_data=f"myphoto:share:{photo_id}")
        ])

    if not locked:
        rows.append([
            InlineKeyboardButton(text=t("myphoto.btn.comments", lang), callback_data=f"myphoto:comments:{photo_id}:0"),
            InlineKeyboardButton(text=t("myphoto.btn.stats", lang), callback_data=f"myphoto:stats:{photo_id}"),
        ])

    # Блок редактирования и удаления (оценки перенесены внутрь редактирования)
    if locked:
        row = []
        if show_premium_cta:
            back_cb = premium_back_cb or "menu:back"
            row.append(InlineKeyboardButton(text=t("myphoto.btn.premium", lang), callback_data=f"premium:open:{back_cb}"))
        row.append(InlineKeyboardButton(text=t("myphoto.btn.delete", lang), callback_data=f"myphoto:delete:{photo_id}"))
        rows.append(row)
    else:
        rows.append([
            InlineKeyboardButton(text=t("myphoto.btn.edit", lang), callback_data=f"myphoto:edit:{photo_id}"),
            InlineKeyboardButton(text=t("myphoto.btn.delete", lang), callback_data=f"myphoto:delete:{photo_id}"),
        ])

    # Добавить / навигация
    nav_row: list[InlineKeyboardButton] = []
    nav_row.append(InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"))
    if can_add_more:
        nav_row.append(InlineKeyboardButton(text=t("myphoto.btn.add", lang), callback_data="myphoto:add_intro:extra"))
    elif nav_prev or nav_next:
        if nav_prev:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data="myphoto:nav:prev"))
        if nav_next:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data="myphoto:nav:next"))
    rows.append(nav_row)

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
    kb.row(InlineKeyboardButton(text="📷 Устройство", callback_data=f"myphoto:edit:device:{photo_id}"))
    kb.row(InlineKeyboardButton(text="🏷 Тег", callback_data=f"myphoto:edit:tag:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    return kb.as_markup()

def build_edit_cancel_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"))
    return kb.as_markup()


def build_edit_desc_kb(photo_id: int, has_description: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_description:
        kb.row(InlineKeyboardButton(text="🗑 Удалить описание", callback_data=f"myphoto:edit:desc_clear:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"))
    return kb.as_markup()

def build_device_type_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📱 Смартфон", callback_data=f"myphoto:device:set:{photo_id}:phone"),
        InlineKeyboardButton(text="📸 Камера", callback_data=f"myphoto:device:set:{photo_id}:camera"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"))
    return kb.as_markup()

def build_tag_kb(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for tag_key, label in EDIT_TAGS:
        kb.row(InlineKeyboardButton(text=label, callback_data=f"myphoto:tag:set:{photo_id}:{tag_key}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:editmenu:{photo_id}"))
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
        can_add_more = len(ids) < 2
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
    if not is_premium_user and len(photos) > 1:
        # все, кроме первого, считаем премиум-локом
        locked_ids = [p["id"] for p in photos[1:]]

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
        limit, current, remaining = await _idea_counters(user, is_premium_user)
        kb = build_upload_intro_kb(remaining=remaining, limit=limit)

        idea_title, idea_hint = _get_daily_idea()
        text = _build_upload_intro_text(
            user,
            idea_label="Идея дня",
            idea_title=idea_title,
            idea_hint=idea_hint,
        )
        await state.update_data(
            myphoto_ids=[],
            myphoto_current_idx=0,
            myphoto_last_id=None,
            myphoto_is_premium=is_premium_user,
        )

        if opened_from_menu:
            sent = await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=kb,
                disable_notification=True,
            )
            try:
                await set_user_screen_msg_id(callback.from_user.id, sent.message_id)
            except Exception:
                pass
            try:
                await callback.message.delete()
            except Exception:
                pass
            data["menu_msg_id"] = None
            await state.set_data(data)
        else:
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=text, reply_markup=kb)
                else:
                    await callback.message.edit_text(text, reply_markup=kb)
                try:
                    await set_user_screen_msg_id(callback.from_user.id, callback.message.message_id)
                except Exception:
                    pass
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
                try:
                    await set_user_screen_msg_id(callback.from_user.id, sent.message_id)
                except Exception:
                    pass

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
    can_add_more = len(photo_ids) < 2
    locked = photo["id"] in locked_ids

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
    can_add_more = len(photo_ids) < 2
    locked_ids = list(data.get("myphoto_locked_ids") or [])
    locked = photo["id"] in locked_ids

    await state.update_data(
        myphoto_ids=photo_ids,
        myphoto_current_idx=current_idx,
        myphoto_last_id=photo["id"],
        myphoto_is_premium=is_premium_user,
        myphoto_locked_ids=locked_ids,
    )

    await _edit_or_replace_my_photo_message(
        callback,
        state,
        photo,
        nav_prev=nav_prev,
        nav_next=nav_next,
        can_add_more=can_add_more,
        is_premium_user=is_premium_user,
        locked=locked,
    )
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

    # Base stats
    try:
        r = await get_photo_ratings_stats(photo_id)
    except Exception:
        await callback.answer("⚠️ Не удалось загрузить статистику. Попробуй ещё раз через пару секунд.", show_alert=True)
        return
    ratings_count = int(r.get("ratings_count") or 0)
    last_rating = r.get("last_rating")
    # Показываем Bayes-рейтинг вместо средней
    smart_score = None
    try:
        smart_score = (await get_photo_stats(photo_id)).get("bayes_score")
    except Exception:
        smart_score = None

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
    lines.append(f"📈 Рейтинг: <b>{_fmt_avg(smart_score)}</b>")
    lines.append(f"🔥 Супер-оценок: <b>{super_count}</b>")
    lines.append(f"💬 Комментариев: <b>{comments_count}</b>")
    lines.append(f"🔗⭐️ Оценки по ссылке: <b>{link_ratings}</b>")

    lines.append("")

    if is_premium_user:
        # Rank in today's top based on results_v2 cache
        dk = str(photo.get("day_key") or "")
        if dk:
            try:
                top_items = await _get_daily_top_photos_v2(dk, limit=50)
                place_now = None
                for i, p in enumerate(top_items, start=1):
                    if int(p.get("id") or 0) == int(photo_id):
                        place_now = i
                        break
            except Exception:
                place_now = None
        else:
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
        back_cb="myphoto:open",
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
        back_cb="myphoto:open",
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
    """Cancel upload wizard and return to My Photo section."""
    try:
        await state.clear()
    except Exception:
        pass

    # Reuse existing My Photo entry handler to render proper UI.
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
    await my_photo_menu(callback, state)


# ---- upload limits helpers ----

def _user_photo_limits(is_premium_user: bool, is_admin: bool) -> tuple[int, int]:
    """
    Возвращает (max_active, daily_limit) для пользователя.
    Админ: до 2 активных, но попыток загрузки в день не ограничиваем (ставим очень большое число).
    """
    if is_admin:
        return 2, 10**9
    if is_premium_user:
        return 2, 3  # две активных, до трёх загрузок в день (можно заменять)
    return 1, 1


async def _can_user_upload_now(user: dict, is_premium_user: bool, is_admin: bool) -> tuple[bool, str | None]:
    max_active, daily_limit = _user_photo_limits(is_premium_user, is_admin)
    user_id = int(user["id"])

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

    today_count = 0
    try:
        today_count = await count_today_photos_for_user(user_id, include_deleted=True)
    except Exception:
        today_count = 0

    if today_count >= daily_limit:
        return False, _format_time_until_next_upload()

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

    is_admin = is_admin_user(user)

    max_active, daily_limit = _user_photo_limits(is_premium_user, is_admin)
    active_count = len(active_photos)

    # Проверка лимита активных фото
    if not is_admin and active_count >= max_active:
        if is_premium_user:
            await callback.answer(
                "У тебя уже 2 активные фотографии. Удали одну, чтобы загрузить новую.",
                show_alert=True,
            )
        else:
            await callback.answer(
                "Вторая фотография доступна в GlowShot Premium 💎.\n\nОформи подписку, чтобы добавить ещё одну.",
                show_alert=True,
            )
        return

    # Проверка дневных попыток
    if not is_admin:
        today_count = await count_today_photos_for_user(user["id"], include_deleted=True)
        if today_count >= daily_limit:
            remaining = _format_time_until_next_upload()
            await callback.answer(
                f"Лимит загрузок на сегодня исчерпан.\n\nНовый кадр можно будет выложить {remaining}.",
                show_alert=True,
            )
            return

    # Админу позволяем перезаливать: если сегодня уже есть активный кадр — мягко удаляем его
    photo = await get_today_photo_for_user(user_id)
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

    is_extra = (callback.data or "") == "myphoto:add:extra"
    if is_extra:
        text = "Загрузка второй фотографии: отправь кадр (1 шт.), который хочешь добавить."
    else:
        text = "Теперь отправь фотографию (1 шт.), которую хочешь выложить."
    kb = build_upload_wizard_kb(back_to="menu")

    # Всегда отправляем новое сообщение, затем удаляем предыдущее
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
            "Теперь напиши название этой работы.\n"
            "<b>Поменять название после загрузки нельзя.</b>\n\n"
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

    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=hint,
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        pass


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
    lang = (user.get("lang") or "ru").split("-")[0] if user else "ru"
    kb = build_my_photo_keyboard(
        photo["id"],
        ratings_enabled=_photo_ratings_enabled(photo),
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
    kb.button(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}")
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
    can_add_more = len(ids) < 2
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

    # Скачиваем оригинал и наносим водяной знак
    try:
        tg_file = await bot.get_file(file_id)
        buff = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buff)
        img_bytes = buff.getvalue()
        ok_quality, quality_msg = _is_photo_quality_ok(img_bytes)
        if not ok_quality:
            await bot.send_message(
                chat_id=chat_id,
                text=f"Фотография отклонена:\n{quality_msg}\n\nЗагрузи файл с более высоким качеством.",
                disable_notification=True,
            )
            await state.clear()
            return

        wm_bytes = apply_text_watermark(img_bytes, f"GlowShot • {author_code}")
    except Exception as e:
        await bot.send_message(
            chat_id=chat_id,
            text="Не удалось обработать фотографию. Попробуй загрузить ещё раз.",
            disable_notification=True,
        )
        print("WATERMARK ERROR:", repr(e))
        await state.clear()
        return

    # Отправляем ватермаркнутую версию, чтобы получить публичный file_id, стараясь переиспользовать текущее сообщение
    media = InputMediaPhoto(media=BufferedInputFile(wm_bytes, filename="glowshot_wm.jpg"), caption="Готовим карточку…")
    sent_msg_id: int | None = None
    file_id_public: str | None = None
    if upload_msg_id and chat_id:
        try:
            res = await bot.edit_message_media(
                chat_id=chat_id,
                message_id=upload_msg_id,
                media=media,
            )
            # aiogram возвращает Message при успехе
            if isinstance(res, Message):
                file_id_public = res.photo[-1].file_id if res.photo else None
                sent_msg_id = res.message_id
            else:
                sent_msg_id = upload_msg_id
        except Exception:
            sent_msg_id = None

    if file_id_public is None:
        try:
            sent_draft = await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(wm_bytes, filename="glowshot_wm.jpg"),
                caption="Готовим карточку…",
                disable_notification=True,
            )
            file_id_public = sent_draft.photo[-1].file_id
            sent_msg_id = sent_draft.message_id
        except Exception as e:
            await bot.send_message(
                chat_id=chat_id,
                text="Не удалось загрузить обработанную фотографию. Попробуй ещё раз.",
                disable_notification=True,
            )
            print("WATERMARK SEND ERROR:", repr(e))
            await state.clear()
            return

    if not sent_msg_id:
        sent_msg_id = upload_msg_id or 0

    # Сохраняем фото в БД, handle unique violation
    try:
        photo_id = await create_today_photo(
            user_id=user_id,
            file_id=file_id_public or file_id,
            file_id_public=file_id_public or file_id,
            file_id_original=file_id,
            title=title,
        )

        # 🔥 streak: successful upload counts as activity
        try:
            tg_id = int(event.from_user.id)
            await streak_record_action_by_tg_id(tg_id, "upload")
        except Exception:
            # Never break upload flow because of streak
            pass

    except UniqueViolationError:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "Ты уже загружал(а) фотографию сегодня.\n\n"
                "Если хочешь заменить — удали текущую в разделе «Моя фотография» и попробуй снова."
            ),
            disable_notification=True,
        )
        await state.clear()
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent_msg_id)
        except Exception:
            pass
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
    can_add_more = len(photo_ids_after) < 2

    is_premium_user = False
    try:
        if hasattr(event, "from_user") and getattr(event.from_user, "id", None):
            is_premium_user = await is_user_premium_active(int(event.from_user.id))
    except Exception:
        is_premium_user = False
    lang = "ru"
    try:
        if hasattr(event, "from_user") and getattr(event.from_user, "id", None):
            u = await get_user_by_tg_id(int(event.from_user.id))
            if u:
                lang = (u.get("lang") or "ru").split("-")[0]
    except Exception:
        lang = "ru"

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
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=sent_msg_id,
            caption=caption,
            reply_markup=kb,
        )
        await _store_photo_message_id(state, sent_msg_id, photo_id=photo["id"])
        final_msg_id = sent_msg_id
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
    return
