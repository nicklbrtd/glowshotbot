

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ПОЛЬЗОВАТЕЛИ ==================================
# =============================================================

from datetime import datetime

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_user_by_username,
    get_user_block_status_by_tg_id,
    get_user_rating_summary,
    get_user_admin_stats,
    get_awards_for_user,
    get_today_photo_for_user,
    get_photo_admin_stats,
)

from .common import _ensure_admin


router = Router()


# =============================================================
# ==== ХЕЛПЕРЫ (пользователи) ==================================
# =============================================================


async def _edit_user_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Держим одно служебное сообщение для диалогов в разделе «Пользователи»."""
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
            # если редактирование не удалось — попробуем удалить и отправить заново
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            try:
                sent = await message.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                )
                await state.update_data(
                    user_prompt_chat_id=sent.chat.id,
                    user_prompt_msg_id=sent.message_id,
                )
                return
            except Exception:
                pass

    # fallback: ответить и запомнить
    try:
        sent = await message.answer(text, reply_markup=reply_markup)
        await state.update_data(
            user_prompt_chat_id=sent.chat.id,
            user_prompt_msg_id=sent.message_id,
        )
    except Exception:
        pass


async def _find_user_by_identifier(identifier: str) -> dict | None:
    """Найти пользователя по tg_id / внутреннему id / @username."""
    ident = (identifier or "").strip()
    if not ident:
        return None

    if ident.isdigit():
        # 1) tg_id
        try:
            tg_id = int(ident)
        except ValueError:
            tg_id = None

        if tg_id is not None:
            try:
                u = await get_user_by_tg_id(tg_id)
            except Exception:
                u = None
            if u:
                return u

        # 2) internal id
        try:
            internal_id = int(ident)
        except ValueError:
            internal_id = None

        if internal_id is not None:
            try:
                u = await get_user_by_id(internal_id)
            except Exception:
                u = None
            if u:
                return u

        return None

    username = ident[1:].strip() if ident.startswith("@") else ident
    if not username:
        return None

    try:
        return await get_user_by_username(username)
    except Exception:
        return None


# =============================================================
# ==== FSM СТЕЙТЫ (пользователи) ===============================
# =============================================================


class UserAdminStates(StatesGroup):
    """FSM для раздела «Пользователи»."""

    waiting_identifier_for_profile = State()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: ВХОД / ПОИСК ==============================
# =============================================================


@router.callback_query(F.data == "admin:users")
async def admin_users_menu(callback: CallbackQuery, state: FSMContext):
    """Вход в раздел «Пользователи»: просим @username или ID."""
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.clear()
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

    text = (
        "<b>Пользователи</b>\n\n"
        "Отправь @username или числовой Telegram ID пользователя.\n\n"
        "Примеры:\n"
        "<code>@nickname</code>\n"
        "<code>123456789</code>\n\n"
        "Я покажу профиль и дам кнопки: фотография, бан, статистика, награды."
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
    """Собрать текст профиля пользователя (для админки)."""
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
    blocked_until = block_status.get("block_until")
    blocked_reason = block_status.get("block_reason")

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
            return str(dt_str)

    if is_premium:
        premium_text = f"активен до { _fmt_dt(premium_until) }" if premium_until else "активен (без срока)"
    else:
        premium_text = "нет"

    if is_blocked:
        block_text = f"да, до { _fmt_dt(blocked_until) }" if blocked_until else "да, без срока"
        if blocked_reason:
            block_text += f"\nПричина: {blocked_reason}"
    else:
        block_text = "нет"

    if avg_rating is not None and ratings_count:
        rating_line = f"• Рейтинг: <b>{avg_rating:.1f}</b> (оценок: {ratings_count})"
    else:
        rating_line = "• Рейтинг: —"

    parts = [
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
        parts.append("")
        parts.append(f"<b>О себе</b>\n{bio}")

    return "\n".join(parts)


@router.message(UserAdminStates.waiting_identifier_for_profile, F.text)
async def admin_users_find_profile(message: Message, state: FSMContext):
    """Поиск и показ подробного профиля пользователя."""
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

    block_status = await get_user_block_status_by_tg_id(tg_id) if tg_id else {}
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

    await state.update_data(
        selected_user_id=internal_id,
        selected_user_tg_id=tg_id,
        selected_user_profile=user,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")

    # награды вынесем в awards.py, но кнопки уже оставляем
    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")

    kb.button(text="🚫 Бан", callback_data="admin:users:ban")

    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    kb.adjust(2, 3, 1, 2)

    await _edit_user_prompt_or_answer(
        message,
        state,
        text=text,
        reply_markup=kb.as_markup(),
    )


@router.message(UserAdminStates.waiting_identifier_for_profile)
async def admin_users_find_profile_non_text(message: Message):
    """Любой не-текст в режиме поиска пользователя удаляем."""
    try:
        await message.delete()
    except Exception:
        pass


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: ФОТО =====================================
# =============================================================


@router.callback_query(F.data == "admin:users:photo")
async def admin_users_photo(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    if not target_user_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

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
            return str(dt_str)

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

    avg_rating = stats.get("avg_rating")
    ratings_count = int(stats.get("ratings_count") or 0)
    if avg_rating is not None and ratings_count > 0:
        lines.append(f"• Средний рейтинг: <b>{float(avg_rating):.1f}</b>")
    else:
        lines.append("• Средний рейтинг: —")

    lines.extend(
        [
            f"• Оценок всего: <b>{int(stats.get('ratings_count') or 0)}</b>",
            f"• Супер-оценок: <b>{int(stats.get('super_ratings_count') or 0)}</b>",
            f"• Комментариев: <b>{int(stats.get('comments_count') or 0)}</b>",
            f"• Жалоб всего: <b>{int(stats.get('reports_total') or 0)}</b>",
            f"• Жалоб в ожидании: <b>{int(stats.get('reports_pending') or 0)}</b>",
            f"• Жалоб решено: <b>{int(stats.get('reports_resolved') or 0)}</b>",
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

    except TelegramBadRequest:
        # иногда падает на caption — попробуем коротко
        safe_caption = caption[:3800] + "..." if len(caption) > 3800 else caption
        try:
            await callback.message.answer(safe_caption, reply_markup=kb.as_markup())
        except Exception:
            pass

    except Exception:
        # fallback: пробуем редактировать подпись, если сообщение уже с фото
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(caption, reply_markup=kb.as_markup())

    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: НАЗАД К ПРОФИЛЮ ===========================
# =============================================================


@router.callback_query(F.data == "admin:users:profile")
async def admin_users_back_to_profile(callback: CallbackQuery, state: FSMContext):
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
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")

    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")

    kb.button(text="🚫 Бан", callback_data="admin:users:ban")
    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    kb.adjust(2, 3, 1, 2)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: СТАТИСТИКА ================================
# =============================================================


@router.callback_query(F.data == "admin:users:stats")
async def admin_users_stats(callback: CallbackQuery, state: FSMContext):
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

    messages_total = int(admin_stats.get("messages_total") or 0)
    ratings_given = int(admin_stats.get("ratings_given") or 0)
    comments_given = int(admin_stats.get("comments_given") or 0)
    reports_created = int(admin_stats.get("reports_created") or 0)
    active_photos = int(admin_stats.get("active_photos") or 0)
    total_photos = int(admin_stats.get("total_photos") or 0)
    upload_bans_count = int(admin_stats.get("upload_bans_count") or 0)

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


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: БАН / РАЗБАН / ОГРАНИЧИТЬ (заглушки) ======
# =============================================================


@router.callback_query(F.data == "admin:users:ban")
async def admin_users_ban(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Бан пользователя — пока заглушка.", show_alert=True)


@router.callback_query(F.data == "admin:users:unban")
async def admin_users_unban(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Разбан пользователя — пока заглушка.", show_alert=True)


@router.callback_query(F.data == "admin:users:limit")
async def admin_users_limit(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Ограничить доступ — пока заглушка.", show_alert=True)