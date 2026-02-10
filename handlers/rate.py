from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
import traceback
from dataclasses import dataclass
from datetime import date, datetime
import os
from utils.time import get_moscow_today, get_moscow_now

from aiogram.types import CallbackQuery, InputMediaPhoto, Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.i18n import t
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from keyboards.common import build_viewed_kb
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from utils.antispam import should_throttle
from utils.moderation import (
    get_report_reasons,
    REPORT_REASON_LABELS,
    REPORT_RATE_LIMIT_MAX,
    REPORT_RATE_LIMIT_WINDOW,
    evaluate_report_rate_limit,
)

from database import (
    get_user_by_tg_id,
    get_random_photo_for_rating,
    add_rating,
    set_super_rating,
    create_comment,
    create_photo_report,
    get_photo_report_stats,
    set_photo_moderation_status,
    get_photo_by_id,
    get_user_by_id,
    get_moderators,
    is_user_premium_active,
    get_daily_skip_info,
    update_daily_skip_info,
    get_awards_for_user,
    link_and_reward_referral_if_needed,
    log_bot_error,
    streak_record_action_by_tg_id,
    get_notify_settings_by_tg_id,
    increment_likes_daily_for_tg_id,
    get_photo_stats,
    get_moderation_message_for_photo,
    upsert_moderation_message_for_photo,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
    get_user_reports_since,
    mark_viewonly_seen,
    get_active_photos_for_user,
    get_random_active_ad,
    get_ads_enabled_by_tg_id,
    has_user_commented,
    get_user_ui_state,
    set_user_rate_kb_msg_id,
    set_user_screen_msg_id,
    set_user_rate_tutorial_seen,
    get_user_rating_day_stats,
    mark_user_suspicious_rating,
)
from handlers.upload import EDIT_TAGS
from html import escape
from config import MODERATION_CHAT_ID, RATE_TUTORIAL_PHOTO_FILE_ID
from utils.banner import ensure_giraffe_banner
from utils.registration_guard import require_user_name

router = Router()

def _lang(user: dict | None) -> str:
    try:
        raw = (user or {}).get("lang") or (user or {}).get("language") or (user or {}).get("language_code")
        if raw:
            return str(raw).split("-")[0].lower()
    except Exception:
        pass
    return "ru"


def _fmt_pub_date(day_key: str | None) -> str:
    s = (day_key or "").strip()
    if not s:
        return "—"
    try:
        return str(s).split("T", 1)[0]
    except Exception:
        return s


def _photo_public_id(photo: dict) -> str | None:
    pid = photo.get("file_id_public") or photo.get("file_id")
    return str(pid) if pid is not None else None


def _shorten_text(text: str, limit: int = 220) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _tag_badge(tag_key: str) -> str:
    t = (tag_key or "").strip()
    if not t:
        return "нет"
    for k, lbl in EDIT_TAGS:
        if k == t:
            parts = (lbl or "").strip().split(" ", 1)
            if parts and parts[0]:
                return parts[0]
            return lbl or t
    return t


def _device_emoji(device_type_raw: str) -> str:
    dt = (device_type_raw or "").lower()
    if "смартфон" in dt or "phone" in dt:
        return "📱"
    if "фотокамера" in dt or "camera" in dt:
        return "📷"
    if dt:
        return "📸"
    return ""


def _extract_tg_username(raw_link: str) -> str | None:
    s = (raw_link or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return s[1:].strip() or None

    lower = s.lower()
    prefixes = (
        "https://t.me/",
        "http://t.me/",
        "t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    )
    for pref in prefixes:
        if lower.startswith(pref):
            rest = s[len(pref):].strip()
            rest = rest.split("/", 1)[0].split("?", 1)[0].strip()
            if rest.startswith("@"):
                rest = rest[1:].strip()
            return rest or None

    if "t.me/" in lower:
        rest = s.split("t.me/", 1)[1]
        rest = rest.split("/", 1)[0].split("?", 1)[0].strip()
        if rest.startswith("@"):
            rest = rest[1:].strip()
        return rest or None
    if "telegram.me/" in lower:
        rest = s.split("telegram.me/", 1)[1]
        rest = rest.split("/", 1)[0].split("?", 1)[0].strip()
        if rest.startswith("@"):
            rest = rest[1:].strip()
        return rest or None

    return None


def _link_button_from_raw(raw_link: str) -> tuple[str, str] | None:
    username = _extract_tg_username(raw_link)
    if not username:
        return None
    label = f"@{username}"
    return (f"🔗 {label}", f"https://t.me/{username}")


def _author_is_premium(photo: dict, author: dict | None = None) -> bool:
    return bool(photo.get("user_is_premium_active") or (author or {}).get("is_premium_active"))


def _get_link_button_from_photo(
    photo: dict,
    author: dict | None = None,
    *,
    require_premium: bool = False,
) -> tuple[str, str] | None:
    if require_premium and not _author_is_premium(photo, author):
        return None
    raw_link = (photo.get("user_tg_channel_link") or photo.get("tg_channel_link") or "").strip()
    if not raw_link and author and author.get("tg_channel_link"):
        raw_link = (author.get("tg_channel_link") or "").strip()
        if raw_link:
            photo["user_tg_channel_link"] = raw_link
    if not raw_link:
        return None
    button = _link_button_from_raw(raw_link)
    if button:
        return button
    if raw_link.startswith("http://") or raw_link.startswith("https://"):
        return (f"🔗 {raw_link}", raw_link)
    return None


async def _ensure_author_premium_active(photo: dict, author: dict | None = None) -> bool:
    """Returns active premium status; updates cached flags in photo/author."""
    active: bool | None = None
    tg_id = None
    try:
        tg_id = int(author.get("tg_id")) if author and author.get("tg_id") else None
    except Exception:
        tg_id = None

    if tg_id:
        try:
            active = await is_user_premium_active(int(tg_id))
        except Exception:
            active = None

    if active is None:
        if "user_is_premium_active" in photo:
            active = bool(photo.get("user_is_premium_active"))
        elif author and "is_premium_active" in author:
            active = bool(author.get("is_premium_active"))
        else:
            active = False

    photo["user_is_premium_active"] = active
    # keep legacy flag aligned for downstream code that still reads it
    photo["user_is_premium"] = active
    if author is not None:
        author["is_premium_active"] = active
    return active

def build_mod_report_keyboard(photo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=f"mod:report_delete:{photo_id}")
    kb.button(text="⛔ Заблокировать", callback_data=f"mod:report_block:{photo_id}")
    kb.button(text="✅ Всё в порядке", callback_data=f"mod:report_ok:{photo_id}")
    kb.adjust(1)
    return kb.as_markup()

async def build_mod_report_caption(
    *,
    photo: dict,
    report_stats: dict,
    last_reason_code: str,
    last_comment: str,
) -> str:
    title = (photo.get("title") or "Без названия").strip()
    tag = (photo.get("tag") or "").strip() or "—"
    author_line = "Автор: —"
    try:
        author = await get_user_by_id(int(photo.get("user_id") or 0))
    except Exception:
        author = None
    if author:
        uname = (author.get("username") or "").strip()
        tg_id = author.get("tg_id")
        uname_display = f"@{uname}" if uname else "—"
        tg_id_display = str(tg_id) if tg_id else "—"
        author_line = f"Автор: {escape(uname_display)} / {escape(tg_id_display)}"

    pub = _fmt_pub_date(photo.get("day_key"))

    try:
        ps = await get_photo_stats(int(photo["id"]))
    except Exception:
        ps = {}

    ratings_count = int(ps.get("ratings_count") or 0)
    score = ps.get("bayes_score")
    score_str = "—"
    if score is not None:
        try:
            score_str = f"{float(score):.2f}".rstrip("0").rstrip(".")
        except Exception:
            score_str = "—"

    pending = int(report_stats.get("total_pending") or 0)
    total = int(report_stats.get("total_all") or 0)

    reason_label = REPORT_REASON_LABELS.get(last_reason_code, last_reason_code)

    lines: list[str] = []
    lines.append(f"🚨 <b>Новая жалоба!</b> #{total}")
    lines.append("")
    lines.append(f"<b>«{escape(title)}»</b>")
    lines.append(f"🏷 Тег: <code>{escape(tag)}</code>")
    lines.append(author_line)
    lines.append("")
    lines.append(f"Опубликовано: <code>{escape(pub)}</code>")
    lines.append(f"Оценок + рейтинг: <b>{ratings_count}</b> = <b>{escape(score_str)}</b>")
    lines.append("")
    lines.append(f"Кол-во жалоб: <b>{pending}</b>")
    lines.append(f"(Последняя) Причина: {escape(reason_label)}")

    last_comment_clean = (last_comment or "").strip()
    if last_comment_clean:
        lines.append(f"(Последний) Комментарий: {escape(last_comment_clean)}")
    else:
        lines.append("(Последний) Комментарий: —")

    return "\n".join(lines)


async def _deny_if_full_banned(callback: CallbackQuery | None = None, message: Message | None = None) -> bool:
    actor_id = None
    if callback is not None:
        actor_id = callback.from_user.id
    elif message is not None:
        actor_id = message.from_user.id

    if not actor_id:
        return False

    try:
        block = await get_user_block_status_by_tg_id(int(actor_id))
    except Exception:
        return False

    is_blocked = bool(block.get("is_blocked"))
    until_str = block.get("block_until")
    reason = (block.get("block_reason") or "").strip()

    if not is_blocked:
        return False

    from utils.time import get_moscow_now
    from datetime import datetime

    until_dt = None
    if until_str:
        try:
            until_dt = datetime.fromisoformat(str(until_str))
        except Exception:
            until_dt = None

    now = get_moscow_now()
    if until_dt is not None and until_dt <= now:
        try:
            await set_user_block_status_by_tg_id(int(actor_id), is_blocked=False, reason=None, until_iso=None)
        except Exception:
            pass
        return False

    if not reason.startswith("FULL_BAN:"):
        return False

    if callback is not None:
        try:
            await callback.answer("⛔ Вы заблокированы модераторами.", show_alert=True)
        except Exception:
            pass
    elif message is not None:
        try:
            await message.answer("⛔ Вы заблокированы модераторами.")
        except Exception:
            pass

    return True


def _moscow_day_key() -> str:
    try:
        return get_moscow_today().isoformat()
    except Exception:
        return str(get_moscow_today())


def _format_report_cooldown(seconds: int) -> str:
    if seconds <= 0:
        return "меньше минуты"
    minutes, secs = divmod(seconds, 60)
    parts: list[str] = []
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and minutes < 5:
        parts.append(f"{secs} сек")
    if not parts:
        parts.append("меньше минуты")
    return " ".join(parts)


_RATE_ONES_DAILY_MIN_TOTAL = int(os.getenv("RATE_ONES_DAILY_MIN_TOTAL", "12"))
_RATE_ONES_DAILY_LIMIT = int(os.getenv("RATE_ONES_DAILY_LIMIT", "10"))
_RATE_ONES_DAILY_HARD_CAP = int(os.getenv("RATE_ONES_DAILY_HARD_CAP", "20"))
_RATE_ONES_DAILY_RATIO = float(os.getenv("RATE_ONES_DAILY_RATIO", "0.85"))


async def _check_rate_spam_block(user: dict, *, value: int) -> tuple[bool, str | None]:
    if not user:
        return False, None
    if user.get("is_admin") or user.get("is_moderator"):
        return False, None
    try:
        day_key = _moscow_day_key()
        stats = await get_user_rating_day_stats(int(user["id"]), str(day_key))
        ones = int(stats.get("ones_count") or 0)
        total = int(stats.get("total_count") or 0)
    except Exception:
        return False, None

    blocked = False
    if ones >= _RATE_ONES_DAILY_HARD_CAP:
        blocked = True
    elif total >= _RATE_ONES_DAILY_MIN_TOTAL and ones >= _RATE_ONES_DAILY_LIMIT:
        ratio = (ones / total) if total > 0 else 0.0
        if ratio >= _RATE_ONES_DAILY_RATIO:
            blocked = True
    elif value == 1:
        ones_next = ones + 1
        total_next = total + 1
        if ones_next >= _RATE_ONES_DAILY_HARD_CAP:
            blocked = True
        elif total_next >= _RATE_ONES_DAILY_MIN_TOTAL and ones_next >= _RATE_ONES_DAILY_LIMIT:
            ratio_next = (ones_next / total_next) if total_next > 0 else 0.0
            if ratio_next >= _RATE_ONES_DAILY_RATIO:
                blocked = True

    if not blocked:
        return False, None

    try:
        await mark_user_suspicious_rating(
            int(user["id"]),
            str(day_key),
            int(ones),
            int(total),
        )
    except Exception:
        pass

    msg = (
        "Сегодня достаточно плохих оценок.\n\n"
        "Сделай паузу и возвращайся позже."
    )
    return True, msg


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    """Простейшее склонение русских слов по числу: 1 день, 3 дня, 5 дней."""
    v = abs(value) % 100
    if 11 <= v <= 19:
        return many
    v = v % 10
    if v == 1:
        return one
    if 2 <= v <= 4:
        return few
    return many


async def _get_report_rate_limit_status(user_id: int):
    """
    Возвращает статус по лимиту жалоб для пользователя.
    Если что-то пошло не так с БД — возвращаем None, чтобы не блокировать пользователя.
    """
    try:
        now = get_moscow_now()
        since_iso = (now - REPORT_RATE_LIMIT_WINDOW).isoformat()
        recent = await get_user_reports_since(int(user_id), since_iso, limit=REPORT_RATE_LIMIT_MAX)
        return evaluate_report_rate_limit(recent, now=now)
    except Exception:
        return None


class RateStates(StatesGroup):
    waiting_comment_text = State()
    waiting_report_text = State()


@dataclass(slots=True)
class RatingCard:
    photo: dict | None
    caption: str
    keyboard: InlineKeyboardMarkup
    photo_file_id: str | None


async def _build_rating_card_from_photo(photo: dict, rater_user_id: int, viewer_tg_id: int) -> RatingCard:
    """Собрать карточку оценивания для конкретной фотографии (с учётом премиума, ачивок и т.д.)."""
    photo = dict(photo)

    author_user_id_raw = photo.get("user_id")
    try:
        author_user_id = int(author_user_id_raw) if author_user_id_raw else 0
    except Exception:
        author_user_id = 0

    try:
        has_beta_award = False
        if author_user_id:
            awards = await get_awards_for_user(author_user_id)
            for award in awards:
                code = (award.get("code") or "").strip()
                title = (award.get("title") or "").strip().lower()
                if code == "beta_tester" or "бета-тестер бота" in title or "бета тестер бота" in title:
                    has_beta_award = True
                    break
        photo["has_beta_award"] = has_beta_award
    except Exception:
        photo["has_beta_award"] = False

    try:
        author_user = await get_user_by_id(author_user_id)
    except Exception:
        author_user = None

    if author_user:
        if not photo.get("user_tg_channel_link") and author_user.get("tg_channel_link"):
            photo["user_tg_channel_link"] = author_user.get("tg_channel_link")

        premium_active = await _ensure_author_premium_active(photo, author_user)

    link_button = _get_link_button_from_photo(photo, author_user, require_premium=True)

    caption = await build_rate_caption(photo, viewer_tg_id=int(viewer_tg_id), show_details=False)

    is_premium_rater = False
    rater_lang = "ru"
    try:
        rater_user = await get_user_by_id(int(rater_user_id))
        if rater_user and rater_user.get("tg_id"):
            is_premium_rater = await is_user_premium_active(rater_user["tg_id"])
        rater_lang = _lang(rater_user)
    except Exception:
        is_premium_rater = False

    is_rateable = bool(photo.get("ratings_enabled", True))
    if not is_rateable:
        caption = caption + "\n\n🚫 <i>Эта фотография не для оценивания.</i>"
        kb = build_view_only_keyboard(
            int(photo["id"]),
            show_details=False,
            is_premium=is_premium_rater,
            link_button=link_button,
            lang=rater_lang,
        )
    else:
        kb = build_rate_keyboard(
            int(photo["id"]),
            is_premium=is_premium_rater,
            show_details=False,
            link_button=link_button,
            lang=rater_lang,
        )
    file_id_str = _photo_public_id(photo)

    return RatingCard(
        photo=photo,
        caption=caption,
        keyboard=kb,
        photo_file_id=file_id_str,
    )


async def _build_rating_card_for_photo(
    photo_id: int,
    rater_user_id: int,
    viewer_tg_id: int,
    prefix: str | None = None,
) -> RatingCard | None:
    try:
        photo = await get_photo_by_id(int(photo_id))
    except Exception:
        photo = None

    if photo is None:
        return None

    card = await _build_rating_card_from_photo(photo, rater_user_id, viewer_tg_id)
    if prefix:
        card.caption = f"{prefix}\n\n{card.caption}"
    return card


async def _build_rate_view(
    photo_id: int,
    viewer_tg_id: int,
    *,
    show_details: bool = False,
) -> tuple[str, InlineKeyboardMarkup, bool] | None:
    try:
        photo = await get_photo_by_id(int(photo_id))
    except Exception:
        photo = None

    if not photo or photo.get("is_deleted"):
        return None

    viewer_is_premium = False
    try:
        viewer_is_premium = await is_user_premium_active(int(viewer_tg_id))
    except Exception:
        viewer_is_premium = False

    viewer_user = await get_user_by_tg_id(int(viewer_tg_id))
    viewer_lang = _lang(viewer_user)

    try:
        author_user_id = int(photo.get("user_id") or 0)
    except Exception:
        author_user_id = 0

    if "has_beta_award" not in photo or not photo.get("has_beta_award"):
        try:
            if author_user_id:
                awards = await get_awards_for_user(author_user_id)
                photo["has_beta_award"] = any(
                    (award.get("code") or "").strip() == "beta_tester"
                    or "бета-тестер бота" in (award.get("title") or "").strip().lower()
                    or "бета тестер бота" in (award.get("title") or "").strip().lower()
                    for award in awards
                )
        except Exception:
            photo["has_beta_award"] = False

    author = None
    try:
        author = await get_user_by_id(int(photo.get("user_id") or 0))
    except Exception:
        author = None

    if author and author.get("tg_channel_link") and not photo.get("user_tg_channel_link"):
        photo["user_tg_channel_link"] = author.get("tg_channel_link")

    if "user_is_premium_active" not in photo:
        await _ensure_author_premium_active(photo, author)

    link_button = _get_link_button_from_photo(photo, author, require_premium=True)

    is_rateable = bool(photo.get("ratings_enabled", True))
    caption = await build_rate_caption(photo, viewer_tg_id=int(viewer_tg_id), show_details=show_details)
    if not is_rateable and "не для оценивания" not in caption.lower():
        caption = caption + "\n\n🚫 <i>Эта фотография не для оценивания.</i>"

    if is_rateable:
        kb = build_rate_keyboard(
            int(photo_id),
            is_premium=viewer_is_premium,
            show_details=show_details,
            more_only=show_details,
            link_button=link_button,
            lang=viewer_lang,
        )
    else:
        kb = build_view_only_keyboard(
            int(photo_id),
            show_details=show_details,
            is_premium=viewer_is_premium,
            more_only=show_details,
            link_button=link_button,
            lang=viewer_lang,
        )

    return caption, kb, is_rateable


async def _build_next_rating_card(rater_user_id: int, viewer_tg_id: int) -> RatingCard:
    photo = await get_random_photo_for_rating(rater_user_id)
    if photo is None:
        viewer_user = await get_user_by_id(int(rater_user_id))
        lang = _lang(viewer_user)
        return RatingCard(
            photo=None,
            caption=build_no_photos_text(),
            keyboard=build_no_photos_keyboard(lang=lang),
            photo_file_id=None,
        )

    return await _build_rating_card_from_photo(photo, rater_user_id, viewer_tg_id)

def _build_rate_reply_keyboard(lang: str = "ru") -> ReplyKeyboardMarkup:
    """Reply‑клавиатура для оценок 1–10."""
    row1 = [KeyboardButton(text=str(i)) for i in range(1, 6)]
    row2 = [KeyboardButton(text=str(i)) for i in range(6, 11)]
    return ReplyKeyboardMarkup(
        keyboard=[row1, row2],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )


def _build_next_only_reply_keyboard(lang: str = "ru") -> ReplyKeyboardMarkup:
    """Reply‑клавиатура только с кнопкой «Дальше»."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("rate.btn.next", lang))]],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )


RATE_TUTORIAL_OK_TEXT = "Все понятно!"
RATE_TUTORIAL_CAPTION = (
    "Название\n"
    "[Тег] · Автор · Устройство · #0/0\n\n"
    "📜 Описание автора из профиля"
)


def _build_rate_tutorial_inline_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Ссылка автора", callback_data="rate:tutor:noop")
    kb.button(text="Коммент", callback_data="rate:tutor:noop")
    kb.button(text="Жалоба", callback_data="rate:tutor:noop")
    kb.button(text="В меню", callback_data="menu:back")
    kb.button(text="Еще", callback_data="rate:tutor:noop")
    kb.adjust(1, 2, 2)
    return kb.as_markup()


def _build_rate_tutorial_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=RATE_TUTORIAL_OK_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )

def _rate_kb_hint(lang: str, mode: str) -> str:
    if mode == "next":
        return t("rate.kb.next", lang)
    if mode == "tutorial":
        return t("rate.kb.tutorial", lang)
    return t("rate.kb.hint", lang)


async def _send_rate_kb_message(
    bot,
    chat_id: int,
    state: FSMContext,
    *,
    reply_markup: ReplyKeyboardMarkup,
    mode: str,
    text: str,
) -> None:
    data = await state.get_data()
    old_msg_id = data.get("rate_kb_msg_id")
    if old_msg_id is None:
        try:
            ui_state = await get_user_ui_state(chat_id)
            old_msg_id = ui_state.get("rate_kb_msg_id")
        except Exception:
            old_msg_id = None

    if data.get("rate_kb_mode") == mode and old_msg_id:
        return

    banner_id: int | None = None
    try:
        banner_id = await ensure_giraffe_banner(
            bot,
            chat_id,
            chat_id,
            text=text,
            reply_markup=reply_markup,
            force_new=False,
        )
    except Exception:
        banner_id = None

    if banner_id is None:
        return

    data["rate_kb_msg_id"] = int(banner_id)
    data["rate_kb_mode"] = mode
    await state.set_data(data)
    try:
        await set_user_rate_kb_msg_id(chat_id, int(banner_id))
    except Exception:
        pass


async def _send_rate_reply_keyboard(bot, chat_id: int, state: FSMContext, lang: str) -> None:
    """Показать reply‑клавиатуру оценок, удаляя старое сообщение."""
    await _send_rate_kb_message(
        bot,
        chat_id,
        state,
        reply_markup=_build_rate_reply_keyboard(lang),
        mode="rate",
        text="🦒",
    )


async def _send_next_only_reply_keyboard(bot, chat_id: int, state: FSMContext, lang: str) -> None:
    """Показать reply‑клавиатуру только «Дальше»."""
    await _send_rate_kb_message(
        bot,
        chat_id,
        state,
        reply_markup=_build_next_only_reply_keyboard(lang),
        mode="next",
        text="🦒",
    )


async def _send_tutorial_reply_keyboard(bot, chat_id: int, state: FSMContext, lang: str) -> None:
    """Сообщение с одной reply‑кнопкой «Все понятно!»."""
    await _send_rate_kb_message(
        bot,
        chat_id,
        state,
        reply_markup=_build_rate_tutorial_reply_keyboard(),
        mode="tutorial",
        text="🦒",
    )


async def _delete_rate_reply_keyboard(bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    msg_id = data.get("rate_kb_msg_id")
    banner_id = None
    if msg_id is None:
        try:
            ui_state = await get_user_ui_state(chat_id)
            msg_id = ui_state.get("rate_kb_msg_id")
            banner_id = ui_state.get("banner_msg_id")
        except Exception:
            msg_id = None
    if banner_id is None:
        try:
            ui_state = await get_user_ui_state(chat_id)
            banner_id = ui_state.get("banner_msg_id")
        except Exception:
            banner_id = None
    data["rate_kb_msg_id"] = None
    data["rate_kb_mode"] = "none"
    await state.set_data(data)
    try:
        await set_user_rate_kb_msg_id(chat_id, None)
    except Exception:
        pass
    if banner_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(banner_id),
                text="🦒",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        except Exception:
            pass
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(msg_id))
        except Exception:
            pass


async def _clear_rate_comment_draft(state: FSMContext) -> None:
    """Очистить временные данные комментария, не трогая ключи текущей карточки."""
    try:
        data = await state.get_data()
    except Exception:
        return
    changed = False
    for key in ("photo_id", "comment_text", "is_public", "comment_saved", "rate_msg_id", "rate_chat_id"):
        if key in data:
            data.pop(key, None)
            changed = True
    if changed:
        try:
            await state.set_data(data)
        except Exception:
            pass


async def _clear_rate_report_draft(state: FSMContext) -> None:
    """Очистить временные данные жалобы, не трогая ключи текущей карточки."""
    try:
        await state.set_state(None)
    except Exception:
        pass
    try:
        data = await state.get_data()
    except Exception:
        return
    changed = False
    for key in ("report_photo_id", "report_msg_id", "report_chat_id", "report_reason"):
        if key in data:
            data.pop(key, None)
            changed = True
    if changed:
        try:
            await state.set_data(data)
        except Exception:
            pass


async def _sync_rate_state_for_card(
    *,
    state: FSMContext,
    card: RatingCard,
    bot,
    chat_id: int,
    lang: str,
) -> None:
    """Обновить состояние оценивания под показанную карточку."""
    try:
        data = await state.get_data()
    except Exception:
        return
    data["rate_current_photo_id"] = card.photo["id"] if card.photo else None
    data["rate_show_details"] = False
    try:
        await state.set_data(data)
    except Exception:
        pass

    if card.photo:
        if bool(card.photo.get("ratings_enabled", True)):
            await _send_rate_reply_keyboard(bot, chat_id, state, lang)
        else:
            await _send_next_only_reply_keyboard(bot, chat_id, state, lang)
    else:
        await _delete_rate_reply_keyboard(bot, chat_id, state)


async def _show_rate_block_banner(
    *,
    bot,
    chat_id: int,
    state: FSMContext,
    text: str,
) -> None:
    """Показать блокировку оценок через баннер и скрыть reply-клавиатуру."""
    data = await state.get_data()
    if data.get("rate_kb_mode") == "block":
        return
    old_msg_id = data.get("rate_kb_msg_id")
    if old_msg_id is None:
        try:
            ui_state = await get_user_ui_state(chat_id)
            old_msg_id = ui_state.get("rate_kb_msg_id")
        except Exception:
            pass

    banner_id = None
    try:
        banner_id = await ensure_giraffe_banner(
            bot,
            chat_id,
            chat_id,
            text=f"🦒\n\n{text}",
            reply_markup=ReplyKeyboardRemove(),
            force_new=False,
        )
    except Exception:
        banner_id = None

    if banner_id:
        data["rate_kb_msg_id"] = None
        data["rate_kb_mode"] = "block"
        await state.set_data(data)
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=int(old_msg_id))
            except Exception:
                pass
        try:
            tmp = await bot.send_message(
                chat_id=chat_id,
                text=".",
                reply_markup=ReplyKeyboardRemove(),
                disable_notification=True,
            )
            try:
                await bot.delete_message(chat_id=chat_id, message_id=tmp.message_id)
            except Exception:
                pass
        except Exception:
            pass
        try:
            await set_user_rate_kb_msg_id(chat_id, None)
        except Exception:
            pass
    else:
        await _delete_rate_reply_keyboard(bot, chat_id, state)


async def _safe_create_comment(
    *,
    user_id: int,
    photo_id: int,
    text: str,
    is_public: bool,
    chat_id: int | None,
    tg_user_id: int | None,
    handler: str,
) -> bool:
    try:
        await create_comment(
            user_id=int(user_id),
            photo_id=int(photo_id),
            text=text,
            is_public=bool(is_public),
        )
        return True
    except Exception as e:
        try:
            await log_bot_error(
                chat_id=int(chat_id) if chat_id is not None else 0,
                tg_user_id=int(tg_user_id) if tg_user_id is not None else 0,
                handler=handler,
                update_type="comment",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass
        return False
async def _apply_rating_card(
    *,
    bot,
    chat_id: int,
    message: Message | None,
    message_id: int | None,
    card: RatingCard,
    state: FSMContext | None = None,
) -> None:
    """Аккуратно применяет карточку оценивания к существующему сообщению или отправляет новое при ошибке."""
    if card.photo_file_id is None:
        # Текстовая карточка (нет фото). Если предыдущее сообщение было с фото — удаляем его, чтобы не оставалась картинка.
        if message is not None and message.photo:
            try:
                await message.delete()
            except Exception:
                pass
            message = None

        if message_id is not None and (message is None or message.photo):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            message_id = None

        # Если есть текстовое сообщение без фото — пробуем отредактировать, иначе шлём новое.
        if message is not None and not message.photo:
            try:
                await message.edit_text(
                    text=card.caption,
                    reply_markup=card.keyboard,
                    parse_mode="HTML",
                )
                if state is not None:
                    data = await state.get_data()
                    data["rate_msg_id"] = message.message_id
                    await state.set_data(data)
                try:
                    await set_user_screen_msg_id(chat_id, message.message_id)
                except Exception:
                    pass
                return
            except Exception:
                message = None

        if message_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=card.caption,
                    reply_markup=card.keyboard,
                    parse_mode="HTML",
                )
                if state is not None:
                    data = await state.get_data()
                    data["rate_msg_id"] = message_id
                    await state.set_data(data)
                try:
                    await set_user_screen_msg_id(chat_id, message_id)
                except Exception:
                    pass
                return
            except Exception:
                message_id = None
        prev_id = None
        if state is not None:
            try:
                prev_id = (await state.get_data()).get("rate_msg_id")
            except Exception:
                prev_id = None

        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=card.caption,
                reply_markup=card.keyboard,
                parse_mode="HTML",
                disable_notification=True,
            )
            if state is not None:
                data = await state.get_data()
                data["rate_msg_id"] = sent.message_id
                await state.set_data(data)
            try:
                await set_user_screen_msg_id(chat_id, sent.message_id)
            except Exception:
                pass
            if prev_id and prev_id != sent.message_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=prev_id)
                except Exception:
                    pass
        except Exception:
            pass
        return

    media = InputMediaPhoto(
        media=card.photo_file_id,
        caption=card.caption,
        parse_mode="HTML",
        show_caption_above_media=True,
    )

    if message is not None and message.photo:
        try:
            await message.edit_media(media=media, reply_markup=card.keyboard)
            if state is not None:
                data = await state.get_data()
                data["rate_msg_id"] = message.message_id
                await state.set_data(data)
            try:
                await set_user_screen_msg_id(chat_id, message.message_id)
            except Exception:
                pass
            return
        except Exception:
            try:
                await message.delete()
            except Exception:
                pass

    if message_id is not None:
        try:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=card.keyboard,
            )
            if state is not None:
                data = await state.get_data()
                data["rate_msg_id"] = message_id
                await state.set_data(data)
            try:
                await set_user_screen_msg_id(chat_id, message_id)
            except Exception:
                pass
            return
        except Exception:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

    prev_id = None
    if state is not None:
        try:
            prev_id = (await state.get_data()).get("rate_msg_id")
        except Exception:
            prev_id = None

    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=card.photo_file_id,
            caption=card.caption,
            reply_markup=card.keyboard,
            parse_mode="HTML",
            disable_notification=True,
            show_caption_above_media=True,
        )
        if state is not None:
            data = await state.get_data()
            data["rate_msg_id"] = sent.message_id
            await state.set_data(data)
        try:
            await set_user_screen_msg_id(chat_id, sent.message_id)
        except Exception:
            pass
        if prev_id and prev_id != sent.message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=prev_id)
            except Exception:
                pass
    except Exception:
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=card.caption,
                reply_markup=card.keyboard,
                parse_mode="HTML",
                disable_notification=True,
            )
            try:
                await set_user_screen_msg_id(chat_id, sent.message_id)
            except Exception:
                pass
        except Exception:
            pass


async def _edit_rate_message(
    message: Message,
    *,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
    show_caption_above_media: bool = True,
) -> None:
    """Редактирование карточки оценивания с сохранением подписи над фото."""
    if message.photo:
        media = InputMediaPhoto(
            media=message.photo[-1].file_id,
            caption=caption,
            parse_mode="HTML",
            show_caption_above_media=show_caption_above_media,
        )
        try:
            await message.edit_media(media=media, reply_markup=reply_markup)
            return
        except Exception:
            pass
        try:
            await message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        await message.edit_text(caption, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        pass


def build_rate_keyboard(
    photo_id: int,
    *,
    is_premium: bool = False,
    show_details: bool = False,
    more_only: bool = False,
    link_button: tuple[str, str] | None = None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if link_button and not more_only:
        link_text, link_url = link_button
        rows.append([InlineKeyboardButton(text=link_text, url=link_url)])

    if not more_only:
        rows.append(
            [
                InlineKeyboardButton(text=t("rate.btn.comment", lang), callback_data=f"rate:comment:{photo_id}"),
                InlineKeyboardButton(text=t("rate.btn.report", lang), callback_data=f"rate:report:{photo_id}"),
            ]
        )

    if show_details and is_premium and not more_only:
        rows.append(
            [
                InlineKeyboardButton(text="💥+15", callback_data=f"rate:super:{photo_id}"),
                InlineKeyboardButton(text=t("rate.btn.award", lang), callback_data=f"rate:award:{photo_id}"),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"),
            InlineKeyboardButton(
                text=(t("rate.btn.hide", lang) if show_details else t("rate.btn.more", lang)),
                callback_data=f"rate:more:{photo_id}:{1 if not show_details else 0}",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_view_only_keyboard(
    photo_id: int,
    *,
    show_details: bool = False,
    is_premium: bool = False,
    more_only: bool = False,
    link_button: tuple[str, str] | None = None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if link_button and not more_only:
        link_text, link_url = link_button
        rows.append([InlineKeyboardButton(text=link_text, url=link_url)])

    if not more_only:
        rows.append(
            [
                InlineKeyboardButton(text=t("rate.btn.comment", lang), callback_data=f"rate:comment:{photo_id}"),
                InlineKeyboardButton(text=t("rate.btn.report", lang), callback_data=f"rate:report:{photo_id}"),
            ]
        )

    if show_details and is_premium and not more_only:
        rows.append([InlineKeyboardButton(text=t("rate.btn.award", lang), callback_data=f"rate:award:{photo_id}")])

    rows.append(
        [
            InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back"),
            InlineKeyboardButton(
                text=(t("rate.btn.hide", lang) if show_details else t("rate.btn.more", lang)),
                callback_data=f"rate:more:{photo_id}:{0 if show_details else 1}",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_comment_notification_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура для уведомления об отзыве:
    используем общий хелпер с кнопкой «Просмотрено».
    """
    return build_viewed_kb(callback_data="comment:seen")


def build_referral_thanks_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопка «Спасибо!» для реферальных уведомлений.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Спасибо!", callback_data="ref:thanks")]
        ]
    )


# --- Дополнительные клавиатуры и тексты для приглашения друзей и окончания фотографий ---
BOT_INVITE_LINK = "https://t.me/glowshotbot"


def build_no_photos_keyboard(*, lang: str = "ru") -> InlineKeyboardMarkup:
    """Клавиатура, когда фотографии для оценивания закончились."""
    share_url = f"https://t.me/share/url?url={BOT_INVITE_LINK}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("rate.btn.invite", lang), url=share_url)],
            [InlineKeyboardButton(text=t("common.menu", lang), callback_data="menu:back")],
        ]
    )


def build_no_photos_text() -> str:
    return (
        "Фотографии на сегодня закончились 😭\n\n"
        "Но есть решение: хочешь больше фоток для оценивания — зови друзей. "
        "Чем больше людей, тем живее лента 🦒\n\n"
        f"Вот ссылка на бота:\n<code>{BOT_INVITE_LINK}</code>\n\n"
        "Либо воспользуйся своей реферальной ссылкой: /ref"
    )


# Специальная подпись для раздела оценивания
async def build_rate_caption(photo: dict, viewer_tg_id: int, show_details: bool = False) -> str:
    """Карточка оценивания:
    💎 <b><code>Название</code></b>
    [бейдж тега] · Имя · #1/2 (если две активные)
    Описание (кратко / полностью по «Еще»)
    Реклама (только непремиум зрителям)
    При «Еще» — детали; для админов/модов включаем username и дату публикации."""

    def quote(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        return f"<blockquote>{escape(text)}</blockquote>"

    title = (photo.get("title") or "").strip() or "Без названия"

    # author tg_id
    author_tg_id = None
    try:
        author_user_id = int(photo.get("user_id") or 0)
    except Exception:
        author_user_id = 0

    author = None
    if author_user_id:
        try:
            author = await get_user_by_id(author_user_id)
        except Exception:
            author = None
        if author and author.get("tg_id"):
            try:
                author_tg_id = int(author.get("tg_id"))
            except Exception:
                author_tg_id = None
        if author and not photo.get("user_tg_channel_link"):
            if author.get("tg_channel_link"):
                photo["user_tg_channel_link"] = author.get("tg_channel_link")

    is_author_premium = await _ensure_author_premium_active(photo, author)

    # имя автора (без @username)
    display_name = (author.get("name") if author else "") or ""
    display_name = display_name.strip()
    if display_name.startswith("@"):
        display_name = display_name.lstrip("@").strip()
    if not display_name:
        display_name = "Автор"
    username = author.get("username") if author else None

    lines: list[str] = []
    # Метка 1/2, если у автора две активные фото
    photo_index_text = ""
    if author_user_id:
        try:
            active_photos = await get_active_photos_for_user(int(author_user_id))
            if len(active_photos) == 2:
                # сортируем по created_at, старое первое
                try:
                    active_photos = sorted(active_photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
                except Exception:
                    pass
                ids = [int(p.get("id") or 0) for p in active_photos]
                if int(photo.get("id") or 0) in ids:
                    idx = ids.index(int(photo.get("id") or 0))
                    photo_index_text = f"#{idx + 1}/2"
        except Exception:
            pass

    premium_badge = "💎 " if is_author_premium else ""
    title_mono = f"«<b><code>{escape(title)}</code></b>»"
    verified_badge = ""
    try:
        if author and bool(author.get("is_author")):
            verified_badge = " ✅ Автор подтвержден"
    except Exception:
        verified_badge = ""

    if show_details:
        # Показ "Еще": скрываем название/параметры, оставляем только детали.
        if bool(photo.get("has_beta_award")):
            lines.append("··· Бета-тестер бота ···")

        rating_str = "—"
        good_cnt = 0
        bad_cnt = 0
        rated_users = 0
        ratings_total = 0
        published = photo.get("day_key") or ""
        if published:
            try:
                pd = datetime.fromisoformat(published.split("T")[0])
                published = pd.strftime("%d.%m.%Y")
            except Exception:
                published = published

        try:
            ps = await get_photo_stats(int(photo["id"]))
            v = ps.get("bayes_score")
            ratings_total = int(ps.get("ratings_count") or 0)
            rated_users = int(ps.get("rated_users") or 0)
            if v is not None:
                rating_str = f"{float(v):.2f}".rstrip("0").rstrip(".")
        except Exception:
            pass

        try:
            from database import get_photo_ratings_stats  # type: ignore
            rs = await get_photo_ratings_stats(int(photo["id"]))
            good_cnt = int(rs.get("good_count") or 0)
            bad_cnt = int(rs.get("bad_count") or 0)
        except Exception:
            pass

        admin_extras: list[str] = []
        try:
            viewer = await get_user_by_tg_id(int(viewer_tg_id))
            if viewer and (viewer.get("is_admin") or viewer.get("is_moderator")):
                if username:
                    admin_extras.append(f"Аккаунт автора: @{username}")
                created_at = photo.get("created_at") or ""
                if published:
                    admin_extras.append(f"Дата публикации: {published}")
                if created_at:
                    admin_extras.append(f"created_at: {created_at}")
        except Exception:
            pass

        details_lines = [
            "📊 Детали:",
            f"Рейтинг: {rating_str}",
            f"Оценок всего: {ratings_total}",
            f"Уникальных оценщиков: {rated_users}",
            f"6–10: {good_cnt} • 1–5: {bad_cnt}",
            f"Дата публикации: {published or '—'}",
        ] + admin_extras

        lines.append(quote("\n".join(details_lines)))
        return "\n".join(lines)

    # Обычный режим: название и параметры
    lines.append(f"{premium_badge}{title_mono}{verified_badge}")

    tag_badge = _tag_badge(str(photo.get("tag") or ""))
    device_icon = _device_emoji(photo.get("device_type") or photo.get("device_info") or "")

    second_parts = [f"[{escape(tag_badge)}]", escape(display_name)]
    if device_icon:
        second_parts.append(device_icon)
    if photo_index_text:
        second_parts.append(photo_index_text)
    lines.append(" · ".join(second_parts))

    # описание из био автора (а не из фото)
    description = ""
    if author:
        description = (author.get("bio") or "").strip()
    description = description.strip()
    if description and description.lower() == "нет":
        description = ""
    if description:
        desc_text = _shorten_text(description)
        lines.append("")
        lines.append(f"📜 {escape(desc_text)}")

    # --- Реклама под описанием ---
    # Реклама: по умолчанию включена, но премиум может выключить в настройках
    viewer_is_premium = False
    try:
        viewer_is_premium = await is_user_premium_active(int(viewer_tg_id))
    except Exception:
        viewer_is_premium = False

    ads_enabled = None
    try:
        ads_enabled = await get_ads_enabled_by_tg_id(int(viewer_tg_id))
    except Exception:
        ads_enabled = None

    # если пользователь не задавал — по умолчанию: у премиум выкл, у остальных вкл
    if ads_enabled is None:
        ads_enabled = not viewer_is_premium

    ad_lines: list[str] = []
    if ads_enabled:
        try:
            ad = await get_random_active_ad()
        except Exception:
            ad = None
        if ad:
            ad_title = (ad.get("title") or "").strip()
            ad_body = (ad.get("body") or "").strip()
            if ad_title or ad_body:
                ad_lines.append("••• реклама •••")
                if ad_title:
                    ad_lines.append(f"<b>{escape(ad_title)}</b>")
                if ad_body:
                    ad_lines.append(quote(ad_body))

    if ad_lines:
        lines.append("")
        lines.extend(ad_lines)

    return "\n".join(lines)

async def show_next_photo_for_rating(
    callback: CallbackQuery | Message,
    user_id: int,
    *,
    replace_message: bool = False,
    state: FSMContext | None = None,
) -> None:
    """
    Показать следующую фотографию для оценивания, стараясь переиспользовать текущее сообщение.

    • Если фотографий нет — меняем текст/подпись текущего сообщения.
    • Если фотография есть:
      – если текущее сообщение уже с фото — меняем медиа;
      – если текущее сообщение текстовое — удаляем его и отправляем новое с фото.
    """
    is_cb = isinstance(callback, CallbackQuery)
    if is_cb:
        if await _deny_if_full_banned(callback=callback):
            return
        bot = callback.message.bot
        chat_id = callback.message.chat.id
        msg = None if replace_message else callback.message
        msg_id = None if replace_message else callback.message.message_id
        old_msg = callback.message if replace_message else None
        viewer_tg_id = int(callback.from_user.id)
    else:
        if await _deny_if_full_banned(message=callback):
            return
        bot = callback.bot
        chat_id = callback.chat.id
        msg = None
        msg_id = None
        old_msg = None
        viewer_tg_id = int(callback.from_user.id)
        if state is not None:
            try:
                data = await state.get_data()
                msg_id = data.get("rate_msg_id")
                if msg_id is None:
                    ui_state = await get_user_ui_state(viewer_tg_id)
                    msg_id = ui_state.get("rate_msg_id") or ui_state.get("screen_msg_id")
            except Exception:
                msg_id = None

    if msg is None and msg_id is None:
        try:
            await ensure_giraffe_banner(bot, chat_id, viewer_tg_id)
        except Exception:
            pass

    viewer = await get_user_by_tg_id(viewer_tg_id)
    lang = _lang(viewer)
    card = await _build_next_rating_card(user_id, viewer_tg_id=viewer_tg_id)

    if state is not None:
        data = await state.get_data()
        data["rate_current_photo_id"] = card.photo["id"] if card.photo else None
        data["rate_show_details"] = False
        await state.set_data(data)

    await _apply_rating_card(
        bot=bot,
        chat_id=chat_id,
        message=msg,
        message_id=msg_id,
        card=card,
        state=state,
    )

    if state is not None:
        if card.photo:
            if bool(card.photo.get("ratings_enabled", True)):
                await _send_rate_reply_keyboard(bot, chat_id, state, lang)
            else:
                await _send_next_only_reply_keyboard(bot, chat_id, state, lang)
        else:
            await _delete_rate_reply_keyboard(bot, chat_id, state)

    if old_msg is not None:
        try:
            await old_msg.delete()
        except Exception:
            pass

    if is_cb:
        try:
            await callback.answer()
        except Exception:
            pass


async def _show_rate_tutorial(callback: CallbackQuery | Message, state: FSMContext) -> None:
    is_cb = isinstance(callback, CallbackQuery)
    if is_cb:
        bot = callback.message.bot
        chat_id = callback.message.chat.id
        old_msg = callback.message
        user_id = int(callback.from_user.id)
    else:
        bot = callback.bot
        chat_id = callback.chat.id
        old_msg = None
        user_id = int(callback.from_user.id)

    try:
        await ensure_giraffe_banner(bot, chat_id, user_id)
    except Exception:
        pass

    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=RATE_TUTORIAL_PHOTO_FILE_ID,
            caption=RATE_TUTORIAL_CAPTION,
            reply_markup=_build_rate_tutorial_inline_kb(),
            parse_mode="HTML",
            disable_notification=True,
            show_caption_above_media=True,
        )
    except Exception:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=RATE_TUTORIAL_CAPTION,
            reply_markup=_build_rate_tutorial_inline_kb(),
            parse_mode="HTML",
            disable_notification=True,
        )

    data = await state.get_data()
    data["rate_tutorial_msg_id"] = sent.message_id
    await state.set_data(data)
    try:
        await set_user_screen_msg_id(user_id, sent.message_id)
    except Exception:
        pass

    await _send_tutorial_reply_keyboard(bot, chat_id, state, _lang(await get_user_by_tg_id(user_id)))

    if old_msg is not None:
        try:
            await old_msg.delete()
        except Exception:
            pass

    if is_cb:
        try:
            await callback.answer()
        except Exception:
            pass


@router.callback_query(F.data == "rate:tutor:noop")
async def rate_tutorial_noop(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("rate:comment:"))
async def rate_comment(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_full_banned(callback=callback):
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Странный комментарий, не понял.", show_alert=True)
        return

    _, _, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странный комментарий, не понял.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    try:
        if user.get("id") and await has_user_commented(int(photo_id), int(user["id"])):
            await callback.answer("Ты уже оставил комментарий к этому фото.", show_alert=True)
            return
    except Exception:
        pass

    # Проверяем, есть ли у пользователя активный премиум
    is_premium = False
    try:
        if user.get("tg_id"):
            is_premium = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium = False

    buttons_row = [
        InlineKeyboardButton(
            text="🙋‍♂ Публично", callback_data=f"rate:comment_mode:public:{photo_id}"
        ),
    ]
    caption_lines = [
        "Как оставить комментарий?\n",
        "• <b>Публично</b> — с твоим именем/юзернеймом.",
    ]

    if is_premium:
        # Премиум-пользователю даём и анонимный вариант
        buttons_row.append(
            InlineKeyboardButton(
                text="🕵 Анонимно", callback_data=f"rate:comment_mode:anon:{photo_id}"
            )
        )
        caption_lines.append("• <b>Анонимно</b> — без указания автора.")
    else:
        # Без премиума — только публичные комментарии
        caption_lines.append(
            "\nАнонимные комментарии доступны только с GlowShot Premium 💎."
        )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            buttons_row,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")],
        ]
    )

    await callback.message.edit_caption(
        caption="\n".join(caption_lines),
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:comment_mode:"))
async def rate_comment_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_full_banned(callback=callback):
        return
    """Пользователь выбрал режим комментария (публичный / анонимный)."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Странные параметры комментария.", show_alert=True)
        return

    _, _, mode, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странные параметры комментария.", show_alert=True)
        return

    is_public = mode == "public"

    # Если пользователь пытается выбрать анонимный режим без премиума — не даём
    if not is_public:
        user = await get_user_by_tg_id(callback.from_user.id)
        if user is None:
            await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
            return
        is_premium = False
        try:
            if user.get("tg_id"):
                is_premium = await is_user_premium_active(user["tg_id"])
        except Exception:
            is_premium = False

        if not is_premium:
            await callback.answer(
                "Анонимные комментарии доступны только с GlowShot Premium 💎.",
                show_alert=True,
            )
            return

    await state.set_state(RateStates.waiting_comment_text)
    await state.update_data(
        photo_id=photo_id,
        is_public=is_public,
        rate_msg_id=callback.message.message_id,
        rate_chat_id=callback.message.chat.id,
    )

    await callback.message.edit_caption(
        caption=(
            "Напиши текст комментария к этой фотографии.\n\n"
            "Он появится под работой автора."
        ),
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:report:"))
async def rate_report(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_full_banned(callback=callback):
        return
    parts = callback.data.split(":")
    # ['rate', 'report', '<photo_id>']
    if len(parts) != 3:
        await callback.answer("Странная жалоба, не понял.", show_alert=True)
        return

    _, _, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странная жалоба, не понял.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    limit_status = await _get_report_rate_limit_status(int(user["id"]))
    if limit_status is not None and not limit_status.allowed:
        wait_text = _format_report_cooldown(limit_status.retry_after_seconds)
        await callback.answer(
            f"🚫 Лимит жалоб исчерпан.\nСледующую жалобу можно отправить через {wait_text}.",
            show_alert=True,
        )
        return

    reasons = get_report_reasons()
    builder = InlineKeyboardBuilder()
    for reason in reasons:
        builder.button(
            text=REPORT_REASON_LABELS[reason],
            callback_data=f"rate:report_reason:{reason}:{photo_id}",
        )
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back"))
    kb = builder.as_markup()

    await callback.message.edit_caption(
        caption=(
            "Выбери причину жалобы на эту фотографию.\n\n"
            "После выбора мы попросим описать, что именно не так."
        ),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:report_reason:"))
async def rate_report_reason(callback: CallbackQuery, state: FSMContext) -> None:
    if await _deny_if_full_banned(callback=callback):
        return
    """Пользователь выбрал причину жалобы."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Странные параметры жалобы.", show_alert=True)
        return

    _, _, reason_code, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странные параметры жалобы.", show_alert=True)
        return

    if reason_code not in get_report_reasons():
        await callback.answer("Неизвестная причина жалобы.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    await state.set_state(RateStates.waiting_report_text)
    await state.update_data(
        report_photo_id=photo_id,
        report_msg_id=callback.message.message_id,
        report_chat_id=callback.message.chat.id,
        report_reason=reason_code,
    )

    await callback.message.edit_caption(
        caption=(
            "Опиши, что не так с этой фотографией.\n\n"
            "Твой текст увидят модераторы."
        ),
        reply_markup=None,
    )
    await callback.answer()


@router.message(RateStates.waiting_comment_text)
async def rate_comment_text(message: Message, state: FSMContext) -> None:
    if await _deny_if_full_banned(message=message):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    photo_id = data.get("photo_id")
    rate_msg_id = data.get("rate_msg_id")
    rate_chat_id = data.get("rate_chat_id")
    is_public = bool(data.get("is_public", True))

    if photo_id is None or rate_msg_id is None or rate_chat_id is None:
        await state.clear()
        await message.delete()
        return

    text = (message.text or "").strip()

    # Пустой комментарий
    if not text:
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=rate_chat_id,
                message_id=rate_msg_id,
                caption="Комментарий не может быть пустым.\n\nНапиши текст комментария.",
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                raise
        return
    if has_links_or_usernames(text) or has_promo_channel_invite(text):
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=rate_chat_id,
                message_id=rate_msg_id,
                caption=(
                    "В комментариях нельзя оставлять @username, ссылки на Telegram, соцсети или сайты, "
                    "а также рекламировать каналы.\n\n"
                    "Напиши комментарий по самой фотографии <b>без контактов</b>."
                ),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                raise
        return

    await message.delete()

    # выходим из ожидания текста комментария, но сохраняем data
    try:
        await state.set_state(None)
    except Exception:
        pass

    user_for_rate = await get_user_by_tg_id(message.from_user.id)
    if user_for_rate is None or not user_for_rate.get("id"):
        return

    photo_for_caption = None
    try:
        photo_for_caption = await get_photo_by_id(int(photo_id))
    except Exception:
        photo_for_caption = None

    is_rateable = bool(photo_for_caption and photo_for_caption.get("ratings_enabled", True))

    # Если фото можно оценивать — комментарий сохраняем в FSM и отправим только после оценки
    if is_rateable:
        await state.update_data(comment_text=text, comment_saved=False)
        prefix = "✅ Комментарий сохранён!\n\nПоставь оценку 1–10, чтобы отправить."

        card = await _build_rating_card_for_photo(
            int(photo_id),
            int(user_for_rate["id"]),
            int(message.from_user.id),
            prefix=prefix,
        )
        if card is not None:
            await _apply_rating_card(
                bot=message.bot,
                chat_id=rate_chat_id,
                message=None,
                message_id=rate_msg_id,
                card=card,
            )
        else:
            try:
                await message.bot.edit_message_text(
                    chat_id=rate_chat_id,
                    message_id=rate_msg_id,
                    text=prefix,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # клавиатура оценок должна быть в чате
        try:
            await _send_rate_reply_keyboard(message.bot, rate_chat_id, state, _lang(user_for_rate))
        except Exception:
            pass
        return

    # Если фото не для оценивания — сохраняем комментарий сразу
    saved = False
    save_error: Exception | None = None
    try:
        if await has_user_commented(int(photo_id), int(user_for_rate["id"])):
            prefix = "ℹ️ Ты уже оставил комментарий к этому фото."
            card = await _build_rating_card_for_photo(
                int(photo_id),
                int(user_for_rate["id"]),
                int(message.from_user.id),
                prefix=prefix,
            )
            if card is not None:
                await _apply_rating_card(
                    bot=message.bot,
                    chat_id=rate_chat_id,
                    message=None,
                    message_id=rate_msg_id,
                    card=card,
                )
            else:
                try:
                    await message.bot.edit_message_text(
                        chat_id=rate_chat_id,
                        message_id=rate_msg_id,
                        text=prefix,
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            try:
                await _send_next_only_reply_keyboard(message.bot, rate_chat_id, state, _lang(user_for_rate))
            except Exception:
                pass
            return
    except Exception:
        pass

    try:
        await create_comment(
            user_id=int(user_for_rate["id"]),
            photo_id=int(photo_id),
            text=text,
            is_public=bool(is_public),
        )
        saved = True
        await state.update_data(comment_saved=True, comment_text=None)
    except Exception as e:
        save_error = e
        try:
            await log_bot_error(
                chat_id=message.chat.id,
                tg_user_id=message.from_user.id,
                handler="rate_comment_text:create_comment",
                update_type="comment",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass

    if not saved:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
        )
        err_txt = "Не удалось сохранить комментарий. Попробуй ещё раз чуть позже."
        if save_error is not None:
            err_txt += f"\n\nПричина: {type(save_error).__name__}: {save_error}"
        await message.bot.edit_message_caption(
            chat_id=rate_chat_id,
            message_id=rate_msg_id,
            caption=err_txt,
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    # Notify author about comment (без оценки)
    try:
        photo = photo_for_caption
        if photo is None:
            photo = await get_photo_by_id(int(photo_id))
    except Exception:
        photo = None

    if photo is not None:
        author_user_id = photo.get("user_id")
        if author_user_id and author_user_id != user_for_rate.get("id"):
            try:
                author = await get_user_by_id(int(author_user_id))
            except Exception:
                author = None

            if author is not None and author.get("tg_id"):
                try:
                    prefs = await get_notify_settings_by_tg_id(int(author["tg_id"]))
                except Exception:
                    prefs = {"comments_enabled": True}

                if bool(prefs.get("comments_enabled", True)):
                    mode_label = "публичный" if is_public else "анонимный"
                    try:
                        await message.bot.send_message(
                            chat_id=int(author["tg_id"]),
                            text=(
                                f"🔔 <b>Новый {mode_label} комментарий к вашей фотографии</b>\n"
                                f"Текст: {text}"
                            ),
                            reply_markup=build_comment_notification_keyboard(),
                            parse_mode="HTML",
                            disable_notification=True,
                        )
                    except Exception:
                        pass

    rater_lang = _lang(user_for_rate)
    try:
        prefix = "✅ Комментарий отправлен!"
        card = await _build_rating_card_for_photo(
            int(photo_id),
            int(user_for_rate["id"]),
            int(message.from_user.id),
            prefix=prefix,
        )
        if card is not None:
            await _apply_rating_card(
                bot=message.bot,
                chat_id=rate_chat_id,
                message=None,
                message_id=rate_msg_id,
                card=card,
            )
        else:
            try:
                await message.bot.edit_message_text(
                    chat_id=rate_chat_id,
                    message_id=rate_msg_id,
                    text=prefix,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        try:
            await _send_next_only_reply_keyboard(message.bot, rate_chat_id, state, rater_lang)
        except Exception:
            pass
    except TelegramBadRequest:
        pass

    return

@router.message(RateStates.waiting_report_text)
async def rate_report_text(message: Message, state: FSMContext) -> None:
    if await _deny_if_full_banned(message=message):
        await _clear_rate_report_draft(state)
        try:
            await message.delete()
        except Exception:
            pass
        return
    data = await state.get_data()
    photo_id = data.get("report_photo_id")
    report_msg_id = data.get("report_msg_id")
    report_chat_id = data.get("report_chat_id")
    report_reason = data.get("report_reason") or "other"

    # from database import get_photo_by_id, get_moderators  # локальный импорт, чтобы избежать циклов

    if photo_id is None or report_msg_id is None or report_chat_id is None:
        await _clear_rate_report_draft(state)
        await message.delete()
        return

    text = (message.text or "").strip()

    # Пустая жалоба
    if not text:
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=report_chat_id,
                message_id=report_msg_id,
                caption=(
                    "Текст жалобы не может быть пустым.\n\n"
                    "Опиши, что не так с этой фотографией."
                ),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                # Просто игнорируем, если текст тот же самый
                pass
            else:
                raise
        return
    if has_links_or_usernames(text) or has_promo_channel_invite(text):
        await message.delete()
        try:
            await message.bot.edit_message_caption(
                chat_id=report_chat_id,
                message_id=report_msg_id,
                caption=(
                    "В тексте жалобы нельзя оставлять @username, ссылки на Telegram, соцсети или сайты, "
                    "а также рекламировать каналы.\n\n"
                    "Опиши словами, что именно не так с этой фотографией <b>без контактов</b>."
                ),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass
            else:
                raise
        return

    try:
        user = await get_user_by_tg_id(message.from_user.id)
    except Exception as e:
        # If DB fails, don't leave the user hanging
        try:
            await log_bot_error(
                chat_id=message.chat.id,
                tg_user_id=message.from_user.id,
                handler="rate_report_text:get_user_by_tg_id",
                update_type="report",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass
        await _clear_rate_report_draft(state)
        return

    # Remove the user's message to keep the chat clean (expected behavior)
    try:
        await message.delete()
    except Exception:
        pass

    if user is None:
        await _clear_rate_report_draft(state)
        return

    limit_status = await _get_report_rate_limit_status(int(user["id"]))
    if limit_status is not None and not limit_status.allowed:
        wait_text = _format_report_cooldown(limit_status.retry_after_seconds)
        prefix = (
            "🚫 Лимит жалоб исчерпан.\n"
            f"Следующую жалобу можно отправить через {wait_text}."
        )

        card = await _build_rating_card_for_photo(
            int(photo_id),
            int(user["id"]),
            int(message.from_user.id),
            prefix=prefix,
        )

        if card is not None:
            await _apply_rating_card(
                bot=message.bot,
                chat_id=report_chat_id,
                message=None,
                message_id=report_msg_id,
                card=card,
            )
        else:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
            )
            try:
                await message.bot.edit_message_text(
                    chat_id=report_chat_id,
                    message_id=report_msg_id,
                    text=prefix,
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await message.bot.send_message(
                        chat_id=report_chat_id,
                        text=prefix,
                        reply_markup=kb,
                        parse_mode="HTML",
                        disable_notification=True,
                    )
                except Exception:
                    pass

        await _clear_rate_report_draft(state)
        return

    reason_code = report_reason

    # Create report + compute stats safely. If anything fails, show an error to the user instead of silence.
    try:
        await create_photo_report(
            int(user["id"]),
            int(photo_id),
            str(reason_code),
            str(text) if text else None,
        )

        try:
            stats_dict = await get_photo_report_stats(int(photo_id))
        except Exception:
            stats_dict = None

        if not isinstance(stats_dict, dict):
            stats_dict = {"total_pending": 0, "total_all": 0}

    except Exception as e:
        # Log and notify the reporter (edit the original report prompt message)
        try:
            await log_bot_error(
                chat_id=message.chat.id,
                tg_user_id=message.from_user.id,
                handler="rate_report_text:create_photo_report",
                update_type="report",
                error_type=type(e).__name__,
                error_text=str(e),
                traceback_text=traceback.format_exc(),
            )
        except Exception:
            pass

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="rate:back")]]
        )
        try:
            await message.bot.edit_message_caption(
                chat_id=report_chat_id,
                message_id=report_msg_id,
                caption=(
                    "⚠️ Не удалось отправить жалобу. Попробуй ещё раз чуть позже.\n\n"
                    "Если проблема повторяется — напиши админу."
                ),
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            # As a fallback, send a message (rare case when prompt msg can't be edited)
            try:
                await message.bot.send_message(
                    chat_id=report_chat_id,
                    text="⚠️ Не удалось отправить жалобу. Попробуй позже.",
                    reply_markup=kb,
                    disable_notification=True,
                )
            except Exception:
                pass

        await _clear_rate_report_draft(state)
        return


    # Always mark photo under review after the first report
    try:
        await set_photo_moderation_status(int(photo_id), "under_review")
    except Exception:
        pass

    # Send/update ONE moderation card in the moderation chat (group) if configured
    if MODERATION_CHAT_ID:
        try:
            photo_for_mod = await get_photo_by_id(int(photo_id))
        except Exception:
            photo_for_mod = None

        if photo_for_mod is not None:
            caption = await build_mod_report_caption(
                photo=photo_for_mod,
                report_stats=stats_dict,
                last_reason_code=str(reason_code),
                last_comment=str(text),
            )
            kb = build_mod_report_keyboard(int(photo_id))

            mapping = None
            try:
                mapping = await get_moderation_message_for_photo(int(photo_id))
            except Exception:
                mapping = None

            if mapping and int(mapping.get("chat_id") or 0) == int(MODERATION_CHAT_ID):
                # Update existing card
                try:
                    await message.bot.edit_message_caption(
                        chat_id=int(MODERATION_CHAT_ID),
                        message_id=int(mapping["message_id"]),
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    # If editing fails (deleted message, etc.) — send a new card and overwrite mapping
                    try:
                        sent = await message.bot.send_photo(
                            chat_id=int(MODERATION_CHAT_ID),
                            photo=photo_for_mod["file_id"],
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=kb,
                            disable_notification=True,
                        )
                        await upsert_moderation_message_for_photo(
                            int(photo_id),
                            int(MODERATION_CHAT_ID),
                            int(sent.message_id),
                        )
                    except Exception:
                        pass
            else:
                # First time: send a new card
                try:
                    sent = await message.bot.send_photo(
                        chat_id=int(MODERATION_CHAT_ID),
                        photo=photo_for_mod["file_id"],
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb,
                        disable_notification=True,
                    )
                    await upsert_moderation_message_for_photo(
                        int(photo_id),
                        int(MODERATION_CHAT_ID),
                        int(sent.message_id),
                    )
                except Exception:
                    pass
    
    card = await _build_next_rating_card(int(user["id"]), viewer_tg_id=int(message.from_user.id))
    await _apply_rating_card(
        bot=message.bot,
        chat_id=report_chat_id,
        message=None,
        message_id=report_msg_id,
        card=card,
    )

    await _sync_rate_state_for_card(
        state=state,
        card=card,
        bot=message.bot,
        chat_id=report_chat_id,
        lang=_lang(user),
    )

    await _clear_rate_report_draft(state)
    return


# Новый хендлер для супер-оценки
@router.callback_query(F.data.startswith("rate:super:"))
async def rate_super_score(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    # ['rate', 'super', '<photo_id>']
    if len(parts) != 3:
        await callback.answer("Странная супер-оценка, не понял.", show_alert=True)
        return

    _, _, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странная супер-оценка, не понял.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    is_premium = False
    try:
        if user.get("tg_id"):
            is_premium = await is_user_premium_active(user["tg_id"])
    except Exception:
        is_premium = False

    if not is_premium:
        await callback.answer(
            "Супер-оценка доступна только с GlowShot Premium 💎.",
            show_alert=True,
        )
        return

    # Базовая оценка для супер-оценки — 10, а в статистике она станет 15
    value = 10

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фото не найдено.", show_alert=True)
        await show_next_photo_for_rating(callback, user["id"], state=state)
        await _clear_rate_comment_draft(state)
        return

    if not bool(photo.get("ratings_enabled", True)):
        await callback.answer("Автор отключил оценки для этого фото.", show_alert=True)
        try:
            await mark_viewonly_seen(int(user["id"]), int(photo_id))
        except Exception:
            pass
        await show_next_photo_for_rating(callback, user["id"], state=state)
        await _clear_rate_comment_draft(state)
        return

    data = await state.get_data()
    comment_photo_id = data.get("photo_id")
    comment_text = data.get("comment_text")
    is_public = data.get("is_public", True)

    if comment_photo_id == photo_id and comment_text and not data.get("comment_saved"):
        # 1) Сохраняем комментарий
        await _safe_create_comment(
            user_id=int(user["id"]),
            photo_id=int(photo_id),
            text=comment_text,
            is_public=bool(is_public),
            chat_id=callback.message.chat.id if callback.message else None,
            tg_user_id=callback.from_user.id if callback.from_user else None,
            handler="rate_super_score:create_comment",
        )

        # 2) Пытаемся уведомить автора фотографии
        if photo is not None:
            author_user_id = photo.get("user_id")

            # Не шлём уведомление самому себе
            if author_user_id and author_user_id != user["id"]:
                try:
                    author = await get_user_by_id(author_user_id)
                except Exception:
                    author = None

                if author is not None:
                    author_tg_id = author.get("tg_id")
                    if author_tg_id:
                        notify_text_lines = [
                            "🔔 <b>Новый комментарий к вашей фотографии</b>",
                            "",
                            f"Текст: {comment_text}",
                            "Оценка: 10 (супер-оценка)",
                        ]
                        notify_text = "\n".join(notify_text_lines)

                        try:
                            await callback.message.bot.send_message(
                                chat_id=author_tg_id,
                                text=notify_text,
                                reply_markup=build_comment_notification_keyboard(),
                                parse_mode="HTML",
                            )
                        except Exception:
                            # Если не получилось доставить уведомление автору — просто игнорируем.
                            pass

        # 3) Чистим состояние, чтобы не тащить комментарий дальше
        await state.clear()

    # Сохраняем обычную оценку 10
    await add_rating(user["id"], photo_id, value)
    # И помечаем её как супер-оценку (+5 баллов в статистике)
    await set_super_rating(user["id"], photo_id)
    # streak: rating counts as daily activity
    try:
        await streak_record_action_by_tg_id(int(callback.from_user.id), "rate")
    except Exception:
        pass
        # notifications: accumulate likes for daily summary (best-effort)
    try:
        photo_row = photo
        author_user_id = photo_row.get("user_id") if photo_row else None
        if author_user_id and int(author_user_id) != int(user["id"]):
            author = await get_user_by_id(int(author_user_id))
            author_tg = (author or {}).get("tg_id")
            if author_tg:
                prefs = await get_notify_settings_by_tg_id(int(author_tg))
                if bool(prefs.get("likes_enabled", True)):
                    await increment_likes_daily_for_tg_id(int(author_tg), _moscow_day_key(), 1)
    except Exception:
        pass
    # Рефералька: проверяем, не пора ли выдать бонусы
    try:
        rewarded, referrer_tg_id, referee_tg_id = await link_and_reward_referral_if_needed(user["tg_id"])
    except Exception:
        rewarded = False
        referrer_tg_id = None
        referee_tg_id = None

    if rewarded:
        # Пуш тому, кто дал ссылку
        if referrer_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referrer_tg_id,
                    text=(
                        "🤝 <b>Друг выполнил условия реферальной программы!</b>\n\n"
                        "Тебе начислено <b>2 дня GlowShot Премиум</b> за приглашение.\n"
                        "Спасибо, что приводишь к нам людей, которым интересна фотография 📸"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Пуш другу
        if referee_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referee_tg_id,
                    text=(
                        "🎉 <b>Ты выполнил условия реферальной программы!</b>\n\n"
                        "За регистрацию и участие в оценке фотографий тебе начислено "
                        "<b>2 дня GlowShot Премиум</b>.\n"
                        "Продолжай выкладывать свои кадры и оценивать работы других 💎"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await show_next_photo_for_rating(callback, user["id"], state=state)

    await _clear_rate_comment_draft(state)


@router.callback_query(F.data.startswith("rate:score:"))
async def rate_score(callback: CallbackQuery, state: FSMContext) -> None:
    if should_throttle(callback.from_user.id, "rate:score", 0.4):
        try:
            await callback.answer()
        except Exception:
            pass
        return
    """
    Обычная оценка фотографии от 1 до 10.

    Важно:
    • Всегда сохраняем оценку в ratings, даже если не было комментария.
    • Для user_id используем ВНУТРЕННИЙ ID (users.id), а не tg_id.
    • После оценки пробуем засчитать реферальный бонус.
    """
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Странная оценка, не понял.", show_alert=True)
        return

    _, _, pid, val = parts
    try:
        photo_id = int(pid)
        value = int(val)
    except ValueError:
        await callback.answer("Странная оценка, не понял.", show_alert=True)
        return

    if not (1 <= value <= 10):
        await callback.answer("Оценка должна быть от 1 до 10.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return
    blocked, block_msg = await _check_rate_spam_block(user, value=value)
    if blocked:
        try:
            await _show_rate_block_banner(
                bot=callback.message.bot,
                chat_id=callback.message.chat.id,
                state=state,
                text=block_msg or "Сегодня лимит оценок исчерпан. Попробуй позже.",
            )
            await callback.answer()
        except Exception:
            pass
        return

    photo = await get_photo_by_id(photo_id)
    if photo is None or photo.get("is_deleted"):
        await callback.answer("Фото не найдено.", show_alert=True)
        await show_next_photo_for_rating(callback, user["id"], state=state)
        await _clear_rate_comment_draft(state)
        return

    if not bool(photo.get("ratings_enabled", True)):
        await callback.answer("Автор отключил оценки для этого фото.", show_alert=True)
        try:
            await mark_viewonly_seen(int(user["id"]), int(photo_id))
        except Exception:
            pass
        await show_next_photo_for_rating(callback, user["id"], state=state)
        await _clear_rate_comment_draft(state)
        return

    # Достаём возможный комментарий из FSM
    data = await state.get_data()
    comment_photo_id = data.get("photo_id")
    comment_text = data.get("comment_text")
    is_public = data.get("is_public", True)

    # Если к этой же фотке только что писали комментарий — сохраняем его и шлём уведомление автору
    if comment_photo_id == photo_id and comment_text and not data.get("comment_saved"):
        await _safe_create_comment(
            user_id=int(user["id"]),
            photo_id=int(photo_id),
            text=comment_text,
            is_public=bool(is_public),
            chat_id=callback.message.chat.id if callback.message else None,
            tg_user_id=callback.from_user.id if callback.from_user else None,
            handler="rate_score:create_comment",
        )

        if photo is not None:
            author_user_id = photo.get("user_id")

            # Не шлём уведомление самому себе
            if author_user_id and author_user_id != user["id"]:
                try:
                    author = await get_user_by_id(author_user_id)
                except Exception:
                    author = None

                if author is not None:
                    author_tg_id = author.get("tg_id")
                    if author_tg_id:
                        notify_text_lines = [
                            "🔔 <b>Новый комментарий к вашей фотографии</b>",
                            "",
                            f"Текст: {comment_text}",
                            f"Оценка: {value}",
                        ]
                        notify_text = "\n".join(notify_text_lines)

                        try:
                            await callback.message.bot.send_message(
                                chat_id=author_tg_id,
                                text=notify_text,
                                reply_markup=build_comment_notification_keyboard(),
                                parse_mode="HTML",
                            )
                        except Exception:
                            # Если не получилось доставить уведомление автору — просто игнорируем.
                            pass

    # ✅ ВАЖНО: Всегда сохраняем оценку (даже если комментария не было)
    await add_rating(user["id"], photo_id, value)
    # streak: rating counts as daily activity
    try:
        await streak_record_action_by_tg_id(int(callback.from_user.id), "rate")
    except Exception:
        pass
        # notifications: accumulate likes for daily summary (best-effort)
    try:
        author_user_id = photo.get("user_id") if photo else None
        if author_user_id and int(author_user_id) != int(user["id"]):
            author = await get_user_by_id(int(author_user_id))
            author_tg = (author or {}).get("tg_id")
            if author_tg:
                prefs = await get_notify_settings_by_tg_id(int(author_tg))
                if bool(prefs.get("likes_enabled", True)):
                    await increment_likes_daily_for_tg_id(int(author_tg), _moscow_day_key(), 1)
    except Exception:
        pass
    # Рефералка: проверяем, не пора ли выдать бонусы
    try:
        rewarded, referrer_tg_id, referee_tg_id = await link_and_reward_referral_if_needed(user["tg_id"])
    except Exception:
        rewarded = False
        referrer_tg_id = None
        referee_tg_id = None

    if rewarded:
        # Пуш тому, кто дал ссылку
        if referrer_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referrer_tg_id,
                    text=(
                        "🤝 <b>Друг выполнил условия реферальной программы!</b>\n\n"
                        "Тебе начислено <b>2 дня GlowShot Премиум</b> за приглашение.\n"
                        "Спасибо, что приводишь к нам людей, которым интересна фотография 📸"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Пуш другу
        if referee_tg_id:
            try:
                await callback.message.bot.send_message(
                    chat_id=referee_tg_id,
                    text=(
                        "🎉 <b>Ты выполнил условия реферальной программы!</b>\n\n"
                        "За регистрацию и участие в оценке фотографий тебе начислено "
                        "<b>2 дня GlowShot Премиум</b>.\n"
                        "Продолжай выкладывать свои кадры и оценивать работы других 💎"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    # Показываем следующую фотографию
    await show_next_photo_for_rating(callback, user["id"], state=state)

    # Чистим состояние (комментарий больше не нужен)
    await _clear_rate_comment_draft(state)


@router.message(F.text & ~F.text.startswith("/"))
async def rate_score_from_keyboard(message: Message, state: FSMContext) -> None:
    if await _deny_if_full_banned(message=message):
        return
    try:
        if not await require_user_name(message):
            return
    except Exception:
        pass
    text = (message.text or "").strip()
    if text == RATE_TUTORIAL_OK_TEXT:
        if should_throttle(message.from_user.id, "rate:tutorial:ok", 0.6):
            try:
                await message.delete()
            except Exception:
                pass
            return

        user = await get_user_by_tg_id(message.from_user.id)
        if user is None:
            try:
                await message.delete()
            except Exception:
                pass
            return

        try:
            await set_user_rate_tutorial_seen(message.from_user.id, True)
        except Exception:
            pass

        data = await state.get_data()
        tut_msg_id = data.get("rate_tutorial_msg_id")
        if tut_msg_id is None:
            try:
                ui_state = await get_user_ui_state(message.from_user.id)
                tut_msg_id = ui_state.get("screen_msg_id")
            except Exception:
                tut_msg_id = None
        if tut_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=int(tut_msg_id))
            except Exception:
                pass

        try:
            await message.delete()
        except Exception:
            pass

        await show_next_photo_for_rating(message, user["id"], state=state, replace_message=False)
        return
    if text.startswith("/"):
        raise SkipHandler
    if text == t("rate.btn.next", "ru") or text == t("rate.btn.next", "en"):
        data = await state.get_data()
        photo_id = data.get("rate_current_photo_id")
        if not photo_id:
            raise SkipHandler
        if should_throttle(message.from_user.id, "rate:skip", 0.6):
            try:
                await message.delete()
            except Exception:
                pass
            return

        user = await get_user_by_tg_id(message.from_user.id)
        if user is None:
            try:
                await message.delete()
            except Exception:
                pass
            return

        try:
            photo = await get_photo_by_id(int(photo_id))
        except Exception:
            photo = None
        if photo and not bool(photo.get("ratings_enabled", True)):
            try:
                await mark_viewonly_seen(int(user["id"]), int(photo_id))
            except Exception:
                pass

        try:
            await message.delete()
        except Exception:
            pass

        await show_next_photo_for_rating(message, user["id"], state=state, replace_message=False)
        return

    if not text.isdigit():
        raise SkipHandler
    value = int(text)
    if not (1 <= value <= 10):
        raise SkipHandler

    data = await state.get_data()
    photo_id = data.get("rate_current_photo_id")
    if not photo_id:
        raise SkipHandler

    if should_throttle(message.from_user.id, "rate:score", 0.4):
        try:
            await message.delete()
        except Exception:
            pass
        return

    user = await get_user_by_tg_id(message.from_user.id)
    if user is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    blocked, block_msg = await _check_rate_spam_block(user, value=value)
    if blocked:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await _show_rate_block_banner(
                bot=message.bot,
                chat_id=message.chat.id,
                state=state,
                text=block_msg or "Сегодня лимит оценок исчерпан. Попробуй позже.",
            )
        except Exception:
            pass
        return

    photo = await get_photo_by_id(int(photo_id))
    if photo is None or photo.get("is_deleted"):
        try:
            await message.delete()
        except Exception:
            pass
        await show_next_photo_for_rating(message, user["id"], state=state, replace_message=False)
        await _clear_rate_comment_draft(state)
        return

    if not bool(photo.get("ratings_enabled", True)):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await mark_viewonly_seen(int(user["id"]), int(photo_id))
        except Exception:
            pass
        await show_next_photo_for_rating(message, user["id"], state=state, replace_message=False)
        await _clear_rate_comment_draft(state)
        return

    # comment flow from FSM if exists
    comment_photo_id = data.get("photo_id")
    comment_text = data.get("comment_text")
    is_public = data.get("is_public", True)

    if comment_photo_id == photo_id and comment_text and not data.get("comment_saved"):
        await _safe_create_comment(
            user_id=int(user["id"]),
            photo_id=int(photo_id),
            text=comment_text,
            is_public=bool(is_public),
            chat_id=message.chat.id,
            tg_user_id=message.from_user.id if message.from_user else None,
            handler="rate_score_from_keyboard:create_comment",
        )

        author_user_id = photo.get("user_id")
        if author_user_id and author_user_id != user["id"]:
            try:
                author = await get_user_by_id(author_user_id)
            except Exception:
                author = None

            if author is not None:
                author_tg_id = author.get("tg_id")
                if author_tg_id:
                    notify_text_lines = [
                        "🔔 <b>Новый комментарий к вашей фотографии</b>",
                        "",
                        f"Текст: {comment_text}",
                        f"Оценка: {value}",
                    ]
                    notify_text = "\n".join(notify_text_lines)
                    try:
                        await message.bot.send_message(
                            chat_id=author_tg_id,
                            text=notify_text,
                            reply_markup=build_comment_notification_keyboard(),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

    await add_rating(user["id"], int(photo_id), value)
    try:
        await streak_record_action_by_tg_id(int(message.from_user.id), "rate")
    except Exception:
        pass

    try:
        author_user_id = photo.get("user_id") if photo else None
        if author_user_id and int(author_user_id) != int(user["id"]):
            author = await get_user_by_id(int(author_user_id))
            author_tg = (author or {}).get("tg_id")
            if author_tg:
                prefs = await get_notify_settings_by_tg_id(int(author_tg))
                if bool(prefs.get("likes_enabled", True)):
                    await increment_likes_daily_for_tg_id(int(author_tg), _moscow_day_key(), 1)
    except Exception:
        pass

    try:
        rewarded, referrer_tg_id, referee_tg_id = await link_and_reward_referral_if_needed(user["tg_id"])
    except Exception:
        rewarded = False
        referrer_tg_id = None
        referee_tg_id = None

    if rewarded:
        if referrer_tg_id:
            try:
                await message.bot.send_message(
                    chat_id=referrer_tg_id,
                    text=(
                        "🤝 <b>Друг выполнил условия реферальной программы!</b>\n\n"
                        "Тебе начислено <b>2 дня GlowShot Премиум</b> за приглашение.\n"
                        "Спасибо, что приводишь к нам людей, которым интересна фотография 📸"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        if referee_tg_id:
            try:
                await message.bot.send_message(
                    chat_id=referee_tg_id,
                    text=(
                        "🎉 <b>Ты выполнил условия реферальной программы!</b>\n\n"
                        "За регистрацию и участие в оценке фотографий тебе начислено "
                        "<b>2 дня GlowShot Премиум</b>.\n"
                        "Продолжай выкладывать свои кадры и оценивать работы других 💎"
                    ),
                    reply_markup=build_referral_thanks_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    try:
        await message.delete()
    except Exception:
        pass

    await show_next_photo_for_rating(message, user["id"], state=state, replace_message=False)
    await _clear_rate_comment_draft(state)

@router.callback_query(F.data.startswith("rate:more:"))
async def rate_more_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    if should_throttle(callback.from_user.id, "rate:more", 0.5):
        try:
            await callback.answer()
        except Exception:
            pass
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Странные параметры.", show_alert=True)
        return

    _, _, pid, flag = parts
    try:
        photo_id = int(pid)
        to_show = bool(int(flag))
    except Exception:
        await callback.answer("Странные параметры.", show_alert=True)
        return

    view = await _build_rate_view(photo_id, int(callback.from_user.id), show_details=to_show)
    if view is None:
        await callback.answer("Фото не найдено.", show_alert=True)
        return
    caption, kb, is_rateable = view

    await _edit_rate_message(
        callback.message,
        caption=caption,
        reply_markup=kb,
        show_caption_above_media=not to_show,
    )

    try:
        data = await state.get_data()
        data["rate_show_details"] = to_show
        await state.set_data(data)
    except Exception:
        pass

    if is_rateable:
        try:
            await _send_rate_reply_keyboard(
                callback.message.bot,
                callback.message.chat.id,
                state,
                _lang(await get_user_by_tg_id(callback.from_user.id)),
            )
        except Exception:
            pass
    else:
        await _delete_rate_reply_keyboard(callback.message.bot, callback.message.chat.id, state)

    await callback.answer("Ок")

@router.callback_query(F.data.startswith("rate:skip:"))
async def rate_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if should_throttle(callback.from_user.id, "rate:skip", 0.6):
        try:
            await callback.answer()
        except Exception:
            pass
        return
    parts = callback.data.split(":")
    # ['rate', 'skip', '<photo_id>']
    if len(parts) != 3:
        await callback.answer("Странный пропуск, не понял.", show_alert=True)
        return

    _, _, pid = parts
    try:
        photo_id = int(pid)
    except ValueError:
        await callback.answer("Странный пропуск, не понял.", show_alert=True)
        return

    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    photo = await get_photo_by_id(photo_id)
    is_rateable = bool((photo or {}).get("ratings_enabled", True))
    if not photo or photo.get("is_deleted"):
        await callback.answer("Фото не найдено.", show_alert=True)
        await show_next_photo_for_rating(callback, user["id"], state=state)
        return

    tg_id = user.get("tg_id")
    is_premium = False
    if tg_id:
        try:
            is_premium = await is_user_premium_active(tg_id)
        except Exception:
            is_premium = False

    if not is_rateable:
        await state.clear()
        try:
            await mark_viewonly_seen(int(user["id"]), photo_id)
        except Exception:
            pass
        try:
            await callback.answer()
        except Exception:
            pass
        await show_next_photo_for_rating(callback, user["id"], state=state)
        return

    # Если пользователь без премиума — ограничиваем 3 пропуска в день
    if not is_premium and tg_id:
        today_str = date.today().isoformat()
        last_date, count = await get_daily_skip_info(tg_id)

        if last_date != today_str:
            # Новый день — сбрасываем счётчик
            count = 0

        if count >= 3:
            await callback.answer(
                "Без премиума можно пропускать не больше 3 фотографий в день.\n\n"
                "Оцени это фото или оформи GlowShot Premium 💎.",
                show_alert=True,
            )
            return

        # Увеличиваем счётчик и сохраняем
        count += 1
        await update_daily_skip_info(tg_id, today_str, count)

    await state.clear()

    # Пропуск реализуем как оценку 0
    await add_rating(user["id"], photo_id, 0)
    await show_next_photo_for_rating(callback, user["id"], state=state)



@router.callback_query(F.data == "rate:start")
async def rate_start(callback: CallbackQuery, state: FSMContext) -> None:
    await rate_root(callback, state=state, replace_message=True)


@router.callback_query(F.data == "rate:back")
async def rate_back(callback: CallbackQuery, state: FSMContext) -> None:
    if should_throttle(callback.from_user.id, "rate:back", 0.6):
        try:
            await callback.answer()
        except Exception:
            pass
        return
    data = await state.get_data()
    photo_id = data.get("rate_current_photo_id")
    show_details = bool(data.get("rate_show_details"))
    if not photo_id:
        await callback.answer()
        return

    view = await _build_rate_view(int(photo_id), int(callback.from_user.id), show_details=show_details)
    if view is None:
        await callback.answer("Фото не найдено.", show_alert=True)
        return
    caption, kb, is_rateable = view

    await _edit_rate_message(
        callback.message,
        caption=caption,
        reply_markup=kb,
        show_caption_above_media=True,
    )

    if is_rateable:
        try:
            await _send_rate_reply_keyboard(
                callback.message.bot,
                callback.message.chat.id,
                state,
                _lang(await get_user_by_tg_id(callback.from_user.id)),
            )
        except Exception:
            pass
    else:
        await _delete_rate_reply_keyboard(callback.message.bot, callback.message.chat.id, state)

    try:
        await callback.answer()
    except Exception:
        pass

@router.callback_query(F.data == "menu:rate")
async def rate_root(callback: CallbackQuery, state: FSMContext | None = None, replace_message: bool = True) -> None:
    if should_throttle(callback.from_user.id, "rate:root", 0.8):
        try:
            await callback.answer("Секунду…", show_alert=False)
        except Exception:
            pass
        return
    try:
        await ensure_giraffe_banner(callback.message.bot, callback.message.chat.id, callback.from_user.id)
    except Exception:
        pass
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None or not (user.get("name") or "").strip():
        if not await require_user_name(callback):
            return
        if user is None:
            await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    if state is not None:
        try:
            ui_state = await get_user_ui_state(callback.from_user.id)
            if not bool(ui_state.get("rate_tutorial_seen")):
                await _show_rate_tutorial(callback, state)
                return
        except Exception:
            pass

    await show_next_photo_for_rating(callback, user["id"], replace_message=replace_message, state=state)
@router.callback_query(F.data == "comment:seen")
async def comment_seen(callback: CallbackQuery) -> None:
    """
    Пользователь нажал «Просмотрено» под уведомлением о новом комментарии.
    Просто удаляем это сообщение, чтобы не захламлять чат.
    """
    try:
        await callback.message.delete()
    except Exception:
        # Если сообщение уже удалено или недоступно — игнорируем.
        pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        # Если callback-query уже протухла — тоже просто игнорируем.
        pass


@router.callback_query(F.data.startswith("rate:award:"))
async def rate_award(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Заглушка для кнопки «Ачивка» в разделе оценивания.
    В дальнейшем здесь можно будет реализовать выдачу ачивок.
    """
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    # Дополнительно проверяем премиум, на всякий случай
    is_premium = False
    try:
        tg_id = user.get("tg_id")
        if tg_id:
            is_premium = await is_user_premium_active(tg_id)
    except Exception:
        is_premium = False

    if not is_premium:
        await callback.answer(
            "Выдавать ачивки из оценивания можно только с GlowShot Premium 💎.",
            show_alert=True,
        )
        return

    await callback.answer(
        "Функция выдачи ачивок из оценивания скоро будет доступна 💎.",
        show_alert=True,
    )
