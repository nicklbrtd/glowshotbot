from aiogram import Router, F
import traceback
from datetime import date
from utils.time import get_moscow_today

from aiogram.types import CallbackQuery, InputMediaPhoto, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from keyboards.common import build_viewed_kb
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from utils.moderation import (
    get_report_reasons,
    REPORT_REASON_LABELS,
    ReportStats,
    decide_after_new_report,
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
    streak_get_status_by_tg_id,
    get_notify_settings_by_tg_id,
    increment_likes_daily_for_tg_id,
)
from html import escape

router = Router()

def _moscow_day_key() -> str:
    try:
        return get_moscow_today().isoformat()
    except Exception:
        return str(get_moscow_today())


class RateStates(StatesGroup):
    waiting_comment_text = State()
    waiting_report_text = State()


def build_rate_keyboard(photo_id: int, is_premium: bool = False, show_details: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    # 1..10 (две строки по 5)
    for i in range(1, 11):
        kb.button(text=str(i), callback_data=f"rate:score:{photo_id}:{i}")
    kb.adjust(5, 5)

    # 💬 + 🚫
    kb.row(
        InlineKeyboardButton(text="💬 Написать", callback_data=f"rate:comment:{photo_id}"),
        InlineKeyboardButton(text="🚫 Жалоба", callback_data=f"rate:report:{photo_id}"),
    )

    # premium extras
    if is_premium:
        kb.row(
            InlineKeyboardButton(text="💥+15", callback_data=f"rate:super:{photo_id}"),
            InlineKeyboardButton(text="🏆 Ачивка", callback_data=f"rate:award:{photo_id}"),
            InlineKeyboardButton(
                text=("🕵️ Скрыть" if show_details else "🕵️ Еще"),
                callback_data=f"rate:more:{photo_id}:{1 if not show_details else 0}",
            ),
        )

    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:back"))
    return kb.as_markup()


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


def build_no_photos_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура, когда фотографии для оценивания закончились."""
    share_url = f"https://t.me/share/url?url={BOT_INVITE_LINK}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Отправить ссылку", url=share_url)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:back")],
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
    """Шаблон:
    💎 «Название» • 🔥N дней
    ••• 🏆 Бета-тестер бота ••• (если имеется)
    🔗 Ссылка: @xxx (если имеется)

    (описание свернутой цитатой)

    Premium (по кнопке 🕵️ Еще) — тоже цитатой:
    <blockquote>
    📊 Статистика:
    Рейтинг:
    Кол-во 6-10:
    Кол-во 1-5:
    </blockquote>
    """

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

        # если нет link в photo — попробуем подтянуть
        if author and not photo.get("user_tg_channel_link"):
            if author.get("tg_channel_link"):
                photo["user_tg_channel_link"] = author.get("tg_channel_link")

    streak_days = 0
    if author_tg_id:
        try:
            st = await streak_get_status_by_tg_id(int(author_tg_id))
            streak_days = int(st.get("streak") or 0)
        except Exception:
            streak_days = 0

    lines: list[str] = []
    lines.append(f"💎 «{escape(title)}» • 🔥<b>{streak_days}</b> дней")

    # beta tester line
    if bool(photo.get("has_beta_award")):
        lines.append("••• 🏆 <b>Бета-тестер бота</b> •••")

    # link line (если имеется)
    raw_link = (photo.get("user_tg_channel_link") or photo.get("tg_channel_link") or "").strip()
    if raw_link:
        display = raw_link
        if raw_link.startswith("https://t.me/") or raw_link.startswith("http://t.me/"):
            username = raw_link.split("t.me/", 1)[1].strip("/").strip()
            if username:
                display = "@" + username
        elif raw_link.startswith("@"):
            display = raw_link
        elif "t.me/" in raw_link:
            username = raw_link.split("t.me/", 1)[1].strip("/").strip()
            if username:
                display = "@" + username
        lines.append(f"🔗 Ссылка: {escape(display)}")

    # description as collapsed quote
    description = (photo.get("description") or "").strip()
    if description:
        lines.append("")
        lines.append(quote(description))

    # premium details only on demand
    viewer_is_premium = False
    try:
        viewer_is_premium = await is_user_premium_active(int(viewer_tg_id))
    except Exception:
        viewer_is_premium = False

    if viewer_is_premium and show_details:
        rating_str = "—"
        good_cnt = 0
        bad_cnt = 0

        # best-effort: эти функции могут отсутствовать — тогда просто покажем нули/прочерки
        try:
            from database import get_photo_stats, get_photo_ratings_stats  # type: ignore

            try:
                ps = await get_photo_stats(int(photo["id"]))
                v = ps.get("bayes_score")
                if v is not None:
                    rating_str = f"{float(v):.2f}".rstrip("0").rstrip(".")
            except Exception:
                rating_str = "—"

            try:
                rs = await get_photo_ratings_stats(int(photo["id"]))
                good_cnt = int(rs.get("good_count") or 0)
                bad_cnt = int(rs.get("bad_count") or 0)
            except Exception:
                good_cnt = 0
                bad_cnt = 0

        except Exception:
            pass

        details = "\n".join([
            "📊 Статистика:",
            f"Рейтинг: {rating_str}",
            f"Кол-во 6–10: {good_cnt}",
            f"Кол-во 1–5: {bad_cnt}",
        ])

        lines.append("")
        lines.append(quote(details))

    return "\n".join(lines)

async def show_next_photo_for_rating(callback: CallbackQuery, user_id: int) -> None:
    """
    Показать следующую фотографию для оценивания, стараясь переиспользовать текущее сообщение.

    • Если фотографий нет — меняем текст/подпись текущего сообщения.
    • Если фотография есть:
      – если текущее сообщение уже с фото — меняем медиа;
      – если текущее сообщение текстовое — удаляем его и отправляем новое с фото.
    """
    photo = await get_random_photo_for_rating(user_id)
    message = callback.message

    # Проверяем, есть ли у оценивающего пользователя активный премиум
    is_premium = False
    try:
        user_for_rate = await get_user_by_id(user_id)
        if user_for_rate and user_for_rate.get("tg_id"):
            is_premium = await is_user_premium_active(user_for_rate["tg_id"])
    except Exception:
        is_premium = False

    #### Нет фотографий для оценивания
    if photo is None:
        kb = build_no_photos_keyboard()
        text = build_no_photos_text()

        try:
            if message.photo:
                # Сообщение с фото — меняем только подпись
                await message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
            else:
                # Обычное текстовое сообщение — меняем текст
                await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            # Если не получилось отредактировать — удаляем текущее и отправляем новое
            try:
                await message.delete()
            except Exception:
                pass

            try:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_notification=True,
                )
            except Exception:
                pass

        await callback.answer()
        return

    #### Фотография найдена
    # Проверяем, есть ли у автора главная ачивка «Бета-тестер бота»
    try:
        has_beta_award = False
        author_user_id = photo.get("user_id")
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
        # Если что-то пошло не так при загрузке наград — просто не показываем ачивку
        photo["has_beta_award"] = False

        # --- подтягиваем данные автора для оценивания (ссылка + активный премиум) ---
    try:
        author_user = await get_user_by_id(int(photo["user_id"]))
        if author_user:
            # Link for caption
            if not photo.get("user_tg_channel_link"):
                photo["user_tg_channel_link"] = author_user.get("tg_channel_link")

            # IMPORTANT: caption shows link only if author has ACTIVE premium
            try:
                tg_id = author_user.get("tg_id")
                if tg_id:
                    photo["user_is_premium"] = await is_user_premium_active(int(tg_id))
                else:
                    photo["user_is_premium"] = bool(author_user.get("is_premium"))
            except Exception:
                photo["user_is_premium"] = bool(author_user.get("is_premium"))
    except Exception:
        pass

    caption = await build_rate_caption(photo, viewer_tg_id=int(callback.from_user.id), show_details=False)
    kb = build_rate_keyboard(photo["id"], is_premium=is_premium, show_details=False)
    if message.photo:
        try:
            await message.edit_media(
                media=InputMediaPhoto(media=photo["file_id"], caption=caption, parse_mode="HTML"),
                reply_markup=kb,
            )
        except Exception:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=photo["file_id"],
                    caption=caption,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_notification=True,
                )
            except Exception:
                pass
    else:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=photo["file_id"],
                caption=caption,
                reply_markup=kb,
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception:
            pass

    await callback.answer()


@router.callback_query(F.data.startswith("rate:comment:"))
async def rate_comment(callback: CallbackQuery, state: FSMContext) -> None:
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
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:rate")],
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

    reasons = get_report_reasons()
    buttons = [
        [
            InlineKeyboardButton(
                text=REPORT_REASON_LABELS[reason],
                callback_data=f"rate:report_reason:{reason}:{photo_id}",
            )
        ]
        for reason in reasons
    ]
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:rate")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

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

    # await state.update_data(comment_text=text)  # removed per instructions
    await message.delete()

    # --- Save comment immediately (so it is visible in upload/my photo) ---
    user_for_rate = await get_user_by_tg_id(message.from_user.id)
    saved = False
    save_error: Exception | None = None

    if user_for_rate and user_for_rate.get("id"):
        try:
            await create_comment(
                user_id=int(user_for_rate["id"]),
                photo_id=int(photo_id),
                text=text,
                is_public=bool(is_public),
            )
            saved = True
            # Mark saved so we don't duplicate-save on score click
            await state.update_data(comment_saved=True)
        except Exception as e:
            save_error = e
            # Log exact reason to DB error logs (so you can see permissions/FK issues)
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
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:rate")]]
        )
        err_txt = "Не удалось сохранить комментарий. Попробуй ещё раз чуть позже."
        if save_error is not None:
            # show short error for debugging (without traceback)
            err_txt += f"\n\nПричина: {type(save_error).__name__}: {save_error}"
        await message.bot.edit_message_caption(
            chat_id=rate_chat_id,
            message_id=rate_msg_id,
            caption=err_txt,
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()
        return

    # Notify photo author about the new comment ONLY if it was saved
    try:
        photo = await get_photo_by_id(int(photo_id))
    except Exception:
        photo = None

    if photo is not None:
        author_user_id = photo.get("user_id")
        if author_user_id and user_for_rate and author_user_id != user_for_rate.get("id"):
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

    # --- Success: return the user to the rating UI and exit the comment state ---
    is_premium_rater = False
    try:
        is_premium_rater = await is_user_premium_active(int(message.from_user.id))
    except Exception:
        is_premium_rater = False

    try:
        photo_for_caption = photo
        if photo_for_caption is None:
            photo_for_caption = await get_photo_by_id(int(photo_id))

        if photo_for_caption is not None:
            base_caption = await build_rate_caption(photo_for_caption, viewer_tg_id=int(message.from_user.id), show_details=False)
            success_caption = "✅ Комментарий отправлен!\n\n" + base_caption
        else:
            success_caption = "✅ Комментарий отправлен!\n\nМожешь поставить оценку этому кадру 👇"

        await message.bot.edit_message_caption(
            chat_id=rate_chat_id,
            message_id=rate_msg_id,
            caption=success_caption,
            reply_markup=build_rate_keyboard(int(photo_id), is_premium=is_premium_rater, show_details=False),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass

    await state.clear()
    return

@router.message(RateStates.waiting_report_text)
async def rate_report_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photo_id = data.get("report_photo_id")
    report_msg_id = data.get("report_msg_id")
    report_chat_id = data.get("report_chat_id")
    report_reason = data.get("report_reason") or "other"

    # from database import get_photo_by_id, get_moderators  # локальный импорт, чтобы избежать циклов

    if photo_id is None or report_msg_id is None or report_chat_id is None:
        await state.clear()
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

    user = await get_user_by_tg_id(message.from_user.id)
    await message.delete()

    if user is None:
        await state.clear()
        return
    
    reason_code = report_reason
    reason_label = REPORT_REASON_LABELS.get(reason_code, "Другое")

    await create_photo_report(
        photo_id=photo_id,
        user_id=user["id"],
        reason=reason_code,
        details=text,
    )

    stats_dict = await get_photo_report_stats(photo_id)
    stats = ReportStats(
        photo_id=photo_id,
        total_pending=stats_dict.get("total_pending", 0),
        total_all=stats_dict.get("total_all", 0),
    )

    decision = decide_after_new_report(stats)

    author_name = user.get("name") or ""
    username = user.get("username")
    if username:
        author = f"{author_name} (@{username})" if author_name else f"@{username}"
    else:
        author = author_name or f"id {user['tg_id']}"

    admin_text_lines = [
        "⚠️ <b>Новая жалоба на фотографию</b>",
        "",
        f"Фото ID: <code>{photo_id}</code>",
        f"От: {author}",
        f"Причина: {reason_label}",
        "",
        "Текст жалобы:",
        text,
    ]
    admin_text = "\n".join(admin_text_lines)
    

    moderators = await get_moderators()
    for moderator in moderators:
        tg_id = moderator.get("tg_id")
        if not tg_id:
            continue
        try:
            await message.bot.send_message(
                chat_id=tg_id,
                text=admin_text,
                parse_mode="HTML",
            )
        except Exception:
        # Если какому-то модератору не можем доставить сообщение, просто идём дальше
            continue
    

    if decision.should_mark_under_review:
        await set_photo_moderation_status(photo_id, "under_review")

        try:
            photo = await get_photo_by_id(photo_id)
        except Exception:
            photo = None

        if photo is not None:
            mod_caption_lines = [
                "⚠️ <b>Фотография отправлена на проверку</b>",
                "",
                f"ID фото: <code>{photo_id}</code>",
                f"Автор user_id: <code>{photo['user_id']}</code>",
                f"Активных жалоб: {stats.total_pending}",
                f"Всего жалоб: {stats.total_all}",
                "",
                "Последняя жалоба:",
                f"Причина: {reason_label}",
                "Описание:",
                text,
            ]
            mod_caption = "\n".join(mod_caption_lines)

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Всё хорошо",
                            callback_data=f"mod:photo_ok:{photo_id}",
                        ),
                        InlineKeyboardButton(
                            text="⛔ Отключить",
                            callback_data=f"mod:photo_block:{photo_id}",
                        ),
                    ]
                ]
            )

            moderators = await get_moderators()
            for moderator in moderators:
                tg_id = moderator.get("tg_id")
                if not tg_id:
                    continue
                try:
                    await message.bot.send_photo(
                        chat_id=tg_id,
                        photo=photo["file_id"],
                        caption=mod_caption,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    # Если какому-то модератору не можем доставить фото, просто идём дальше
                    continue

    photo = await get_photo_by_id(photo_id)
    if photo is not None:
        caption = await build_rate_caption(photo, viewer_tg_id=int(message.from_user.id), show_details=False)

        is_premium = False
        try:
            user_for_rate = await get_user_by_tg_id(message.from_user.id)
            if user_for_rate and user_for_rate.get("tg_id"):
                is_premium = await is_user_premium_active(user_for_rate["tg_id"])
        except Exception:
            is_premium = False

        kb = build_rate_keyboard(photo_id, is_premium=is_premium, show_details=False)
        try:
            await message.bot.edit_message_caption(
                chat_id=report_chat_id,
                message_id=report_msg_id,
                caption=caption,
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            try:
                await message.bot.delete_message(chat_id=report_chat_id, message_id=report_msg_id)
            except Exception:
                pass

            try:
                await message.bot.send_photo(
                    chat_id=report_chat_id,
                    photo=photo["file_id"],
                    caption=caption,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_notification=True,
                )
            except Exception:
                pass

    await state.clear()
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

    data = await state.get_data()
    comment_photo_id = data.get("photo_id")
    comment_text = data.get("comment_text")
    is_public = data.get("is_public", True)

    if comment_photo_id == photo_id and comment_text and not data.get("comment_saved"):
        # 1) Сохраняем комментарий
        await create_comment(
            user_id=user["id"],
            photo_id=photo_id,
            text=comment_text,
            is_public=bool(is_public),
        )

        # 2) Пытаемся уведомить автора фотографии
        try:
            photo = await get_photo_by_id(photo_id)
        except Exception:
            photo = None

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
        photo_row = await get_photo_by_id(int(photo_id))
        if photo_row:
            author_user_id = photo_row.get("user_id")
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

    await show_next_photo_for_rating(callback, user["id"])

    await state.clear()


@router.callback_query(F.data.startswith("rate:score:"))
async def rate_score(callback: CallbackQuery, state: FSMContext) -> None:
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

    # Достаём возможный комментарий из FSM
    data = await state.get_data()
    comment_photo_id = data.get("photo_id")
    comment_text = data.get("comment_text")
    is_public = data.get("is_public", True)

    # Если к этой же фотке только что писали комментарий — сохраняем его и шлём уведомление автору
    if comment_photo_id == photo_id and comment_text and not data.get("comment_saved"):
        await create_comment(
            user_id=user["id"],
            photo_id=photo_id,
            text=comment_text,
            is_public=bool(is_public),
        )

        try:
            photo = await get_photo_by_id(photo_id)
        except Exception:
            photo = None

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
        photo_row = await get_photo_by_id(int(photo_id))
        if photo_row:
            author_user_id = photo_row.get("user_id")
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
    await show_next_photo_for_rating(callback, user["id"])

    # Чистим состояние (комментарий больше не нужен)
    await state.clear()

@router.callback_query(F.data.startswith("rate:more:"))
async def rate_more_toggle(callback: CallbackQuery) -> None:
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

    viewer_is_premium = False
    try:
        viewer_is_premium = await is_user_premium_active(int(callback.from_user.id))
    except Exception:
        viewer_is_premium = False

    if not viewer_is_premium:
        await callback.answer("Это доступно только в GlowShot Premium 💎", show_alert=True)
        return

    try:
        photo = await get_photo_by_id(photo_id)
    except Exception:
        photo = None

    if not photo or photo.get("is_deleted"):
        await callback.answer("Фото не найдено.", show_alert=True)
        return

    # подтянем флаг и ссылку best-effort
    if "has_beta_award" not in photo:
        photo["has_beta_award"] = False
    if "user_tg_channel_link" not in photo:
        try:
            author = await get_user_by_id(int(photo.get("user_id") or 0))
        except Exception:
            author = None
        if author and author.get("tg_channel_link"):
            photo["user_tg_channel_link"] = author.get("tg_channel_link")

    caption = await build_rate_caption(photo, viewer_tg_id=int(callback.from_user.id), show_details=to_show)
    kb = build_rate_keyboard(photo_id, is_premium=True, show_details=to_show)

    try:
        await callback.message.edit_caption(caption=caption, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

    await callback.answer("Ок")

@router.callback_query(F.data.startswith("rate:skip:"))
async def rate_skip(callback: CallbackQuery, state: FSMContext) -> None:
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

    tg_id = user.get("tg_id")
    is_premium = False
    if tg_id:
        try:
            is_premium = await is_user_premium_active(tg_id)
        except Exception:
            is_premium = False

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
    await show_next_photo_for_rating(callback, user["id"])



@router.callback_query(F.data == "rate:start")
async def rate_start(callback: CallbackQuery) -> None:
    await rate_root(callback)

@router.callback_query(F.data == "menu:rate")
async def rate_root(callback: CallbackQuery) -> None:
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, попробуй /start.", show_alert=True)
        return

    await show_next_photo_for_rating(callback, user["id"])
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