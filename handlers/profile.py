from aiogram import Router, F
import html
from aiogram.types import InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta

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
    get_most_popular_photo_for_user,
    get_weekly_rank_for_user,
    get_user_premium_status,
    is_user_premium_active,
    get_awards_for_user,
    get_user_by_id,
    update_user_city,
    update_user_country,
    set_user_city_visibility,
    set_user_country_visibility,
)
from keyboards.common import build_back_kb, build_confirm_kb
from utils.validation import has_links_or_usernames, has_promo_channel_invite
from utils.places import validate_place, validate_city_and_country, validate_city_and_country_full
from utils.flags import country_to_flag, country_display

router = Router()


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
    name = html.escape(str(name_raw), quote=False)
    age = user.get("age")
    age_part = f", {age}" if age else ""

    # Пол смайликом
    gender_raw = user.get("gender")
    if gender_raw == "Парень":
        gender_icon = "🙋‍♂️"
    elif gender_raw == "Девушка":
        gender_icon = "🙋‍♀️"
    elif gender_raw in ("Другое", "Other"):
        gender_icon = "🙋"
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

    # Реальная статистика по фото
    total_photos = "—"
    avg_rating_text = "—"
    popular_photo_title = "—"
    popular_photo_metric = "—"
    weekly_top_position = "—"

    user_id = user.get("id")
    tg_id = user.get("tg_id")

    if user_id:
        # Всего загружено фото
        try:
            total = await count_photos_by_user(user_id)          # теперь “всё время”
            active = await count_active_photos_by_user(user_id)  # “сейчас”
            total_photos = f"{total} (активных: {active})"
        except Exception:
            total_photos = "—"

        # Средняя оценка и количество оценок
        try:
            summary = await get_user_rating_summary(user_id)
            avg = summary.get("avg_rating")
            cnt = summary.get("ratings_count") or 0
            if avg is not None and cnt > 0:
                avg_str = f"{float(avg):.2f}".rstrip("0").rstrip(".")
                avg_rating_text = f"{avg_str} ({cnt} оценок)"
            elif cnt > 0:
                avg_rating_text = f"{cnt} оценок"
            else:
                avg_rating_text = "—"
        except Exception:
            avg_rating_text = "—"

        # Самое популярное фото
        try:
            popular = await get_most_popular_photo_for_user(user_id)
            if popular:
                popular_photo_title = html.escape(str(popular.get("title") or "Без названия"), quote=False)
                ratings_count = popular.get("ratings_count") or 0
                avg_pop = popular.get("avg_rating")
                if avg_pop is not None:
                    avg_str = f"{float(avg_pop):.2f}".rstrip("0").rstrip(".")
                    popular_photo_metric = f"{avg_str}★, {ratings_count} оценок"
                else:
                    popular_photo_metric = f"{ratings_count} оценок"
        except Exception:
            pass

        # Позиция в топе недели
        try:
            rank = await get_weekly_rank_for_user(user_id)
            if rank is not None:
                weekly_top_position = str(rank)
        except Exception:
            weekly_top_position = "—"

    # GlowShot Premium статус
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
                        premium_status_line = f"активен (до {human_until})"

                        # Считаем, сколько дней осталось, если дата в будущем
                        try:
                            days_left = (dt.date() - datetime.now().date()).days
                            if days_left >= 0:
                                days_text = _plural_ru(
                                    days_left,
                                    "день",
                                    "дня",
                                    "дней",
                                )
                                premium_extra_line = f"Осталось: {days_left} {days_text}."
                        except Exception:
                            # Дополнительную строку можно не показывать, если что-то пошло не так
                            pass
                    except Exception:
                        premium_status_line = f"активен до {until}"
                else:
                    premium_status_line = "активен (бессрочно)"
                    premium_extra_line = "Подписка без ограничения по дате."
                premium_badge = " 💎"
            else:
                # Если флаг стоит, но срок истёк
                if had_premium and raw_status.get("premium_until"):
                    premium_status_line = "срок действия истёк"
                    premium_extra_line = "Ты можешь продлить подписку через кнопку ниже."
                elif had_premium:
                    premium_status_line = "срок действия истёк"
                    premium_extra_line = "Ты можешь заново оформить подписку через кнопку ниже."
                else:
                    premium_extra_line = "Оформить премиум можно через кнопку ниже."
        except Exception:
            # В случае ошибки не ломаем профиль
            pass

    text_lines = [
        f"👤<b>Твой профиль</b>{premium_badge}",
        f"Имя: {name}{age_part} лет" if age else f"Имя: {name}",
        f"Пол: {gender_icon}",
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
        text_lines.append(f"{flag} Локация: {', '.join(loc_parts)}")

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

        text_lines.append(f"🔗 Ссылка: {display_link}")

    # Описание
    text_lines.extend([
        "",
        "📝 <b>Описание:</b>",
        html.escape(str(user.get("bio") or "—"), quote=False),
        "",
    ])

    # --- "Свернутая" статистика через spoiler ---
    stats_lines = [
        f"Всего загрузил: {total_photos}",
        f"Дней в боте: {days_in_bot}",
        f"Средняя оценка: {avg_rating_text}",
        f"Самое популярное фото: {popular_photo_title} ({popular_photo_metric})",
    ]

    # --- Статистика как цитата ---
    stats_body = "\n".join([html.escape(line, quote=False) for line in stats_lines])
    text_lines.append("📊 <b>Моя статистика</b>")
    text_lines.append(f"<blockquote expandable>{stats_body}</blockquote>")

    # Premium
    text_lines.extend([
        "",
        "💎 <b>GlowShot Premium</b>",
        f"статус: {premium_status_line}",
    ])

    if premium_extra_line:
        text_lines.append(premium_extra_line)
    text = "\n".join(text_lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="🏆 Награды", callback_data="profile:awards")
    kb.button(text="🏅 Итоги", callback_data="myresults:0")
    kb.button(text="✏️ Редактировать профиль", callback_data="profile:edit")
    kb.button(text="⚙️ Настройки", callback_data="profile:settings")

    premium_button_text = "💎 Оформить премиум" if not premium_active else "💎 Мой премиум"
    kb.button(text=premium_button_text, callback_data="profile:premium")

    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(2, 2, 1, 1)
    return text, kb.as_markup()


@router.callback_query(F.data == "profile:open")
async def profile_menu(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе, странно. Попробуй /start.", show_alert=True)
        return

    text, markup = await build_profile_view(user)

    # Профиль — всегда текстовый. Меню-сообщение НЕ удаляем.
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        # Если это меню-картинка или нельзя редактировать — просто отправляем профиль отдельным сообщением.
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_notification=True,
        )

    await callback.answer()


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

    try:
        await callback.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
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


@router.callback_query(F.data == "profile:edit")
async def profile_edit_menu(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user is None:
        await callback.answer("Тебя нет в базе. Попробуй /start.", show_alert=True)
        return

    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="🪪 Имя", callback_data="profile:edit_name")
    kb.button(text="🎂 Возраст", callback_data="profile:edit_age")

    kb.button(text="📝 Описание", callback_data="profile:edit_bio")
    kb.button(text="⚧️ Пол", callback_data="profile:edit_gender")

    kb.button(text="📡 Ссылка", callback_data="profile:edit_channel")
    kb.button(text="🏙 Город", callback_data="profile:edit_city")

    kb.button(text="🗑 Удалить аккаунт", callback_data="profile:delete")
    kb.button(text="⬅️ Назад", callback_data="menu:profile")
    kb.adjust(2, 2, 2, 1, 1)

    await callback.message.edit_text(
        "✏️ Что хочешь изменить в профиле?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:edit_name")
async def profile_edit_name(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEditStates.waiting_new_name)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)
    await callback.message.edit_text(
        "🪪 Введи новое имя для профиля.",
        reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
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

    tg_id = user.get("tg_id")
    is_active = False
    if tg_id:
        try:
            is_active = await is_user_premium_active(tg_id)
        except Exception:
            is_active = False

    if not is_active:
        await callback.answer(
            "Привязка ссылки доступна только с GlowShot Premium 💎",
            show_alert=True,
        )
        return

    await state.set_state(ProfileEditStates.waiting_new_channel)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    await callback.message.edit_text(
        "📡 Отправь ссылку на свой Telegram-канал или профиль.\n\n"
        "Принимаются только Telegram-ссылки:\n"
        "• <code>https://t.me/username</code>\n"
        "• <code>https://telegram.me/username</code>\n"
        "• или просто <code>@username</code>.\n\n"
        "Если хочешь убрать ссылку — отправь слово <code>удалить</code>.",
        reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
    )
    await callback.answer()


# -------------------- City / Country edit --------------------

def _build_city_kb(user: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    show_city = bool(user.get("show_city", 1))
    kb.button(text="✍️ Изменить", callback_data="profile:city:change")
    kb.button(text="🗑 Удалить", callback_data="profile:city:delete")
    kb.button(text=("🙈 Скрыть" if show_city else "👁 Показать"), callback_data="profile:city:toggle")
    kb.button(text="⬅️ Назад", callback_data="profile:edit")
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

    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)

    city = (user.get("city") or "").strip() or "—"
    show_city = bool(user.get("show_city", 1))
    vis = "показан" if show_city else "скрыт"

    text = (
        "🏙 <b>Город</b>\n\n"
        f"Текущий: <b>{city}</b>\n"
        f"Отображение в профиле: <b>{vis}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=_build_city_kb(user), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "profile:city:change")
async def profile_city_change(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEditStates.waiting_new_city)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)
    await callback.message.edit_text(
        "🏙 <b>Город</b>\n\n"
        "Введи город одним сообщением. Можно с маленькой буквы — я поправлю.\n\n"
        "Если хочешь убрать — напиши <code>удалить</code>.",
        reply_markup=build_back_kb(callback_data="profile:edit_city", text="⬅️ Назад"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "profile:city:delete")
async def profile_city_delete(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user and user.get("id"):
        await update_user_city(int(user["id"]), None)

    user = await get_user_by_tg_id(callback.from_user.id)
    await callback.message.edit_text("🏙 Город удалён.", reply_markup=_build_city_kb(user or {}), parse_mode="HTML")
    await callback.answer("Готово!")


@router.callback_query(F.data == "profile:city:toggle")
async def profile_city_toggle(callback: CallbackQuery):
    user = await get_user_by_tg_id(callback.from_user.id)
    if user and user.get("id"):
        current = bool(user.get("show_city", 1))
        await set_user_city_visibility(int(user["id"]), not current)

    user = await get_user_by_tg_id(callback.from_user.id)
    city = (user.get("city") or "").strip() or "—"
    show_city = bool(user.get("show_city", 1))
    vis = "показан" if show_city else "скрыт"

    text = (
        "🏙 <b>Город</b>\n\n"
        f"Текущий: <b>{city}</b>\n"
        f"Отображение в профиле: <b>{vis}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=_build_city_kb(user), parse_mode="HTML")
    await callback.answer("Ок!")


@router.message(ProfileEditStates.waiting_new_city, F.text)
async def profile_set_city(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    raw = (message.text or "").strip()

    if raw.lower() in ("удалить", "delete", "remove"):
        u = await get_user_by_tg_id(message.from_user.id)
        if u and u.get("id"):
            await update_user_city(int(u["id"]), None)
            await update_user_country(int(u["id"]), None)
        await state.clear()
        await message.delete()
        user = await get_user_by_tg_id(message.from_user.id)
        text, markup = await build_profile_view(user)
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
                text=(
                    "❌ Не могу найти такой город.\n\n"
                    "Попробуй написать точнее (без лишних символов), например: <code>Орёл</code>, <code>Moscow</code>, <code>Berlin</code>.\n"
                    "Если это небольшой населённый пункт — попробуй добавить регион в одной строке."
                ),
                reply_markup=build_back_kb(callback_data="profile:edit_city", text="⬅️ Назад"),
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
    text, markup = await build_profile_view(user)
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

    # Удаление ссылки
    if raw.lower() in ("удалить", "delete", "remove"):
        u = await get_user_by_tg_id(message.from_user.id)
        if u and u.get("id"):
            await update_user_channel_link(int(u["id"]), None)
        await state.clear()
        await message.delete()

        user = await get_user_by_tg_id(message.from_user.id)
        text, markup = await build_profile_view(user)
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=text,
            reply_markup=markup,
        )
        return

    value = raw

    # Нормализация: если человек прислал @username — превращаем в ссылку
    if value.startswith("@"):
        username = value[1:].strip()
        if not username:
            await message.delete()
            try:
                await message.bot.edit_message_text(
                    chat_id=edit_chat_id,
                    message_id=edit_msg_id,
                    text=(
                        "Это не похоже на корректный @username.\n\n"
                        "Отправь ссылку вида <code>https://t.me/username</code> "
                        "или просто <code>@username</code>."
                    ),
                    reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
                )
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    raise
            return
        value = f"https://t.me/{username}"

    # Добавляем схему, если человек прислал t.me/username без https://
    lower = value.lower().strip()
    if lower.startswith("t.me/"):
        value = "https://" + value.lstrip()

    if lower.startswith("telegram.me/"):
        value = "https://" + value.lstrip()

    # Проверяем, что это именно телеграм-ссылка
    lower = value.lower().strip()
    if not (
        lower.startswith("https://t.me/")
        or lower.startswith("http://t.me/")
        or lower.startswith("https://telegram.me/")
        or lower.startswith("http://telegram.me/")
        or lower.startswith("tg://")
    ):
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=(
                    "Можно указать только ссылку на Telegram.\n\n"
                    "Подойдёт:\n"
                    "• <code>https://t.me/username</code>\n"
                    "• <code>https://telegram.me/username</code>\n"
                    "• или просто <code>@username</code>."
                ),
                reply_markup=build_back_kb(callback_data="profile:edit", text="⬅️ Назад"),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_channel_link(int(u["id"]), value)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await build_profile_view(user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
    )


@router.message(ProfileEditStates.waiting_new_name, F.text)
async def profile_set_name(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    new_name = message.text.strip()

    # Пустое имя
    if not new_name:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=(
                    "Имя не может быть пустым.\n\n"
                    "Напиши, как тебя записать в профиле — имя или творческий псевдоним."
                ),
                reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
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
                text=(
                    "В имени нельзя оставлять @username, ссылки на Telegram, соцсети или сайты, "
                    "а также рекламировать каналы.\n\n"
                    "Напиши имя или свой псевдоним <b>без контактов</b>."
                ),
                reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
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
    text, markup = await build_profile_view(user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
    )


@router.callback_query(F.data == "profile:edit_gender")
async def profile_edit_gender(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="Парень", callback_data="profile:set_gender:male")
    kb.button(text="Девушка", callback_data="profile:set_gender:female")
    kb.button(text="Другое", callback_data="profile:set_gender:other")
    kb.button(text="Не важно", callback_data="profile:set_gender:na")
    kb.button(text="⬅️ Назад", callback_data="menu:profile")
    kb.adjust(2, 2, 1)

    await callback.message.edit_text(
        "Выбери, как тебя указать в профиле.\n\n",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile:set_gender:"))
async def profile_set_gender(callback: CallbackQuery):
    _, _, code = callback.data.split(":", 2)
    mapping = {
        "male": "Парень",
        "female": "Девушка",
        "other": "Другое",
        "na": "Не важно",
    }
    gender = mapping.get(code, "Не важно")
    u = await get_user_by_tg_id(callback.from_user.id)
    if u and u.get("id"):
        await update_user_gender(int(u["id"]), gender)

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await build_profile_view(user)
    await callback.message.edit_text(
        text,
        reply_markup=markup,
    )
    await callback.answer("Пол обновлён.")


@router.callback_query(F.data == "profile:edit_age")
async def profile_edit_age(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEditStates.waiting_new_age)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить / убрать возраст", callback_data="profile:age_clear")
    kb.adjust(2, 2, 1, 1)
    await callback.message.edit_text(
        "📅 Введи новый возраст числом или нажми «Пропустить / убрать возраст».",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:age_clear")
async def profile_age_clear(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id", callback.message.message_id)
    edit_chat_id = data.get("edit_chat_id", callback.message.chat.id)

    u = await get_user_by_tg_id(callback.from_user.id)
    if u and u.get("id"):
        await update_user_age(int(u["id"]), None)
    await state.clear()

    user = await get_user_by_tg_id(callback.from_user.id)
    text, markup = await build_profile_view(user)
    await callback.message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
    )
    await callback.answer("Возраст убран.")


@router.message(ProfileEditStates.waiting_new_age, F.text)
async def profile_set_age(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    text = message.text.strip()
    if not text.isdigit():
        await message.delete()
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=(
                "Возраст должен быть числом.\n\n"
                "Напиши только цифры, например: <code>18</code>."
            ),
            reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
        )
        return

    age = int(text)
    if age < 5 or age > 120:
        await message.delete()
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=(
                "Ты уверен(а), что это твой реальный возраст?\n\n"
                "Введи реальный возраст или нажми «Пропустить / убрать возраст»."
            ),
            reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
        )
        return

    u = await get_user_by_tg_id(message.from_user.id)
    if u and u.get("id"):
        await update_user_age(int(u["id"]), age)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    text, markup = await build_profile_view(user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
    )


@router.callback_query(F.data == "profile:edit_bio")
async def profile_edit_bio(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileEditStates.waiting_new_bio)
    await state.update_data(edit_msg_id=callback.message.message_id, edit_chat_id=callback.message.chat.id)
    await callback.message.edit_text(
        "📝 Напиши пж новое описание одним сообщением.",
        reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
    )
    await callback.answer()


@router.message(ProfileEditStates.waiting_new_bio, F.text)
async def profile_set_bio(message: Message, state: FSMContext):
    data = await state.get_data()
    edit_msg_id = data.get("edit_msg_id")
    edit_chat_id = data.get("edit_chat_id")

    bio = message.text.strip()

    # Пустое описание
    if not bio:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=(
                    "Описание не может быть пустым.\n\n"
                    "Напиши пару слов о себе: что любишь снимать и какой у тебя стиль."
                ),
                reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
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
                text=(
                    "В описании профиля нельзя оставлять @username, ссылки на Telegram, соцсети или сайты, "
                    "а также рекламировать каналы.\n\n"
                    "Напиши пару слов о себе как о фотографе <b>без контактов</b>."
                ),
                reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
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
    text, markup = await build_profile_view(user)
    await message.bot.edit_message_text(
        chat_id=edit_chat_id,
        message_id=edit_msg_id,
        text=text,
        reply_markup=markup,
    )



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


@router.callback_query(F.data == "profile:settings")
async def profile_settings_menu(callback: CallbackQuery):
    """
    Раздел настроек (пока заглушка для уведомлений).
    """
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "Скоро здесь появятся удобные переключатели:\n"
        "• вкл/выкл уведомления о лайках ❤️\n"
        "• вкл/выкл уведомления о комментариях 💬\n"
    )

    await callback.message.edit_text(
        text,
        reply_markup=build_back_kb(callback_data="menu:profile", text="⬅️ Назад"),
    )
    await callback.answer()


@router.callback_query(F.data == "profile:premium_benefits")
async def profile_premium_benefits(callback: CallbackQuery):
    """
    Список преимуществ премиум-аккаунта (пока статический текст).
    """
    text = (
        "✨ <b>Преимущества GlowShot Premium</b>\n\n"
        "Планируемые фичи для премиум-подписки:\n"
        "• Прикрепить свой TG-канал (ТГК) в профиль\n"
        "• Расширенные лимиты на загрузку фото\n"
        "• Приоритет в показе фотографий в ленте\n"
        "• Бейдж 'Premium' в профиле\n"
        "• Дополнительная аналитика по лайкам и просмотрам\n\n"
        "Список будет дополняться."
    )

    await callback.message.edit_text(
        text,
        reply_markup=build_back_kb(callback_data="profile:premium", text="⬅️ Назад"),
    )
    await callback.answer()




@router.callback_query(F.data == "profile:delete")
async def profile_delete_confirm(callback: CallbackQuery):
    kb = build_confirm_kb(
        yes_callback="profile:delete_confirm",
        no_callback="menu:profile",
        yes_text="❌ Да, удалить аккаунт",
        no_text="⬅️ Отмена",
    )

    await callback.message.edit_text(
        "⚠️ Точно удалить аккаунт?\n\n"
        "Твой профиль будет деактивирован, участие в рейтинках остановится. "
        "Фотографии и оценки могут остаться в общей статистике, но новый контент от тебя "
        "появляться не будет.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "profile:delete_confirm")
async def profile_delete_do(callback: CallbackQuery, state: FSMContext):
    await soft_delete_user(callback.from_user.id)
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Зарегистрироваться заново", callback_data="auth:start")
    kb.adjust(2, 2, 1, 1)

    await callback.message.edit_text(
        "✅ Аккаунт деактивирован.\n\nЕсли захочешь вернуться, "
        "нажми «Зарегистрироваться заново».",
        reply_markup=kb.as_markup(),
    )
    await callback.answer("Аккаунт удалён.")