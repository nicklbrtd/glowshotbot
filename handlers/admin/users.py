

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ПОЛЬЗОВАТЕЛИ ==================================
# =============================================================

from datetime import datetime, timedelta
import html

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
    set_user_block_status_by_tg_id,
    get_user_rating_summary,
    get_user_admin_stats,
    get_awards_for_user,
    get_today_photo_for_user,
    get_photo_admin_stats,
    get_premium_users,
    set_user_premium_status,
    hide_active_photos_for_user,
    restore_photos_from_status,
    get_all_users_tg_ids,
)

from .common import (
    _ensure_admin,
)
from utils.time import get_moscow_now

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
                parse_mode="HTML",
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
                    parse_mode="HTML",
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
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
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


# -------------------- Premium helpers --------------------

def build_premium_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="➕ Выдать", callback_data="admin:premium:grant")
    kb.button(text="➖ Убрать", callback_data="admin:premium:revoke")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2)
    return kb.as_markup()


def _parse_premium_until(raw: str) -> str | None:
    """Accept:
    - days: '30'
    - date: '31.12.2025'
    - forever: 'навсегда' / 'без срока' / 'бессрочно' / 'forever' / '∞' / '-'

    Returns ISO datetime string or None (forever).
    Raises ValueError on invalid input.
    """
    s = (raw or "").strip()

    # Treat empty as invalid here because user can't actually send “empty” in Telegram.
    # We keep forever via explicit tokens/button.
    if not s:
        raise ValueError("empty")

    s_low = s.lower()
    forever_tokens = {
        "навсегда",
        "без срока",
        "безсрока",
        "бессрочно",
        "forever",
        "infinite",
        "∞",
        "-",
        "0",
    }
    if s_low in forever_tokens:
        return None

    if s.isdigit():
        days = int(s)
        if days <= 0:
            return None
        until = datetime.now() + timedelta(days=days)
        return until.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    try:
        dt = datetime.strptime(s, "%d.%m.%Y")
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
        return dt.isoformat()
    except Exception:
        raise ValueError("invalid")




async def _edit_premium_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Keep one service message for Premium section to prevent spam."""
    data = await state.get_data()
    chat_id = data.get("premium_prompt_chat_id")
    msg_id = data.get("premium_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    try:
        sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        await state.update_data(premium_prompt_chat_id=sent.chat.id, premium_prompt_msg_id=sent.message_id)
    except Exception:
        pass


# === Premium "soft clear" helper ===
async def _premium_soft_clear(state: FSMContext):
    """Clear FSM state/data but keep IDs of the premium service message to avoid message spam."""
    data = await state.get_data()
    chat_id = data.get("premium_prompt_chat_id")
    msg_id = data.get("premium_prompt_msg_id")

    await state.clear()

    if chat_id and msg_id:
        await state.update_data(premium_prompt_chat_id=chat_id, premium_prompt_msg_id=msg_id)



def build_premium_notice_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Просмотрено", callback_data="user:premium:seen")
    kb.adjust(1)
    return kb.as_markup()
# -------------------- Premium notification helper --------------------

async def _notify_user_premium_change(
    bot,
    tg_id: int,
    *,
    is_enabled: bool,
    until_iso: str | None,
    admin_label: str,
):
    """Best-effort notification to user about premium changes."""

    def _fmt_until(iso: str | None) -> str:
        if not iso:
            return "бессрочно"
        try:
            return "до " + datetime.fromisoformat(iso).strftime("%d.%m.%Y")
        except Exception:
            return "до " + str(iso)

    admin_label_safe = html.escape(admin_label or "админа", quote=False)

    if is_enabled:
        until_text = _fmt_until(until_iso)
        text = (
            "💎 <b>GlowShot Premium активирован</b>\n\n"
            f"Вы получили Premium от {admin_label_safe}.\n"
            f"Срок: <b>{html.escape(until_text, quote=False)}</b>\n\n"
            "Теперь вам доступны премиум‑функции в боте ✨"
        )
    else:
        text = (
            "💤 <b>GlowShot Premium отключён</b>\n\n"
            f"Premium был отключён админом {admin_label_safe}.\n"
            "Если это ошибка — напишите в поддержку."
        )

    try:
        await bot.send_message(
            chat_id=tg_id,
            text=text,
            parse_mode="HTML",
            reply_markup=build_premium_notice_kb(),
            disable_notification=True,
        )
    except Exception:
        # User may block the bot or disallow messages; ignore silently.
        return


# =============================================================
# ==== FSM СТЕЙТЫ (пользователи) ===============================
# =============================================================


class UserAdminStates(StatesGroup):
    waiting_identifier_for_profile = State()
    waiting_ban_reason = State()
    waiting_ban_days = State()

class PremiumAdminStates(StatesGroup):
    waiting_identifier_for_grant = State()
    waiting_premium_until = State()
    waiting_identifier_for_revoke = State()
    waiting_fest_name = State()
    waiting_fest_days = State()



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
        msg = await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        msg = await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

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
    username_raw = user.get("username")
    username = html.escape(str(username_raw), quote=False) if username_raw else None
    name = html.escape(str(user.get("name") or "Без имени"), quote=False)
    gender = html.escape(str(user.get("gender") or "—"), quote=False)
    age = user.get("age")
    bio = html.escape(str((user.get("bio") or "").strip()), quote=False)
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
            block_text += f"\nПричина: {html.escape(str(blocked_reason), quote=False)}"
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

    if bool(block_status.get("is_blocked")):
        kb.button(text="🔓 Разбан", callback_data="admin:users:unban")
    else:
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
            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")

        await callback.answer()
        return

    stats = await get_photo_admin_stats(photo["id"])

    title = html.escape((photo.get("title") or "Без названия").strip(), quote=False)
    device_type = html.escape((photo.get("device_type") or "").strip(), quote=False)
    device_info = html.escape((photo.get("device_info") or "").strip(), quote=False)
    description = html.escape((photo.get("description") or "").strip(), quote=False)
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
            parse_mode="HTML",
        )

        await state.update_data(
            user_prompt_chat_id=sent.chat.id,
            user_prompt_msg_id=sent.message_id,
        )

    except TelegramBadRequest:
        # иногда падает на caption — попробуем коротко
        safe_caption = caption[:3800] + "..." if len(caption) > 3800 else caption
        try:
            sent = await callback.message.answer(safe_caption, reply_markup=kb.as_markup(), parse_mode="HTML")
            await state.update_data(user_prompt_chat_id=sent.chat.id, user_prompt_msg_id=sent.message_id)
        except Exception:
            pass

    except Exception:
        # fallback: пробуем редактировать подпись, если сообщение уже с фото
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            sent = await callback.message.answer(caption, reply_markup=kb.as_markup(), parse_mode="HTML")
            await state.update_data(user_prompt_chat_id=sent.chat.id, user_prompt_msg_id=sent.message_id)

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
        await callback.message.edit_text("\n".join(text_lines), reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(text_lines), reply_markup=kb.as_markup(), parse_mode="HTML")

    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: БАН / РАЗБАН / ОГРАНИЧИТЬ (заглушки) ======
# =============================================================


@router.callback_query(F.data == "admin:users:ban")
async def admin_users_ban(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_tg_id = data.get("selected_user_tg_id")
    target_user_id = data.get("selected_user_id")
    if not target_tg_id or not target_user_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for days in (1, 3, 7, 30):
        kb.button(text=f"{days} дн.", callback_data=f"admin:users:ban_days:{days}")
    kb.button(text="∞ Бессрочно", callback_data="admin:users:ban_days:0")
    kb.button(text="⬅️ Назад к профилю", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1, 1)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        "Выбери срок бана пользователя.",
        reply_markup=kb.as_markup(),
    )
    await state.update_data(
        admin_ban_user_tg_id=target_tg_id,
        admin_ban_user_id=target_user_id,
    )
    await state.set_state(UserAdminStates.waiting_ban_days)
    await callback.answer()


@router.callback_query(F.data == "admin:users:unban")
async def admin_users_unban(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_tg_id = data.get("selected_user_tg_id")
    target_user_id = data.get("selected_user_id")
    if not target_tg_id or not target_user_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    try:
        await set_user_block_status_by_tg_id(int(target_tg_id), is_blocked=False, reason=None, until_iso=None)
        await restore_photos_from_status(int(target_user_id), from_status="blocked_by_ban", to_status="active")
    except Exception:
        await callback.answer("Не удалось снять бан. Попробуй позже.", show_alert=True)
        return

    # Обновим карточку
    user = await get_user_by_id(int(target_user_id)) or await get_user_by_tg_id(int(target_tg_id))
    block_status = await get_user_block_status_by_tg_id(int(target_tg_id))
    rating_summary = await get_user_rating_summary(int(target_user_id))
    admin_stats = await get_user_admin_stats(int(target_user_id))
    awards = await get_awards_for_user(int(target_user_id))

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
        text=text,
        reply_markup=kb.as_markup(),
    )

    try:
        await callback.answer("Блокировка снята.")
    except Exception:
        pass


@router.callback_query(F.data == "admin:users:limit")
async def admin_users_limit(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Ограничить доступ — пока заглушка.", show_alert=True)


@router.callback_query(F.data.startswith("admin:users:ban_days:"))
async def admin_users_ban_days(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return
    try:
        days = int(parts[3])
    except Exception:
        await callback.answer("Некорректный срок.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("admin_ban_user_tg_id"):
        await callback.answer("Сначала выбери пользователя.", show_alert=True)
        return

    await state.update_data(admin_ban_days=days)
    prompt = (
        "Введи причину бана одним сообщением.\n"
        "Она будет отправлена пользователю."
    )
    await _edit_user_prompt_or_answer(callback.message, state, prompt)
    await state.set_state(UserAdminStates.waiting_ban_reason)
    await callback.answer()


@router.message(UserAdminStates.waiting_ban_reason)
async def admin_users_ban_reason(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    tg_id = data.get("admin_ban_user_tg_id")
    internal_id = data.get("admin_ban_user_id")
    days = int(data.get("admin_ban_days") or 0)

    if not tg_id or not internal_id:
        await _edit_user_prompt_or_answer(message, state, "Сессия бана потеряна. Открой профиль заново.")
        await state.clear()
        return

    reason_raw = (message.text or "").strip() or "Без причины"
    until_iso = None
    until_dt = None
    if days > 0:
        until_dt = get_moscow_now() + timedelta(days=days)
        until_iso = until_dt.isoformat()

    reason_db = f"ADMIN_BAN: {reason_raw}"

    try:
        await set_user_block_status_by_tg_id(int(tg_id), is_blocked=True, reason=reason_db, until_iso=until_iso)
        await hide_active_photos_for_user(int(internal_id), new_status="blocked_by_ban")
    except Exception:
        await _edit_user_prompt_or_answer(message, state, "Не удалось применить бан. Попробуй позже.")
        await state.clear()
        return

    # Уведомим пользователя
    try:
        lines = []
        if days > 0:
            lines.append(f"⛔ Вы забанены админом бота на {days} дней.")
            if until_dt:
                lines.append(f"До: {until_dt.strftime('%d.%m.%Y %H:%M')} (МСК)")
        else:
            lines.append("⛔ Вы забанены админом бота.")
        lines.append(f"Причина: {reason_raw}")
        await message.bot.send_message(chat_id=int(tg_id), text="\n".join(lines))
    except Exception:
        pass

    # Обновим карточку в админке
    user = await get_user_by_id(int(internal_id)) or await get_user_by_tg_id(int(tg_id))
    block_status = await get_user_block_status_by_tg_id(int(tg_id))
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
    kb.button(text="👁 Посмотреть профиль", callback_data="admin:users:profile")
    kb.button(text="📸 Фотография", callback_data="admin:users:photo")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")
    kb.button(text="🏆 Награды / ачивки", callback_data="admin:users:awards")
    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")
    kb.button(text="🔓 Разбан", callback_data="admin:users:unban")
    kb.button(text="🔁 Другой пользователь", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 3, 1, 2)

    await _edit_user_prompt_or_answer(message, state, text, reply_markup=kb.as_markup())
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)
    try:
        await message.answer("Бан применён.", disable_notification=True)
    except Exception:
        pass
# ===============================================================
# ============== ПОЛЬЗОВАТЕЛИ: ПРЕМИУМ ==========================
# ===============================================================


@router.callback_query(F.data == "admin:premium")
async def admin_premium_menu(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)

    text = (
        "<b>Премиум</b>\n\n"
        "• 📋 Список — текущие премиум-пользователи\n"
        "• ➕ Выдать — по @username/ID и сроку\n"
        "• ➖ Убрать — снять премиум\n"
    )

    try:
        msg = await callback.message.edit_text(text, reply_markup=build_premium_menu_kb(), parse_mode="HTML")
    except Exception:
        msg = await callback.message.answer(text, reply_markup=build_premium_menu_kb(), parse_mode="HTML")

    await state.update_data(premium_prompt_chat_id=msg.chat.id, premium_prompt_msg_id=msg.message_id)
    await callback.answer()


@router.callback_query(F.data == "admin:premium:list")
async def admin_premium_list(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    users = await get_premium_users()
    if not users:
        text = "Сейчас нет ни одного премиум-пользователя."
    else:
        lines = ["<b>Премиум-пользователи</b>", ""]
        now = datetime.utcnow()
        shown = 0
        for u in users[:400]:
            uname = u.get("username")
            label = f"@{uname}" if uname else (u.get("name") or "Без имени")
            label = html.escape(str(label), quote=False)

            until = u.get("premium_until")
            if until:
                try:
                    until_dt = datetime.fromisoformat(until)
                except Exception:
                    until_dt = None

                # Пропускаем просроченный премиум
                if until_dt and until_dt <= now:
                    continue

                if until_dt:
                    days_left = max(1, int((until_dt - now).total_seconds() // 86400 + 1))
                    until_str = until_dt.strftime("%d.%m.%Y")
                    lines.append(f"• {label} — до {until_str} ({days_left} дн.)")
                else:
                    lines.append(f"• {label} — до {html.escape(str(until), quote=False)}")
            else:
                lines.append(f"• {label} — бессрочно")

            shown += 1
            if shown >= 200:
                break

        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_fest_name, F.text)
async def admin_premium_festive_name(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    fest_name = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not fest_name:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)
        await _edit_premium_prompt_or_answer(
            message,
            state,
            "Название праздника не может быть пустым. Введи ещё раз.",
            kb.as_markup(),
        )
        return

    await state.update_data(fest_name=fest_name)
    await state.set_state(PremiumAdminStates.waiting_fest_days)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_premium_prompt_or_answer(
        message,
        state,
        f"🎉 {html.escape(fest_name)}\n\nТеперь введи срок премиума в днях (целое число > 0).",
        kb.as_markup(),
    )


@router.message(PremiumAdminStates.waiting_fest_days, F.text)
async def admin_premium_festive_days(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    fest_name = (data.get("fest_name") or "").strip()
    if not fest_name:
        await _premium_soft_clear(state)
        await _edit_premium_prompt_or_answer(message, state, "Сессия потерялась. Открой «Премиум» заново.", build_premium_menu_kb())
        return

    try:
        days = int(raw)
    except Exception:
        days = 0

    if days <= 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1, 1)
        await _edit_premium_prompt_or_answer(
            message,
            state,
            "Нужно целое число дней больше нуля. Введи снова.",
            kb.as_markup(),
        )
        return

    now = datetime.now()
    until_dt = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
    until_iso = until_dt.isoformat()

    tg_ids = await get_all_users_tg_ids()
    total = len(tg_ids)
    updated = 0
    notified = 0

    notice_text = (
        f"🎉 <b>Вам выдан премиум «{html.escape(fest_name, quote=False)}»</b>\n\n"
        f"Срок: <b>{days}</b> дн. (до {until_dt.strftime('%d.%m.%Y')})\n"
        "Поздравляем!"
    )

    for uid in tg_ids:
        try:
            await set_user_premium_status(int(uid), True, premium_until=until_iso)
            updated += 1
        except Exception:
            continue

        try:
            await message.bot.send_message(
                chat_id=int(uid),
                text=notice_text,
                parse_mode="HTML",
            )
            notified += 1
        except Exception:
            pass

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1, 1)

    summary = (
        f"✅ Праздничный премиум «{html.escape(fest_name, quote=False)}» выдан.\n\n"
        f"Срок: <b>{days}</b> дн. (до {until_dt.strftime('%d.%m.%Y')})\n"
        f"Обновили статус: <b>{updated}/{total}</b>\n"
        f"Уведомлений доставлено: <b>{notified}</b>"
    )

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(message, state, summary, kb.as_markup())


@router.callback_query(F.data == "admin:premium:grant")
async def admin_premium_grant(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)

    kb = InlineKeyboardBuilder()
    kb.button(text="🎉 Праздничный всем", callback_data="admin:premium:grant:festive")
    kb.button(text="🎯 Выборочно", callback_data="admin:premium:grant:selective")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1, 1, 1)

    text = (
        "➕ <b>Выдать премиум</b>\n\n"
        "Выбери вариант:\n"
        "• 🎉 Праздничный всем — задать праздник и срок, выдать всем активным пользователям;\n"
        "• 🎯 Выборочно — по @username / ID и сроку."
    )

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:premium:grant:selective")
async def admin_premium_grant_selective(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_identifier_for_grant)

    text = (
        "🎯 <b>Выборочная выдача</b>\n\n"
        "Отправь Telegram ID или @username пользователя.\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:premium:grant:festive")
async def admin_premium_grant_festive(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_fest_name)

    text = (
        "🎉 <b>Праздничный премиум</b>\n\n"
        "Введи название праздника (например, «Новый год»). После этого я спрошу срок в днях и выдам премиум всем активным пользователям с уведомлением."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_identifier_for_grant, F.text)
async def admin_premium_grant_get_user(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    u = await _find_user_by_identifier(ident)
    if not u:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data="admin:premium:grant")
        kb.button(text="⬅️ Назад", callback_data="admin:premium")
        kb.adjust(1)
        await _edit_premium_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    await state.update_data(pending_premium_user=u)
    await state.set_state(PremiumAdminStates.waiting_premium_until)

    text = (
        "💎 <b>Срок премиума</b>\n\n"
        "Выбери вариант:\n"
        "• число дней (например <code>30</code>)\n"
        "• дату окончания <code>31.12.2025</code>\n"
        "• или нажми кнопку <b>♾ Бессрочно</b>\n\n"
        "Можно также написать словом: <code>навсегда</code> / <code>без срока</code> / <code>бессрочно</code>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="♾ Бессрочно", callback_data="admin:premium:grant:forever")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(message, state, text, kb.as_markup())


# Handler for "Бессрочно" button
@router.callback_query(F.data == "admin:premium:grant:forever")
async def admin_premium_grant_forever(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    data = await state.get_data()
    u = data.get("pending_premium_user")
    if not u or not u.get("tg_id"):
        await callback.answer("Сначала выбери пользователя.", show_alert=True)
        return

    tg_id = int(u["tg_id"])

    await set_user_premium_status(tg_id, True, premium_until=None)

    # Notify user (best-effort)
    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        callback.message.bot,
        tg_id,
        is_enabled=True,
        until_iso=None,
        admin_label=admin_label,
    )

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(callback.message, state, f"✅ Премиум выдан: <b>{label}</b>\nСрок: <b>бессрочно</b>", kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_premium_until, F.text)
async def admin_premium_grant_set_until(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    u = data.get("pending_premium_user")
    if not u or not u.get("tg_id"):
        await _premium_soft_clear(state)
        await _edit_premium_prompt_or_answer(message, state, "Сессия сбилась. Открой «Премиум» заново.", build_premium_menu_kb())
        return

    tg_id = int(u["tg_id"])
    try:
        premium_until = _parse_premium_until(raw)
    except ValueError:
        kb = InlineKeyboardBuilder()
        kb.button(text="♾ Бессрочно", callback_data="admin:premium:grant:forever")
        kb.button(text="⬅️ Назад", callback_data="admin:premium")
        kb.adjust(1)

        await _edit_premium_prompt_or_answer(
            message,
            state,
            "❌ Не понял срок.\n\nВведи <code>30</code> (дней) или <code>31.12.2025</code>, либо нажми <b>♾ Бессрочно</b>.",
            kb.as_markup(),
        )
        return

    # ВАЖНО: await, иначе “пишет выдано, но не выдано”
    await set_user_premium_status(tg_id, True, premium_until=premium_until)

    # Notify user (best-effort)
    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        message.bot,
        tg_id,
        is_enabled=True,
        until_iso=premium_until,
        admin_label=admin_label,
    )

    until_text = "бессрочно"
    if premium_until:
        try:
            until_text = "до " + datetime.fromisoformat(premium_until).strftime("%d.%m.%Y")
        except Exception:
            until_text = html.escape(str(premium_until), quote=False)

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(message, state, f"✅ Премиум выдан: <b>{label}</b>\nСрок: <b>{until_text}</b>", kb.as_markup())


@router.callback_query(F.data == "admin:premium:revoke")
async def admin_premium_revoke(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_identifier_for_revoke)

    text = (
        "➖ <b>Снять премиум</b>\n\n"
        "Отправь Telegram ID или @username пользователя.\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_identifier_for_revoke, F.text)
async def admin_premium_revoke_do(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    u = await _find_user_by_identifier(ident)
    if not u or not u.get("tg_id"):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data="admin:premium:revoke")
        kb.button(text="⬅️ Назад", callback_data="admin:premium")
        kb.adjust(1)
        await _edit_premium_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    tg_id = int(u["tg_id"])

    # ВАЖНО: await
    await set_user_premium_status(tg_id, False, premium_until=None)

    # Notify user (best-effort)
    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        message.bot,
        tg_id,
        is_enabled=False,
        until_iso=None,
        admin_label=admin_label,
    )

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(message, state, f"✅ Премиум снят: <b>{label}</b>", kb.as_markup())


# ===============================================================
# ============== ПОЛЬЗОВАТЕЛИ: ПРЕМИУМ УВЕДЫ ====================
# ===============================================================

@router.callback_query(F.data == "user:premium:seen")
async def user_premium_notice_seen(callback: CallbackQuery):
    try:
        if callback.message:
            await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.answer("Ок")
    except Exception:
        pass
