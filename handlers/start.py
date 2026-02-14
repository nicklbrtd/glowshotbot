import os
import random
import html
import hashlib
from utils.i18n import t
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, LinkPreviewOptions, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import InlineKeyboardMarkup

import database as db
from keyboards.common import build_main_menu
from utils.antispam import should_throttle
from handlers.upload import my_photo_menu, myphoto_archive
from handlers.rate import rate_root
from handlers.profile import profile_menu
from handlers.results import results_menu
from handlers.premium import maybe_send_premium_expiry_warning
from config import MASTER_ADMIN_ID
from utils.time import get_moscow_now, get_moscow_today, is_happy_hour
from utils.banner import ensure_giraffe_banner
from utils.update_guard import should_block as should_block_update, send_notice_once, UPDATE_DEFAULT_TEXT

router = Router()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# --- Глобальный блокировщик на время обновления (не для админов/модераторов) ---
@router.message()
async def _update_guard_message(message: Message):
    """
    Глобальная проверка режима обновления. Если блокировки нет — не перехватываем событие.
    В любом случае пробрасываем дальше через SkipHandler.
    """
    try:
        await should_block_update(message)
    finally:
        raise SkipHandler


@router.callback_query()
async def _update_guard_callback(callback: CallbackQuery):
    try:
        await should_block_update(callback)
    finally:
        raise SkipHandler


def _pick_lang(user: dict | None, tg_lang_code: str | None) -> str:
    """Return "ru" or "en".

    Be defensive: DB may store language as "en-US" / "ru-RU" or under different keys.
    """
    if user:
        try:
            raw = (
                user.get("lang")
                or user.get("language")
                or user.get("language_code")
                or user.get("locale")
            )
            if raw:
                s = str(raw).strip().lower().split("-")[0]
                if s in ("ru", "en"):
                    return s
        except Exception:
            pass

    code = (tg_lang_code or "").lower()
    return "ru" if code.startswith("ru") else "en"


# Channel required to use the bot (subscription gate)
SUBSCRIPTION_GATE_ENABLED = os.getenv("SUBSCRIPTION_GATE_ENABLED", "false").lower() == "true"
REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@nyqcreative")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/nyqcreative")

# Advertising channel shown inside the menu text (not the gate)
AD_CHANNEL_LINK = os.getenv("AD_CHANNEL_LINK", "https://t.me/glowshotchannel")

# Рандом-строки для рекламного блока (вторая строка)
AD_LINES_RU: list[str] = [
    "Хочешь халявный премиум? приглашай друзей и получай премиум на 2 дня",
    "Оценивай больше — чаще попадаешь в топы 🏁",
    "Публикуй свой лучший кадр и проси друзей оценить через ссылку 🔗⭐️",
]

AD_LINES_EN: list[str] = [
    "Want free Premium? Invite friends and get 2 days of Premium",
    "Rate more — show up in results more often 🏁",
    "Post your best shot and ask friends to rate via a link 🔗⭐️",
]

def _is_premium_promo_day(now_dt: datetime | None = None) -> bool:
    """
    Цикл 6 дней: 2 дня кнопки нет, 4 дня — есть.
    """
    dt = now_dt or get_moscow_now()
    day_num = (dt.date().toordinal() - 737791)  # anchor 2025-01-01 approx
    return (day_num % 6) >= 2

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


def _normalize_chat_id(value: str) -> str:
    """Convert a link like https://t.me/name to @name for get_chat_member."""
    v = (value or "").strip()
    if not v:
        return "@nyqcreative"
    if v.startswith("https://t.me/"):
        tail = v.split("https://t.me/", 1)[1].strip("/")
        if tail:
            return "@" + tail
    if v.startswith("t.me/"):
        tail = v.split("t.me/", 1)[1].strip("/")
        if tail:
            return "@" + tail
    return v


async def _delete_message_safely(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _send_fresh_menu(
    *,
    bot,
    chat_id: int,
    user_id: int,
    state: FSMContext,
    lang_hint: str | None = None,
) -> None:
    """Унифицированная выдача главного меню.
    Сначала отправляем новое меню, затем удаляем старое (если было), чтобы не было пустоты."""

    try:
        await ensure_giraffe_banner(bot, chat_id, user_id, force_new=False)
    except Exception:
        pass

    data = await state.get_data()
    prev_menu_id = data.get("menu_msg_id")
    prev_rate_kb_id = data.get("rate_kb_msg_id")
    prev_screen_id = None
    prev_banner_id = None
    try:
        ui_state = await db.get_user_ui_state(user_id)
        if prev_menu_id is None:
            prev_menu_id = ui_state.get("menu_msg_id")
        if prev_rate_kb_id is None:
            prev_rate_kb_id = ui_state.get("rate_kb_msg_id")
        prev_screen_id = ui_state.get("screen_msg_id")
        prev_banner_id = ui_state.get("banner_msg_id")
    except Exception:
        pass
    user = await db.get_user_by_tg_id(user_id)
    lang = _pick_lang(user, lang_hint)
    is_admin = _get_flag(user, "is_admin")
    is_moderator = _get_flag(user, "is_moderator")
    is_premium = await db.is_user_premium_active(user_id)

    # Если у пользователя нет имени — не даём меню, принуждаем регистрацию
    user_name = (user.get("name") or "").strip() if user else ""
    if not user_name:
        kb = InlineKeyboardBuilder()
        kb.button(text="Добавить имя", callback_data="auth:start")
        kb.adjust(1)
        prompt_text = "Чтобы перейти в этот раздел вам нужно добавить свое имя."

        sent_msg_id = None
        if prev_menu_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(prev_menu_id),
                    text=prompt_text,
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
                sent_msg_id = int(prev_menu_id)
            except Exception:
                sent_msg_id = None

        if sent_msg_id is None:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=prompt_text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
                parse_mode="HTML",
            )
            sent_msg_id = sent.message_id

        data["menu_msg_id"] = sent_msg_id
        data["rate_kb_msg_id"] = None
        data["rate_kb_mode"] = "none"
        await state.set_data(data)
        try:
            await db.set_user_menu_msg_id(user_id, sent_msg_id)
            await db.set_user_screen_msg_id(user_id, sent_msg_id)
        except Exception:
            pass

        if prev_menu_id and prev_menu_id != sent_msg_id:
            await _delete_message_safely(bot, chat_id, prev_menu_id)
        if prev_rate_kb_id and prev_rate_kb_id != sent_msg_id:
            await _delete_message_safely(bot, chat_id, prev_rate_kb_id)
            try:
                await db.set_user_rate_kb_msg_id(user_id, None)
            except Exception:
                pass
        if prev_screen_id and prev_screen_id not in (sent_msg_id, prev_menu_id, prev_rate_kb_id):
            await _delete_message_safely(bot, chat_id, prev_screen_id)
        return

    menu_text = await build_menu_text(tg_id=user_id, user=user, is_premium=is_premium, lang=lang)
    main_kb = await _build_dynamic_main_menu(
        user=user,
        lang=lang,
        is_admin=is_admin,
        is_moderator=is_moderator,
        is_premium=is_premium,
    )
    # Сообщение меню без дополнительных inline‑кнопок, с reply‑клавиатурой сразу
    sent = await bot.send_message(
        chat_id=chat_id,
        text=menu_text,
        reply_markup=main_kb,
        disable_notification=True,
        link_preview_options=NO_PREVIEW,
        parse_mode="HTML",
    )
    # Отдельно выставляем reply‑клавиатуру скрытым «пингуем»
    data["menu_msg_id"] = sent.message_id
    data["rate_kb_msg_id"] = None
    data["rate_kb_mode"] = "none"
    await state.set_data(data)
    try:
        await db.set_user_menu_msg_id(user_id, sent.message_id)
        await db.set_user_screen_msg_id(user_id, sent.message_id)
    except Exception:
        pass

    if prev_menu_id and prev_menu_id != sent.message_id:
        await _delete_message_safely(bot, chat_id, prev_menu_id)
    if prev_rate_kb_id and prev_rate_kb_id != sent.message_id:
        await _delete_message_safely(bot, chat_id, prev_rate_kb_id)
        try:
            await db.set_user_rate_kb_msg_id(user_id, None)
        except Exception:
            pass
    if prev_screen_id and prev_screen_id not in (sent.message_id, prev_menu_id, prev_rate_kb_id):
        await _delete_message_safely(bot, chat_id, prev_screen_id)


def _main_menu_button_key(text: str | None) -> str | None:
    """Определяем, какая кнопка главного меню была отправлена как текст."""
    if not text:
        return None
    s = text.strip()
    mapping: dict[str, set[str]] = {
        "myphoto": {
            t("kb.main.myphoto", "ru"),
            t("kb.main.myphoto", "en"),
            t("kb.main.myphoto.empty", "ru"),
            t("kb.main.myphoto.empty", "en"),
            t("kb.main.myphoto.filled", "ru"),
            t("kb.main.myphoto.filled", "en"),
        },
        "rate": {
            t("kb.main.rate", "ru"),
            t("kb.main.rate", "en"),
            t("kb.main.rate.empty", "ru"),
            t("kb.main.rate.empty", "en"),
        },
        "profile": {t("kb.main.profile", "ru"), t("kb.main.profile", "en")},
        "results": {t("kb.main.results", "ru"), t("kb.main.results", "en")},
        "myarchive": {"📚 Мой архив", "📚 My Archive"},
        "menu": {t("kb.back_to_menu", "ru"), t("kb.back_to_menu", "en")},
    }
    for key, variants in mapping.items():
        if s in variants:
            return key
    return None


class _MessageAsCallback:
    """Простейший shim, чтобы переиспользовать callback-хендлеры для reply-кнопок."""

    def __init__(self, message: Message):
        self.message = message
        self.from_user = message.from_user
        self.bot = message.bot
        self.chat = message.chat
        self.message_id = message.message_id
        self.data = ""

    async def answer(self, *args, **kwargs):
        return None


async def is_user_subscribed(bot, user_id: int) -> bool:
    if not SUBSCRIPTION_GATE_ENABLED:
        return True

    try:
        member = await bot.get_chat_member(chat_id=_normalize_chat_id(str(REQUIRED_CHANNEL_ID)), user_id=user_id)
    except TelegramBadRequest:
        return False

    return member.status in ("member", "administrator", "creator")


def build_subscribe_keyboard(lang: str) -> InlineKeyboardMarkup:
    """
    Клавиатура для экрана обязательной подписки:
    «Подписаться» + «Готово», сразу в виде InlineKeyboardMarkup.
    """
    kb = InlineKeyboardBuilder()

    channel_link = REQUIRED_CHANNEL_LINK

    kb.button(
        text=t("start.subscribe.btn", lang),
        url=channel_link,
    )
    kb.button(
        text=t("start.subscribe.ready", lang),
        callback_data="sub:check",
    )
    kb.adjust(1)
    return kb.as_markup()


async def _build_dynamic_main_menu(
    *,
    user: dict | None,
    lang: str,
    is_admin: bool,
    is_moderator: bool,
    is_premium: bool,
) -> ReplyKeyboardMarkup:
    has_photo = False
    has_rate_targets = True

    try:
        if user and user.get("id"):
            photos = await db.get_active_photos_for_user(int(user["id"]))
            has_photo = bool(photos)
    except Exception:
        has_photo = False

    try:
        if user and user.get("id"):
            candidate = await db.get_random_photo_for_rating(int(user["id"]))
            has_rate_targets = candidate is not None
    except Exception:
        has_rate_targets = True

    return build_main_menu(
        is_admin=is_admin,
        is_moderator=is_moderator,
        is_premium=is_premium,
        lang=lang,
        has_photo=has_photo,
        has_rate_targets=has_rate_targets,
    )

async def build_menu_text(*, tg_id: int, user: dict | None, is_premium: bool, lang: str) -> str:
    """Формирует текст главного меню по новым сценариям."""

    def _fmt_rating(v: float | None) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(v)

    def _pick_advice(photos: list[dict], user: dict | None, is_premium: bool) -> str:
        advices: list[str] = []

        if not photos:
            advices.append("Добавь фотографию для своих первых оценок.")
            advices.append("Люди могут оставлять комментарии к твоим фотографиям.")
        else:
            advices.append('Ты можешь поделиться ссылкой: «Моя фотография» → «Поделиться фотографией».')
            advices.append(
                'Если не хочешь получать оценки — выключи их: «Моя фотография» → «Редактировать» → «Оценки».'
                " Фото останется видимым, но без оценок."
            )
            if not is_premium:
                advices.append("С премиум можно делиться ссылкой в тгк и других соцсетях без ограничений.")

            ph = photos[-1]
            if not (ph.get("device") or ph.get("device_type")):
                advices.append("Укажи устройство: «Моя фотография» → «Редактировать» → «Устройство».")
            if not ph.get("tag"):
                advices.append("Добавь тег: «Моя фотография» → «Редактировать» → «Тег».")

        if not (user or {}).get("bio"):
            advices.append("Заполни описание в профиле — так тебя легче запомнят.")

        if not advices:
            return "💡 Совет недоступен."

        import time

        bucket = int(time.time() // (4 * 3600))  # новый совет каждые 4 часа
        idx = bucket % len(advices)
        return "💡 " + advices[idx]

    # Подгружаем активные фото (до 2)
    photos: list[dict] = []
    if user and user.get("id"):
        try:
            photos = await db.get_active_photos_for_user(int(user["id"]))
            photos = sorted(photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
            photos = photos[:2]
        except Exception:
            photos = []

    lines: list[str] = []

    credits_line = None
    if user and user.get("id"):
        try:
            stats = await db.get_user_stats(int(user["id"]))
            credits = int(stats.get("credits") or 0)
            tokens = int(stats.get("show_tokens") or 0)
            mult = 4 if is_happy_hour() else 2
            approx = credits * mult + tokens
            credits_line = f"💳 Credits: {credits} (≈ {approx} показов)"
        except Exception:
            credits_line = None

    if credits_line:
        lines.append(credits_line)
    try:
        latest_results = await db.get_latest_daily_results_cache()
    except Exception:
        latest_results = None
    if latest_results and latest_results.get("submit_day"):
        lines.append(f"🏆 Итоги доступны: {latest_results.get('submit_day')}")
    else:
        lines.append("🏆 Итоги: пока нет опубликованных")
    lines.append("")

    # Сценарий 1: нет фото
    if not photos:
        lines.append("У тебя нет активной фотографии.")
        lines.append("Загрузи её по кнопке «Загрузить».")
        lines.append("")
        lines.append("🌱 Вы можете отключить возможность оценивать Вашу фотографию, если хотите.")
        lines.append("")
        lines.append(_pick_advice(photos, user, is_premium))
        lines.append("")
        tagline = "Публикуй · Оценивай · Побеждай"
        lines.append(f"💎 {tagline}" if is_premium else tagline)
        return "\n".join(lines)

    # Сценарий 2: одна фото
    if len(photos) == 1:
        ph = photos[0]
        title = html.escape((ph.get("title") or "Без названия").strip(), quote=False)
        bayes = None
        try:
            st = await db.get_photo_stats(int(ph["id"]))
            bayes = st.get("bayes_score")
        except Exception:
            bayes = None
        lines.append(f"🎞️ Текущая работа: <code>\"{title}\"</code>")
        lines.append(f"Рейтинг: { _fmt_rating(bayes) }")
        lines.append("")
        lines.append(_pick_advice(photos, user, is_premium))
        lines.append("")
        tagline = "Публикуй · Оценивай · Побеждай"
        lines.append(f"💎 {tagline}" if is_premium else tagline)
        return "\n".join(lines)

    # Сценарий 3: две фото
    best_title = "—"
    best_score = None
    try:
        stats_list = []
        for ph in photos:
            st = await db.get_photo_stats(int(ph["id"]))
            stats_list.append((ph, st.get("bayes_score")))
        best_ph, best_score = max(stats_list, key=lambda x: (x[1] if x[1] is not None else -1))
        best_title = html.escape((best_ph.get("title") or "Без названия").strip(), quote=False)
    except Exception:
        pass

    lines.append("🎞️ Две активные фотографии!")
    lines.append(f"Лучшая: <code>\"{best_title}\"</code> — { _fmt_rating(best_score) }")
    lines.append("")
    lines.append(_pick_advice(photos, user, is_premium))
    lines.append("")
    tagline = "Публикуй · Оценивай · Побеждай"
    lines.append(f"💎 {tagline}" if is_premium else tagline)
    return "\n".join(lines)
    if ratings_count > 0 or comments_count > 0:
        lines.append("🔔 На твою фотографию уже приходили оценки/комментарии.")
    else:
        lines.append("🌿 Пока новых оценок нет — это ок, система подберёт зрителей.")

    # --- Блок подсказок (макс 2) ---
    hints: list[str] = []
    if not active_photo:
        hints.append("💡 Загрузи фотографию, чтобы начать получать оценки.")
    else:
        if ratings_count < 20:
            hints.append("💡 Поделись ссылкой на фото — оценки по ссылке учитываются.")
        hints.append("💡 Пригласи двоих друзей через /ref — после этого сможешь участвовать в итогах дня.")
        hints.append("💡 Оценивай работы других — система подберёт больше зрителей для твоего кадра.")
    # Настройка рекламы: подсказка для премиум и непремиум
    if is_premium:
        hints.append("💡 Рекламу в оценках можно выключить в настройках профиля.")
    else:
        hints.append("💡 Premium позволяет отключить рекламу в оценках.")
        # список советов в коде можно расширять здесь

    if hints:
        lines.append("")
        # детерминированный выбор до 2 подсказок (по пользователю и дате)
        seed_str = f"{tg_id}-{get_moscow_today()}"
        seed = int(hashlib.sha256(seed_str.encode()).hexdigest(), 16)
        pool = list(hints)
        selected: list[str] = []
        while pool and len(selected) < 2:
            idx = seed % len(pool)
            selected.append(pool.pop(idx))
            seed = seed // 7 or 1
        for h in selected:
            lines.append(h)

    # --- Опциональная бренд-строка ---
    lines.append("")
    lines.append("Публикуй · Оценивай · Побеждай")

    return "\n".join(lines)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    user = await db.get_user_by_tg_id(message.from_user.id)
    is_allowed = bool(
        message.from_user.id == MASTER_ADMIN_ID
        or (user and (user.get("is_admin") or user.get("is_moderator") or user.get("is_support")))
    )
    if not is_allowed:
        try:
            await message.delete()
        except Exception:
            pass
        return

    chat_type = getattr(message.chat, "type", "unknown")
    await message.answer(
        f"chat_id: <code>{message.chat.id}</code>\nтип: <code>{chat_type}</code>",
        parse_mode="HTML",
    )


@router.message(F.photo)
async def cmd_fileid_photo(message: Message):
    caption = (message.caption or "").strip()
    if not caption.startswith("/fileid"):
        raise SkipHandler

    user = await db.get_user_by_tg_id(message.from_user.id)
    is_allowed = bool(
        message.from_user.id == MASTER_ADMIN_ID
        or (user and (user.get("is_admin") or user.get("is_moderator") or user.get("is_support")))
    )
    if not is_allowed:
        try:
            await message.delete()
        except Exception:
            pass
        return

    photo = message.photo[-1] if message.photo else None
    if not photo:
        return

    await message.answer(
        f"file_id: <code>{photo.file_id}</code>\nunique_id: <code>{photo.file_unique_id}</code>",
        parse_mode="HTML",
        disable_notification=True,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_main_menu_reply_buttons(message: Message, state: FSMContext):
    """
    Переводим нажатия reply‑кнопок в действия:
    - удаляем сообщение пользователя;
    - по переходу в другие разделы убираем главное меню;
    - вызываем нужный раздел или возвращаем меню.
    """
    key = _main_menu_button_key(message.text)
    if key is None:
        raise SkipHandler
    if getattr(message.chat, "type", None) not in ("private",):
        return

    # Инлайн-кнопки с правилами больше нет; текстовую кнопку игнорируем
    data = await state.get_data()
    current_menu_id = data.get("menu_msg_id")

    # Блокируем доступ к разделам, если имя не указано
    try:
        u = await db.get_user_by_tg_id(message.from_user.id)
    except Exception:
        u = None
    if u is not None and not (u.get("name") or "").strip():
        kb = InlineKeyboardBuilder()
        kb.button(text="Добавить имя", callback_data="auth:start")
        kb.adjust(1)
        prompt_text = "Чтобы перейти в этот раздел вам нужно добавить свое имя."
        try:
            data = await state.get_data()
            menu_msg_id = data.get("menu_msg_id")
        except Exception:
            menu_msg_id = None
        if menu_msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(menu_msg_id),
                    text=prompt_text,
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML",
                )
            except Exception:
                try:
                    await message.answer(
                        prompt_text,
                        reply_markup=kb.as_markup(),
                        disable_notification=True,
                    )
                except Exception:
                    pass
        else:
            try:
                await message.answer(
                    prompt_text,
                    reply_markup=kb.as_markup(),
                    disable_notification=True,
                )
            except Exception:
                pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    pseudo_cb = _MessageAsCallback(message)
    # При переходе в разделы — удаляем текущее меню, чтобы не мешало
    if key != "menu" and current_menu_id:
        await _delete_message_safely(message.bot, message.chat.id, current_menu_id)
        data["menu_msg_id"] = None
        try:
            await db.set_user_menu_msg_id(message.from_user.id, None)
        except Exception:
            pass
        await state.set_data(data)

    if key == "menu":
        await _send_fresh_menu(
            bot=message.bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            state=state,
            lang_hint=getattr(message.from_user, "language_code", None),
        )
    elif key == "myphoto":
        await my_photo_menu(pseudo_cb, state)
    elif key == "myarchive":
        pseudo_cb.data = "myphoto:archive:0"
        await myphoto_archive(pseudo_cb, state)
    elif key == "rate":
        await rate_root(pseudo_cb, state=state, replace_message=True)
    elif key == "profile":
        await profile_menu(pseudo_cb, state)
    elif key == "results":
        await results_menu(pseudo_cb, state)

    # После успешного перехода удаляем сообщение пользователя и старое меню
    try:
        await message.delete()
    except Exception:
        pass


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if should_throttle(message.from_user.id, "cmd_start", 1.5):
        try:
            await message.answer("Секунду…", disable_notification=True)
        except Exception:
            pass
        return
    # Запрещаем /start в группах, чтобы не спамить меню
    if getattr(message.chat, "type", None) in ("group", "supergroup"):
        try:
            await message.delete()
        except Exception:
            pass
        return
    try:
        await _cmd_start_inner(message, state)
    except Exception as e:
        try:
            await message.answer("Не удалось обработать /start, попробуй ещё раз через минуту.")
        except Exception:
            pass
        # Для отладки оставляем исключение в логах
        raise


async def _cmd_start_inner(message: Message, state: FSMContext):
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
        lang = _pick_lang(user, getattr(message.from_user, "language_code", None))

        is_premium = await db.is_user_premium_active(message.from_user.id)
        lang = _pick_lang(user, getattr(message.from_user, "language_code", None))

        if payload == "payment_success":
            if is_premium:
                payment_note = t("start.payment.success_active", lang)
            else:
                payment_note = t("start.payment.success_pending", lang)
        else:
            payment_note = t("start.payment.fail", lang)

        # Пытаемся обновить уже существующее сообщение меню (не спамим чат)
        data = await state.get_data()
        menu_msg_id = data.get("menu_msg_id")

        if user:
            is_admin = _get_flag(user, "is_admin")
            is_moderator = _get_flag(user, "is_moderator")
        else:
            is_admin = False
            is_moderator = False

        # Отправляем статус оплаты отдельным сообщением (не мешаем меню)
        try:
            await message.answer(
                payment_note,
                disable_notification=True,
                link_preview_options=NO_PREVIEW,
                parse_mode="HTML",
            )
        except Exception:
            pass

        await _send_fresh_menu(
            bot=message.bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            state=state,
            lang_hint=getattr(message.from_user, "language_code", None),
        )

        # Убираем сам /start, чтобы не плодить сообщения
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Проверяем наличие пользователя даже если он помечен удалённым, чтобы учесть блокировки
    user_any = await db.get_user_by_tg_id_any(message.from_user.id)
    if user_any and bool(user_any.get("is_blocked")):
        reason = (user_any.get("block_reason") or "").strip()
        # Админский/модераторский бан: блокируем вход.
        if reason.startswith("FULL_BAN:"):
            await message.answer(
                "Твой аккаунт заблокирован модератором. Восстановление недоступно.",
                disable_notification=True,
            )
            return
        # Неадминский блок (например, юзер блокировал бота) — снимаем.
        try:
            await db.set_user_block_status_by_tg_id(
                int(message.from_user.id),
                is_blocked=False,
                reason=None,
                until_iso=None,
            )
        except Exception:
            pass

    user = await db.get_user_by_tg_id(message.from_user.id)

    # Если запись существует, но помечена удалённой — считаем это деактивацией: по /start включаем обратно
    if user is None and user_any and user_any.get("is_deleted"):
        try:
            await db.reactivate_user_by_tg_id(int(message.from_user.id))
            user = await db.get_user_by_tg_id(message.from_user.id)
        except Exception:
            user = None

    lang = _pick_lang(user, getattr(message.from_user, "language_code", None))

    if user is None:
        # Если был soft-delete, сбрасываем состояние на всякий случай
        await state.clear()

        # Если человек зашёл по реферальной ссылке вида /start ref_CODE — сохраняем pending
        # Но не даём реферальный бонус, если аккаунт уже существовал (даже если сейчас удалён).
        if payload and payload.startswith("ref_") and not user_any:
            ref_code = payload[4:].strip()
            if ref_code:
                try:
                    await db.save_pending_referral(message.from_user.id, ref_code)
                except Exception:
                    pass

        # Если включён режим обновления — сразу предупреждаем новым пользователям одним сообщением
        try:
            upd_state = await db.get_update_mode_state()
            if upd_state.get("update_enabled"):
                await message.answer(
                    upd_state.get("update_notice_text") or UPDATE_DEFAULT_TEXT,
                    disable_notification=True,
                )
        except Exception:
            pass

        # Приветственный экран для новых пользователей
        welcome_text = (
            "GlowShot — это новый Телеграм бот для тех, кто любит фотографию.\n"
            "Выкладывай фото, делись им по ссылке, получай оценки.\n"
            "Начнем? Жми «Сыыыыр 📸»"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="Наш телеграм", url="https://t.me/glowshotchannel")
        kb.button(text="Сыыыыр 📸", callback_data="auth:start")
        kb.adjust(1, 1)

        try:
            await message.answer(
                welcome_text,
                reply_markup=kb.as_markup(),
                disable_notification=True,
                parse_mode="HTML",
            )
        except Exception:
            # Fallback без разметки/клавы, чтобы пользователь точно увидел ответ
            try:
                await message.answer(welcome_text)
            except Exception:
                # Последний шанс — игнорируем, чтобы не падать
                pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Если пользователь есть, но имя не заполнено — принуждаем завершить регистрацию
    if not (user.get("name") or "").strip():
        kb = InlineKeyboardBuilder()
        kb.button(text="Добавить имя", callback_data="auth:start")
        kb.adjust(1)
        try:
            await message.answer(
                "Чтобы перейти в этот раздел вам нужно добавить свое имя.",
                reply_markup=kb.as_markup(),
                disable_notification=True,
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    if SUBSCRIPTION_GATE_ENABLED and not await is_user_subscribed(message.bot, message.from_user.id):
        sub_kb = build_subscribe_keyboard(lang)
        await message.answer(
            t("start.subscribe.prompt", lang),
            reply_markup=sub_kb,
            disable_notification=True,
            parse_mode="HTML",
        )
    else:
        await _send_fresh_menu(
            bot=message.bot,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            state=state,
            lang_hint=getattr(message.from_user, "language_code", None),
        )
        # при включённом режиме обновления показываем уведомление один раз, но не блокируем
        try:
            await send_notice_once(message)
        except Exception:
            pass

        # Напоминание о скором окончании премиума (за 2 дня)
        try:
            await maybe_send_premium_expiry_warning(
                message.bot,
                tg_id=message.from_user.id,
                chat_id=message.chat.id,
                lang=lang,
            )
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data == "sub:check")
async def subscription_check(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user = await db.get_user_by_tg_id(user_id)
    lang = _pick_lang(user, getattr(callback.from_user, "language_code", None))

    if SUBSCRIPTION_GATE_ENABLED and not await is_user_subscribed(callback.bot, user_id):
        await callback.answer(
            t("start.subscribe.not_yet", lang),
            show_alert=True,
        )
        return

    await _send_fresh_menu(
        bot=callback.message.bot,
        chat_id=callback.message.chat.id,
        user_id=user_id,
        state=state,
        lang_hint=getattr(callback.from_user, "language_code", None),
    )
    try:
        if SUBSCRIPTION_GATE_ENABLED:
            await callback.answer(t("start.subscribe.thanks", lang), show_alert=False)
        else:
            await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "menu:back")
async def menu_back(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "menu:back", 1.2):
        try:
            await callback.answer("Секунду…", show_alert=False)
        except Exception:
            pass
        return
    try:
        await callback.answer()
    except TelegramBadRequest:
        # query уже протух — просто игнорируем, для UX это не критично
        pass
    chat_id = callback.message.chat.id
    data = await state.get_data()
    photo_msg_id = data.get("myphoto_photo_msg_id")
    # Сбрасываем контекст оценивания, чтобы цифры не считались оценкой вне раздела
    if "rate_current_photo_id" in data or "rate_show_details" in data:
        data.pop("rate_current_photo_id", None)
        data.pop("rate_show_details", None)
        await state.set_data(data)

    await _send_fresh_menu(
        bot=callback.message.bot,
        chat_id=chat_id,
        user_id=callback.from_user.id,
        state=state,
        lang_hint=getattr(callback.from_user, "language_code", None),
    )
    # удаляем старый экран, чтобы не висел рядом с меню
    try:
        await callback.message.delete()
    except Exception:
        pass
