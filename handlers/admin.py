from __future__ import annotations

from typing import Optional, Union

from datetime import timedelta, datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from handlers.payments import TARIFFS

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_user_block_status_by_tg_id,
    set_user_admin_by_tg_id,
    get_total_users,
    get_all_users_tg_ids,
    get_moderators,
    get_helpers,
    get_support_users,
    set_user_moderator_by_tg_id,
    set_user_helper_by_tg_id,
    set_user_support_by_tg_id,
    get_user_by_username,
    get_premium_users,
    set_user_premium_role_by_tg_id,
    set_user_premium_status,
    give_achievement_to_user_by_code,
    get_awards_for_user,
    get_award_by_id,
    delete_award_by_id,
    update_award_text,
    update_award_icon,
    create_custom_award_for_user,
    get_bot_error_logs_page,
    get_bot_error_logs_count,
    clear_bot_error_logs,
    get_users_sample,
    get_active_users_last_24h,
    get_online_users_recent,
    get_total_activity_events,
    get_new_users_last_days,
    get_premium_stats,
    get_blocked_users_page,
    get_users_with_multiple_daily_top3,
    get_user_admin_stats,
    get_user_rating_summary,
    get_today_photo_for_user,
    get_photo_admin_stats,
    get_payments_count,
    get_payments_page,
    get_revenue_summary,
    get_subscriptions_total,
    get_subscriptions_page,
)

router = Router()

# ================= LOGS / ERRORS (Admin) =================

_LOGS_PAGE_LIMIT = 10
_MAX_TG_TEXT = 3900  # safe margin for Telegram 4096


def _cut_text(s: str | None, limit: int = _MAX_TG_TEXT) -> str:
    if not s:
        return "—"
    s = str(s)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _fmt_dt_safe(dt_str: str | None) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(dt_str)


async def _render_logs_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    page = max(1, int(page))

    total = await get_bot_error_logs_count()
    total_pages = max(1, (total + _LOGS_PAGE_LIMIT - 1) // _LOGS_PAGE_LIMIT)
    if page > total_pages:
        page = total_pages

    offset = (page - 1) * _LOGS_PAGE_LIMIT
    rows = await get_bot_error_logs_page(offset=offset, limit=_LOGS_PAGE_LIMIT)

    lines: list[str] = [
        "🧾 <b>Логи / ошибки</b>",
        f"Всего записей: <b>{total}</b>",
        f"Страница: <b>{page}</b> / <b>{total_pages}</b>",
        "",
    ]

    if not rows:
        lines.append("Пока нет ошибок. Красота ✨")
    else:
        for r in rows:
            rid = r.get("id")
            created_at = _fmt_dt_safe(r.get("created_at"))
            error_type = r.get("error_type") or "Error"
            handler = r.get("handler") or "—"
            tg_user_id = r.get("tg_user_id")
            update_type = r.get("update_type") or "—"

            # короткая строка
            lines.append(
                f"<b>#{rid}</b> · {created_at}\n"
                f"• <b>{error_type}</b> в <code>{handler}</code> · {update_type}\n"
                f"• user: <code>{tg_user_id if tg_user_id is not None else '—'}</code>"
            )
            lines.append("")

    text = "\n".join(lines).strip()

    kb = InlineKeyboardBuilder()

    # Кнопки "Подробнее" по каждой записи (до 5, чтобы не раздувать клаву)
    if rows:
        for r in rows[:5]:
            rid = r.get("id")
            if rid is not None:
                kb.button(text=f"🔎 #{rid}", callback_data=f"admin:logs:view:{rid}:{page}")
        kb.adjust(5)

    # пагинация
    prev_cb = f"admin:logs:page:{page-1}" if page > 1 else None
    next_cb = f"admin:logs:page:{page+1}" if page < total_pages else None

    if prev_cb or next_cb:
        if prev_cb:
            kb.button(text="⬅️", callback_data=prev_cb)
        if next_cb:
            kb.button(text="➡️", callback_data=next_cb)

    # действия
    kb.button(text="🧹 Очистить логи", callback_data=f"admin:logs:clear:confirm:{page}")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    # раскладка: сначала 1 ряд подробностей (если есть), затем стрелки, затем действия
    # InlineKeyboardBuilder сам соберёт; фиксируем адекватно:
    # если были кнопки подробностей, они уже adjust(5), дальше будет ещё ряд.
    kb.adjust(5, 2, 1, 1)

    return text, kb.as_markup()


@router.callback_query(F.data.startswith("admin:logs:page:"))
async def admin_logs_page(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    page = 1
    if len(parts) >= 4 and parts[3].isdigit():
        page = int(parts[3])

    text, markup = await _render_logs_page(page)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


@router.callback_query(F.data.startswith("admin:logs:view:"))
async def admin_logs_view(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    # формат: admin:logs:view:<log_id>:<back_page>
    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Не удалось открыть запись.", show_alert=True)
        return

    try:
        log_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный id записи.", show_alert=True)
        return

    back_page = 1
    try:
        back_page = int(parts[4])
    except Exception:
        back_page = 1

    # Берём одну запись через страницу (быстро и без новой функции):
    # ищем в первых 200 самых свежих; если не нашли — скажем, что не найдено.
    # (Это компромисс. Если захочешь — добавим get_bot_error_log_by_id.)
    row = None
    # пробуем дернуть прямым SQL через уже имеющийся пул нельзя отсюда,
    # поэтому используем get_bot_error_logs_page c увеличенным лимитом.
    # Админке этого хватит.
    try:
        recent = await get_bot_error_logs_page(offset=0, limit=200)
        for r in recent:
            if int(r.get("id", -1)) == log_id:
                row = r
                break
    except Exception:
        row = None

    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:logs:page:{back_page}")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)
        try:
            await callback.message.edit_text("Запись не найдена (возможно, слишком старая).", reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer("Запись не найдена (возможно, слишком старая).", reply_markup=kb.as_markup())
        await callback.answer()
        return

    created_at = _fmt_dt_safe(row.get("created_at"))
    error_type = row.get("error_type") or "Error"
    handler = row.get("handler") or "—"
    update_type = row.get("update_type") or "—"
    chat_id = row.get("chat_id")
    tg_user_id = row.get("tg_user_id")

    error_text = _cut_text(row.get("error_text"), 1200)
    tb = _cut_text(row.get("traceback_text"), _MAX_TG_TEXT)

    text = (
        "🧾 <b>Детали ошибки</b>\n\n"
        f"ID: <code>{row.get('id')}</code>\n"
        f"Когда: <b>{created_at}</b>\n"
        f"Тип: <b>{error_type}</b>\n"
        f"Хендлер: <code>{handler}</code>\n"
        f"Update: <code>{update_type}</code>\n"
        f"chat_id: <code>{chat_id if chat_id is not None else '—'}</code>\n"
        f"user_id: <code>{tg_user_id if tg_user_id is not None else '—'}</code>\n\n"
        f"<b>Сообщение</b>\n<code>{error_text}</code>\n\n"
        f"<b>Traceback</b>\n<code>{tb}</code>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к списку", callback_data=f"admin:logs:page:{back_page}")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except TelegramBadRequest:
        # Иногда Telegram ругается на слишком длинный текст даже после обрезки
        safe_text = _cut_text(text, _MAX_TG_TEXT)
        try:
            await callback.message.edit_text(safe_text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(safe_text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(_cut_text(text, _MAX_TG_TEXT), reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin:logs:clear:confirm:"))
async def admin_logs_clear_confirm(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    back_page = 1
    try:
        back_page = int(parts[4])
    except Exception:
        back_page = 1

    text = (
        "🧹 <b>Очистить логи?</b>\n\n"
        "Это удалит <b>все</b> записи ошибок из базы.\n"
        "Если хочешь сохранить историю — лучше сначала сфоткай/скопируй нужные записи."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, очистить", callback_data=f"admin:logs:clear:do:{back_page}")
    kb.button(text="❌ Отмена", callback_data=f"admin:logs:page:{back_page}")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin:logs:clear:do:"))
async def admin_logs_clear_do(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    back_page = 1
    try:
        back_page = int(parts[4])
    except Exception:
        back_page = 1

    await clear_bot_error_logs()

    text, markup = await _render_logs_page(1)
    # добавим небольшую плашку
    text = "✅ Логи очищены.\n\n" + text

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# ================= HELPER: Edit last user prompt or answer =================
async def _edit_user_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """
    Универсальный helper для раздела «Пользователи».
    Пытаемся отредактировать последнее служебное сообщение,
    в котором ведём диалог по пользователю.
    """
    data = await state.get_data()
    chat_id = data.get("user_prompt_chat_id")
    msg_id = data.get("user_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            # Try to delete the old message
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            # Send a new message
            try:
                sent = await message.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                # Update FSM state with the new message id
                await state.update_data(
                    user_prompt_chat_id=sent.chat.id,
                    user_prompt_msg_id=sent.message_id,
                )
                return
            except Exception:
                pass

    # If no stored prompt message or all above failed, answer and store new ids
    try:
        sent = await message.answer(text, reply_markup=reply_markup)
        await state.update_data(
            user_prompt_chat_id=sent.chat.id,
            user_prompt_msg_id=sent.message_id,
        )
    except Exception:
        pass


# Помощник: поиск пользователя по тому, что ввёл админ (ID / @username)
async def _find_user_by_identifier(identifier: str) -> dict | None:
    """
    Пытаемся найти пользователя:
    - если строка состоит только из цифр — сначала считаем, что это tg_id,
      если не нашли, пробуем как внутренний id;
    - если начинается с @ — ищем по username;
    - иначе — тоже пытаемся как username.
    Никаких исключений наружу не кидаем — максимум возвращаем None.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None

    # --- Вариант 1: только цифры → пробуем как tg_id и как внутренний id ---
    if ident.isdigit():
        # Сначала считаем, что это Telegram ID
        try:
            tg_id = int(ident)
        except ValueError:
            tg_id = None

        if tg_id is not None:
            try:
                user = await get_user_by_tg_id(tg_id)
            except Exception:
                user = None
            if user:
                return user

        # Если по tg_id не нашли — пробуем как внутренний id в таблице users
        try:
            internal_id = int(ident)
        except ValueError:
            internal_id = None

        if internal_id is not None:
            try:
                user = await get_user_by_id(internal_id)
            except Exception:
                user = None
            if user:
                return user

        return None

    # --- Вариант 2: username с @ или без ---
    username = ident
    if username.startswith("@"):
        username = username[1:].strip()

    if not username:
        return None

    try:
        user = await get_user_by_username(username)
    except Exception:
        user = None

    return user


class UserAdminStates(StatesGroup):
    """
    FSM для раздела «Пользователи»:
    - ожидание идентификатора пользователя (ID / @username);
    - дальнейшие операции выполняются через callback-и (фото, бан, ограничить, статистика).
    """
    waiting_identifier_for_profile = State()


# Состояния для выдачи кастомной награды пользователю
class UserAwardsStates(StatesGroup):
    """Состояния для выдачи кастомной награды пользователю."""
    waiting_custom_award_text = State()


@router.callback_query(F.data == "admin:users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext):
    """
    Главный вход в раздел «Пользователи».

    Сразу просим у админа @username или Telegram ID нужного пользователя.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    # При входе в раздел очищаем предыдущее состояние и ставим ожидание идентификатора
    await state.clear()
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

    text = (
        "<b>Пользователи</b>\n\n"
        "Отправь @username или числовой Telegram ID пользователя.\n\n"
        "Примеры:\n"
        "<code>@nickname</code>\n"
        "<code>123456789</code>\n\n"
        "Я покажу полный профиль и дам кнопки: фотография, бан, ограничение, статистика."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        msg = await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        msg = await callback.message.answer(text, reply_markup=kb.as_markup())

    await state.update_data(
        user_prompt_chat_id=msg.chat.id,
        user_prompt_msg_id=msg.message_id,
        selected_user_id=None,
        selected_user_tg_id=None,
        selected_user_profile=None,
    )

    await callback.answer()


async def _render_admin_user_profile(
    user: dict,
    block_status: dict,
    rating_summary: dict,
    admin_stats: dict,
    awards: list[dict],
) -> str:
    """
    Сборка текста профиля пользователя для админ-раздела.
    Вынесено в отдельную функцию, чтобы можно было переиспользовать (например, для кнопки «Назад к профилю»).
    """
    internal_id = user["id"]
    tg_id = user.get("tg_id")
    username = user.get("username")
    name = user.get("name") or "Без имени"
    gender = user.get("gender") or "—"
    age = user.get("age")
    bio = (user.get("bio") or "").strip()
    created_at = user.get("created_at")
    updated_at = user.get("updated_at")
    is_admin_flag = bool(user.get("is_admin"))
    is_moderator_flag = bool(user.get("is_moderator"))
    is_support_flag = bool(user.get("is_support"))
    is_helper_flag = bool(user.get("is_helper"))
    is_deleted = bool(user.get("is_deleted"))
    is_premium = bool(user.get("is_premium"))
    premium_until = user.get("premium_until")

    is_blocked = bool(block_status.get("is_blocked"))
    blocked_until = block_status.get("blocked_until")
    blocked_reason = block_status.get("blocked_reason")

    avg_rating = rating_summary.get("avg_rating")
    ratings_count = rating_summary.get("ratings_count")

    messages_total = int(admin_stats.get("messages_total", 0) or 0) if admin_stats else 0
    ratings_given = int(admin_stats.get("ratings_given", 0) or 0) if admin_stats else 0
    comments_given = int(admin_stats.get("comments_given", 0) or 0) if admin_stats else 0
    reports_created = int(admin_stats.get("reports_created", 0) or 0) if admin_stats else 0
    active_photos = int(admin_stats.get("active_photos", 0) or 0) if admin_stats else 0
    total_photos = int(admin_stats.get("total_photos", 0) or 0) if admin_stats else 0
    upload_bans_count = int(admin_stats.get("upload_bans_count", 0) or 0) if admin_stats else 0

    awards_count = len(awards)
    has_beta_award = any(
        (a.get("code") == "beta_tester")
        or ("бета-тестер бота" in (a.get("title") or "").lower())
        for a in awards
    )

    def _fmt_dt(dt_str: str | None) -> str:
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return dt_str

    if is_premium:
        if premium_until:
            premium_text = f"активен до { _fmt_dt(premium_until) }"
        else:
            premium_text = "активен (без срока)"
    else:
        premium_text = "нет"

    if is_blocked:
        if blocked_until:
            block_text = f"да, до { _fmt_dt(blocked_until) }"
        else:
            block_text = "да, без срока"
        if blocked_reason:
            block_text += f"\nПричина: {blocked_reason}"
    else:
        block_text = "нет"

    if avg_rating is not None and ratings_count:
        rating_line = f"• Рейтинг: <b>{avg_rating:.1f}</b> (оценок: {ratings_count})"
    else:
        rating_line = "• Рейтинг: —"

    header_parts = [
        "<b>Профиль пользователя</b>",
        "",
        f"ID в базе: <code>{internal_id}</code>",
        f"Telegram ID: <code>{tg_id}</code>",
        f"Username: {'@' + username if username else '—'}",
        f"Имя: {name}",
        "",
        f"Пол: {gender}",
        f"Возраст: {age if age is not None else '—'}",
        "",
        f"Регистрация: { _fmt_dt(created_at) }",
        f"Последнее обновление: { _fmt_dt(updated_at) }",
        "",
        "<b>Роли</b>",
        f"• Админ: {'да' if is_admin_flag else 'нет'}",
        f"• Модератор: {'да' if is_moderator_flag else 'нет'}",
        f"• Поддержка: {'да' if is_support_flag else 'нет'}",
        f"• Помощник: {'да' if is_helper_flag else 'нет'}",
        "",
        "<b>Статусы</b>",
        f"• Премиум: {premium_text}",
        f"• Бан на загрузку: {block_text}",
        f"• Удалён из базы: {'да' if is_deleted else 'нет'}",
        "",
        "<b>Активность</b>",
        rating_line,
        f"• Всего действий (оценки / комментарии / жалобы): <b>{messages_total}</b>",
        f"• Оценок поставил: <b>{ratings_given}</b>",
        f"• Комментариев: <b>{comments_given}</b>",
        f"• Жалоб на фото отправил: <b>{reports_created}</b>",
        f"• Фото сейчас активно: <b>{active_photos}</b>",
        f"• Всего фото загружал: <b>{total_photos}</b>",
        f"• Ограничений на загрузку: <b>{upload_bans_count}</b>",
        "",
        "<b>Награды</b>",
        f"• Всего наград: <b>{awards_count}</b>",
        f"• Есть «Бета‑тестер бота»: {'да' if has_beta_award else 'нет'}",
    ]

    if bio:
        header_parts.append("")
        header_parts.append(f"<b>О себе</b>\n{bio}")

    return "\n".join(header_parts)


@router.message(UserAdminStates.waiting_identifier_for_profile, F.text)
async def admin_users_find_profile(message: Message, state: FSMContext):
    """Поиск и показ подробного профиля пользователя для админа."""
    identifier = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not identifier:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Пустой запрос. Пришли @username или числовой ID пользователя.",
        )
        return

    user = await _find_user_by_identifier(identifier)
    if user is None:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)

        await _edit_user_prompt_or_answer(
            message,
            state,
            "Пользователь не найден. Проверь @username или ID и попробуй ещё раз.",
            reply_markup=kb.as_markup(),
        )
        return

    internal_id = user["id"]
    tg_id = user.get("tg_id")

    # Статусы блокировки (ограничения на загрузку)
    block_status = await get_user_block_status_by_tg_id(tg_id) if tg_id else {}
    # Рейтинг и активность
    rating_summary = await get_user_rating_summary(internal_id)
    admin_stats = await get_user_admin_stats(internal_id)
    awards = await get_awards_for_user(internal_id)

    text = await _render_admin_user_profile(
        user=user,
        block_status=block_status,
        rating_summary=rating_summary,
        admin_stats=admin_stats,
        awards=awards,
    )

    # Сохраняем выбранного пользователя в FSM для дальнейших действий
    await state.update_data(
        selected_user_id=internal_id,
        selected_user_tg_id=tg_id,
        selected_user_profile=user,
    )

    kb = InlineKeyboardBuilder()
    # Просмотр и аналитика
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")

    # Награды
    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")

    # Блокировки / возврат
    is_blocked = bool(block_status.get("is_blocked"))
    if is_blocked:
        kb.button(text="♻️ Разбан", callback_data="admin:users:unban")
    else:
        kb.button(text="🚫 Бан", callback_data="admin:users:ban")

    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    kb.adjust(2, 3, 2, 2)

    await _edit_user_prompt_or_answer(
        message,
        state,
        text=text,
        reply_markup=kb.as_markup(),
    )


@router.message(UserAdminStates.waiting_identifier_for_profile)
async def admin_users_find_profile_non_text(message: Message):
    """Любой не-текст в режиме поиска пользователя просто удаляем."""
    try:
        await message.delete()
    except Exception:
        pass


# ========== USERS: ФОТОГРАФИЯ ==========
@router.callback_query(F.data == "admin:users:photo")
async def admin_users_photo(callback: CallbackQuery, state: FSMContext):
    """
    Показать актуальную фотографию выбранного пользователя
    с подробной статистикой по этому кадру.
    """
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    if not target_user_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    # Берём «текущую» фотографию пользователя — по аналогии с разделом «Моя фотография».
    photo = await get_today_photo_for_user(target_user_id)
    if not photo or photo.get("is_deleted"):
        text = (
            "У этого пользователя сейчас нет активной актуальной фотографии.\n\n"
            "Он либо ещё ничего не загружал сегодня, либо работа уже удалена."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад к профилю", callback_data="admin:users:profile")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)

        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

        await callback.answer()
        return

    stats = await get_photo_admin_stats(photo["id"])

    title = (photo.get("title") or "Без названия").strip()
    device_type = (photo.get("device_type") or "").strip()
    device_info = (photo.get("device_info") or "").strip()
    description = (photo.get("description") or "").strip()
    created_at = photo.get("created_at")
    moderation_status = (photo.get("moderation_status") or "active").strip()

    def _fmt_dt(dt_str: str | None) -> str:
        if not dt_str:
            return "—"
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return dt_str

    device_line = "устройство не указано"
    if device_type and device_info:
        device_line = f"{device_type} — {device_info}"
    elif device_type:
        device_line = device_type
    elif device_info:
        device_line = device_info

    lines: list[str] = [
        "<b>Фотография пользователя</b>",
        "",
        f"ID фото: <code>{photo['id']}</code>",
        f"Название: <b>{title}</b>",
        f"Устройство: {device_line}",
        f"Загружена: { _fmt_dt(created_at) }",
        f"Статус модерации: {moderation_status}",
        "",
        "<b>Статистика по кадру</b>",
    ]

    avg_rating = stats["avg_rating"]
    if avg_rating is not None and stats["ratings_count"] > 0:
        lines.append(f"• Средний рейтинг: <b>{avg_rating:.1f}</b>")
    else:
        lines.append("• Средний рейтинг: —")

    lines.extend(
        [
            f"• Оценок всего: <b>{stats['ratings_count']}</b>",
            f"• Супер-оценок: <b>{stats['super_ratings_count']}</b>",
            f"• Комментариев: <b>{stats['comments_count']}</b>",
            f"• Жалоб всего: <b>{stats['reports_total']}</b>",
            f"• Жалоб в ожидании: <b>{stats['reports_pending']}</b>",
            f"• Жалоб решено: <b>{stats['reports_resolved']}</b>",
        ]
    )

    if description:
        lines.append("")
        lines.append(f"📝 {description}")

    caption = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к профилю", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    # Чтобы не плодить сообщения: пробуем удалить старое и отправить новое с фото.
    try:
        try:
            await callback.message.delete()
        except Exception:
            pass

        sent = await callback.message.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=kb.as_markup(),
            disable_notification=True,
        )

        await state.update_data(
            user_prompt_chat_id=sent.chat.id,
            user_prompt_msg_id=sent.message_id,
        )
    except Exception:
        # В крайнем случае просто пробуем отредактировать подпись, если сообщение уже с фото.
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(caption, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:users:profile")
async def admin_users_back_to_profile(callback: CallbackQuery, state: FSMContext):
    """
    Вернуться к экрану профиля пользователя, не запрашивая идентификатор заново.
    """
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    user = data.get("selected_user_profile")
    internal_id = data.get("selected_user_id")
    tg_id = data.get("selected_user_tg_id")

    if not user or not internal_id or not tg_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    block_status = await get_user_block_status_by_tg_id(tg_id)
    rating_summary = await get_user_rating_summary(internal_id)
    admin_stats = await get_user_admin_stats(internal_id)
    awards = await get_awards_for_user(internal_id)

    text = await _render_admin_user_profile(
        user=user,
        block_status=block_status,
        rating_summary=rating_summary,
        admin_stats=admin_stats,
        awards=awards,
    )

    kb = InlineKeyboardBuilder()
    # Просмотр и аналитика
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")

    # Награды
    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")

    # Блокировки / возврат
    is_blocked = bool(block_status.get("is_blocked"))
    if is_blocked:
        kb.button(text="♻️ Разбан", callback_data="admin:users:unban")
    else:
        kb.button(text="🚫 Бан", callback_data="admin:users:ban")

    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    kb.adjust(2, 3, 2, 2)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()



@router.callback_query(F.data == "admin:users:stats")
async def admin_users_stats(callback: CallbackQuery, state: FSMContext):
    """
    Отдельный экран со статистикой пользователя.
    """
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")

    if not internal_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    rating_summary = await get_user_rating_summary(internal_id)
    admin_stats = await get_user_admin_stats(internal_id)

    avg_rating = rating_summary.get("avg_rating")
    ratings_count = rating_summary.get("ratings_count")

    messages_total = admin_stats["messages_total"]
    ratings_given = admin_stats["ratings_given"]
    comments_given = admin_stats["comments_given"]
    reports_created = admin_stats["reports_created"]
    active_photos = admin_stats["active_photos"]
    total_photos = admin_stats["total_photos"]
    upload_bans_count = admin_stats["upload_bans_count"]

    if avg_rating is not None and ratings_count:
        rating_line = f"• Рейтинг: <b>{avg_rating:.1f}</b> (оценок: {ratings_count})"
    else:
        rating_line = "• Рейтинг: —"

    text_lines = [
        "<b>Статистика пользователя</b>",
        "",
        rating_line,
        f"• Всего действий (оценки / комментарии / жалобы): <b>{messages_total}</b>",
        f"• Оценок поставил: <b>{ratings_given}</b>",
        f"• Комментариев: <b>{comments_given}</b>",
        f"• Жалоб на фото отправил: <b>{reports_created}</b>",
        f"• Фото сейчас активно: <b>{active_photos}</b>",
        f"• Всего фото загружал: <b>{total_photos}</b>",
        f"• Ограничений на загрузку: <b>{upload_bans_count}</b>",
    ]

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к профилю", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer("\n".join(text_lines), reply_markup=kb.as_markup())

    await callback.answer()


# ====== USER AWARDS: просмотр, выдача «бета-тестера», кастомная награда ======


@router.callback_query(F.data == "admin:users:awards")
async def admin_users_awards(callback: CallbackQuery, state: FSMContext):
    """Экран со списком наград пользователя и действиями по ним."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    user = data.get("selected_user_profile")

    if not internal_id or not user:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    awards = await get_awards_for_user(internal_id)

    lines: list[str] = [
        "🏆 <b>Награды пользователя</b>",
        "",
        f"ID в базе: <code>{internal_id}</code>",
    ]

    kb = InlineKeyboardBuilder()

    if not awards:
        lines.append("")
        lines.append("Пока нет ни одной награды. Можно выдать первую ачивку ниже ✨")
    else:
        lines.append("")
        for a in awards:
            icon = a.get("icon") or "🏅"
            title = a.get("title") or "Без названия"
            desc = (a.get("description") or "").strip()
            award_id = a.get("id")

            line = f"{icon} <b>{title}</b>"
            if desc:
                line += f"\n   {desc}"
            lines.append(line)

            # Кнопка удаления конкретной награды
            if award_id is not None:
                safe_title = title[:20]
                kb.button(
                    text=f"🗑 Удалить: {safe_title}",
                    callback_data=f"admin:users:award:del:{award_id}",
                )

    text = "\n".join(lines)

    # Общие кнопки управления наградами
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)
@router.callback_query(F.data.startswith("admin:users:award:del:"))
async def admin_users_award_delete(callback: CallbackQuery, state: FSMContext):
    """Удаление конкретной награды пользователя из админ-раздела."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")

    if not internal_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Не удалось определить награду.", show_alert=True)
        return

    try:
        award_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный идентификатор награды.", show_alert=True)
        return

    award = await get_award_by_id(award_id)
    if not award or int(award.get("user_id", 0)) != int(internal_id):
        await callback.answer("Эта награда не найдена или принадлежит другому пользователю.", show_alert=True)
        return

    await delete_award_by_id(award_id)

    # Перерисовываем список наград
    awards = await get_awards_for_user(internal_id)

    lines: list[str] = [
        "✅ Награда удалена.",
        "",
        "🏆 <b>Награды пользователя</b>",
        "",
        f"ID в базе: <code>{internal_id}</code>",
    ]

    kb = InlineKeyboardBuilder()

    if not awards:
        lines.append("")
        lines.append("Пока нет ни одной награды. Можно выдать первую ачивку ниже ✨")
    else:
        lines.append("")
        for a in awards:
            icon = a.get("icon") or "🏅"
            title = a.get("title") or "Без названия"
            desc = (a.get("description") or "").strip()
            aid = a.get("id")

            line = f"{icon} <b>{title}</b>"
            if desc:
                line += f"\n   {desc}"
            lines.append(line)

            if aid is not None:
                safe_title = title[:20]
                kb.button(
                    text=f"🗑 Удалить: {safe_title}",
                    callback_data=f"admin:users:award:del:{aid}",
                )

    text = "\n".join(lines)

    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:users:award:beta")
async def admin_users_award_beta(callback: CallbackQuery, state: FSMContext):
    """Выдать пользователю фиксированную ачивку «Бета‑тестер бота» по одному нажатию."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    target_tg_id = data.get("selected_user_tg_id")

    if not internal_id or not target_tg_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    # Пытаемся выдать ачивку
    created = await give_achievement_to_user_by_code(
        user_tg_id=target_tg_id,
        code="beta_tester",
        granted_by_tg_id=callback.from_user.id,
    )

    awards = await get_awards_for_user(internal_id)

    if created:
        prefix = "Ачивка «Бета‑тестер бота» выдана.\n\n"
    else:
        prefix = "У пользователя уже есть ачивка «Бета‑тестер бота».\n\n"

    # Если ачивка реально выдана впервые — отправляем пользователю пуш
    if created and target_tg_id:
        notify_text = (
            "🏆 <b>Новая награда!</b>\n\n"
            "Тебе выдана ачивка: <b>Бета‑тестер бота</b>.\n"
            "Ты помог(ла) тестировать GlowShot на ранних стадиях до релиза 💚"
        )
        kb_notify = InlineKeyboardBuilder()
        kb_notify.button(text="✅ Просмотрено", callback_data="award:seen")
        kb_notify.adjust(1)
        try:
            await callback.message.bot.send_message(
                chat_id=target_tg_id,
                text=notify_text,
                reply_markup=kb_notify.as_markup(),
                disable_notification=False,
            )
        except Exception:
            pass

    lines: list[str] = [prefix.rstrip(), "🏆 <b>Награды пользователя</b>", "", f"ID в базе: <code>{internal_id}</code>"]

    if not awards:
        lines.append("")
        lines.append("Пока нет ни одной награды.")
    else:
        lines.append("")
        for a in awards:
            icon = a.get("icon") or "🏅"
            title = a.get("title") or "Без названия"
            desc = (a.get("description") or "").strip()
            line = f"{icon} <b>{title}</b>"
            if desc:
                line += f"\n   {desc}"
            lines.append(line)

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 2)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:users:award:create")
async def admin_users_award_create(callback: CallbackQuery, state: FSMContext):
    """Запросить у админа текст кастомной награды для выбранного пользователя."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")

    if not internal_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    await state.set_state(UserAwardsStates.waiting_custom_award_text)
    await state.update_data(edit_chat_id=callback.message.chat.id, edit_msg_id=callback.message.message_id)

    text = (
        "🎁 <b>Новая награда</b>\n\n"
        "Отправь текст награды в формате:\n"
        "<b>Название</b> (первая строка)\n"
        "Описание (вторая строка, по желанию).\n\n"
        "Пример:\n"
        "<code>Лучший фотограф недели\nВсегда приносит в ленту очень сильные кадры.</code>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к наградам", callback_data="admin:users:awards")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.message(UserAwardsStates.waiting_custom_award_text, F.text)
async def admin_users_award_create_text(message: Message, state: FSMContext):
    """Получаем от админа текст награды и создаём её для выбранного пользователя."""
    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    edit_chat_id = data.get("edit_chat_id")
    edit_msg_id = data.get("edit_msg_id")

    if not internal_id:
        await state.clear()
        await message.answer("Пользователь не выбран. Начни с раздела «Пользователи» заново.")
        return

    raw = (message.text or "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not raw:
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=(
                    "Текст награды пустой.\n\n"
                    "Отправь хотя бы название (первая строка), описание — по желанию второй строкой."
                ),
            )
        except Exception:
            pass
        return

    parts = raw.split("\n", 1)
    title = parts[0].strip()
    description = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    if not title:
        try:
            await message.bot.edit_message_text(
                chat_id=edit_chat_id,
                message_id=edit_msg_id,
                text=(
                    "В первой строке должно быть название награды.\n\n"
                    "Пример:\n"
                    "<code>Самый активный\nКаждый день ставит оценки и оставляет комментарии.</code>"
                ),
            )
        except Exception:
            pass
        return

    admin_db_user = await get_user_by_tg_id(message.from_user.id)
    granted_by_user_id = admin_db_user["id"] if admin_db_user else None

    await create_custom_award_for_user(
        user_id=internal_id,
        title=title,
        description=description,
        icon="🏅",
        code=None,
        is_special=False,
        granted_by_user_id=granted_by_user_id,
    )

    # Пытаемся найти tg_id пользователя, чтобы отправить пуш
    target_tg_id = None
    user = data.get("selected_user_profile")
    if user and user.get("tg_id"):
        target_tg_id = user["tg_id"]
    else:
        try:
            db_user = await get_user_by_id(internal_id)
            if db_user and db_user.get("tg_id"):
                target_tg_id = db_user["tg_id"]
        except Exception:
            target_tg_id = None

    if target_tg_id:
        notify_lines = [
            "🏆 <b>Новая награда!</b>",
            "",
            f"Тебе выдана награда: <b>{title}</b>",
        ]
        if description:
            notify_lines.append("")
            notify_lines.append(description)
        notify_text = "\n".join(notify_lines)

        kb_notify = InlineKeyboardBuilder()
        kb_notify.button(text="✅ Просмотрено", callback_data="award:seen")
        kb_notify.adjust(1)
        try:
            await message.bot.send_message(
                chat_id=target_tg_id,
                text=notify_text,
                reply_markup=kb_notify.as_markup(),
                disable_notification=False,
            )
        except Exception:
            pass

    # После создания — возвращаемся к экрану наград и показываем обновлённый список
    awards = await get_awards_for_user(internal_id)

    lines: list[str] = [
        "✅ Награда добавлена.",
        "",
        "🏆 <b>Награды пользователя</b>",
        "",
        f"ID в базе: <code>{internal_id}</code>",
        "",
    ]

    if not awards:
        lines.append("Пока нет ни одной награды.")
    else:
        for a in awards:
            icon = a.get("icon") or "🏅"
            atitle = a.get("title") or "Без названия"
            desc = (a.get("description") or "").strip()
            line = f"{icon} <b>{atitle}</b>"
            if desc:
                line += f"\n   {desc}"
            lines.append(line)

    text = "\n".join(lines)

    # Возвращаем состояние к пользовательскому разделу
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 2)

    try:
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await message.bot.send_message(
            chat_id=edit_chat_id,
            text=text,
            reply_markup=kb.as_markup(),
            disable_notification=True,
        )


# ====== PAYMENTS: STATES & HELPERS ======
class PaymentsStates(StatesGroup):
    """
    Состояния для раздела «Платежи».
    Пока нужны только для хранения служебного сообщения.
    """
    idle = State()


async def _edit_payments_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """
    Helper для раздела «Платежи»: стараемся всегда держать одно служебное сообщение.
    """
    data = await state.get_data()
    chat_id = data.get("payments_chat_id")
    msg_id = data.get("payments_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    try:
        sent = await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        sent = await message.answer(text, reply_markup=reply_markup)

    await state.update_data(
        payments_chat_id=sent.chat.id,
        payments_msg_id=sent.message_id,
    )


# ====== PAYMENTS: MAIN MENU ======
@router.callback_query(F.data == "admin:payments")
async def admin_payments_menu(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    text = (
        "<b>Платежи и подписки</b>\n\n"
        "Здесь можно посмотреть:\n"
        "• список успешных платежей;\n"
        "• доходы за день / неделю / месяц;\n"
        "• список активных тарифов;\n"
        "• (позже) управление тарифами;\n"
        "• подписки пользователей.\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📜 Список платежей", callback_data="admin:payments:list:1")
    kb.button(text="💰 Доходы", callback_data="admin:payments:revenue")
    kb.button(text="🏷 Тарифы и продукты", callback_data="admin:payments:tariffs")
    kb.button(text="⚙️ Управление тарифами", callback_data="admin:payments:tariffs_manage")
    kb.button(text="👥 Подписки пользователей", callback_data="admin:payments:subs:1")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ====== PAYMENTS: LIST ======
@router.callback_query(F.data.startswith("admin:payments:list"))
async def admin_payments_list(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    page = 1
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 1

    total = await get_payments_count()
    page_size = 20
    max_page = max(1, (total + page_size - 1) // page_size)
    if page > max_page:
        page = max_page

    rows = await get_payments_page(page, page_size=page_size)

    lines: list[str] = [
        "<b>Список успешных платежей</b>",
        "",
        f"Всего платежей: <b>{total}</b>",
    ]

    if not rows:
        lines.append("")
        lines.append("Пока нет ни одного успешного платежа.")
    else:
        lines.append("")
        start_idx = (page - 1) * page_size + 1
        for idx, p in enumerate(rows, start=start_idx):
            created_at = p.get("created_at")
            try:
                dt = datetime.fromisoformat(created_at) if created_at else None
                created_human = dt.strftime("%d.%m.%Y %H:%M") if dt else created_at
            except Exception:
                created_human = created_at or "—"

            username = p.get("user_username")
            name = p.get("user_name") or ""
            tg_id = p.get("user_tg_id")
            user_label = f"@{username}" if username else (name or f"ID {tg_id}")

            method = p.get("method")
            method_label = "💳 RUB" if method == "rub" else "⭐ Stars"

            currency = p.get("currency")
            amount = int(p.get("amount") or 0)
            if currency == "RUB":
                amount_human = f"{amount / 100:.2f} ₽"
            else:
                amount_human = f"{amount} ⭐"

            period_code = p.get("period_code")
            days = p.get("days")

            lines.append(
                f"{idx}. {created_human} — {user_label}\n"
                f"   Тариф: {period_code} ({days} дн.), сумма: {amount_human}, способ: {method_label}"
            )

    lines.append("")
    lines.append(f"Страница <b>{page}</b> из <b>{max_page}</b>.")

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(
            text="◀️ Назад",
            callback_data=f"admin:payments:list:{page-1}",
        )
    if page < max_page:
        kb.button(
            text="▶️ Вперёд",
            callback_data=f"admin:payments:list:{page+1}",
        )
    kb.button(text="⬅️ К разделу платежей", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ====== PAYMENTS: REVENUE ======
@router.callback_query(F.data == "admin:payments:revenue")
async def admin_payments_revenue(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    day = await get_revenue_summary("day")
    week = await get_revenue_summary("week")
    month = await get_revenue_summary("month")

    def fmt_block(label: str, data: dict) -> str:
        rub_total = data.get("rub_total", 0.0) or 0.0
        rub_count = data.get("rub_count", 0) or 0
        stars_total = data.get("stars_total", 0) or 0
        stars_count = data.get("stars_count", 0) or 0
        return (
            f"<b>{label}</b>\n"
            f"• RUB: {rub_total:.2f} ₽ ({rub_count} платежей)\n"
            f"• Stars: {stars_total} ⭐ ({stars_count} платежей)"
        )

    lines = [
        "<b>Доходы</b>",
        "",
        fmt_block("За последние 24 часа", day),
        "",
        fmt_block("За последние 7 дней", week),
        "",
        fmt_block("За последние 30 дней", month),
    ]

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К разделу платежей", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ====== PAYMENTS: TARIFFS VIEW ======
@router.callback_query(F.data == "admin:payments:tariffs")
async def admin_payments_tariffs(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    lines = [
        "<b>Тарифы и продукты</b>",
        "",
        "Сейчас доступны такие тарифы GlowShot Premium:",
        "",
    ]

    for code, t in TARIFFS.items():
        days = t.get("days")
        price_rub = t.get("price_rub")
        price_stars = t.get("price_stars")
        title = t.get("title")
        lines.append(
            f"• <b>{title}</b>\n"
            f"  Код: <code>{code}</code>, длительность: {days} дн.\n"
            f"  Цена: {price_rub} ₽ или {price_stars} ⭐"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Управление тарифами", callback_data="admin:payments:tariffs_manage")
    kb.button(text="⬅️ К разделу платежей", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ====== PAYMENTS: TARIFFS MANAGE (stub) ======
@router.callback_query(F.data == "admin:payments:tariffs_manage")
async def admin_payments_tariffs_manage(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    text = (
        "<b>Управление тарифами</b>\n\n"
        "Сейчас тарифы заданы в коде (константа TARIFFS).\n"
        "Позже здесь можно будет добавлять, скрывать и менять тарифы прямо из админки."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🏷 Посмотреть тарифы", callback_data="admin:payments:tariffs")
    kb.button(text="⬅️ К разделу платежей", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ====== PAYMENTS: SUBSCRIPTIONS ======
@router.callback_query(F.data.startswith("admin:payments:subs"))
async def admin_payments_subscriptions(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    page = 1
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = 1

    total_users = await get_subscriptions_total()
    page_size = 20
    max_page = max(1, (total_users + page_size - 1) // page_size)
    if page > max_page:
        page = max_page

    rows = await get_subscriptions_page(page, page_size=page_size)

    lines: list[str] = [
        "<b>Подписки пользователей</b>",
        "",
        f"Всего платящих пользователей: <b>{total_users}</b>",
    ]

    if not rows:
        lines.append("")
        lines.append("Пока нет ни одного пользователя с платёжной историей.")
    else:
        lines.append("")
        start_idx = (page - 1) * page_size + 1
        for idx, row in enumerate(rows, start=start_idx):
            username = row.get("user_username")
            name = row.get("user_name") or ""
            tg_id = row.get("user_tg_id")
            user_label = f"@{username}" if username else (name or f"ID {tg_id}")

            last_payment_at = row.get("last_payment_at")
            try:
                dt = datetime.fromisoformat(last_payment_at) if last_payment_at else None
                last_payment_human = dt.strftime("%d.%m.%Y %H:%M") if dt else last_payment_at
            except Exception:
                last_payment_human = last_payment_at or "—"

            payments_count = int(row.get("payments_count") or 0)
            total_days = int(row.get("total_days") or 0)
            total_rub = float(row.get("total_rub") or 0.0)
            total_stars = int(row.get("total_stars") or 0)

            lines.append(
                f"{idx}. {user_label}\n"
                f"   Последний платёж: {last_payment_human}\n"
                f"   Всего платежей: {payments_count}, всего дней: {total_days}\n"
                f"   Оплачено: {total_rub:.2f} ₽ и {total_stars} ⭐"
            )

    lines.append("")
    lines.append(f"Страница <b>{page}</b> из <b>{max_page}</b>.")

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(
            text="◀️ Назад",
            callback_data=f"admin:payments:subs:{page-1}",
        )
    if page < max_page:
        kb.button(
            text="▶️ Вперёд",
            callback_data=f"admin:payments:subs:{page+1}",
        )
    kb.button(text="⬅️ К разделу платежей", callback_data="admin:payments")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)

    await _edit_payments_prompt_or_answer(
        callback.message,
        state,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()
    

# ====== USERS: БАН / РАЗБАН / ОГРАНИЧИТЬ (ЗАГЛУШКИ) ======

@router.callback_query(F.data == "admin:users:ban")
async def admin_users_ban(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Бан пользователя — пока заглушка.", show_alert=True)

@router.callback_query(F.data == "admin:users:unban")
async def admin_users_unban(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Разбан пользователя — пока заглушка.", show_alert=True)

@router.callback_query(F.data == "admin:users:limit")
async def admin_users_limit(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Ограничить доступ — пока заглушка.", show_alert=True)
# ====== AchievementStates: FSM для работы с ачивками ======
class AchievementStates(StatesGroup):
    """
    Состояния для работы с ачивками / наградами в админке.
    Пока есть:
    • выдача статуса «Бета-тестер бота»;
    • выдача кастомной ачивки;
    • управление (просмотр/редактирование/удаление) ачивок пользователя.
    """
    waiting_user_for_beta = State()
    waiting_custom_user = State()
    waiting_custom_title = State()
    waiting_custom_description = State()
    waiting_custom_icon = State()
    waiting_custom_level = State()
    waiting_manage_user = State()
    waiting_edit_award_text = State()
    waiting_edit_award_icon = State()

from keyboards.common import build_admin_menu, build_back_kb
from utils.time import get_moscow_now
from config import ADMIN_PASSWORD, MASTER_ADMIN_ID

ADMIN_PANEL_TEXT = "<b>Админ-панель</b>\n\nВыбери раздел:"

# ================= НАСТРОЙКИ АДМИНКИ =================


class AdminStates(StatesGroup):
    waiting_password = State()


# ====== RoleStates: FSM для управления ролями ======
class RoleStates(StatesGroup):
    """
    Состояния для управления ролями (модераторы, помощники, поддержка, премиум).
    """
    waiting_user_for_add = State()
    waiting_user_for_remove = State()
    waiting_premium_duration = State()


class BroadcastStates(StatesGroup):
    """
    Состояния для рассылки:
    • аудитория выбирается через callback;
    • ждём текст сообщения от админа;
    • показываем превью и ждём подтверждения отправки.
    """
    waiting_text = State()


UserEvent = Union[Message, CallbackQuery]


async def _get_admin_context(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get("admin_chat_id"), data.get("admin_msg_id")


# ================= HELPER: Edit last role prompt or answer =================
async def _edit_role_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """
    Попробовать отредактировать последнее служебное сообщение бота в разделе ролей.
    Если данных о сообщении нет или редактирование не удалось — отправляем новый ответ.
    """
    data = await state.get_data()
    chat_id = data.get("role_prompt_chat_id")
    msg_id = data.get("role_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=reply_markup)


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================


async def _get_from_user(event: UserEvent):
    if isinstance(event, CallbackQuery):
        return event.from_user
    return event.from_user


async def _ensure_user(event: UserEvent) -> Optional[dict]:
    from_user = await _get_from_user(event)
    user = await get_user_by_tg_id(from_user.id)
    if user is None:
        text = "Сначала нужно зарегистрироваться через /start."
        if isinstance(event, CallbackQuery):
            await event.message.answer(text)
        else:
            await event.answer(text)
        return None
    return user



async def _ensure_admin(event: UserEvent) -> Optional[dict]:
    user = await _ensure_user(event)
    if user is None:
        return None

    from_user = await _get_from_user(event)

    if MASTER_ADMIN_ID and from_user.id == MASTER_ADMIN_ID:
        return user

    if not user.get("is_admin"):
        text = "У тебя нет прав администратора."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None

    return user

# ================= КЛАВИАТУРЫ =================


def build_password_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="admin:cancel")
    kb.adjust(1)
    return kb.as_markup()


def build_roles_menu_kb() -> InlineKeyboardMarkup:
    """
    Клавиатура для раздела управления ролями.
    Здесь будут модераторы, помощники, поддержка и премиум-пользователи.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Модераторы", callback_data="admin:roles:moderator")
    kb.button(text="🤝 Помощники", callback_data="admin:roles:helper")
    kb.button(text="👨‍💻 Поддержка", callback_data="admin:roles:support")
    kb.button(text="💎 Премиум", callback_data="admin:roles:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


# ================= ВХОД В АДМИНКУ =================


@router.message(F.text == "/admin")
async def admin_entry(message: Message, state: FSMContext):
    user = await _ensure_user(message)
    if user is None:
        return

    # MASTER_ADMIN_ID попадает в админку без пароля
    if MASTER_ADMIN_ID and message.from_user.id == MASTER_ADMIN_ID:
        await set_user_admin_by_tg_id(message.from_user.id, True)
        await state.clear()
        await message.answer(
            ADMIN_PANEL_TEXT,
            reply_markup=build_admin_menu(),
        )
        return

    if user.get("is_admin"):
        await state.clear()
        await message.answer(
            ADMIN_PANEL_TEXT,
            reply_markup=build_admin_menu(),
        )
        return

    await state.clear()
    await state.set_state(AdminStates.waiting_password)
    await state.update_data(admin_attempts=0)

    prompt = await message.answer(
        "Введи пароль администратора:\n\n"
        "Если передумал — нажми «Отмена».",
        reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
    )
    # Запоминаем сообщение, которое будем редактировать на каждом шаге ввода пароля
    await state.update_data(
        admin_chat_id=prompt.chat.id,
        admin_msg_id=prompt.message_id,
    )


# ================= ОТМЕНА ВХОДА =================


@router.callback_query(AdminStates.waiting_password, F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    chat_id, msg_id = await _get_admin_context(state)
    await state.clear()
    text = "Вход в админ-панель отменён.\n\nТы по-прежнему в обычном режиме."

    if chat_id and msg_id:
        try:
            await callback.message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
            await callback.answer()
            return
        except Exception:
            pass
    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)
    await callback.answer()


# ================= ПРОВЕРКА ПАРОЛЯ =================


@router.message(AdminStates.waiting_password, F.text)
async def admin_check_password(message: Message, state: FSMContext):
    chat_id, msg_id = await _get_admin_context(state)
    if not chat_id or not msg_id:
        await state.clear()
        await message.delete()
        await message.answer(
            "Сессия ввода пароля сбилась.\n\n"
            "Напиши /admin, чтобы попробовать снова.",
        )
        return

    data = await state.get_data()
    attempts = int(data.get("admin_attempts", 0))

    text = (message.text or "").strip()

    #####  Неверный пароль
    if text != ADMIN_PASSWORD:
        attempts += 1
        await state.update_data(admin_attempts=attempts)
        await message.delete()

        if attempts >= 3:
            # Слишком много попыток — выходим из режима ввода
            await state.clear()
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=(
                        "Пароль несколько раз введён неверно.\n"
                        "Режим входа в админ-панель закрыт. Напиши /admin, чтобы попробовать снова."
                    ),
                )
            except Exception:
                await message.answer(
                    "Пароль несколько раз введён неверно.\n"
                    "Режим входа в админ-панель закрыт. Напиши /admin, чтобы попробовать снова.",
                )
            return

        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    "Неверный пароль. Попробуй ещё раз.\n\n"
                    f"Осталось попыток: <b>{3 - attempts}</b>"
                ),
                reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
            )
        except Exception:
            await message.answer(
                "Неверный пароль. Попробуй ещё раз.\n\n"
                f"Осталось попыток: <b>{3 - attempts}</b>",
                reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
            )
        return

    #####  Пароль верный
    await set_user_admin_by_tg_id(message.from_user.id, True)
    await state.clear()
    await message.delete()

    user = await get_user_by_tg_id(message.from_user.id)
    name = user.get("name") or "админ"

    try:
        await message.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=ADMIN_PANEL_TEXT,
            reply_markup=build_admin_menu(),
        )
    except Exception:
        await message.answer(
            ADMIN_PANEL_TEXT,
            reply_markup=build_admin_menu(),
        )


@router.message(AdminStates.waiting_password)
async def admin_waiting_password_non_text(message: Message):
    await message.delete()


# ================= МЕНЮ АДМИНА =================

@router.callback_query(F.data == "admin:menu")
async def admin_menu_callback(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.clear()

    text = ADMIN_PANEL_TEXT
    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_admin_menu(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=build_admin_menu(),
        )

    try:
        await callback.answer()
    except TelegramBadRequest as e:
        # старый/протухший callback – можно тихо игнорировать
        if "query is too old" in str(e) or "query ID is invalid" in str(e):
            pass
        else:
            raise


# ====== Конфиг ролей для управления ======
ROLE_CONFIG = {
    "moderator": {
        "code": "moderator",
        "name_single": "модератора",
        "name_plural": "модераторы",
        "get_list": get_moderators,
        "set_func": set_user_moderator_by_tg_id,
    },
    "helper": {
        "code": "helper",
        "name_single": "помощника",
        "name_plural": "помощники",
        "get_list": get_helpers,
        "set_func": set_user_helper_by_tg_id,
    },
    "support": {
        "code": "support",
        "name_single": "поддержки",
        "name_plural": "поддержка",
        "get_list": get_support_users,
        "set_func": set_user_support_by_tg_id,
    },
    "premium": {
        "code": "premium",
        "name_single": "премиум-подписку",
        "name_plural": "премиум-пользователи",
        "get_list": get_premium_users,
        "set_func": set_user_premium_role_by_tg_id,
    },
}


@router.callback_query(F.data == "admin:roles")
async def admin_roles_menu(callback: CallbackQuery, state: FSMContext):
    """
    Раздел управления ролями:
    - модераторы
    - помощники
    - поддержка.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    # очищаем FSM, если до этого были шаги выдачи/удаления ролей
    await state.clear()

    text = (
        "<b>Роли</b>\n\n"
        "Здесь можно управлять командами проекта и особыми статусами:\n"
        "• 🛡 Модераторы — следят за контентом и жалобами\n"
        "• 🤝 Помощники — помогают с ручными задачами, тестами и проверками\n"
        "• 👨‍💻 Поддержка — отвечают пользователям в саппорт-боте\n"
        "• 💎 Премиум — пользователи с платными/особенными возможностями\n\n"
        "Выбери нужную роль, чтобы посмотреть список, добавить или удалить участника."
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=build_roles_menu_kb(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=build_roles_menu_kb(),
        )

    await callback.answer()


# ====== Управление ролями: FSM и роутеры ======

@router.callback_query(F.data.startswith("admin:roles:"))
async def admin_roles_router(callback: CallbackQuery, state: FSMContext):
    """
    Управление конкретной ролью:
    admin:roles:<role>           — меню роли
    admin:roles:<role>:list      — список участников
    admin:roles:<role>:add       — запросить ID / username для добавления
    admin:roles:<role>:remove    — запросить ID / username для удаления
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    # ожидаем минимум admin:roles:<role>
    if len(parts) < 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    role_code = parts[2]
    cfg = ROLE_CONFIG.get(role_code)
    if cfg is None:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    # Если только admin:roles:<role> — показываем меню роли
    if len(parts) == 3:
        # При входе в меню роли сбрасываем состояние выдачи/удаления
        await state.clear()

        text = (
            f"<b>Роль: {cfg['name_plural'].capitalize()}</b>\n\n"
            "Что хочешь сделать?\n"
            "• 📋 Посмотреть список\n"
            f"• ➕ Добавить {cfg['name_single']}\n"
            f"• ➖ Удалить {cfg['name_single']}\n"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="📋 Список", callback_data=f"admin:roles:{role_code}:list")
        kb.button(text="➕ Добавить", callback_data=f"admin:roles:{role_code}:add")
        kb.button(text="➖ Удалить", callback_data=f"admin:roles:{role_code}:remove")
        kb.button(text="⬅️ Назад к ролям", callback_data="admin:roles")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=kb.as_markup(),
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=kb.as_markup(),
            )

        await callback.answer()
        return

    # Есть действие: list / add / remove
    action = parts[3]

    # Показать список
    if action == "list":
        users_with_role = await cfg["get_list"]()

        if not users_with_role:
            if role_code == "premium":
                text = "Сейчас нет ни одного премиум-пользователя."
            else:
                text = f"Сейчас нет ни одного {cfg['name_single']}."
        else:
            # Отдельный формат для премиум-пользователей
            if role_code == "premium":
                now_date = get_moscow_now().date()
                lines: list[str] = ["<b>Премиум-пользователи</b>", ""]
                for u in users_with_role:
                    username = u.get("username")
                    name = u.get("name") or "Без имени"
                    label = f"@{username}" if username else name

                    premium_until = u.get("premium_until")
                    if premium_until:
                        try:
                            until_dt = datetime.fromisoformat(premium_until)
                            until_str = until_dt.strftime("%d.%m.%Y")
                            days_left = (until_dt.date() - now_date).days
                            if days_left < 0:
                                duration = f"до {until_str} (истёк)"
                            elif days_left == 0:
                                duration = "до конца дня"
                            else:
                                duration = f"до {until_str}"
                        except Exception:
                            duration = premium_until
                    else:
                        duration = "бессрочно"

                    lines.append(f"• {label} — ({duration})")

                text = "\n".join(lines)
            else:
                lines = []
                for u in users_with_role:
                    username = u.get("username")
                    line = f"• {u.get('name') or 'Без имени'} — ID <code>{u.get('tg_id')}</code>"
                    if username:
                        line += f" (@{username})"
                    lines.append(line)

                text = (
                    f"<b>{cfg['name_plural'].capitalize()}</b>\n\n" +
                    "\n".join(lines)
                )

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.button(text="⬅️ В роли", callback_data="admin:roles")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1)

        try:
            await callback.message.edit_text(
                text,
                reply_markup=kb.as_markup(),
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=kb.as_markup(),
            )

        await callback.answer()
        return

    # Добавление или удаление — запускаем FSM
    if action in ("add", "remove"):
        if action == "add":
            await state.set_state(RoleStates.waiting_user_for_add)
        else:
            await state.set_state(RoleStates.waiting_user_for_remove)

        await state.update_data(role_code=role_code, action=action)

        text = (
            f"Введи ID или @username пользователя, которому нужно "
            f"{'выдать' if action == 'add' else 'снять'} роль {cfg['name_single']}.\n\n"
            "Пример: <code>123456789</code> или <code>@username</code>.\n\n"
            "Если передумал — нажми «Назад», чтобы вернуться в меню роли."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)

        try:
            prompt = await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            prompt = await callback.message.answer(text, reply_markup=kb.as_markup())

        await state.update_data(
            role_prompt_chat_id=prompt.chat.id,
            role_prompt_msg_id=prompt.message_id,
        )

        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(RoleStates.waiting_user_for_add, F.text)
async def role_add_user(message: Message, state: FSMContext):
    """
    Добавить пользователя к выбранной роли по ID или @username.
    Для роли 'premium' дополнительно спрашиваем срок действия.
    """
    data = await state.get_data()
    role_code = data.get("role_code")
    cfg = ROLE_CONFIG.get(role_code)

    if cfg is None:
        await state.clear()
        await message.answer("Сессия назначения роли потерялась. Открой раздел ролей заново.")
        return

    identifier = (message.text or "").strip()
    # Удаляем сообщение админа из чата, чтобы не плодить лишний текст
    try:
        await message.delete()
    except Exception:
        pass

    user = await _find_user_by_identifier(identifier)

    if not user:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username.",
        )
        return

    tg_id = user.get("tg_id")
    username = user.get("username")
    name = user.get("name") or "Без имени"

    # Для премиума сначала спрашиваем срок
    if role_code == "premium":
        await state.update_data(
            role_code=role_code,
            pending_premium_tg_id=tg_id,
            pending_premium_username=username,
            pending_premium_name=name,
        )

        await state.set_state(RoleStates.waiting_premium_duration)
        extra = f" (@{username})" if username else ""

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)

        await _edit_role_prompt_or_answer(
            message,
            state,
            f"Выдаём премиум-подписку пользователю {name} — ID <code>{tg_id}</code>{extra}.\n\n"
            "На какой срок выдать премиум?\n"
            "• Напиши количество дней (например: <code>7</code> или <code>30</code>);\n"
            "• или отправь <b>навсегда</b>, чтобы выдать бессрочный премиум.",
            reply_markup=kb.as_markup(),
        )
        return

    # Все остальные роли — как раньше
    await cfg["set_func"](tg_id, True)
    extra = f" (@{username})" if username else ""

    # Клавиатура после успешной выдачи роли
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В роли", callback_data="admin:roles")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_role_prompt_or_answer(
        message,
        state,
        f"Роль {cfg['name_single']} выдана пользователю {name} — ID <code>{tg_id}</code>{extra} ✅",
        reply_markup=kb.as_markup(),
    )

    # Уведомление пользователю о новой роли
    try:
        notif_kb = InlineKeyboardBuilder()
        notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
        notif_kb.adjust(1)

        if role_code == "moderator":
            notif_text = (
                "🛡 <b>Тебе выдана роль модератора</b>\n\n"
                "Теперь ты можешь помогать следить за порядком и жалобами в GlowShot."
            )
        elif role_code == "helper":
            notif_text = (
                "🤝 <b>Тебе выдана роль помощника</b>\n\n"
                "Ты помогаешь проекту с ручными задачами и тестами. Спасибо за поддержку!"
            )
        elif role_code == "support":
            notif_text = (
                "👨‍💻 <b>Тебе выдана роль поддержки</b>\n\n"
                "Теперь ты можешь отвечать пользователям и помогать им в саппорте."
            )
        else:
            notif_text = (
                "⭐️ <b>Тебе выдана новая роль</b>\n\n"
                "Спасибо, что помогаешь проекту GlowShot."
            )

        await message.bot.send_message(
            chat_id=tg_id,
            text=notif_text,
            reply_markup=notif_kb.as_markup(),
        )
    except Exception:
        pass

    await state.clear()


# Новый хендлер для срока премиума
@router.message(RoleStates.waiting_premium_duration, F.text)
async def role_set_premium_duration(message: Message, state: FSMContext):
    """
    Обработчик срока премиум-подписки после выбора пользователя.
    """
    data = await state.get_data()
    tg_id = data.get("pending_premium_tg_id")
    name = data.get("pending_premium_name") or "Без имени"
    username = data.get("pending_premium_username")

    if not tg_id:
        await state.clear()
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Данные о пользователе потерялись. Попробуй выдать премиум ещё раз.",
        )
        return

    raw = (message.text or "").strip().lower()
    # Удаляем сообщение админа, чтобы не копить текст
    try:
        await message.delete()
    except Exception:
        pass

    # Клавиатура для возврата после успешной выдачи премиума
    kb_done = InlineKeyboardBuilder()
    kb_done.button(text="⬅️ В роли", callback_data="admin:roles")
    kb_done.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb_done.adjust(1, 1)

    # Бессрочный премиум
    if raw in ("навсегда", "бессрочно", "навечно", "forever", "∞"):
        await set_user_premium_status(tg_id, True, premium_until=None)

        extra = f" (@{username})" if username else ""
        await _edit_role_prompt_or_answer(
            message,
            state,
            f"Премиум-подписка выдана пользователю {name} — ID <code>{tg_id}</code>{extra} "
            f"на <b>бессрочный</b> период ✅",
            reply_markup=kb_done.as_markup(),
        )

        await state.clear()

        # Уведомление пользователю о бессрочном премиуме
        notif_kb = InlineKeyboardBuilder()
        notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
        notif_kb.adjust(1)

        notif_text = (
            "💎 <b>GlowShot Premium выдан!</b>\n\n"
            "Твой премиум-статус активен <b>без ограничения по времени</b>.\n\n"
            "Спасибо, что поддерживаешь проект 💙"
        )

        try:
            await message.bot.send_message(
                chat_id=tg_id,
                text=notif_text,
                reply_markup=notif_kb.as_markup(),
            )
        except Exception:
            pass

        return

    # Пытаемся разобрать количество дней
    try:
        days = int(raw)
    except ValueError:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Не понял срок премиума.\n\n"
            "Напиши число дней (например: <code>7</code> или <code>30</code>) "
            "или отправь <b>навсегда</b>.",
        )
        return

    if days <= 0:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Срок должен быть больше нуля. Попробуй ещё раз.",
        )
        return

    # Рассчитываем дату окончания по московскому времени
    now = get_moscow_now()
    until_dt = now + timedelta(days=days)
    premium_until_iso = until_dt.isoformat(timespec="seconds")
    human_until = until_dt.strftime("%d.%m.%Y")

    await set_user_premium_status(tg_id, True, premium_until=premium_until_iso)

    extra = f" (@{username})" if username else ""
    await _edit_role_prompt_or_answer(
        message,
        state,
        f"Премиум-подписка выдана пользователю {name} — ID <code>{tg_id}</code>{extra} "
        f"на <b>{days}</b> дн. (до {human_until}) ✅",
        reply_markup=kb_done.as_markup(),
    )

    await state.clear()

    # Уведомление пользователю о премиуме с конкретным сроком
    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
    notif_kb.adjust(1)

    notif_text = (
        "💎 <b>GlowShot Premium выдан!</b>\n\n"
        f"Твой премиум-статус активен до <b>{human_until}</b> "
        f"(на {days} дн.).\n\n"
        "Спасибо, что поддерживаешь проект 💙"
    )

    try:
        await message.bot.send_message(
            chat_id=tg_id,
            text=notif_text,
            reply_markup=notif_kb.as_markup(),
        )
    except Exception:
        pass


@router.message(RoleStates.waiting_user_for_remove, F.text)
async def role_remove_user(message: Message, state: FSMContext):
    """
    Удалить пользователя из выбранной роли по ID или @username.
    """
    data = await state.get_data()
    role_code = data.get("role_code")
    cfg = ROLE_CONFIG.get(role_code)

    if cfg is None:
        await state.clear()
        await message.answer("Сессия управления ролью потерялась. Открой раздел ролей заново.")
        return

    identifier = (message.text or "").strip()
    # Удаляем сообщение админа
    try:
        await message.delete()
    except Exception:
        pass

    user = await _find_user_by_identifier(identifier)

    if not user:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username.",
        )
        return

    tg_id = user.get("tg_id")
    username = user.get("username")
    name = user.get("name") or "Без имени"

    await cfg["set_func"](tg_id, False)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В роли", callback_data="admin:roles")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    extra = f" (@{username})" if username else ""
    await _edit_role_prompt_or_answer(
        message,
        state,
        f"Роль {cfg['name_single']} снята с пользователя {name} — ID <code>{tg_id}</code>{extra} ✅",
        reply_markup=kb.as_markup(),
    )

    await state.clear()


# ====== Универсальный хендлер для кнопки «Просмотрено» ======
@router.callback_query(F.data == "admin:notif_read")
async def admin_notif_read(callback: CallbackQuery):
    """
    Универсальная кнопка для пуш-уведомлений:
    по нажатию удаляет сообщение с уведомлением.
    """
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.answer("Уведомление скрыто.")
        except Exception:
            pass


async def _edit_broadcast_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """
    Аналог _edit_role_prompt_or_answer, но для раздела «Рассылка».
    Пытаемся отредактировать последнее служебное сообщение,
    если нет — шлём новое.
    """
    data = await state.get_data()
    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass

    await message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "admin:help_reports")
async def admin_help_reports(callback: CallbackQuery):
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Жалобы на фотографии</b>\n\n"
        "• Пользователь может нажать кнопку «🚫 Пожаловаться» под фотографией в разделе оценивания.\n"
        "• Бот попросит описать проблему с кадром.\n"
        "• Текст жалобы приходит админу в личные сообщения.\n\n"
        "Важно: сейчас жалобы <b>не скрывают фотографию автоматически</b> и не блокируют автора.\n"
        "Решение остаётся за тобой как за модератором.\n\n"
        "В будущем сюда можно добавить:\n"
        "• список всех жалоб,\n"
        "• быстрые кнопки для скрытия фото и блокировки пользователей."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.button(text="⬅️ В обычное меню", callback_data="menu:back")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats_menu(callback: CallbackQuery):
    """
    Раздел «Статистика».
    Показывает список доступных метрик и фильтров.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Статистика</b>\n\n"
        "Выбери, что посмотреть:\n"
        "• 👥 Общее количество пользователей\n"
        "• 📈 Активные за последние 24 часа\n"
        "• ⏱ Онлайн сейчас (за последние 5 минут)\n"
        "• 📬 Основные действия / события\n"
        "• ➕ Новые пользователи за последние 3 дня\n"
        "• 💎 Премиум-пользователи\n"
        "• ⛔️ Пользователи в бане\n"
        "• 🏆 Неоднократные победители в топ-3 дня"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Кол-во пользователей", callback_data="admin:stats:total_users")
    kb.button(text="📈 Активные за 24 часа", callback_data="admin:stats:active")
    kb.button(text="⏱ Онлайн сейчас", callback_data="admin:stats:online")
    kb.button(text="📬 Сообщения / действия", callback_data="admin:stats:messages")
    kb.button(text="➕ Новые (3 дня)", callback_data="admin:stats:new")
    kb.button(text="💎 Премиум-пользователи", callback_data="admin:stats:premium")
    kb.button(text="⛔️ В бане", callback_data="admin:stats:banned")
    kb.button(text="🏆 С неоднократными победами", callback_data="admin:stats:top_winners")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data == "admin:stats:total_users")
async def admin_stats_total_users(callback: CallbackQuery):
    """
    Подраздел статистики: общее количество пользователей.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    total_users = await get_total_users()
    users_sample = []
    if total_users <= 20 and total_users > 0:
        users_sample = await get_users_sample(limit=20)

    lines: list[str] = [
        "<b>Статистика → Кол-во пользователей</b>",
        "",
        f"Всего пользователей: <b>{total_users}</b>.",
    ]

    if users_sample:
        lines.append("")
        lines.append("Список пользователей:")
        for u in users_sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


# ====== Новые хендлеры статистики ======


@router.callback_query(F.data == "admin:stats:active")
async def admin_stats_active(callback: CallbackQuery):
    """
    Активные за последние 24 часа (по updated_at).
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    total, sample = await get_active_users_last_24h(limit=20)

    lines: list[str] = [
        "<b>Статистика → Активные за 24 часа</b>",
        "",
        f"За последние 24 часа пользовались ботом: <b>{total}</b> человек.",
    ]

    if sample and total <= 20:
        lines.append("")
        lines.append("Пользователи:")
        for u in sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:stats:online")
async def admin_stats_online(callback: CallbackQuery):
    """
    Онлайн сейчас: активность за последние 5 минут.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    total, sample = await get_online_users_recent(window_minutes=5, limit=20)

    lines: list[str] = [
        "<b>Статистика → Онлайн сейчас</b>",
        "",
        "Считаем онлайн тех, у кого была активность за последние 5 минут.",
        "",
        f"Прямо сейчас онлайн: <b>{total}</b>.",
    ]

    if sample and total <= 20:
        lines.append("")
        lines.append("Пользователи сейчас онлайн:")
        for u in sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:stats:messages")
async def admin_stats_messages(callback: CallbackQuery):
    """
    Статистика по количеству обработанных действий.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    total = await get_total_activity_events()

    text = (
        "<b>Статистика → Сообщения / действия</b>\n\n"
        "Считаем количество ключевых действий пользователей:\n"
        "загрузки фото, оценки, супер-оценки, комментарии и жалобы.\n\n"
        f"Всего таких событий обработано: <b>{total}</b>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:stats:new")
async def admin_stats_new(callback: CallbackQuery):
    """
    Новые пользователи за последние 3 дня.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    total, sample = await get_new_users_last_days(days=3, limit=20)

    lines: list[str] = [
        "<b>Статистика → Новые за 3 дня</b>",
        "",
        f"За последние 3 дня впервые запустили бота: <b>{total}</b> человек.",
    ]

    if sample and total <= 20:
        lines.append("")
        lines.append("Новые пользователи:")
        for u in sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:stats:premium")
async def admin_stats_premium(callback: CallbackQuery):
    """
    Премиум-пользователи: купившие и получившие от создателя.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    stats = await get_premium_stats(limit=20)
    total = stats["total"]
    total_paid = stats["total_paid"]
    total_gift = stats["total_gift"]
    paid_sample = stats["paid_sample"]
    gift_sample = stats["gift_sample"]

    lines: list[str] = [
        "<b>Статистика → Премиум-пользователи</b>",
        "",
        f"Всего премиум-пользователей: <b>{total}</b>.",
        f"• Купили премиум: <b>{total_paid}</b>",
        f"• Получили от создателя (бессрочно): <b>{total_gift}</b>",
    ]

    if paid_sample and total_paid <= 20:
        lines.append("")
        lines.append("Купили премиум:")
        for u in paid_sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    if gift_sample and total_gift <= 20:
        lines.append("")
        lines.append("Премиум от создателя:")
        for u in gift_sample:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            if username:
                lines.append(f"• @{username} ({name})")
            else:
                lines.append(f"• {name}")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


# ====== В бане и топ-победители ======

async def _render_banned_page(callback: CallbackQuery, page: int) -> None:
    PAGE_SIZE = 20
    if page < 1:
        page = 1

    total, users_page = await get_blocked_users_page(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    # Если страница вышла за пределы — нормализуем
    if page > total_pages:
        page = total_pages
        total, users_page = await get_blocked_users_page(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)

    lines: list[str] = [
        "<b>Статистика → В бане</b>",
        "",
        f"Всего заблокировано: <b>{total}</b> пользовател(ей).",
    ]

    if not users_page:
        lines.append("")
        lines.append("Сейчас в бане никого нет.")
    else:
        lines.append("")
        lines.append("Список заблокированных:")
        for u in users_page:
            username = u.get("username")
            name = u.get("name") or "Без имени"
            label = f"@{username}" if username else name

            blocked_until = u.get("blocked_until")
            if blocked_until:
                try:
                    until_dt = datetime.fromisoformat(blocked_until)
                    until_str = until_dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    until_str = blocked_until
            else:
                until_str = "бессрочно"

            reason = u.get("blocked_reason") or "без указания причины"
            lines.append(f"• {label} — до {until_str}")
            lines.append(f"  причина: {reason}")

    lines.append("")
    lines.append(f"Страница <b>{page}</b> из <b>{total_pages}</b>.")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if total_pages > 1:
        if page > 1:
            kb.button(
                text="⬅️ Назад",
                callback_data=f"admin:stats:banned:page:{page - 1}",
            )
        if page < total_pages:
            kb.button(
                text="➡️ Вперёд",
                callback_data=f"admin:stats:banned:page:{page + 1}",
            )
    kb.button(text="⬅️ К статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:stats:banned")
async def admin_stats_banned(callback: CallbackQuery):
    """
    Статистика: пользователи в бане (постранично).
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await _render_banned_page(callback, page=1)


@router.callback_query(F.data.startswith("admin:stats:banned:page:"))
async def admin_stats_banned_page(callback: CallbackQuery):
    """
    Переключение страниц списка забаненных.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    try:
        _, _, _, _, page_str = callback.data.split(":", 4)
        page = int(page_str)
    except Exception:
        page = 1

    await _render_banned_page(callback, page=page)


@router.callback_query(F.data == "admin:stats:top_winners")
async def admin_stats_top_winners(callback: CallbackQuery):
    """
    Пользователи, которые несколько раз попадали в топ-3 дня.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    winners = await get_users_with_multiple_daily_top3(min_wins=2, limit=50)

    lines: list[str] = [
        "<b>Статистика → Неоднократные победители</b>",
        "",
        "Здесь показываем пользователей, чьи работы попадали в топ-3 дня больше двух раз.",
    ]

    if not winners:
        lines.append("")
        lines.append("Пока нет пользователей с более чем двумя попаданиями в топ-3.")
    else:
        lines.append("")
        for w in winners:
            username = w.get("username")
            name = w.get("name") or "Без имени"
            wins = w.get("wins_count") or 0
            if username:
                lines.append(f"• @{username} ({name}) — {wins} раз(а) в топ-3")
            else:
                lines.append(f"• {name} — {wins} раз(а) в топ-3")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад к статистике", callback_data="admin:stats")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_menu(callback: CallbackQuery, state: FSMContext):
    """
    Раздел «Рассылка».
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    # чистим состояние рассылки
    await state.clear()

    text = (
        "<b>Рассылка</b>\n\n"
        "Кому отправляем сообщение?\n\n"
        "• 📢 Всем пользователям\n"
        "• 💎 Только премиум-пользователям\n"
        "• 👥 Составу (модераторы, поддержка, помощники)\n"
        "• 🧪 Тестовая рассылка (только тебе)\n\n"
        "Дальше я попрошу ввести текст и покажу превью перед отправкой."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Всем пользователям", callback_data="admin:broadcast:all")
    kb.button(text="💎 Премиум-пользователям", callback_data="admin:broadcast:premium")
    kb.button(text="👥 Составу", callback_data="admin:broadcast:staff")
    kb.button(text="🧪 Тестовая (мне)", callback_data="admin:broadcast:test")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:staff")
async def admin_broadcast_staff_menu(callback: CallbackQuery, state: FSMContext):
    """
    Подраздел рассылки: кому из состава слать.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Рассылка составу</b>\n\n"
        "Выбери аудиторию:\n"
        "• Модераторы\n"
        "• Поддержка\n"
        "• Помощники\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Модераторам", callback_data="admin:broadcast:staff:moderators")
    kb.button(text="👨‍💻 Поддержке", callback_data="admin:broadcast:staff:support")
    kb.button(text="🤝 Помощникам", callback_data="admin:broadcast:staff:helpers")
    kb.button(text="⬅️ Назад в рассылку", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(
    F.data.in_(
        (
            "admin:broadcast:all",
            "admin:broadcast:premium",
            "admin:broadcast:test",
            "admin:broadcast:staff:moderators",
            "admin:broadcast:staff:support",
            "admin:broadcast:staff:helpers",
        )
    )
)
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """
    Аудитория выбрана — просим текст рассылки.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    data_key = callback.data
    if data_key == "admin:broadcast:all":
        target = "all"
        audience = "всем пользователям"
    elif data_key == "admin:broadcast:premium":
        target = "premium"
        audience = "премиум-пользователям"
    elif data_key == "admin:broadcast:test":
        target = "test"
        audience = "только тебе (тестовая рассылка)"
    elif data_key == "admin:broadcast:staff:moderators":
        target = "moderators"
        audience = "модераторам"
    elif data_key == "admin:broadcast:staff:support":
        target = "support"
        audience = "поддержке"
    else:
        target = "helpers"
        audience = "помощникам"

    text = (
        f"<b>Рассылка {audience}</b>\n\n"
        "Отправь ОДНИМ сообщением текст, который нужно разослать.\n\n"
        "Твой исходный текст я удалю из чата, но использую для пуша.\n"
        "Перед отправкой покажу превью с кнопками «Отправить» и «Отмена».\n\n"
        "Если передумал — нажми «Назад в рассылку»."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад в рассылку", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        prompt = await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        prompt = await callback.message.answer(text, reply_markup=kb.as_markup())

    await state.set_state(BroadcastStates.waiting_text)
    await state.update_data(
        broadcast_target=target,
        broadcast_prompt_chat_id=prompt.chat.id,
        broadcast_prompt_msg_id=prompt.message_id,
    )

    await callback.answer()


@router.message(BroadcastStates.waiting_text, F.text)
async def admin_broadcast_preview(message: Message, state: FSMContext):
    """
    Админ ввёл текст рассылки — показываем превью + кнопки «Отправить» / «Отмена».
    Сообщение админа удаляем.
    """
    data = await state.get_data()
    target = data.get("broadcast_target")

    if not target:
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Сессия рассылки потерялась. Открой раздел «Рассылка» заново.")
        return

    raw_text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(broadcast_text=raw_text)

    if target == "all":
        header = "📢 <b>Сообщение для всех пользователей</b>"
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    preview_text = (
        f"{header}\n\n"
        f"{raw_text}\n\n"
        "Отправить это сообщение выбранной аудитории?"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="admin:broadcast:send")
    kb.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
    kb.adjust(1, 1)

    data = await state.get_data()
    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=preview_text,
                reply_markup=kb.as_markup(),
            )
            return
        except Exception:
            pass

    await message.answer(preview_text, reply_markup=kb.as_markup())


@router.message(BroadcastStates.waiting_text)
async def admin_broadcast_waiting_text_other(message: Message):
    """
    В режиме ввода текста рассылки — удаляем любой не-текст.
    """
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(BroadcastStates.waiting_text, F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Отмена рассылки после ввода текста — возвращаемся в меню «Рассылка».
    """
    await state.clear()
    await admin_broadcast_menu(callback, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_text, F.data == "admin:broadcast:send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение отправки рассылки.
    Реально шлём сообщение выбранной аудитории с кнопкой «Просмотрено».
    """
    data = await state.get_data()
    target = data.get("broadcast_target")
    text_body = data.get("broadcast_text")

    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if not target or not text_body:
        await state.clear()
        try:
            await callback.message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="Сессия рассылки потерялась. Открой раздел «Рассылка» заново.",
            )
        except Exception:
            await callback.message.answer("Сессия рассылки потерялась. Открой раздел «Рассылка» заново.")
        await callback.answer()
        return

    # Собираем аудиторию
    tg_ids: list[int] = []

    if target == "all":
        tg_ids = await get_all_users_tg_ids()
    elif target == "premium":
        users = await get_premium_users()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "test":
        tg_ids = [callback.from_user.id]
    elif target == "moderators":
        users = await get_moderators()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "support":
        users = await get_support_users()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "helpers":
        users = await get_helpers()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]

    # Убираем дубликаты и нули
    tg_ids = list({uid for uid in tg_ids if uid})

    if target == "all":
        header = "📢 <b>Обновление GlowShot</b>"
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    send_text = f"{header}\n\n{text_body}"

    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
    notif_kb.adjust(1)
    notif_markup = notif_kb.as_markup()

    total = len(tg_ids)
    sent = 0

    for uid in tg_ids:
        try:
            await callback.message.bot.send_message(
                chat_id=uid,
                text=send_text,
                reply_markup=notif_markup,
            )
            sent += 1
        except Exception:
            # заблокировал бота / ограничил сообщения и т.п. — просто пропускаем
            continue

    await state.clear()

    summary = (
        "✅ Рассылка завершена.\n\n"
        f"Всего получателей в выборке: <b>{total}</b>.\n"
        f"Успешно отправлено (по данным Telegram) как минимум <b>{sent}</b> пользователям.\n\n"
        "Можешь вернуться в раздел «Рассылка» или в главное админ-меню."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 В раздел «Рассылка»", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        if chat_id and msg_id:
            await callback.message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=summary,
                reply_markup=kb.as_markup(),
            )
        else:
            await callback.message.answer(summary, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(summary, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users_menu(callback: CallbackQuery):
    """
    Раздел «Пользователи».
    Пока описываем структуру и возможности.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Пользователи</b>\n\n"
        "Планируемые возможности:\n"
        "• 🔍 Найти пользователя (по ID / username)\n"
        "• 🚫 Блок / разбан\n"
        "• ⭐️ Выдать роль (админ, модератор и т.п.)\n"
        "• 📄 Инфо о пользователе (когда зарегистрирован, сколько раз заходил, что делал)\n"
        "• 🧾 Список заблокированных\n\n"
        "Сейчас это описание раздела. Функционал будет добавлен позже."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти пользователя", callback_data="admin:stub:users_search")
    kb.button(text="🚫 Блок / разбан", callback_data="admin:stub:users_block")
    kb.button(text="⭐️ Выдать роль", callback_data="admin:stub:users_role")
    kb.button(text="📄 Инфо о пользователе", callback_data="admin:stub:users_info")
    kb.button(text="🧾 Список заблокированных", callback_data="admin:stub:users_banned")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data == "admin:payments")
async def admin_payments_menu(callback: CallbackQuery):
    """
    Раздел «Платежи».
    Здесь позже будет аналитика по доходам и подпискам.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Платежи</b>\n\n"
        "Планируемые возможности:\n"
        "• 💵 Список платежей\n"
        "• Доход за день/неделю/месяц\n"
        "• 📦 Тарифы / продукты\n"
        "• Добавить / изменить / скрыть тариф\n"
        "• 👤 Подписки пользователей\n"
        "• Кто на что подписан\n"
        "• Заканчивающиеся подписки\n\n"
        "Сейчас это заглушка — логика биллинга и подписок будет добавлена позже."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="💵 Список платежей", callback_data="admin:stub:pay_list")
    kb.button(text="📈 Доходы (день/неделя/месяц)", callback_data="admin:stub:pay_income")
    kb.button(text="📦 Тарифы / продукты", callback_data="admin:stub:pay_plans")
    kb.button(text="✏️ Управление тарифами", callback_data="admin:stub:pay_plans_edit")
    kb.button(text="👤 Подписки пользователей", callback_data="admin:stub:pay_subs")
    kb.button(text="⏰ Заканчивающиеся подписки", callback_data="admin:stub:pay_expiring")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data == "admin:logs")
async def admin_logs_menu(callback: CallbackQuery):
    """
    Раздел «Логи / ошибки».
    Здесь будут логи и последние ошибки.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Логи / ошибки</b>\n\n"
        "Планируемые возможности:\n"
        "• ⚠️ Последние ошибки\n"
        "• 📜 Логи действий админов\n"
        "• 📆 Логи за сегодня / неделю\n"
        "• 📤 Скинуть лог файлом\n\n"
        "Сейчас это описание раздела. Подключение логов будет добавлено позже."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Последние ошибки", callback_data="admin:stub:logs_errors")
    kb.button(text="📜 Логи действий админов", callback_data="admin:stub:logs_admins")
    kb.button(text="📆 Логи за период", callback_data="admin:stub:logs_range")
    kb.button(text="📤 Скинуть лог файлом", callback_data="admin:stub:logs_export")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("admin:stub:"))
async def admin_stub_placeholder(callback: CallbackQuery):
    """
    Заглушка для ещё не реализованных разделов админки.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await callback.answer(
        "Этот раздел ещё в разработке. Скоро тут будут новые инструменты админа 🛠",
        show_alert=True,
    )

# ================= КОНЕЦ АДМИНКИ =================


# ================= ДРУГИЕ КОМАНДЫ ================

@router.message(Command("ping"))
async def ping(message: Message):
    await message.answer("pong")


@router.message(Command("adminstatus"))
async def admin_status(message: Message):
    user = await _ensure_user(message)
    if user is None:
        return

    is_admin = user.get("is_admin", False)
    text = "Ты админ." if is_admin else "Ты не админ."
    await message.answer(text)


@router.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Твой ID: <code>{message.from_user.id}</code>")


@router.message(Command("users"))
async def total_users(message: Message):
    total = await get_total_users()
    await message.answer(f"Всего пользователей в боте: <b>{total}</b>")

# ================= КОНЕЦ ДРУГИХ КОМАНД ==============

@router.callback_query(F.data == "admin:achievements")
async def admin_achievements_menu(callback: CallbackQuery, state: FSMContext):
    """
    Раздел «Награды / ачивки».

    Здесь админы могут:
    • выдавать особую ачивку «Бета-тестер бота»;
    • выдавать кастомные ачивки с любым названием/текстом/смайликом;
    • управлять ачивками конкретного пользователя (редактировать/удалять).
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Награды / ачивки</b>\n\n"
        "Доступные действия:\n"
        "• 🏆 <b>Бета-тестер бота</b> — особая статусная ачивка для ранних тестеров\n"
        "• 🎨 Кастомная ачивка — любое название, описание и смайлик\n"
        "• 🧾 Ачивки пользователя — посмотреть, отредактировать или удалить\n\n"
        "Выбери действие:"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🏆 Выдать «Бета-тестер бота»",
        callback_data="admin:ach:beta:start",
    )
    kb.button(
        text="🎨 Выдать кастомную ачивку",
        callback_data="admin:ach:custom:start",
    )
    kb.button(
        text="🧾 Ачивки пользователя",
        callback_data="admin:ach:user:start",
    )
    kb.button(
        text="⬅️ В админ-меню",
        callback_data="admin:menu",
    )
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


# ====== Хендлеры для выдачи кастомной ачивки ======

@router.callback_query(F.data == "admin:ach:custom:start")
async def admin_achievements_custom_start(callback: CallbackQuery, state: FSMContext):
    """
    Старт выдачи кастомной ачивки с любым названием/текстом/смайликом.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.set_state(AchievementStates.waiting_custom_user)
    await state.update_data(
        ach_prompt_chat_id=callback.message.chat.id,
        ach_prompt_msg_id=callback.message.message_id,
    )

    text = (
        "🎨 <b>Новая кастомная ачивка</b>\n\n"
        "Шаг 1/4 — выбери пользователя.\n\n"
        "Отправь ID или @username пользователя, которому нужно выдать ачивку.\n\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)

    await callback.answer()


@router.message(AchievementStates.waiting_custom_user, F.text)
async def admin_achievements_custom_user(message: Message, state: FSMContext):
    """
    Шаг 1/4: выбор пользователя для кастомной ачивки.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    identifier = (message.text or "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    target_user = await _find_user_by_identifier(identifier)
    if not target_user:
        text = (
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username."
        )
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
                return
            except Exception:
                pass

        await message.answer(text)
        await state.clear()
        return

    target_tg_id = target_user.get("tg_id")
    target_name = target_user.get("name") or "Без имени"
    target_username = target_user.get("username")
    target_internal_id = target_user.get("id")

    await state.update_data(
        custom_target_tg_id=target_tg_id,
        custom_target_user_id=target_internal_id,
        custom_target_name=target_name,
        custom_target_username=target_username,
    )

    extra = f" (@{target_username})" if target_username else ""
    text = (
        "🎨 <b>Новая кастомная ачивка</b>\n\n"
        "Шаг 2/4 — название.\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n\n"
        "Отправь <b>название</b> ачивки (коротко и понятно)."
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
        except Exception:
            await message.answer(text)
    else:
        await message.answer(text)

    await state.set_state(AchievementStates.waiting_custom_title)


@router.message(AchievementStates.waiting_custom_title, F.text)
async def admin_achievements_custom_title(message: Message, state: FSMContext):
    """
    Шаг 2/4: название кастомной ачивки.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    title = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not title:
        text = "Название не может быть пустым. Отправь, пожалуйста, название ачивки ещё раз."
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
                return
            except Exception:
                pass
        await message.answer(text)
        return

    await state.update_data(custom_title=title)

    target_name = data.get("custom_target_name") or "Без имени"
    target_tg_id = data.get("custom_target_tg_id")
    target_username = data.get("custom_target_username")
    extra = f" (@{target_username})" if target_username else ""

    text = (
        "🎨 <b>Новая кастомная ачивка</b>\n\n"
        "Шаг 3/4 — описание.\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n"
        f"Название: <b>{title}</b>\n\n"
        "Отправь <b>описание</b> ачивки.\n"
        "Если хочешь оставить без описания — напиши «-»."
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
        except Exception:
            await message.answer(text)
    else:
        await message.answer(text)

    await state.set_state(AchievementStates.waiting_custom_description)


@router.message(AchievementStates.waiting_custom_description, F.text)
async def admin_achievements_custom_description(message: Message, state: FSMContext):
    """
    Шаг 3/4: описание кастомной ачивки.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    raw_desc = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    description = None if raw_desc in ("-", "—", "нет", "ничего") else raw_desc

    await state.update_data(custom_description=description)

    target_name = data.get("custom_target_name") or "Без имени"
    target_tg_id = data.get("custom_target_tg_id")
    target_username = data.get("custom_target_username")
    title = data.get("custom_title") or "Без названия"
    extra = f" (@{target_username})" if target_username else ""

    text = (
        "🎨 <b>Новая кастомная ачивка</b>\n\n"
        "Шаг 4/4 — смайлик и уровень.\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: {description or '—'}\n\n"
        "Сначала отправь <b>смайлик</b> для этой ачивки.\n"
        "Можно отправить один эмодзи. Если оставить пустым или отправить «стандарт» — будет использован 🏆."
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
        except Exception:
            await message.answer(text)
    else:
        await message.answer(text)

    await state.set_state(AchievementStates.waiting_custom_icon)


@router.message(AchievementStates.waiting_custom_icon, F.text)
async def admin_achievements_custom_icon(message: Message, state: FSMContext):
    """
    Шаг 4/4 (часть 1): выбор смайлика для кастомной ачивки.
    После этого спросим уровень.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    raw_icon = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not raw_icon or raw_icon.lower() in ("стандарт", "standard", "default"):
        icon = "🏆"
    else:
        # Берём первый символ, чтобы не раздувать подписи
        icon = raw_icon[0]

    await state.update_data(custom_icon=icon)

    target_name = data.get("custom_target_name") or "Без имени"
    target_tg_id = data.get("custom_target_tg_id")
    target_username = data.get("custom_target_username")
    title = data.get("custom_title") or "Без названия"
    description = data.get("custom_description") or "—"
    extra = f" (@{target_username})" if target_username else ""

    text = (
        "🎨 <b>Новая кастомная ачивка</b>\n\n"
        "Финальный шаг — уровень ачивки.\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: {description}\n"
        f"Смайлик: {icon}\n\n"
        "Отправь число уровня (например: <code>1</code>, <code>2</code>, <code>8</code>, <code>10</code>).\n"
        "Если отправишь что-то непонятное — уровень будет выбран автоматически:\n"
        "1 — обычный пользователь\n"
        "2 — премиум\n"
        "8 — модератор\n"
        "10 — админ."
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
        except Exception:
            await message.answer(text)
    else:
        await message.answer(text)

    await state.set_state(AchievementStates.waiting_custom_level)


@router.message(AchievementStates.waiting_custom_level, F.text)
async def admin_achievements_custom_level(message: Message, state: FSMContext):
    """
    Финальный шаг: уровень ачивки + сохранение в БД.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    raw_level = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    # Определяем базовый уровень по роли выдавшего
    issuer = await get_user_by_tg_id(message.from_user.id)
    base_level = 1
    if issuer:
        if issuer.get("is_admin"):
            base_level = 10
        elif issuer.get("is_moderator"):
            base_level = 8
        elif issuer.get("is_premium"):
            base_level = 2

    try:
        level = int(raw_level)
        if level <= 0:
            level = base_level
    except ValueError:
        level = base_level

    # Собираем данные ачивки
    target_tg_id = data.get("custom_target_tg_id")
    target_user_id = data.get("custom_target_user_id")
    target_name = data.get("custom_target_name") or "Без имени"
    target_username = data.get("custom_target_username")
    title = data.get("custom_title") or "Без названия"
    description_raw = data.get("custom_description")
    icon = data.get("custom_icon") or "🏆"
    extra = f" (@{target_username})" if target_username else ""

    # В описание добавляем уровень (пока без отдельного поля в БД).
    if description_raw:
        description = f"[Уровень {level}]\n\n{description_raw}"
    else:
        description = f"[Уровень {level}]"

    # Уникальный code для кастомной ачивки
    ts = int(datetime.utcnow().timestamp())
    code = f"custom_l{level}_{target_user_id}_{ts}"

    granted_by_user_id = issuer.get("id") if issuer else None

    # Создаём запись ачивки через общий слой базы данных
    await create_custom_award_for_user(
        user_id=target_user_id,
        title=title,
        description=description,
        icon=icon,
        code=code,
        is_special=False,
        granted_by_user_id=granted_by_user_id,
    )

    # Подтверждение админу
    result_text = (
        "🎉 <b>Кастомная ачивка выдана!</b>\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n"
        f"Смайлик: {icon}\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: {description}\n"
        f"Уровень: <b>{level}</b>"
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=result_text,
            )
        except Exception:
            await message.answer(result_text)
    else:
        await message.answer(result_text)

    await state.clear()

    # Уведомление пользователю о новой ачивке
    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="user:notify_seen")
    notif_kb.adjust(1)

    notif_text = (
        f"{icon} <b>Новая награда!</b>\n\n"
        f"Ты получил(а) ачивку: <b>{title}</b>.\n\n"
        f"{description}\n\n"
        "Спасибо, что остаёшься с нами 💙"
    )

    try:
        await message.bot.send_message(
            chat_id=target_tg_id,
            text=notif_text,
            reply_markup=notif_kb.as_markup(),
        )
    except Exception:
        pass


# ====== Хендлеры для управления ачивками пользователя ======

@router.callback_query(F.data == "admin:ach:user:start")
async def admin_achievements_user_start(callback: CallbackQuery, state: FSMContext):
    """
    Старт управления ачивками конкретного пользователя:
    просмотр списка, редактирование, удаление.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.set_state(AchievementStates.waiting_manage_user)
    await state.update_data(
        ach_prompt_chat_id=callback.message.chat.id,
        ach_prompt_msg_id=callback.message.message_id,
    )

    text = (
        "🧾 <b>Ачивки пользователя</b>\n\n"
        "Отправь ID или @username пользователя, чьи ачивки хочешь посмотреть/изменить.\n\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)

    await callback.answer()


@router.message(AchievementStates.waiting_manage_user, F.text)
async def admin_achievements_user_list(message: Message, state: FSMContext):
    """
    Поиск пользователя и показ списка его ачивок.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    identifier = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    target_user = await _find_user_by_identifier(identifier)
    if not target_user:
        text = (
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username."
        )
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
                return
            except Exception:
                pass

        await message.answer(text)
        await state.clear()
        return

    target_user_id = target_user.get("id")
    target_tg_id = target_user.get("tg_id")
    target_name = target_user.get("name") or "Без имени"
    target_username = target_user.get("username")
    extra = f" (@{target_username})" if target_username else ""

    awards = await get_awards_for_user(target_user_id)
    if not awards:
        text = (
            "🧾 <b>Ачивки пользователя</b>\n\n"
            f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}\n\n"
            "У этого пользователя пока нет ачивок."
        )
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
            except Exception:
                await message.answer(text)
        else:
            await message.answer(text)

        await state.clear()
        return

    await state.update_data(
        manage_target_user_id=target_user_id,
        manage_target_tg_id=target_tg_id,
        manage_target_name=target_name,
        manage_target_username=target_username,
    )

    lines = [
        "🧾 <b>Ачивки пользователя</b>",
        "",
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}",
        "",
        "Выбери ачивку, чтобы отредактировать или удалить:",
        "",
    ]
    kb = InlineKeyboardBuilder()

    for award in awards:
        award_id = award["id"]
        icon = award.get("icon") or "🏅"
        title = award.get("title") or "Без названия"
        short_title = title if len(title) <= 24 else title[:21] + "..."
        lines.append(f"{icon} {title}")
        kb.button(
            text=f"{icon} {short_title}",
            callback_data=f"admin:ach:award:{award_id}",
        )

    kb.button(
        text="⬅️ В раздел ачивок",
        callback_data="admin:achievements",
    )
    kb.adjust(1)

    text = "\n".join(lines)

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb.as_markup(),
            )
        except Exception:
            await message.answer(text, reply_markup=kb.as_markup())
    else:
        await message.answer(text, reply_markup=kb.as_markup())

    # Остаёмся в этом же состоянии или выходим? Для простоты выходим из FSM.
    await state.clear()


@router.callback_query(F.data.startswith("admin:ach:award:"))
async def admin_achievements_award_menu(callback: CallbackQuery, state: FSMContext):
    """
    Меню конкретной ачивки: редактирование и удаление.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    try:
        award_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный ID ачивки.", show_alert=True)
        return

    award = await get_award_by_id(award_id)
    if not award:
        await callback.answer("Ачивка не найдена (возможно, уже удалена).", show_alert=True)
        return

    title = award.get("title") or "Без названия"
    description = award.get("description") or "—"
    icon = award.get("icon") or "🏅"
    code = award.get("code") or "—"
    created_at = award.get("created_at") or "—"

    text = (
        "⚙️ <b>Управление ачивкой</b>\n\n"
        f"{icon} <b>{title}</b>\n\n"
        f"{description}\n\n"
        f"<code>code:</code> <code>{code}</code>\n"
        f"<code>created_at:</code> <code>{created_at}</code>\n\n"
        "Что сделать с этой ачивкой?"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Изменить текст",
        callback_data=f"admin:ach:award_edit_text:{award_id}",
    )
    kb.button(
        text="🎨 Сменить смайлик",
        callback_data=f"admin:ach:award_edit_icon:{award_id}",
    )
    kb.button(
        text="🗑 Удалить ачивку",
        callback_data=f"admin:ach:award_delete:{award_id}",
    )
    kb.button(
        text="⬅️ В раздел ачивок",
        callback_data="admin:achievements",
    )
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=kb.as_markup(),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("admin:ach:award_delete:"))
async def admin_achievements_award_delete(callback: CallbackQuery):
    """
    Удаление ачивки по ID.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    try:
        award_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный ID ачивки.", show_alert=True)
        return

    await delete_award_by_id(award_id)

    text = (
        "🗑 <b>Ачивка удалена.</b>\n\n"
        "Ты можешь вернуться в раздел ачивок или выбрать другие действия."
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⬅️ В раздел ачивок",
        callback_data="admin:achievements",
    )
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin:ach:award_edit_text:"))
async def admin_achievements_award_edit_text_start(callback: CallbackQuery, state: FSMContext):
    """
    Старт редактирования текста ачивки (название + описание).
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    try:
        award_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный ID ачивки.", show_alert=True)
        return

    await state.set_state(AchievementStates.waiting_edit_award_text)
    await state.update_data(
        ach_prompt_chat_id=callback.message.chat.id,
        ach_prompt_msg_id=callback.message.message_id,
        edit_award_id=award_id,
    )

    text = (
        "✏️ <b>Изменение текста ачивки</b>\n\n"
        "Отправь новое название и описание ачивки.\n"
        "Формат: первая строка — название, остальные строки — описание."
    )

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)

    await callback.answer()


@router.message(AchievementStates.waiting_edit_award_text, F.text)
async def admin_achievements_award_edit_text_save(message: Message, state: FSMContext):
    """
    Сохранение нового названия и описания ачивки.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")
    award_id = data.get("edit_award_id")

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not award_id:
        await state.clear()
        await message.answer("Сессия редактирования потерялась. Открой управление ачивками заново.")
        return

    if not raw:
        text = "Текст не может быть пустым. Попробуй ещё раз."
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
                return
            except Exception:
                pass
        await message.answer(text)
        return

    lines = raw.splitlines()
    title = lines[0].strip()
    description = "\n".join(lines[1:]).strip() if len(lines) > 1 else None

    await update_award_text(award_id, title, description)

    award = await get_award_by_id(award_id)
    if not award:
        # На всякий случай, если ачивку удалили параллельно
        result_text = "Ачивка была изменена или удалена."
    else:
        icon = award.get("icon") or "🏅"
        description = award.get("description") or "—"

        result_text = (
            "✅ <b>Текст ачивки обновлён.</b>\n\n"
            f"{icon} <b>{title}</b>\n\n"
            f"{description}"
        )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=result_text,
            )
        except Exception:
            await message.answer(result_text)
    else:
        await message.answer(result_text)

    await state.clear()


@router.callback_query(F.data.startswith("admin:ach:award_edit_icon:"))
async def admin_achievements_award_edit_icon_start(callback: CallbackQuery, state: FSMContext):
    """
    Старт изменения смайлика ачивки.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    try:
        award_id = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный ID ачивки.", show_alert=True)
        return

    await state.set_state(AchievementStates.waiting_edit_award_icon)
    await state.update_data(
        ach_prompt_chat_id=callback.message.chat.id,
        ach_prompt_msg_id=callback.message.message_id,
        edit_award_id=award_id,
    )

    text = (
        "🎨 <b>Изменение смайлика ачивки</b>\n\n"
        "Отправь новый смайлик для этой ачивки.\n"
        "Если отправишь несколько символов — будет использован первый."
    )

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)

    await callback.answer()


@router.message(AchievementStates.waiting_edit_award_icon, F.text)
async def admin_achievements_award_edit_icon_save(message: Message, state: FSMContext):
    """
    Сохранение нового смайлика ачивки.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")
    award_id = data.get("edit_award_id")

    raw_icon = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not award_id:
        await state.clear()
        await message.answer("Сессия редактирования потерялась. Открой управление ачивками заново.")
        return

    if not raw_icon:
        icon = "🏅"
    else:
        # Берём только первый символ (один emoji / символ)
        icon = raw_icon[0]

    await update_award_icon(award_id, icon)

    award = await get_award_by_id(award_id)
    if not award:
        result_text = "Ачивка была изменена или удалена."
    else:
        title = award.get("title") or "Без названия"
        description = award.get("description") or "—"

        result_text = (
            "✅ <b>Смайлик ачивки обновлён.</b>\n\n"
            f"{icon} <b>{title}</b>\n\n"
            f"{description}"
        )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=result_text,
            )
        except Exception:
            await message.answer(result_text)
    else:
        await message.answer(result_text)

    await state.clear()


@router.callback_query(F.data == "admin:ach:beta:start")
async def admin_achievements_beta_start(callback: CallbackQuery, state: FSMContext):
    """
    Старт выдачи ачивки «Бета-тестер бота».

    TODO: ачивки могут и премиум-аккаунты выдавать, не только админ.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.set_state(AchievementStates.waiting_user_for_beta)
    await state.update_data(
        ach_prompt_chat_id=callback.message.chat.id,
        ach_prompt_msg_id=callback.message.message_id,
    )

    text = (
        "🏆 Выдача награды «Бета-тестер бота».\n\n"
        "Отправь ID или @username пользователя, которому нужно выдать эту ачивку.\n\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    try:
        await callback.message.edit_text(text)
    except Exception:
        await callback.message.answer(text)

    await callback.answer()


@router.message(AchievementStates.waiting_user_for_beta, F.text)
async def admin_achievements_beta_grant(message: Message, state: FSMContext):
    """
    Выдача ачивки «Бета-тестер бота» по введённому ID или @username.
    """
    data = await state.get_data()
    chat_id = data.get("ach_prompt_chat_id")
    msg_id = data.get("ach_prompt_msg_id")

    identifier = (message.text or "").strip()
    # Стараемся не плодить новых сообщений — удаляем ввод админа
    try:
        await message.delete()
    except Exception:
        pass

    target_user = await _find_user_by_identifier(identifier)
    if not target_user:
        text = (
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username."
        )
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                )
                return
            except Exception:
                pass

        await message.answer(text)
        await state.clear()
        return

    target_tg_id = target_user.get("tg_id")
    target_name = target_user.get("name") or "Без имени"
    target_username = target_user.get("username")

    # Пытаемся выдать ачивку через локальную функцию
    granted = await give_achievement_to_user_by_code(
        user_tg_id=target_tg_id,
        code="beta_tester",
        granted_by_tg_id=message.from_user.id,
    )

    if granted:
        status_line = "Ачивка выдана ✅"
    else:
        status_line = "У этого пользователя уже есть эта ачивка."

    extra = f" (@{target_username})" if target_username else ""
    result_text = (
        f"🏆 Награда «Бета-тестер бота»\n\n"
        f"{status_line}\n\n"
        f"Пользователь: {target_name} — ID <code>{target_tg_id}</code>{extra}"
    )

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=result_text,
            )
        except Exception:
            await message.answer(result_text)
    else:
        await message.answer(result_text)

    await state.clear()

    # Уведомление самому пользователю о новой ачивке
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="user:notify_seen")
    notif_kb.adjust(1)

    notif_text = (
        "🏆 <b>Новая награда!</b>\n\n"
        "Ты получил(а) ачивку: <b>Бета-тестер бота</b>.\n\n"
        "Описание: ты помог(ла) тестировать GlowShot на ранних стадиях до релиза.\n\n"
        "Спасибо, что был(а) с нами с самого начала 💙"
    )

    try:
        await message.bot.send_message(
            chat_id=target_tg_id,
            text=notif_text,
            reply_markup=notif_kb.as_markup(),
        )
    except Exception:
        # Не кричим, если не получилось отправить (например, пользователь закрыл ЛС)
        pass


LOGS_PAGE_SIZE = 10


def _short(s: str | None, n: int = 120) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


@router.callback_query(F.data.startswith("admin:logs:page:"))
async def admin_logs_page(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    page = 1
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except Exception:
            page = 1

    if page < 1:
        page = 1

    total, rows = await get_bot_error_logs_page(limit=LOGS_PAGE_SIZE, offset=(page - 1) * LOGS_PAGE_SIZE)
    max_page = max(1, (total + LOGS_PAGE_SIZE - 1) // LOGS_PAGE_SIZE)
    if page > max_page:
        page = max_page
        total, rows = await get_bot_error_logs_page(limit=LOGS_PAGE_SIZE, offset=(page - 1) * LOGS_PAGE_SIZE)

    lines = [
        "<b>🧾 Логи / ошибки</b>",
        f"Всего: <b>{total}</b>",
        f"Страница: <b>{page}/{max_page}</b>",
        "",
    ]

    if not rows:
        lines.append("Пока пусто. И это охуенно 😌")
    else:
        for r in rows:
            created = r.get("created_at")
            try:
                created_h = created.strftime("%d.%m.%Y %H:%M:%S") if created else "—"
            except Exception:
                created_h = str(created) if created else "—"

            lines.append(
                f"#{r['id']} — <b>{created_h}</b>\n"
                f"• handler: <code>{_short(r.get('handler'), 40) or '—'}</code>\n"
                f"• type: <code>{_short(r.get('error_type'), 40) or '—'}</code>\n"
                f"• text: {_short(r.get('error_text'), 160) or '—'}\n"
            )

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 1:
        kb.button(text="⬅️", callback_data=f"admin:logs:page:{page-1}")
    kb.button(text="🧹 Очистить", callback_data="admin:logs:clear")
    if page < max_page:
        kb.button(text="➡️", callback_data=f"admin:logs:page:{page+1}")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(3, 1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:logs:clear")
async def admin_logs_clear(callback: CallbackQuery):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    await clear_bot_error_logs()
    await callback.answer("Логи очищены ✅", show_alert=True)

    # обновим экран
    try:
        callback.data = "admin:logs:page:1"
    except Exception:
        pass
    await admin_logs_page(callback)
# ====== USERS: AWARDS / ACHIEVEMENTS ======


@router.callback_query(F.data == "admin:users:awards")
async def admin_users_awards(callback: CallbackQuery, state: FSMContext):
    """Показать список наград пользователя и быстрые действия."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    if not internal_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    awards = await get_awards_for_user(int(internal_id))

    lines: list[str] = [
        "<b>Награды / ачивки пользователя</b>",
        "",
    ]

    if not awards:
        lines.append("Пока нет ни одной награды.")
    else:
        for a in awards:
            icon = (a.get("icon") or "🏅").strip() or "🏅"
            title = (a.get("title") or a.get("code") or "—").strip()
            code = (a.get("code") or "—").strip()
            created_at = a.get("created_at")
            try:
                dt = datetime.fromisoformat(created_at) if created_at else None
                created_human = dt.strftime("%d.%m.%Y %H:%M") if dt else (created_at or "—")
            except Exception:
                created_human = created_at or "—"
            lines.append(f"• {icon} <b>{title}</b>  (<code>{code}</code>) — {created_human}")

    lines.append("")
    lines.append("Быстрое действие: можно выдать «Бета‑тестер» одной кнопкой ниже.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")
    kb.button(text="⬅️ Назад к профилю", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:users:award:beta")
async def admin_users_award_beta(callback: CallbackQuery, state: FSMContext):
    """Выдать пользователю ачивку beta_tester (без дублей)."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_tg_id = data.get("selected_user_tg_id")
    internal_id = data.get("selected_user_id")

    if not target_tg_id or not internal_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    ok = False
    try:
        ok = await give_achievement_to_user_by_code(int(target_tg_id), "beta_tester", granted_by_tg_id=callback.from_user.id)
    except Exception:
        ok = False

    # Пере-рендерим профиль, чтобы сразу увидеть награду
    user = await get_user_by_id(int(internal_id))
    if not user:
        await callback.answer("Не удалось обновить профиль пользователя.", show_alert=True)
        return

    block_status = await get_user_block_status_by_tg_id(int(target_tg_id))
    rating_summary = await get_user_rating_summary(int(internal_id))
    admin_stats = await get_user_admin_stats(int(internal_id))
    awards = await get_awards_for_user(int(internal_id))

    text = await _render_admin_user_profile(
        user=user,
        block_status=block_status,
        rating_summary=rating_summary,
        admin_stats=admin_stats,
        awards=awards,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")
    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")

    is_blocked = bool(block_status.get("is_blocked"))
    if is_blocked:
        kb.button(text="♻️ Разбан", callback_data="admin:users:unban")
    else:
        kb.button(text="🚫 Бан", callback_data="admin:users:ban")

    kb.button(text="⛔ Ограничить доступ", callback_data="admin:users:limit")
    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 2)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text=text,
        reply_markup=kb.as_markup(),
    )

    await callback.answer("✅ Выдано" if ok else "ℹ️ Уже было / не удалось")
# Хендлер для кнопки "Просмотрено" — удаляет пуш-сообщение о награде
@router.callback_query(F.data == "award:seen")
async def award_seen(callback: CallbackQuery):
    """Пользователь подтверждает, что увидел пуш о награде — удаляем сообщение."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    try:
        await callback.answer("Спасибо! 🎉")
    except Exception:
        pass