from aiogram import Router, F
import html
from utils.i18n import t
from utils.banner import ensure_giraffe_banner
from aiogram.types import InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from datetime import datetime
from handlers.streak import (
    get_profile_streak_badge_and_line,
    get_profile_streak_status,
    toggle_profile_streak_notify_and_status,
)
from handlers.premium import _render_premium_menu

from database import (
    get_user_by_tg_id,
    update_user_name,
    update_user_gender,
    update_user_age,
    update_user_bio,
    update_user_channel_link,
    soft_delete_user,
    count_photos_by_user,
    count_active_photos_by_user,
    get_user_rating_summary,
    get_user_rank_by_tg_id,
    get_most_popular_photo_for_user,
    get_user_premium_status,
    is_user_premium_active,
    get_awards_for_user,
    get_user_by_id,
    update_user_city,
    update_user_country,
    set_user_city_visibility,
    get_notify_settings_by_tg_id,
    toggle_likes_notify_by_tg_id,
    toggle_comments_notify_by_tg_id,
    set_user_language_by_tg_id,
    get_ads_enabled_by_tg_id,
    set_ads_enabled_by_tg_id,
    get_user_block_status_by_tg_id,
    ensure_user_author_code,
    set_all_user_photos_ratings_enabled,
    set_user_allow_ratings_by_tg_id,
    set_user_screen_msg_id,
)
from keyboards.common import build_back_kb, build_confirm_kb
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from utils.antispam import should_throttle
from utils.places import validate_city_and_country_full
from utils.flags import country_to_flag, country_display
from utils.ranks import rank_from_points, format_rank, RANK_BEGINNER, RANK_AMATEUR, RANK_EXPERT, rank_progress_bar
from utils.time import get_moscow_now

router = Router()


# Helper to get language from user dict
def _get_lang(user: object | None) -> str:
    if not user:
        return "ru"

    keys = ("lang", "language", "language_code", "locale")

    def _read(u: object, k: str):
        try:
            if isinstance(u, dict):
                return u.get(k)
        except Exception:
            pass
        try:
            if k in u:  # type: ignore[operator]
                return u[k]  # type: ignore[index]
        except Exception:
            pass
        try:
            return getattr(u, k)
        except Exception:
            return None

    raw = None
    for k in keys:
        v = _read(user, k)
        if v:
            raw = v
            break

    if not raw:
        try:
            d = dict(user)  # type: ignore[arg-type]
            for k in keys:
                if d.get(k):
                    raw = d.get(k)
                    break
        except Exception:
            pass

    if not raw:
        return "ru"

    try:
        s = str(raw).strip().lower().split("-")[0]
        return s if s in ("ru", "en") else "ru"
    except Exception:
        return "ru"


class ProfileEditStates(StatesGroup):
    waiting_new_name = State()
    waiting_new_age = State()
    waiting_new_bio = State()
    waiting_new_channel = State()
    waiting_new_city = State()
    # legacy: country is derived from city, but keep state to avoid crashes from old callbacks
    waiting_new_country = State()



def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    """
    Простое склонение русских слов по числу:
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


def _pick_rated_verb(user: dict | None, lang: str) -> str:
    """Выбирает форму глагола для строки «Ты оценил/оценила»."""
    if lang == "en":
        return "rated"

    gender_val = None
    if user:
        for k in (
            "gender",
            "sex",
            "profile_gender",
            "gender_code",
            "gender_value",
        ):
            try:
                v = user.get(k)
            except Exception:
                v = None
            if v is not None:
                gender_val = v
                break

    gender_s = str(gender_val).strip().lower() if gender_val is not None else ""

    def _is_female(s: str) -> bool:
        return (
            s in {"f", "female", "woman", "girl", "ж", "жен", "женский", "♀", "девушка", "женщина"}
            or "жен" in s
            or "дев" in s
        )

    def _is_male(s: str) -> bool:
        return (
            s in {"m", "male", "man", "boy", "м", "муж", "мужской", "♂", "парень", "мужчина"}
            or "муж" in s
            or "пар" in s
        )

    if _is_female(gender_s):
        return "оценила"
    if _is_male(gender_s):
        return "оценил"
    return "оценил(а)"


@router.callback_query(F.data.startswith("myresults:"))
async def profile_my_results(callback: CallbackQuery):
    """Temporary stub: user said they will change the logic for "Мои итоги" later."""
    await callback.message.edit_text(
        "🏅 <b>Мои итоги</b>\n\n"
        "Пока тут заглушка — скоро будет новая логика итогов 💅",
        reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ В профиль"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_profile(callback: CallbackQuery):
    await callback.answer()


async def build_profile_view(user: dict):
    """
    Собирает основной вид профиля с новой структурой и реальными данными.
    """
    name_raw = user.get("name") or "—"
    name_clean = str(name_raw).strip()
    if name_clean.startswith("@"):
        name_clean = name_clean.lstrip("@").strip() or "—"
    name = html.escape(name_clean, quote=False)
    age = user.get("age")
    age_part = f", {age}" if age else ""

    # Пол смайликом
    gender_raw = user.get("gender")
    if gender_raw == "Парень":
        gender_icon = "🙋‍♂️"
    elif gender_raw == "Девушка":
        gender_icon = "🙋‍♀️"
    elif gender_raw in ("Не важно", None, ""):
        gender_icon = "❔"
    else:
        gender_icon = "❔"

    # Дни в боте по created_at, если есть
    days_in_bot = "—"
    created_at = user.get("created_at")
    if created_at:
        try:
            # Пытаемся разобрать ISO-дату или стандартный формат
            try:
                dt = datetime.fromisoformat(created_at)
            except ValueError:
                dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            delta = datetime.now() - dt
            days = max(1, delta.days + 1)
            days_in_bot = str(days)
        except Exception:
            days_in_bot = "—"

    author_code = "—"
    tg_id = user.get("tg_id")
    if tg_id:
        try:
            author_code = await ensure_user_author_code(int(tg_id))
        except Exception:
            author_code = "—"

    # Реальная статистика по фото
    total_photos = "—"
    avg_rating_text = "—"
    popular_photo_line = "—"
    weekly_top_position = "—"
    rated_by_me_text = "—"

    user_id = user.get("id")
    safe_user_id_int = None
    safe_tg_id_int = None
    try:
        safe_user_id_int = int(user_id) if user_id is not None else None
    except Exception:
        safe_user_id_int = None
    try:
        safe_tg_id_int = int(tg_id) if tg_id is not None else None
    except Exception:
        safe_tg_id_int = None

    rater_ids_for_given: list[int] = []
    for rid in (safe_user_id_int, safe_tg_id_int):
        if rid is None:
            continue
        if rid not in rater_ids_for_given:
            rater_ids_for_given.append(rid)
    ratings_given_count: int | None = 0 if rater_ids_for_given else None

    lang = _get_lang(user)
    rated_verb = _pick_rated_verb(user, lang)

    # Streak badge (optional)
    streak_badge = ""
    streak_short_line = ""
    if tg_id:
        streak_badge, streak_short_line = await get_profile_streak_badge_and_line(int(tg_id))

    # Rank (cached)
    rank_label = None
    rank_points_value: int | None = None
    if tg_id:
        try:
            r_raw = await get_user_rank_by_tg_id(int(tg_id))
            if not r_raw:
                r = {}
            elif isinstance(r_raw, dict):
                r = r_raw
            else:
                # asyncpg.Record / mapping-like
                try:
                    r = dict(r_raw)
                except Exception:
                    r = {}

            by_code = {
                "beginner": RANK_BEGINNER,
                "amateur": RANK_AMATEUR,
                "expert": RANK_EXPERT,
            }

            def _render(code: str) -> str | None:
                code = (code or "").strip().lower()
                rk = by_code.get(code)
                if not rk:
                    return None
                title = t(rk.i18n_key, lang)
                return f"{rk.emoji} {title}".strip()

            # Prefer points (best, fully localizable)
            rank_points = r.get("rank_points") or r.get("points") or r.get("rank_pts")
            if rank_points is not None:
                rank_points_value = int(rank_points)
                rk = rank_from_points(rank_points_value)
                rank_label = f"{rk.emoji} {t(rk.i18n_key, lang)}".strip()
            else:
                # Prefer code if available
                rank_code = (r.get("rank_code") or r.get("code") or "").strip().lower()
                rank_label = _render(rank_code)

                # Legacy: sometimes DB stores i18n key or code in `rank_label`
                if not rank_label:
                    legacy = r.get("rank_label")
                    if isinstance(legacy, str):
                        s = legacy.strip()
                        if s.startswith("rank."):
                            # e.g. rank.beginner
                            rank_label = _render(s.split(".", 1)[1]) or f"{RANK_BEGINNER.emoji} {t(s, lang)}".strip()
                        elif s in by_code:
                            rank_label = _render(s)
                        else:
                            # already human label
                            rank_label = s or None

        except Exception:
            rank_label = None

    # Полоска прогресса рядом с рангом
    progress_bar = rank_progress_bar(rank_points_value or 0)
    rank_display = f"{rank_label or format_rank(0, lang=lang)} ({progress_bar})"

    if user_id:
        # Всего загружено фото
        try:
            total = await count_photos_by_user(user_id)          # теперь “всё время”
            active = await count_active_photos_by_user(user_id)  # “сейчас”
            total_photos = t("profile.stats.total_photos.value", lang, total=total, active=active)
        except Exception:
            total_photos = "—"

        # Средняя оценка и количество оценок (умная/байесовская)
        try:
            summary = await get_user_rating_summary(user_id) or {}
            if ratings_given_count is not None:
                try:
                    if safe_user_id_int in rater_ids_for_given:
                        rater_ids_for_given.remove(safe_user_id_int)
                except Exception:
                    pass
                try:
                    ratings_given_count += int(summary.get("ratings_given") or 0)
                except Exception:
                    pass

            cnt = int(summary.get("ratings_received") or 0)
            avg_raw = summary.get("avg_received")
            bayes_raw = summary.get("bayes_received")

            avg_val = float(avg_raw) if avg_raw is not None else None
            bayes_val = float(bayes_raw) if bayes_raw is not None else None

            if cnt <= 0:
                avg_rating_text = "—"
            else:
                # Show only the smart (Bayesian) average
                if bayes_val is not None:
                    bayes_str = f"{bayes_val:.2f}".rstrip("0").rstrip(".")
                    avg_rating_text = t("profile.stats.avg_rating.value", lang, score=bayes_str, count=cnt)
                elif avg_val is not None:
                    avg_str = f"{avg_val:.2f}".rstrip("0").rstrip(".")
                    avg_rating_text = t("profile.stats.avg_rating.value", lang, score=avg_str, count=cnt)
                else:
                    avg_rating_text = t("profile.stats.avg_rating.value", lang, score="—", count=cnt)
        except Exception:
            avg_rating_text = "—"

        # Самое популярное фото (по умному скору)
        try:
            popular = await get_most_popular_photo_for_user(user_id)
            # Fallback: some legacy rows or callers may require tg_id matching
            if not popular and tg_id:
                try:
                    popular = await get_most_popular_photo_for_user(int(tg_id))
                except Exception:
                    popular = None

            if popular:
                title = html.escape(str(popular.get("title") or "Без названия"), quote=False)
                is_deleted = bool(popular.get("is_deleted") or 0)
                if is_deleted:
                    title = f"{title} (архив)"

                ratings_count = int(popular.get("ratings_count") or 0)
                avg_pop_raw = popular.get("avg_rating")
                bayes_pop_raw = popular.get("bayes_score")

                avg_pop = float(avg_pop_raw) if avg_pop_raw is not None else None
                bayes_pop = float(bayes_pop_raw) if bayes_pop_raw is not None else None

                metric_parts: list[str] = []
                if bayes_pop is not None:
                    metric_parts.append(f"{f'{bayes_pop:.2f}'.rstrip('0').rstrip('.')}★")
                elif avg_pop is not None:
                    metric_parts.append(f"{f'{avg_pop:.2f}'.rstrip('0').rstrip('.')}★")

                if ratings_count > 0:
                    metric_parts.append(t("profile.stats.popular_photo.ratings", lang, count=ratings_count))
                else:
                    metric_parts.append(t("profile.stats.popular_photo.no_ratings", lang))

                metric = ", ".join(metric_parts) if metric_parts else t("profile.stats.popular_photo.no_ratings", lang)
                popular_photo_line = f"{title} ({metric})"
            else:
                popular_photo_line = "—"
        except Exception as e:
            popular_photo_line = "—"
            print("POPULAR PHOTO ERROR:", repr(e))

        # Позиция в топе недели
        weekly_top_position = "—"

    # Добавляем оценки, поставленные с альтернативного ID (например, tg_id)
    if ratings_given_count is not None and rater_ids_for_given:
        for rid in list(rater_ids_for_given):
            try:
                extra_summary = await get_user_rating_summary(rid) or {}
                ratings_given_count += int(extra_summary.get("ratings_given") or 0)
            except Exception:
                pass
        rater_ids_for_given.clear()

    if ratings_given_count is not None:
        rated_by_me_text = str(ratings_given_count)

    # GlowShot Premium status
    if lang == "en":
        premium_status_line = "inactive (available to buy)"
    else:
        premium_status_line = "нет (доступно для покупки)"

    premium_badge = ""
    premium_extra_line = ""
    premium_active = False

    if tg_id:
        try:
            raw_status = await get_user_premium_status(tg_id)
            is_active = await is_user_premium_active(tg_id)
            had_premium = bool(raw_status and raw_status.get("is_premium"))

            if is_active:
                premium_active = True
                until = raw_status.get("premium_until")
                if until:
                    try:
                        dt = datetime.fromisoformat(until)
                        human_until = dt.strftime("%d.%m.%Y")

                        if lang == "en":
                            premium_status_line = f"active (until {human_until})"
                        else:
                            premium_status_line = f"активен (до {human_until})"

                        # Days left (only if date is in the future)
                        try:
                            days_left = (dt.date() - datetime.now().date()).days
                            if days_left >= 0:
                                if lang == "en":
                                    day_word = "day" if days_left == 1 else "days"
                                    premium_extra_line = f"Left: {days_left} {day_word}."
                                else:
                                    days_text = _plural_ru(days_left, "день", "дня", "дней")
                                    premium_extra_line = f"Осталось: {days_left} {days_text}."
                        except Exception:
                            pass
                    except Exception:
                        if lang == "en":
                            premium_status_line = f"active until {until}"
                        else:
                            premium_status_line = f"активен до {until}"
                else:
                    if lang == "en":
                        premium_status_line = "active (no expiry)"
                        premium_extra_line = "Subscription has no end date."
                    else:
                        premium_status_line = "активен (бессрочно)"
                        premium_extra_line = "Подписка без ограничения по дате."
                premium_badge = " 💎"
            else:
                # Flag may be set but the subscription expired
                if had_premium and raw_status.get("premium_until"):
                    if lang == "en":
                        premium_status_line = "expired"
                        premium_extra_line = "You can extend your subscription using the button below."
                    else:
                        premium_status_line = "срок действия истёк"
                        premium_extra_line = "Ты можешь продлить подписку через кнопку ниже."
                elif had_premium:
                    if lang == "en":
                        premium_status_line = "expired"
                        premium_extra_line = "You can buy Premium again using the button below."
                    else:
                        premium_status_line = "срок действия истёк"
                        premium_extra_line = "Ты можешь заново оформить подписку через кнопку ниже."
                else:
                    if lang == "en":
                        premium_extra_line = "You can buy Premium using the button below."
                    else:
                        premium_extra_line = "Оформить премиум можно через кнопку ниже."
        except Exception:
            # В случае ошибки не ломаем профиль
            pass

    text_lines = [
        f"{t('profile.title', lang)}{premium_badge}{streak_badge}",
        t("profile.name_age", lang, name=name, age=age) if age else t("profile.name", lang, name=name),
        t("profile.rank", lang, rank=rank_display),
        t("profile.gender_line", lang, gender=gender_icon),
        f"🧾 Код автора: <code>{html.escape(str(author_code), quote=False)}</code>",
    ]

    # Локация (опционально + можно скрыть)
    city = (user.get("city") or "").strip()
    country = (user.get("country") or "").strip()
    show_city = bool(user.get("show_city", 1))
    show_country = bool(user.get("show_country", 1))

    loc_parts: list[str] = []
    # по запросу: сначала страна, потом город
    if country and show_country:
        loc_parts.append(country_display(country))
    if city and show_city:
        loc_parts.append(city)

    if loc_parts:
        flag = country_to_flag(country) if (country and show_country) else "📍"
        text_lines.append(t("profile.location_line", lang, flag=flag, loc=", ".join(loc_parts)))

    # Ссылка (для премиум-пользователей, если указана)
    tg_link = user.get("tg_channel_link")
    if tg_link:
        display_link = tg_link.strip()
        lower = display_link.lower()
        username = None

        # Если уже @username — просто нормализуем
        if display_link.startswith("@"):
            username = display_link[1:].strip() or None
        else:
            # Пытаемся вытащить username из ссылки вида t.me/username или telegram.me/username
            if "t.me/" in lower:
                part = display_link.split("t.me/", 1)[1]
                part = part.split("/", 1)[0]
                part = part.split("?", 1)[0]
                username = part.strip() or None
            elif "telegram.me/" in lower:
                part = display_link.split("telegram.me/", 1)[1]
                part = part.split("/", 1)[0]
                part = part.split("?", 1)[0]
                username = part.strip() or None

        if username:
            display_link = f"@{username}"

        text_lines.append(t("profile.link", lang, link=display_link))

    # Описание (свернутое)
    bio_raw = html.escape(str(user.get("bio") or "—"), quote=False)
    text_lines.extend([
        "",
        t("profile.section.bio", lang),
        f"<blockquote expandable>{bio_raw}</blockquote>",
        "",
    ])

    # --- "Свернутая" статистика  ---
    stats_lines = [
        t("profile.stats.total_photos", lang, value=total_photos),
        t("profile.stats.days_in_bot", lang, value=days_in_bot),
        t("menu.rated_by_me", lang, verb=rated_verb, value=rated_by_me_text),
        streak_short_line or t("profile.stats.streak", lang, value="—"),
        t("profile.stats.avg_rating", lang, value=avg_rating_text),
        t("profile.stats.popular_photo", lang, value=popular_photo_line),
    ]

    # --- Статистика как цитата ---
    stats_body = "\n".join([html.escape(line, quote=False) for line in stats_lines])
    text_lines.append(t("profile.section.stats", lang))
    text_lines.append(f"<blockquote expandable>{stats_body}</blockquote>")

    # Premium
    text_lines.extend([
        "",
        t("profile.section.premium", lang),
        t("profile.premium.status", lang, status=premium_status_line),
    ])

    if premium_extra_line:
        text_lines.append(premium_extra_line)
    text = "\n".join(text_lines)

    kb = InlineKeyboardBuilder()
    if user.get("is_author"):
        kb.button(text=t("profile.btn.author_menu", lang), callback_data="author:menu")
    else:
        kb.button(text=t("profile.btn.be_author", lang), callback_data="profile:be_author")
    kb.button(text=t("profile.btn.awards", lang), callback_data="profile:awards")
    kb.button(text=t("profile.btn.edit", lang), callback_data="profile:edit")
    kb.button(text=t("profile.btn.settings", lang), callback_data="profile:settings")
    kb.button(text=t("profile.btn.streak", lang), callback_data="profile:streak")

    premium_button_text = t("profile.btn.premium.my", lang) if premium_active else "💎 Оформить"
    kb.button(text=t("profile.btn.menu", lang), callback_data="menu:back")
    kb.button(text=premium_button_text, callback_data="profile:premium")
    kb.adjust(1, 2, 2, 2)
    return text, kb.as_markup()


@router.callback_query(F.data == "profile:open")
async def profile_menu(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "profile:open", 1.0):
        try:
            await callback.answer("Секунду…", show_alert=False)
        except Exception:
            pass
        return
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, странно. Попробуй /start.", show_alert=True)
        return
    try:
        await ensure_giraffe_banner(callback.message.bot, callback.message.chat.id, callback.from_user.id)
    except Exception:
        pass

    text, markup = await build_profile_view(user)

    sent = await callback.message.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
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

    await callback.answer()



@router.callback_query(F.data == "profile:premium_toggle_admin")
async def profile_premium_toggle_admin(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    if not (user.get("is_admin") or user.get("is_moderator")):
        await callback.answer("Доступно только администраторам и модераторам.", show_alert=True)
        return

    tg_id = int(user.get("tg_id") or callback.from_user.id)
    current = await is_user_premium_active(tg_id)

    # бесcрочно: ставим premium_until далеко в будущем
    from database import set_user_premium_status  # type: ignore
    import datetime

    if current:
        await set_user_premium_status(tg_id, False, premium_until=None)
        new_state = False
    else:
        distant = (datetime.datetime.utcnow() + datetime.timedelta(days=3650)).isoformat()
        await set_user_premium_status(tg_id, True, premium_until=distant)
        new_state = True

    # Перестраиваем меню премиума (остаемся в том же экране)
    try:
        await _render_premium_menu(callback, back_cb="menu:profile", from_menu=False)
    except Exception:
        # fallback: обновить профиль
        text, markup = await build_profile_view(await get_user_by_tg_id(tg_id))
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass

    await callback.answer("Премиум включён" if new_state else "Премиум выключён")


# Handler for menu:profile to return to profile view from nested sections
@router.callback_query(F.data == "menu:profile")
async def profile_back_to_profile(callback: CallbackQuery):
    """
    Возврат к просмотру профиля из вложенных разделов (награды, настройки и т.п.).
    """
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, странно. Попробуй /start.", show_alert=True)
        return

    text, markup = await build_profile_view(user)

    # Внутри раздела стараемся идти через редактирование, чтобы не плодить сообщения.
    try:
        await callback.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        # если не редактируется (например, было фото) — удалим и отправим новое
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_notification=True,
        )

    await callback.answer()


async def _build_profile_edit_screen(callback_or_msg, user: dict, state: FSMContext | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Собирает текст и клавиатуру меню редактирования профиля.
    Сохраняет message_id/chat_id в state, если передан FSMContext.
    """
    lang = _get_lang(user)
    from_user = getattr(callback_or_msg, "from_user", None)
    tg_id = getattr(from_user, "id", None) or user.get("tg_id")
    block_status = await get_user_block_status_by_tg_id(tg_id)
    is_blocked = bool(block_status.get("is_blocked"))
    until_dt = None
    raw_until = block_status.get("block_until")
    if raw_until:
        try:
            until_dt = datetime.fromisoformat(str(raw_until))
        except Exception:
            until_dt = None
    if is_blocked and until_dt is not None and until_dt <= get_moscow_now():
        is_blocked = False

    kb = InlineKeyboardBuilder()
    text_lines: list[str] = [t("profile.edit.title", lang)]

    if is_blocked:
        text_lines.append("")
        text_lines.append("⛔ Аккаунт заблокирован модераторами.")
        if until_dt is not None:
            text_lines.append(f"Блокировка до {until_dt.strftime('%d.%m.%Y %H:%M')} (МСК).")
        text_lines.append("Можно только удалить аккаунт.")
        kb.button(text=t("profile.edit.btn.delete", lang), callback_data="profile:delete")
        kb.button(text=t("common.back", lang), callback_data="menu:profile")
        kb.adjust(1, 1)
    else:
        kb.button(text=t("profile.edit.btn.name", lang), callback_data="profile:edit_name")
        kb.button(text=t("profile.edit.btn.age", lang), callback_data="profile:edit_age")
        kb.button(text=t("profile.edit.btn.bio", lang), callback_data="profile:edit_bio")
        kb.button(text=t("profile.edit.btn.gender", lang), callback_data="profile:edit_gender")
        kb.button(text=t("profile.edit.btn.channel", lang), callback_data="profile:edit_channel")
        kb.button(text=t("profile.edit.btn.city", lang), callback_data="profile:edit_city")
        kb.button(text=t("profile.edit.btn.delete", lang), callback_data="profile:delete")
        kb.button(text=t("common.back", lang), callback_data="menu:profile")
        kb.adjust(2, 2, 2, 1, 1)
        text_lines.append("")
        text_lines.append(t("profile.edit.choose", lang))

    text = "\n".join(text_lines)

    if state is not None and getattr(callback_or_msg, "message", None):
        try:
            await state.update_data(
                edit_msg_id=callback_or_msg.message.message_id,
                edit_chat_id=callback_or_msg.message.chat.id,
            )
        except Exception:
            pass

    return text, kb.as_markup()


def _build_bio_edit_kb(user: dict, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if (user.get("bio") or "").strip():
        kb.button(text=t("profile.edit.bio.clear_btn", lang), callback_data="profile:bio_clear")
    kb.button(text=t("common.back", lang), callback_data="profile:edit")
    kb.adjust(1, 1)
    return kb.as_markup()


@router.callback_query(F.data == "profile:edit")
async def profile_edit_menu(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("User not found", show_alert=True)
        return

    text, markup = await _build_profile_edit_screen(callback, user, state)

    await callback.message.edit_text(
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit_name")
async def profile_edit_name(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    await state.set_state(ProfileEditStates.waiting_new_name)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    await callback.message.edit_text(
        t("profile.edit.name.ask", lang),
        reply_markup=build_back_kb(callback_data="profile:edit", text=t("common.back", lang)),
        parse_mode="HTML",
    )
    await callback.answer()





@router.callback_query(F.data == "profile:edit_channel")
async def profile_edit_channel(callback: CallbackQuery, state: FSMContext):
    """
    Настройка ссылки на Telegram для профиля (доступно только с премиумом).
    Принимаем только телеграм-ссылки или @username.
    """
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    lang = _get_lang(user)

    tg_id = user.get("tg_id")
    is_active = False
    if tg_id:
        try:
            is_active = await is_user_premium_active(tg_id)
        except Exception:
            is_active = False

    is_author = bool(user.get("is_author"))

    if (not is_active) and (not is_author) and not (user.get("tg_channel_link")):
        await callback.answer(
            t("profile.edit.channel.premium_only", lang) + ("\n" + t("profile.edit.channel.author_allowed", lang) if is_author else ""),
            show_alert=True,
        )
        return

    await state.set_state(ProfileEditStates.waiting_new_channel)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    kb = InlineKeyboardBuilder()
    if (user.get("tg_channel_link") or "").strip():
        kb.button(text=t("profile.edit.channel.clear_btn", lang), callback_data="profile:channel_clear")
    kb.button(text=t("common.back", lang), callback_data="profile:edit")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        t("profile.edit.channel.ask", lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


# -------------------- City / Country edit --------------------

def _build_city_kb(user: dict, lang: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    show_city = bool(user.get("show_city", 1))
    kb.button(text=t("profile.edit.city.btn.change", lang), callback_data="profile:city:change")
    kb.button(text=t("profile.edit.city.btn.delete", lang), callback_data="profile:city:delete")
    if show_city:
        kb.button(text=t("profile.edit.city.btn.hide", lang), callback_data="profile:city:toggle")
    else:
        kb.button(text=t("profile.edit.city.btn.show", lang), callback_data="profile:city:toggle")
    kb.button(text=t("common.back", lang), callback_data="profile:edit")
    kb.adjust(2, 2)
    return kb.as_markup()


def _build_country_kb(user: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    show_country = bool(user.get("show_country", 1))
    kb.button(text="✍️ Изменить", callback_data="profile:country:change")
    kb.button(text="🗑 Удалить", callback_data="profile:country:delete")
    kb.button(text=("🙈 Скрыть" if show_country else "👁 Показать"), callback_data="profile:country:toggle")
    kb.button(text="⬅️ Назад", callback_data="profile:edit")
    kb.adjust(2, 2)
    return kb.as_markup()


@router.callback_query(F.data == "profile:edit_city")
async def profile_edit_city(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    lang = _get_lang(user)

    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    city = (user.get("city") or "").strip() or "—"
    show_city = bool(user.get("show_city", 1))
    vis = t("profile.edit.city.vis.on", lang) if show_city else t("profile.edit.city.vis.off", lang)

    text = (
        f"{t('profile.edit.city.title', lang)}\n\n"
        f"{t('profile.edit.city.current', lang, city=city)}\n"
        f"{t('profile.edit.city.visibility', lang, vis=vis)}\n"
    )
    await callback.message.edit_text(text, reply_markup=_build_city_kb(user, lang), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "profile:city:change")
async def profile_city_change(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    await state.set_state(ProfileEditStates.waiting_new_city)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)
    await callback.message.edit_text(
        t("profile.edit.city.ask", lang),
        reply_markup=build_back_kb(callback_data="profile:edit_city", text=t("common.back", lang)),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:city:delete")
async def profile_city_delete(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    if user and user.get("id"):
        await update_user_city(int(user["id"]), None)

    user = await get_user_by_tg_id(callback.from_user.id)
    await callback.message.edit_text(
        t("profile.edit.city.deleted", lang),
        reply_markup=_build_city_kb(user or {}, lang),
        parse_mode="HTML"
    )
    await callback.answer(t("profile.edit.city.deleted_toast", lang))


@router.callback_query(F.data == "profile:city:toggle")
async def profile_city_toggle(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)
    if user and user.get("id"):
        current = bool(user.get("show_city", 1))
        await set_user_city_visibility(int(user["id"]), not current)

    user = await get_user_by_tg_id(callback.from_user.id)
    city = (user.get("city") or "").strip() or "—"
    show_city = bool(user.get("show_city", 1))
    vis = t("profile.edit.city.vis.on", lang) if show_city else t("profile.edit.city.vis.off", lang)

    text = (
        f"{t('profile.edit.city.title', lang)}\n\n"
        f"{t('profile.edit.city.current', lang, city=city)}\n"
        f"{t('profile.edit.city.visibility', lang, vis=vis)}\n"
    )
    await callback.message.edit_text(text, reply_markup=_build_city_kb(user, lang), parse_mode="HTML")
    await callback.answer(t("profile.edit.city.changed_toast", lang))


@router.message(ProfileEditStates.waiting_new_city, F.text)
async def profile_set_city(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    raw = (message.text or "").strip()
    u0 = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(u0)

    if raw.lower() in ("удалить", "delete", "remove"):
        u = await get_user_by_tg_id(message.from_user.id)
        if u and u.get("id"):
            await update_user_city(int(u["id"]), None)
            await update_user_country(int(u["id"]), None)
        await state.clear()
        await message.delete()
        user = await get_user_by_tg_id(message.from_user.id)
        text, markup = await _build_profile_edit_screen(message, user)
        await message.bot.edit_message_text(chat_id=edit_chat_id, message_id=edit_msg_id, text=text, reply_markup=markup, parse_mode="HTML")
        return

    if has_links_or_usernames(raw) or has_promo_channel_invite(raw) or not raw:
        await message.delete()
        return

    # Validate + normalize city + infer country
    is_ok, canonical_city, canonical_country, canonical_country_code, _used_geocoder = await validate_city_and_country_full(raw)
    if not is_ok:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.city.not_found", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_city", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_city(int(u["id"]), canonical_city)
        # Auto-update country if we could infer it
        # Prefer storing ISO code (RU/US/ES/...) when available
        if canonical_country_code:
            await update_user_country(int(u["id"]), canonical_country_code)
        elif canonical_country:
            await update_user_country(int(u["id"]), canonical_country)

    await state.clear()
    await message.delete()
    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await _build_profile_edit_screen(message, user)
    await message.bot.edit_message_text(chat_id=edit_chat_id, message_id=edit_msg_id, text=text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "profile:edit_country")
async def profile_edit_country(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    # Country is derived from city now; manual editing is disabled
    await callback.message.edit_text(
        "🌍 <b>Страна</b>\n\n"
        "Страна определяется автоматически по городу.\n"
        "Чтобы изменить страну — просто измени город 🏙✨",
        reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:country:change")
async def profile_country_change(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🌍 <b>Страна</b>\n\n"
        "Страна определяется автоматически по городу.\n"
        "Чтобы изменить страну — просто измени город 🏙✨",
        reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:country:delete")
async def profile_country_delete(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌍 <b>Страна</b>\n\n"
        "Страна определяется автоматически по городу.\n"
        "Чтобы изменить страну — просто измени город 🏙✨",
        reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:country:toggle")
async def profile_country_toggle(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌍 <b>Страна</b>\n\n"
        "Страна определяется автоматически по городу.\n"
        "Чтобы изменить страну — просто измени город 🏙✨",
        reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ProfileEditStates.waiting_new_country, F.text)
async def profile_set_country(message: Message, state: FSMContext):
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass


# Handler to set channel link for premium users
@router.message(ProfileEditStates.waiting_new_channel, F.text)
async def profile_set_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    raw = (message.text or "").strip()
    u0 = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(u0)

    # Удаление ссылки
    if raw.lower() in ("удалить", "delete", "remove"):
        u = await get_user_by_tg_id(message.from_user.id)
        if u and u.get("id"):
            await update_user_channel_link(int(u["id"]), None)
        await state.clear()
        await message.delete()

        user = await get_user_by_tg_id(message.from_user.id)
        text, markup = await _build_profile_edit_screen(message, user)
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        return

    value = raw.strip()

    # Нормализация: @username или t.me/username -> @username
    username = None
    if value.startswith("@"):
        username = value[1:].strip()
    else:
        lower = value.lower()
        if lower.startswith("https://t.me/") or lower.startswith("http://t.me/"):
            username = value.split("t.me/", 1)[1].strip().strip("/")
        elif lower.startswith("https://telegram.me/") or lower.startswith("http://telegram.me/"):
            username = value.split("telegram.me/", 1)[1].strip().strip("/")
        elif lower.startswith("t.me/"):
            username = value.split("t.me/", 1)[1].strip().strip("/")
        elif lower.startswith("telegram.me/"):
            username = value.split("telegram.me/", 1)[1].strip().strip("/")

    if username is None or not username:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.channel.only_tg", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_channel", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    value = f"@{username}"

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_channel_link(int(u["id"]), value)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await _build_profile_edit_screen(message, user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "profile:channel_clear")
async def profile_channel_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id", callback.message.message_id)
    edit_chat_id = data.get("edit_chat_id", callback.message.chat.id)

    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    if user and user.get("id"):
        await update_user_channel_link(int(user["id"]), None)
    await state.clear()

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await _build_profile_edit_screen(callback, user)
    await callback.message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer(t("profile.edit.channel.deleted", lang))


@router.message(ProfileEditStates.waiting_new_name, F.text)
async def profile_set_name(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)

    new_name = message.text.strip()

    # Пустое имя
    if not new_name:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.name.empty", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_name", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    # Запрет ссылок, доменов, @username и рекламы каналов
    if has_links_or_usernames(new_name) or has_promo_channel_invite(new_name):
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.name.invalid", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_name", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_name(int(u["id"]), new_name)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await _build_profile_edit_screen(message, user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "profile:edit_gender")
async def profile_edit_gender(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.edit.gender.male", lang), callback_data="profile:set_gender:male")
    kb.button(text=t("profile.edit.gender.female", lang), callback_data="profile:set_gender:female")
    kb.button(text=t("profile.edit.gender.na", lang), callback_data="profile:set_gender:na")
    kb.button(text=t("common.back", lang), callback_data="profile:edit")
    kb.adjust(2, 1, 1)

    await callback.message.edit_text(
        t("profile.edit.gender.ask", lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:set_gender:"))
async def profile_set_gender(callback: CallbackQuery):
    _, _, code = callback.data.split(":", 2)
    mapping = {
        "male": "Парень",
        "female": "Девушка",
        "na": "Не важно",
    }
    gender = mapping.get(code, "Не важно")
    u = await get_user_by_tg_id(callback.from_user.id)
    if u and u.get("id"):
        await update_user_gender(int(u["id"]), gender)

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await _build_profile_edit_screen(callback, user)
    user_lang = _get_lang(u)

    await callback.message.edit_text(
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer(t("profile.edit.gender.saved", user_lang))


@router.callback_query(F.data == "profile:edit_age")
async def profile_edit_age(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    await state.set_state(ProfileEditStates.waiting_new_age)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.edit.age.clear_btn", lang), callback_data="profile:age_clear")
    kb.button(text=t("common.back", lang), callback_data="profile:edit")
    kb.adjust(1, 1)

    await callback.message.edit_text(
        t("profile.edit.age.ask", lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:age_clear")
async def profile_age_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id", callback.message.message_id)
    edit_chat_id = data.get("edit_chat_id", callback.message.chat.id)

    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    if user and user.get("id"):
        await update_user_age(int(user["id"]), None)
    await state.clear()

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await _build_profile_edit_screen(callback, user)
    await callback.message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer(t("profile.edit.age.cleared", lang))


@router.message(ProfileEditStates.waiting_new_age, F.text)
async def profile_set_age(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)

    text_val = message.text.strip()
    if not text_val.isdigit():
        await message.delete()
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=t("profile.edit.age.invalid", lang),
            reply_markup=build_back_kb(callback_data="profile:edit_age", text=t("common.back", lang)),
            parse_mode="HTML",
        )
        return

    age = int(text_val)
    if age < 5 or age > 120:
        await message.delete()
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=t("profile.edit.age.range", lang),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=t("profile.edit.age.clear_btn", lang), callback_data="profile:age_clear")],
                    [InlineKeyboardButton(text=t("common.back", lang), callback_data="profile:edit")],
                ]
            ),
            parse_mode="HTML",
        )
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_age(int(u["id"]), age)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await _build_profile_edit_screen(message, user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "profile:edit_bio")
async def profile_edit_bio(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    await state.set_state(ProfileEditStates.waiting_new_bio)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    await callback.message.edit_text(
        t("profile.edit.bio.ask", lang),
        reply_markup=_build_bio_edit_kb(user, lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ProfileEditStates.waiting_new_bio, F.text)
async def profile_set_bio(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    user = await get_user_by_tg_id(message.from_user.id)
    lang = _get_lang(user)

    bio = message.text.strip()

    # Пустое описание
    if not bio:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.bio.empty", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_bio", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    # Запрет ссылок, доменов, @username и рекламы каналов
    if has_links_or_usernames(bio) or has_promo_channel_invite(bio):
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=t("profile.edit.bio.invalid", lang),
                reply_markup=build_back_kb(callback_data="profile:edit_bio", text=t("common.back", lang)),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_bio(int(u["id"]), bio)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await _build_profile_edit_screen(message, user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "profile:bio_clear")
async def profile_bio_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id", callback.message.message_id)
    edit_chat_id = data.get("edit_chat_id", callback.message.chat.id)

    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    if user and user.get("id"):
        await update_user_bio(int(user["id"]), None)
    await state.clear()

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await _build_profile_edit_screen(callback, user)
    await callback.message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer(t("profile.edit.bio.deleted", lang))



@router.callback_query(F.data.startswith("profile:awards"))
async def profile_awards_menu(callback: CallbackQuery):
    """
    Раздел наград с фильтрами и пагинацией.

    Формат:
    1. 🏆 Название (11.12.2025) - от Создателя / @username
       Комментарий: текст  (только для наград от создателя)

    Фильтры:
    - Все награды (по умолчанию)
    - Только «от Создателя» (is_special = 1)
    - Только «от других» (is_special = 0)

    Пагинация:
    - Показываем по 5 наград на страницу.
    - Кнопки «⬅️» / «➡️» только там, где есть куда листать.
    """
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, странно. Попробуй /start.", show_alert=True)
        return

    user_id = user.get("id")
    if not user_id:
        await callback.answer("Не получилось загрузить награды. Попробуй позже.", show_alert=True)
        return

    # Разбираем callback_data: profile:awards[:filter[:page]]
    data = callback.data or "profile:awards"
    parts = data.split(":")
    filter_type = "all"
    page = 1

    if len(parts) >= 3:
        # profile:awards:filter
        filter_type = parts[2] or "all"
    if len(parts) >= 4:
        try:
            page = max(1, int(parts[3]))
        except ValueError:
            page = 1

    awards = await get_awards_for_user(user_id)

    # Если наград вообще нет — простое сообщение и только «Назад»
    if not awards:
        text = (
            "🏆 <b>Награды</b>\n\n"
            "У тебя пока нет наград.\n\n"
            "За активность, участие в жизни GlowShot и особые достижения "
            "здесь будут появляться твои трофеи."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(2, 2, 1, 1)
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    # Фильтрация по типу
    if filter_type == "creator":
        filtered = [a for a in awards if bool(a.get("is_special"))]
    elif filter_type == "others":
        filtered = [a for a in awards if not bool(a.get("is_special"))]
    else:
        filter_type = "all"
        filtered = list(awards)

    # Если после фильтрации ничего не осталось
    if not filtered:
        header = "🏆 <b>Награды</b>"
        if filter_type == "creator":
            header += "\n\nУ тебя пока нет наград от Создателя."
        elif filter_type == "others":
            header += "\n\nУ тебя пока нет наград от других пользователей."
        else:
            header += "\n\nУ тебя пока нет наград."

        text = header
        kb = InlineKeyboardBuilder()
        # Фильтры доступны даже если наград этого типа нет — можно переключиться
        kb.button(text="От Создателя", callback_data="profile:awards:creator:1")
        kb.button(text="От других", callback_data="profile:awards:others:1")
        kb.button(text="⬅️ Назад", callback_data="menu:profile")
        kb.adjust(2, 1)
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
        await callback.answer()
        return

    # Пагинация
    page_size = 5
    total = len(filtered)
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        page = total_pages

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = filtered[start_idx:end_idx]

    lines: list[str] = [
        "🏆 <b>Награды</b>",
        "",
    ]

    # Можно при желании подсветить текущий фильтр
    if filter_type == "creator":
        lines.append("Фильтр: <b>от Создателя</b>")
        lines.append("")
    elif filter_type == "others":
        lines.append("Фильтр: <b>от других пользователей</b>")
        lines.append("")

    # Нумерация глобальная — по всем отфильтрованным, а не только на странице
    for local_idx, award in enumerate(page_items, start=1):
        idx = start_idx + local_idx  # 1-based номер в общем списке
        icon = award.get("icon") or "🏅"
        title = award.get("title") or "Без названия"
        description = (award.get("description") or "").strip()
        created_at = award.get("created_at")
        is_special = bool(award.get("is_special"))
        granted_by_user_id = award.get("granted_by_user_id")

        # Форматируем дату получения
        human_date = "дата неизвестна"
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at)
                human_date = dt.strftime("%d.%m.%Y")
            except Exception:
                human_date = created_at

        # Кто выдал награду
        from_label = "—"
        if is_special:
            # Спец-награды считаем «от Создателя»
            from_label = "от Создателя"
        elif granted_by_user_id:
            try:
                giver = await get_user_by_id(int(granted_by_user_id))
            except Exception:
                giver = None

            if giver:
                giver_username = giver.get("username") or ""
                giver_name = giver.get("name") or ""
                if giver_username:
                    from_label = f"@{giver_username}"
                elif giver_name:
                    from_label = giver_name
                else:
                    from_label = "неизвестно"
            else:
                from_label = "неизвестно"

        # Основная строка:
        # 1. 🏆 Название (11.12.2025) - от Создателя / @username
        line = f"{idx}. {icon} {title} ({human_date}) - {from_label}"
        lines.append(line)

        # Для наград от создателя показываем комментарий (описание), если есть
        if is_special and description:
            lines.append(f"Комментарий: {description}")

        lines.append("")

    # Информация о страницах, если их больше одной
    if total_pages > 1:
        lines.append(f"Страница {page} из {total_pages}")

    text = "\n".join(lines).rstrip()

    # Собираем клавиатуру: навигация по страницам + фильтры + назад
    kb = InlineKeyboardBuilder()

    if total_pages > 1:
        # Навигационные кнопки «назад/вперёд» только если есть куда листать
        has_prev = page > 1
        has_next = page < total_pages

        if has_prev:
            kb.button(
                text="⬅️ Назад",
                callback_data=f"profile:awards:{filter_type}:{page - 1}",
            )
        if has_next:
            kb.button(
                text="➡️ Вперёд",
                callback_data=f"profile:awards:{filter_type}:{page + 1}",
            )

    # Кнопки фильтров
    kb.button(text="От Создателя", callback_data="profile:awards:creator:1")
    kb.button(text="От других", callback_data="profile:awards:others:1")

    # Кнопка назад в профиль
    kb.button(text="⬅️ В профиль", callback_data="menu:profile")

    # Раскладка кнопок
    if total_pages > 1:
        has_prev = page > 1
        has_next = page < total_pages
        if has_prev and has_next:
            # 2 навигационные, 2 фильтра, 1 назад
            kb.adjust(2, 2, 1)
        elif has_prev or has_next:
            # 1 навигационная, 2 фильтра, 1 назад
            kb.adjust(1, 2, 1)
        else:
            # Теоретически сюда не попадём, но на всякий случай
            kb.adjust(2, 1)
    else:
        # Только фильтры + назад
        kb.adjust(2, 1)

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# -------------------- Settings UI (notifications) --------------------

def _kb_profile_settings(
    notify: dict,
    streak_status: dict | None,
    lang: str = "ru",
    *,
    ads_enabled: bool | None = None,
    is_premium: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("settings.btn.notifications", lang), callback_data="profile:settings:notifications")
    kb.button(text=t("settings.lang.btn", lang), callback_data="profile:settings:toggle:lang")
    if is_premium:
        ads_state = " ✅" if ads_enabled else " ❌"
        kb.button(text=f"Реклама{ads_state}", callback_data="profile:settings:toggle:ads")
    kb.button(text=t("common.back", lang), callback_data="menu:profile")
    if is_premium:
        kb.adjust(2, 1, 1, 1)
    else:
        kb.adjust(2, 1, 1)
    return kb.as_markup()


def _render_profile_settings(
    notify: dict,
    streak_status: dict | None,
    lang: str = "ru",
    *,
    ads_enabled: bool | None = None,
    is_premium: bool = False,
) -> str:
    def _lang_label(code: str) -> str:
        if code.startswith("ru"):
            return "🇷🇺 Русский"
        return "🇬🇧 English"

    lang_label = _lang_label(lang)
    ads_line = ""
    if is_premium:
        ads_state = "включена" if ads_enabled else "выключена"
        ads_line = f"\nРеклама в оценках: {ads_state}\nМожно переключить здесь."
    else:
        ads_line = "\nРеклама отключается в Premium."

    return (
        f"{t('settings.title', lang)}\n\n"
        f"🌐 {t('settings.lang.line', lang, value=lang_label)}\n"
        f"{t('settings.lang.hint', lang, value=lang_label)}\n\n"
        f"🔔 {t('settings.notifications.hint', lang)}"
        f"{ads_line}"
    )


def _render_notifications_settings(
    notify: dict,
    streak_status: dict | None,
    lang: str = "ru",
) -> str:
    likes_enabled = bool((notify or {}).get("likes_enabled", True))
    comments_enabled = bool((notify or {}).get("comments_enabled", True))
    streak_enabled = bool((streak_status or {}).get("notify_enabled", True))

    likes_line = "❤️ " + (t("settings.state.on", lang) if likes_enabled else t("settings.state.off", lang))
    comm_line = "💬 " + (t("settings.state.on", lang) if comments_enabled else t("settings.state.off", lang))
    streak_line = "🔥 " + (t("settings.state.on", lang) if streak_enabled else t("settings.state.off", lang))

    return (
        f"🔔 <b>{t('settings.notifications.title', lang)}</b>\n\n"
        f"{likes_line}\n"
        f"{t('settings.likes.hint', lang)}\n\n"
        f"{comm_line}\n"
        f"{t('settings.comments.hint', lang)}\n\n"
        f"{streak_line}\n"
        f"{t('settings.streak.hint', lang)}"
    )


def _kb_notifications_settings(
    notify: dict,
    streak_status: dict | None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    likes_enabled = bool((notify or {}).get("likes_enabled", True))
    comments_enabled = bool((notify or {}).get("comments_enabled", True))
    streak_enabled = bool((streak_status or {}).get("notify_enabled", True))

    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("settings.btn.likes", lang) + (" ✅" if likes_enabled else " ❌"),
        callback_data="profile:settings:toggle:likes",
    )
    kb.button(
        text=t("settings.btn.comments", lang) + (" ✅" if comments_enabled else " ❌"),
        callback_data="profile:settings:toggle:comments",
    )
    kb.button(
        text=t("settings.btn.streak", lang) + (" ✅" if streak_enabled else " ❌"),
        callback_data="profile:settings:toggle:streak",
    )
    kb.button(text=t("common.back", lang), callback_data="profile:settings")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


@router.callback_query(F.data == "profile:settings:toggle:ads")
async def profile_settings_toggle_ads(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)
    premium_active = await is_user_premium_active(tg_id)
    if not premium_active:
        await callback.answer("Отключение рекламы доступно в Premium.", show_alert=True)
        return

    current = await get_ads_enabled_by_tg_id(tg_id)
    if current is None:
        current = False  # по умолчанию премиум без рекламы
    new_state = not current
    await set_ads_enabled_by_tg_id(tg_id, new_state)

    notify = await get_notify_settings_by_tg_id(tg_id)
    streak_status = await get_profile_streak_status(tg_id)
    ads_enabled = new_state
    lang = _get_lang(user)

    text = _render_profile_settings(
        notify,
        streak_status,
        lang,
        ads_enabled=ads_enabled,
        is_premium=premium_active,
    )
    kb = _kb_profile_settings(
        notify,
        streak_status,
        lang,
        ads_enabled=ads_enabled,
        is_premium=premium_active,
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass

    await callback.answer("Обновил настройки рекламы.")


@router.callback_query(F.data == "profile:settings")
async def profile_settings_open(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    tg_id = int(user.get("tg_id") or callback.from_user.id)

    notify = await get_notify_settings_by_tg_id(tg_id)

    streak_status = await get_profile_streak_status(tg_id)
    ads_enabled = await get_ads_enabled_by_tg_id(tg_id)
    premium_active = await is_user_premium_active(tg_id)
    if ads_enabled is None:
        ads_enabled = not premium_active

    lang = _get_lang(user)
    text = _render_profile_settings(
        notify,
        streak_status,
        lang,
        ads_enabled=ads_enabled,
        is_premium=premium_active,
    )
    kb = _kb_profile_settings(
        notify,
        streak_status,
        lang,
        ads_enabled=ads_enabled,
        is_premium=premium_active,
    )

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer()


@router.callback_query(F.data == "profile:settings:notifications")
async def profile_settings_notifications(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)
    notify = await get_notify_settings_by_tg_id(tg_id)
    streak_status = await get_profile_streak_status(tg_id)
    lang = _get_lang(user)

    text = _render_notifications_settings(notify, streak_status, lang)
    kb = _kb_notifications_settings(notify, streak_status, lang)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer()


@router.callback_query(F.data == "profile:settings:toggle:lang")
async def profile_settings_toggle_lang(callback: CallbackQuery):
    tg_id = int(callback.from_user.id)
    current_user = await get_user_by_tg_id(tg_id)
    current_lang = _get_lang(current_user)
    new_lang = "en" if current_lang == "ru" else "ru"

    try:
        await set_user_language_by_tg_id(tg_id, new_lang)
    except Exception:
        await callback.answer(t("settings.lang.save_error", current_lang), show_alert=True)
        return

    user = await get_user_by_tg_id(tg_id)
    lang = _get_lang(user)
    notify = await get_notify_settings_by_tg_id(tg_id)
    streak_status = await get_profile_streak_status(tg_id)

    try:
        await callback.message.edit_text(
            _render_profile_settings(
                notify,
                streak_status,
                lang,
            ),
            reply_markup=_kb_profile_settings(
                notify,
                streak_status,
                lang,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer(t("settings.lang.saved", lang))


@router.callback_query(F.data == "profile:settings:toggle:likes")
async def profile_settings_toggle_likes(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    notify = await toggle_likes_notify_by_tg_id(tg_id)

    streak_status = await get_profile_streak_status(tg_id)
    lang = _get_lang(user)

    try:
        await callback.message.edit_text(
            _render_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            reply_markup=_kb_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer(t("settings.toast.ok", lang))


@router.callback_query(F.data == "profile:settings:toggle:comments")
async def profile_settings_toggle_comments(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    notify = await toggle_comments_notify_by_tg_id(tg_id)

    streak_status = await get_profile_streak_status(tg_id)
    lang = _get_lang(user)

    try:
        await callback.message.edit_text(
            _render_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            reply_markup=_kb_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer(t("settings.toast.ok", lang))


@router.callback_query(F.data == "profile:settings:toggle:streak")
async def profile_settings_toggle_streak(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    tg_id = int((user or {}).get("tg_id") or callback.from_user.id)

    streak_status = await toggle_profile_streak_notify_and_status(tg_id)
    notify = await get_notify_settings_by_tg_id(tg_id)
    lang = _get_lang(user)

    try:
        await callback.message.edit_text(
            _render_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            reply_markup=_kb_notifications_settings(
                notify,
                streak_status,
                lang,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer(t("settings.toast.ok", lang))


@router.callback_query(F.data == "profile:delete")
async def profile_delete_confirm(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    kb = build_confirm_kb(
        yes_callback="profile:delete_confirm",
        no_callback="menu:profile",
        yes_text=t("profile.delete.btn.yes", lang),
        no_text=t("profile.delete.btn.no", lang),
    )

    await callback.message.edit_text(
        f"{t('profile.delete.confirm.title', lang)}\n\n{t('profile.delete.confirm.text', lang)}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:delete_confirm")
async def profile_delete_do(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    lang = _get_lang(user)

    await soft_delete_user(callback.from_user.id)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Вернуться", callback_data="profile:delete:return")
    kb.adjust(2, 2, 1, 1)

    await callback.message.edit_text(
        t("profile.delete.done.text", lang),
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer(t("profile.delete.done.toast", lang))


@router.callback_query(F.data == "profile:delete:return")
async def profile_delete_return(callback: CallbackQuery, state: FSMContext):
    """Удаляет сообщение об удалении и показывает приветствие для регистрации."""
    try:
        await callback.message.delete()
    except Exception:
        pass

    welcome_text = (
        "GlowShot — это новый Телеграм бот для тех, кто любит фотографию.\n"
        "Выкладывай фото, делись им по ссылке, получай оценки.\n"
        "Начнем? Жми «Сыыыыр»"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="Наш телеграм", url="https://t.me/glowshotchannel")
    kb.button(text="🥛 Сыыыыр", callback_data="auth:start")
    kb.adjust(1, 1)

    try:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=welcome_text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass

    try:
        await callback.answer()
    except Exception:
        pass
