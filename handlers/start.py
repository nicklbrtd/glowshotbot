import os
import random
import html
import hashlib
from utils.i18n import t
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, LinkPreviewOptions
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import InlineKeyboardMarkup

import database as db
from keyboards.common import build_main_menu
from utils.time import get_moscow_now, get_moscow_today

router = Router()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

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
) -> InlineKeyboardMarkup:
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
    """Формирует текст главного меню (без рекламы, с адаптивными подсказками)."""

    def _fmt_rating(v: float | None) -> str:
        if v is None:
            return "—"
        try:
            return f"{float(v):.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(v)

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

    title_prefix = "💎 " if is_premium else ""
    lines: list[str] = []
    lines.append(f"{title_prefix}Привет, {safe_name}!")

    # --- Блок статуса фотографии ---
    active_photo = None
    stats = None
    comments_count = 0
    ratings_count = 0
    avg_rating = None

    if user and user.get("id"):
        try:
            photos = await db.get_active_photos_for_user(int(user["id"]))
            if photos:
                # берем самую свежую как основную, но показываем, что их может быть 2
                photos_sorted = sorted(photos, key=lambda p: (p.get("created_at") or "", p.get("id") or 0))
                active_photo = photos_sorted[-1]
                other_count = len(photos_sorted) - 1
        except Exception:
            active_photo = None
            other_count = 0

    lines.append("")
    if not active_photo:
        lines.append("📷 У тебя нет активной фотографии.")
        lines.append("Загрузи кадр, чтобы получать оценки и комментарии.")
    else:
        try:
            stats = await db.get_photo_stats(int(active_photo["id"]))
            ratings_count = int(stats.get("ratings_count") or 0)
            avg_rating = stats.get("avg_rating")
            comments_count = int(stats.get("comments_count") or 0)
        except Exception:
            stats = None

        title = (active_photo.get("title") or "Без названия").strip()
        suffix = ""
        try:
            if other_count > 0:
                suffix = f"  •  ещё {other_count} активн." if other_count == 1 else f"  •  ещё {other_count} активных"
        except Exception:
            suffix = ""
        lines.append(f"🎞 Текущая работа: «{html.escape(title, quote=False)}»{suffix}")
        if ratings_count == 0:
            lines.append("Оценок пока нет — это нормально, подбор аудитории займёт немного времени.")
        else:
            lines.append(f"Рейтинг: { _fmt_rating(avg_rating) }   ·   Оценок: {ratings_count}")
        if comments_count > 0:
            lines.append(f"Комментариев: {comments_count}")

    # --- Блок активности / обратной связи ---
    lines.append("")
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

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
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

        menu_text = await build_menu_text(tg_id=message.from_user.id, user=user, is_premium=is_premium, lang=lang)
        reply_kb = await _build_dynamic_main_menu(
            user=user,
            lang=lang,
            is_admin=is_admin,
            is_moderator=is_moderator,
            is_premium=is_premium,
        )

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

        edited = False
        if menu_msg_id:
            try:
                await message.bot.edit_message_text(
                    menu_text,
                    chat_id=message.chat.id,
                    message_id=menu_msg_id,
                    reply_markup=reply_kb,
                    link_preview_options=NO_PREVIEW,
                    parse_mode="HTML",
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
                link_preview_options=NO_PREVIEW,
                parse_mode="HTML",
            )
            data["menu_msg_id"] = sent.message_id
            await state.set_data(data)

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
    lang = _pick_lang(user, getattr(message.from_user, "language_code", None))

    if user is None:
        # Если был soft-delete, сбрасываем состояние на всякий случай
        await state.clear()
        # Реактивируем запись (снимаем is_deleted), если она была
        if user_any and user_any.get("is_deleted"):
            try:
                await db.reactivate_user_by_tg_id(int(message.from_user.id))
            except Exception:
                pass

        # Если человек зашёл по реферальной ссылке вида /start ref_CODE — сохраняем pending
        # Но не даём реферальный бонус, если аккаунт уже существовал (даже если сейчас удалён).
        if payload and payload.startswith("ref_") and not user_any:
            ref_code = payload[4:].strip()
            if ref_code:
                try:
                    await db.save_pending_referral(message.from_user.id, ref_code)
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

    if SUBSCRIPTION_GATE_ENABLED and not await is_user_subscribed(message.bot, message.from_user.id):
        sub_kb = build_subscribe_keyboard(lang)
        await message.answer(
            t("start.subscribe.prompt", lang),
            reply_markup=sub_kb,
            disable_notification=True,
            parse_mode="HTML",
        )
    else:
        # флаги ролей
        is_admin = _get_flag(user, "is_admin")
        is_moderator = _get_flag(user, "is_moderator")
        is_premium = await db.is_user_premium_active(message.from_user.id)
        main_kb = await _build_dynamic_main_menu(
            user=user,
            lang=lang,
            is_admin=is_admin,
            is_moderator=is_moderator,
            is_premium=is_premium,
        )

        chat_id = message.chat.id
        data = await state.get_data()
        menu_msg_id = data.get("menu_msg_id")

        sent_message = None
        menu_text = await build_menu_text(tg_id=message.from_user.id, user=user, is_premium=is_premium, lang=lang)

        if menu_msg_id:
            # Пытаемся отредактировать уже существующее сообщение меню
            try:
                await message.bot.edit_message_text(
                    menu_text,
                    chat_id=chat_id,
                    message_id=menu_msg_id,
                    reply_markup=main_kb,
                    link_preview_options=NO_PREVIEW,
                    parse_mode="HTML",
                )
            except TelegramBadRequest:
                # Если редактирование не удалось (сообщение удалено/устарело) — отправляем новое
                sent_message = await message.answer(
                    menu_text,
                    reply_markup=main_kb,
                    disable_notification=True,
                    link_preview_options=NO_PREVIEW,
                    parse_mode="HTML",
                )
        else:
            # Меню ещё ни разу не показывалось — отправляем новое сообщение
            sent_message = await message.answer(
                menu_text,
                reply_markup=main_kb,
                disable_notification=True,
                link_preview_options=NO_PREVIEW,
                parse_mode="HTML",
            )

        # Если мы отправили новое меню — запоминаем его message_id в состоянии
        if sent_message is not None:
            data["menu_msg_id"] = sent_message.message_id
            await state.set_data(data)
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data == "sub:check")
async def subscription_check(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await db.get_user_by_tg_id(user_id)
    lang = _pick_lang(user, getattr(callback.from_user, "language_code", None))

    if SUBSCRIPTION_GATE_ENABLED and not await is_user_subscribed(callback.bot, user_id):
        await callback.answer(
            t("start.subscribe.not_yet", lang),
            show_alert=True,
        )
        return

    # достаём пользователя и флаги ролей
    user = await db.get_user_by_tg_id(user_id)
    is_admin = _get_flag(user, "is_admin")
    is_moderator = _get_flag(user, "is_moderator")
    is_premium = await db.is_user_premium_active(user_id)
    menu_text = await build_menu_text(tg_id=user_id, user=user, is_premium=is_premium, lang=lang)
    main_kb = await _build_dynamic_main_menu(
        user=user,
        lang=lang,
        is_admin=is_admin,
        is_moderator=is_moderator,
        is_premium=is_premium,
    )
    try:
        await callback.message.edit_text(
            menu_text,
            reply_markup=main_kb,
            link_preview_options=NO_PREVIEW,
            parse_mode="HTML",
        )
    except Exception:
        try:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=menu_text,
                reply_markup=main_kb,
                disable_notification=True,
                link_preview_options=NO_PREVIEW,
                parse_mode="HTML",
            )
        except Exception:
            await callback.message.answer(
                menu_text,
                reply_markup=main_kb,
                disable_notification=True,
                link_preview_options=NO_PREVIEW,
                parse_mode="HTML",
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
    try:
        await callback.answer()
    except TelegramBadRequest:
        # query уже протух — просто игнорируем, для UX это не критично
        pass
    chat_id = callback.message.chat.id
    data = await state.get_data()
    photo_msg_id = data.get("myphoto_photo_msg_id")

    user = await db.get_user_by_tg_id(callback.from_user.id)
    lang = _pick_lang(user, getattr(callback.from_user, "language_code", None))
    is_admin = _get_flag(user, "is_admin")
    is_moderator = _get_flag(user, "is_moderator")
    is_premium = await db.is_user_premium_active(callback.from_user.id)
    main_kb = await _build_dynamic_main_menu(
        user=user,
        lang=lang,
        is_admin=is_admin,
        is_moderator=is_moderator,
        is_premium=is_premium,
    )

    menu_text = await build_menu_text(tg_id=callback.from_user.id, user=user, is_premium=is_premium, lang=lang)
    # Сначала шлём новое меню...
    try:
        sent = await callback.message.bot.send_message(
            chat_id=chat_id,
            text=menu_text,
            reply_markup=main_kb,
            disable_notification=True,
            link_preview_options=NO_PREVIEW,
            parse_mode="HTML",
        )
    except Exception:
        sent = await callback.message.answer(
            menu_text,
            reply_markup=main_kb,
            disable_notification=True,
            link_preview_options=NO_PREVIEW,
            parse_mode="HTML",
        )
    menu_msg_id = sent.message_id

    # ...затем удаляем старое сообщение раздела
    try:
        await callback.message.delete()
    except Exception:
        pass

    # удаляем висевшее сообщение с фото "моя фотография" (если было)
    if photo_msg_id:
        try:
            if photo_msg_id != menu_msg_id:
                await callback.message.bot.delete_message(chat_id=chat_id, message_id=photo_msg_id)
        except Exception:
            pass
        data["myphoto_photo_msg_id"] = None

    data["menu_msg_id"] = menu_msg_id
    await state.set_data(data)
