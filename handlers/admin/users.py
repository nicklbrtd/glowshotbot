

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ПОЛЬЗОВАТЕЛИ ==================================
# =============================================================

import html
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_user_by_username,
    update_user_name,
    get_user_block_status_by_tg_id,
    set_user_block_status_by_tg_id,
    get_user_rating_summary,
    get_user_admin_stats,
    get_awards_for_user,
    get_active_photos_for_user,
    get_photo_admin_stats,
    get_photo_report_stats,
    get_photo_stats,
    hide_active_photos_for_user,
    restore_photos_from_status,
)

from .common import (
    _ensure_admin,
    ensure_primary_bot,
)
from utils.validation import has_links_or_usernames, has_promo_channel_invite
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


# =============================================================
# ==== FSM СТЕЙТЫ (пользователи) ===============================
# =============================================================


class UserAdminStates(StatesGroup):
    waiting_identifier_for_profile = State()
    waiting_ban_reason = State()
    waiting_ban_days = State()
    waiting_new_name = State()


BAN_REASON_PRESETS: dict[str, str] = {
    "name_ads": "Реклама/ссылки в имени",
    "bio_ads": "Реклама в био",
    "spam": "Спам/флуд",
    "hate": "Оскорбления/хейт",
    "fraud": "Мошенничество",
}


def _fmt_admin_dt(dt_raw: str | None) -> str:
    if not dt_raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(dt_raw))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(dt_raw)


def _truncate(text: str | None, max_len: int) -> str:
    value = (text or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _render_roles_line(user: dict) -> str:
    roles: list[str] = []
    if bool(user.get("is_admin")):
        roles.append("🛡 Админ")
    if bool(user.get("is_moderator")):
        roles.append("🧑‍⚖️ Модер")
    if bool(user.get("is_support")):
        roles.append("🎧 Support")
    if bool(user.get("is_helper")):
        roles.append("🧩 Helper")
    return " · ".join(roles) if roles else "обычный"


def _premium_text(user: dict) -> str:
    if not bool(user.get("is_premium")):
        return "нет"
    if user.get("premium_until"):
        return f"до {_fmt_admin_dt(user.get('premium_until'))}"
    return "активен"


def _block_text_one_line(block_status: dict) -> str:
    if not bool(block_status.get("is_blocked")):
        return "нет"
    block_until = block_status.get("block_until")
    if block_until:
        return f"до {_fmt_admin_dt(block_until)}"
    return "бессрочно"


def _safe_rating_line(rating_summary: dict) -> tuple[str, int]:
    avg_rating = rating_summary.get("avg_rating")
    ratings_count = int(rating_summary.get("ratings_count") or 0)
    if avg_rating is None or ratings_count <= 0:
        return "—", ratings_count
    return f"{float(avg_rating):.1f}", ratings_count


def _build_user_admin_profile_kb(*, is_blocked: bool, full: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото активное", callback_data="admin:users:photo")
    kb.button(text="📚 Архив фото", callback_data="admin:users:photo_archive")
    kb.button(text="📊 Статистика", callback_data="admin:users:stats")
    kb.button(text="🏆 Награды", callback_data="admin:users:awards")
    if is_blocked:
        kb.button(text="🔓 Разбан", callback_data="admin:users:unban")
    else:
        kb.button(text="🚫 Бан", callback_data="admin:users:ban")
    kb.button(text="✏️ Имя", callback_data="admin:users:rename")
    kb.button(text="🧹 Скрыть активные", callback_data="admin:users:hide_active")
    kb.button(text="🔁 Восстановить фото", callback_data="admin:users:restore_hidden")
    if full:
        kb.button(text="📌 Summary", callback_data="admin:users:profile")
    else:
        kb.button(text="📄 Подробнее", callback_data="admin:users:profile_full")
    kb.button(text="🔁 Другой", callback_data="admin:users")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 2, 2, 1, 2)
    return kb.as_markup()


def _build_user_admin_photo_kb(*, is_blocked: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад (Summary)", callback_data="admin:users:profile")
    kb.button(text="📄 Подробнее", callback_data="admin:users:profile_full")
    if is_blocked:
        kb.button(text="🔓 Разбан", callback_data="admin:users:unban")
    else:
        kb.button(text="🚫 Бан", callback_data="admin:users:ban")
    kb.button(text="🧹 Скрыть активные", callback_data="admin:users:hide_active")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()



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
        "Поддерживается поиск по:\n"
        "• @username\n"
        "• Telegram ID\n\n"
        "Покажу краткий профиль и быстрые действия."
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


async def _render_admin_user_profile_summary(
    user: dict,
    block_status: dict,
    rating_summary: dict,
    admin_stats: dict,
    awards: list[dict],
) -> str:
    """Короткая сводка для админки (по умолчанию)."""
    internal_id = user["id"]
    tg_id = user.get("tg_id")
    username_raw = user.get("username")
    username = html.escape(str(username_raw), quote=False) if username_raw else None
    name = html.escape(str(user.get("name") or "Без имени"), quote=False)
    messages_total = int(admin_stats.get("messages_total", 0) or 0) if admin_stats else 0
    ratings_given = int(admin_stats.get("ratings_given", 0) or 0) if admin_stats else 0
    comments_given = int(admin_stats.get("comments_given", 0) or 0) if admin_stats else 0
    reports_created = int(admin_stats.get("reports_created", 0) or 0) if admin_stats else 0
    active_photos = int(admin_stats.get("active_photos", 0) or 0) if admin_stats else 0
    total_photos = int(admin_stats.get("total_photos", 0) or 0) if admin_stats else 0
    avg_rating, ratings_count = _safe_rating_line(rating_summary)
    username_part = f" @{username}" if username else ""
    role_line = _render_roles_line(user)
    _ = awards
    return "\n".join(
        [
            "<b>Пользователь</b>",
            f"👤 <b>{name}</b>{username_part}",
            f"ID: <code>{internal_id}</code> · TG: <code>{tg_id}</code>",
            f"💎 Premium: {_premium_text(user)} · 🚫 Бан загрузок: {_block_text_one_line(block_status)}",
            f"📸 Фото: активных <b>{active_photos}</b> · всего <b>{total_photos}</b>",
            f"⭐ Рейтинг: <b>{avg_rating}</b> · 🗳 оценок: <b>{ratings_count}</b>",
            f"🧮 Активность: 🗳 <b>{ratings_given}</b> · 💬 <b>{comments_given}</b> · 🚨 <b>{reports_created}</b> · Σ <b>{messages_total}</b>",
            f"Роли: {role_line}",
        ]
    )


async def _render_admin_user_profile_full(
    user: dict,
    block_status: dict,
    rating_summary: dict,
    admin_stats: dict,
    awards: list[dict],
) -> str:
    """Подробная карточка пользователя для админки."""
    internal_id = user["id"]
    tg_id = user.get("tg_id")
    username_raw = user.get("username")
    username = html.escape(str(username_raw), quote=False) if username_raw else None
    name = html.escape(str(user.get("name") or "Без имени"), quote=False)
    gender = (user.get("gender") or "").strip()
    age = user.get("age")
    bio = _truncate(user.get("bio"), 900)
    created_at = user.get("created_at")
    updated_at = user.get("updated_at")
    messages_total = int(admin_stats.get("messages_total", 0) or 0) if admin_stats else 0
    ratings_given = int(admin_stats.get("ratings_given", 0) or 0) if admin_stats else 0
    comments_given = int(admin_stats.get("comments_given", 0) or 0) if admin_stats else 0
    reports_created = int(admin_stats.get("reports_created", 0) or 0) if admin_stats else 0
    active_photos = int(admin_stats.get("active_photos", 0) or 0) if admin_stats else 0
    total_photos = int(admin_stats.get("total_photos", 0) or 0) if admin_stats else 0
    upload_bans_count = int(admin_stats.get("upload_bans_count", 0) or 0) if admin_stats else 0
    avg_rating, ratings_count = _safe_rating_line(rating_summary)
    awards_count = len(awards)
    has_beta_award = any(
        (a.get("code") == "beta_tester")
        or ("бета-тестер бота" in (a.get("title") or "").lower())
        for a in awards
    )
    roles_line = _render_roles_line(user)
    status_lines = [
        f"• Премиум: {_premium_text(user)}",
        f"• Бан на загрузку: {_block_text_one_line(block_status)}",
        f"• Удалён из базы: {'да' if bool(user.get('is_deleted')) else 'нет'}",
    ]
    block_reason = (block_status.get("block_reason") or "").strip()
    if block_reason:
        status_lines.append(f"• Причина бана: {html.escape(block_reason, quote=False)}")

    lines = [
        "<b>Профиль пользователя</b>",
        f"👤 <b>{name}</b>" + (f" @{username}" if username else ""),
        f"ID в базе: <code>{internal_id}</code> · Telegram ID: <code>{tg_id}</code>",
        f"Роли: {roles_line}",
        "",
        "<b>Статусы</b>",
        *status_lines,
        "",
        "<b>Активность</b>",
        f"• Рейтинг: <b>{avg_rating}</b> (оценок: <b>{ratings_count}</b>)",
        f"• 🧮 Активность (суммарно): <b>{messages_total}</b>",
        f"• Оценок поставил: <b>{ratings_given}</b>",
        f"• Комментариев: <b>{comments_given}</b>",
        f"• Жалоб создал: <b>{reports_created}</b>",
        f"• Фото активно: <b>{active_photos}</b> · всего: <b>{total_photos}</b>",
        f"• Ограничений на загрузку: <b>{upload_bans_count}</b>",
        "",
        "<b>Награды</b>",
        f"• Всего наград: <b>{awards_count}</b>",
        f"• Есть «Бета‑тестер бота»: {'да' if has_beta_award else 'нет'}",
    ]
    if gender:
        lines.append(f"• Пол: {html.escape(gender, quote=False)}")
    if age is not None:
        lines.append(f"• Возраст: {age}")
    if created_at:
        lines.append(f"• Регистрация: {_fmt_admin_dt(created_at)}")
    if updated_at:
        lines.append(f"• Последнее обновление: {_fmt_admin_dt(updated_at)}")
    if bio:
        lines.extend(["", f"<b>О себе</b>\n{html.escape(bio, quote=False)}"])
    return "\n".join(lines)


async def _refresh_selected_user_profile(
    state: FSMContext,
    *,
    full: bool,
) -> tuple[str, InlineKeyboardMarkup] | None:
    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    tg_id = data.get("selected_user_tg_id")
    if not internal_id:
        return None
    user = await get_user_by_id(int(internal_id))
    if not user and tg_id:
        user = await get_user_by_tg_id(int(tg_id))
    if not user:
        return None
    new_tg_id = user.get("tg_id")
    block_status = await get_user_block_status_by_tg_id(int(new_tg_id)) if new_tg_id else {}
    rating_summary = await get_user_rating_summary(int(user["id"]))
    admin_stats = await get_user_admin_stats(int(user["id"]))
    awards = await get_awards_for_user(int(user["id"]))
    await state.update_data(
        selected_user_id=int(user["id"]),
        selected_user_tg_id=new_tg_id,
        selected_user_profile=user,
    )
    if full:
        text = await _render_admin_user_profile_full(
            user=user,
            block_status=block_status,
            rating_summary=rating_summary,
            admin_stats=admin_stats,
            awards=awards,
        )
    else:
        text = await _render_admin_user_profile_summary(
            user=user,
            block_status=block_status,
            rating_summary=rating_summary,
            admin_stats=admin_stats,
            awards=awards,
        )
    kb = _build_user_admin_profile_kb(is_blocked=bool(block_status.get("is_blocked")), full=full)
    return text, kb


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
            (
                "Пользователь не найден.\n\n"
                "Проверь:\n"
                "• @username без пробелов\n"
                "• числовой Telegram ID\n"
                "• если username недавно сменился — ищи по TG ID"
            ),
            reply_markup=kb.as_markup(),
        )
        return

    internal_id = user["id"]
    tg_id = user.get("tg_id")

    block_status = await get_user_block_status_by_tg_id(tg_id) if tg_id else {}
    rating_summary = await get_user_rating_summary(internal_id)
    admin_stats = await get_user_admin_stats(internal_id)
    awards = await get_awards_for_user(internal_id)

    text = await _render_admin_user_profile_summary(
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

    await _edit_user_prompt_or_answer(
        message,
        state,
        text=text,
        reply_markup=_build_user_admin_profile_kb(
            is_blocked=bool(block_status.get("is_blocked")),
            full=False,
        ),
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


def _build_admin_user_photo_caption(photo: dict, stats: dict, reports: dict) -> str:
    title = html.escape(_truncate(photo.get("title") or "Без названия", 120), quote=False)
    tag = html.escape(str((photo.get("category") or photo.get("tag") or "—")).strip(), quote=False)
    device_type = html.escape(str((photo.get("device_type") or "").strip()), quote=False)
    device_info = html.escape(str((photo.get("device_info") or "").strip()), quote=False)
    description = html.escape(_truncate(photo.get("description"), 280), quote=False)
    moderation_status = html.escape(str((photo.get("moderation_status") or "active").strip()), quote=False)

    bayes_raw = stats.get("bayes_score")
    bayes_text = f"{float(bayes_raw):.2f}" if bayes_raw is not None else "—"
    ratings_count = int(stats.get("ratings_count") or 0)
    comments_count = int(stats.get("comments_count") or 0)
    reports_pending = int(reports.get("pending") or 0)
    reports_total = int(reports.get("total") or 0)

    device_line = "—"
    if device_type and device_info:
        device_line = f"{device_type} · {device_info}"
    elif device_type:
        device_line = device_type
    elif device_info:
        device_line = device_info

    lines = [
        f"<b>Фото пользователя</b> · <code>ID {photo['id']}</code>",
        f"<code>\"{title}\"</code>",
        f"🏷️ {tag} · 📱 {device_line}",
        f"⭐ Bayes: <b>{bayes_text}</b> · 🗳 <b>{ratings_count}</b> · 💬 <b>{comments_count}</b>",
        f"🚨 Жалобы: <b>{reports_pending}</b> pending / {reports_total} all",
        f"🕒 Загружена: {_fmt_admin_dt(photo.get('created_at'))}",
        f"📌 Статус: {moderation_status}",
    ]
    if description:
        lines.append(f"📝 {description}")
    return "\n".join(lines)


async def _upsert_admin_user_photo(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    data = await state.get_data()
    chat_id = data.get("user_prompt_chat_id")
    msg_id = data.get("user_prompt_msg_id")

    async def _send_new() -> None:
        sent = await callback.message.bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=file_id,
            caption=caption,
            reply_markup=reply_markup,
            disable_notification=True,
            parse_mode="HTML",
        )
        await state.update_data(user_prompt_chat_id=sent.chat.id, user_prompt_msg_id=sent.message_id)

    if chat_id and msg_id:
        try:
            await callback.message.bot.edit_message_media(
                chat_id=chat_id,
                message_id=msg_id,
                media=InputMediaPhoto(media=file_id, caption=caption, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest:
            try:
                await callback.message.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
                return
            except Exception:
                pass
        except Exception:
            pass
        try:
            await callback.message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await _send_new()
        return

    await _send_new()


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

    active_photos = await get_active_photos_for_user(int(target_user_id), limit=1)
    photo = active_photos[0] if active_photos else None
    if not photo or photo.get("is_deleted"):
        text = "У пользователя нет активного фото.\n\nВозможно оно уже скрыто, удалено или завершился срок показа."
        profile_payload = await _refresh_selected_user_profile(state, full=False)
        kb = profile_payload[1] if profile_payload else _build_user_admin_profile_kb(is_blocked=False, full=False)
        await _edit_user_prompt_or_answer(
            callback.message,
            state,
            text=text,
            reply_markup=kb,
        )
        await callback.answer()
        return

    photo_stats = await get_photo_stats(photo["id"])
    legacy_stats = await get_photo_admin_stats(photo["id"])
    reports_stats = await get_photo_report_stats(photo["id"])
    merged_stats = {**legacy_stats, **photo_stats}
    caption = _build_admin_user_photo_caption(photo, merged_stats, reports_stats)

    profile_payload = await _refresh_selected_user_profile(state, full=False)
    is_blocked = False
    if profile_payload:
        data = await state.get_data()
        selected_tg_id = data.get("selected_user_tg_id")
        if selected_tg_id:
            block_status = await get_user_block_status_by_tg_id(int(selected_tg_id))
            is_blocked = bool(block_status.get("is_blocked"))
    await _upsert_admin_user_photo(
        callback,
        state,
        file_id=str(photo["file_id"]),
        caption=caption,
        reply_markup=_build_user_admin_photo_kb(is_blocked=is_blocked),
    )

    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: НАЗАД К ПРОФИЛЮ ===========================
# =============================================================


@router.callback_query(F.data == "admin:users:profile")
async def admin_users_back_to_profile(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    payload = await _refresh_selected_user_profile(state, full=False)
    if not payload:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    text, kb = payload

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin:users:profile_full")
async def admin_users_profile_full(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return
    payload = await _refresh_selected_user_profile(state, full=True)
    if not payload:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return
    text, kb = payload
    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text,
        reply_markup=kb,
    )
    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: ИЗМЕНИТЬ ИМЯ ==============================
# =============================================================


@router.callback_query(F.data == "admin:users:rename")
async def admin_users_rename_start(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    if not target_user_id:
        await callback.answer("Сначала найди пользователя по @username или ID.", show_alert=True)
        return

    await state.set_state(UserAdminStates.waiting_new_name)

    user = data.get("selected_user_profile") or {}
    current_name = (user.get("name") or "—").strip()
    text = (
        "<b>Изменить имя пользователя</b>\n\n"
        f"Текущее имя: <b>{html.escape(current_name)}</b>\n\n"
        "Отправь новое имя одним сообщением.\n"
        "Без ссылок, @username и упоминаний каналов."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад (Summary)", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        text=text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.message(UserAdminStates.waiting_new_name, F.text)
async def admin_users_rename_input(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    target_tg_id = data.get("selected_user_tg_id")
    if not target_user_id:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Сначала найди пользователя по @username или ID.",
        )
        await state.set_state(UserAdminStates.waiting_identifier_for_profile)
        return

    new_name = (message.text or "").strip()
    if not new_name:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Имя не может быть пустым. Отправь новое имя одним сообщением.",
        )
        return

    if has_links_or_usernames(new_name) or has_promo_channel_invite(new_name):
        await _edit_user_prompt_or_answer(
            message,
            state,
            "В имени нельзя оставлять @username, ссылки или рекламу. Отправь другое имя.",
        )
        return

    try:
        await update_user_name(int(target_user_id), new_name)
    except Exception:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Не удалось обновить имя. Попробуй ещё раз.",
        )
        return

    # Обновим профиль для админки
    user = await get_user_by_id(int(target_user_id)) or (
        await get_user_by_tg_id(int(target_tg_id)) if target_tg_id else None
    )
    if not user:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Пользователь не найден. Попробуй ещё раз через поиск.",
        )
        await state.set_state(UserAdminStates.waiting_identifier_for_profile)
        return

    await state.update_data(
        selected_user_profile=user,
        selected_user_tg_id=user.get("tg_id"),
    )
    target_tg_id = user.get("tg_id") or target_tg_id

    payload = await _refresh_selected_user_profile(state, full=False)
    if not payload:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Профиль не удалось обновить. Найди пользователя заново.",
        )
        await state.set_state(UserAdminStates.waiting_identifier_for_profile)
        return
    text, kb = payload

    await _edit_user_prompt_or_answer(
        message,
        state,
        text=text,
        reply_markup=kb,
    )
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

    # Уведомляем пользователя
    if target_tg_id:
        try:
            kb_user = InlineKeyboardBuilder()
            kb_user.button(text="Понятно", callback_data="user:notify_seen")
            kb_user.adjust(1)
            main_bot = ensure_primary_bot(message.bot)
            await main_bot.send_message(
                chat_id=int(target_tg_id),
                text=(
                    "Не уследили за запретами в нике...\n\n"
                    f"Ваш ник был изменен админом на <b>{html.escape(new_name)}</b>."
                ),
                reply_markup=kb_user.as_markup(),
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception:
            pass


@router.message(UserAdminStates.waiting_new_name)
async def admin_users_rename_input_non_text(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


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
    kb.button(text="⬅️ Назад (Summary)", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2)

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        "\n".join(text_lines),
        reply_markup=kb.as_markup(),
    )

    await callback.answer()


# =============================================================
# ==== ПОЛЬЗОВАТЕЛИ: БАН / РАЗБАН / ОГРАНИЧИТЬ (заглушки) ======
# =============================================================


def _build_ban_days_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for days in (1, 3, 7, 30):
        kb.button(text=f"{days} дн.", callback_data=f"admin:users:ban_days:{days}")
    kb.button(text="∞ Бессрочно", callback_data="admin:users:ban_days:0")
    kb.button(text="⬅️ Назад (Summary)", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _build_ban_reasons_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Реклама/ссылки в имени", callback_data="admin:users:ban_reason:name_ads")
    kb.button(text="Реклама в био", callback_data="admin:users:ban_reason:bio_ads")
    kb.button(text="Спам/флуд", callback_data="admin:users:ban_reason:spam")
    kb.button(text="Оскорбления/хейт", callback_data="admin:users:ban_reason:hate")
    kb.button(text="Мошенничество", callback_data="admin:users:ban_reason:fraud")
    kb.button(text="📝 Другое (ввести текст)", callback_data="admin:users:ban_reason:other")
    kb.button(text="⬅️ Назад (срок)", callback_data="admin:users:ban")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1, 1, 1, 1)
    return kb.as_markup()


def _ban_reason_text(days: int) -> str:
    duration_text = f"{days} дн." if days > 0 else "бессрочно"
    return (
        "<b>Бан загрузок</b>\n\n"
        f"Срок: <b>{duration_text}</b>\n"
        "Выбери причину кнопкой.\n\n"
        "Если шаблон не подходит — нажми «Другое (ввести текст)»."
    )


async def _apply_admin_ban(
    message: Message,
    state: FSMContext,
    *,
    reason_text: str,
) -> bool:
    data = await state.get_data()
    tg_id = data.get("admin_ban_user_tg_id")
    internal_id = data.get("admin_ban_user_id")
    days = int(data.get("admin_ban_days") or 0)
    if not tg_id or not internal_id:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Сессия бана потеряна. Открой профиль пользователя заново.",
        )
        await state.set_state(UserAdminStates.waiting_identifier_for_profile)
        return False

    reason_clean = _truncate(reason_text, 240) or "Без причины"
    until_iso = None
    if days > 0:
        until_iso = (get_moscow_now() + timedelta(days=days)).isoformat()

    try:
        await set_user_block_status_by_tg_id(
            int(tg_id),
            is_blocked=True,
            reason=f"ADMIN_BAN: {reason_clean}",
            until_iso=until_iso,
        )
        await hide_active_photos_for_user(int(internal_id), new_status="blocked_by_ban")
    except Exception:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Не удалось применить бан. Попробуй позже.",
        )
        await state.set_state(UserAdminStates.waiting_identifier_for_profile)
        return False

    try:
        duration_line = f"{days} дн." if days > 0 else "бессрочно"
        main_bot = ensure_primary_bot(message.bot)
        await main_bot.send_message(
            chat_id=int(tg_id),
            text=(
                f"⛔ Ограничение на загрузку фото: {duration_line}\n"
                f"Причина: {reason_clean}\n"
                "Если это ошибка — напишите в поддержку."
            ),
            disable_notification=True,
        )
    except Exception:
        pass

    payload = await _refresh_selected_user_profile(state, full=False)
    if payload:
        text, kb = payload
        await _edit_user_prompt_or_answer(message, state, text, reply_markup=kb)
    else:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Бан применён, но профиль не удалось обновить. Открой пользователя заново.",
        )
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)
    return True


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

    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        "Выбери срок ограничения на загрузку фото.",
        reply_markup=_build_ban_days_kb(),
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

    payload = await _refresh_selected_user_profile(state, full=False)
    if payload:
        text, kb = payload
        await _edit_user_prompt_or_answer(
            callback.message,
            state,
            text=text,
            reply_markup=kb,
        )
    else:
        await _edit_user_prompt_or_answer(
            callback.message,
            state,
            "Блокировка снята, но профиль не удалось обновить. Открой пользователя заново.",
        )
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

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
    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        _ban_reason_text(days),
        reply_markup=_build_ban_reasons_kb(),
    )
    await state.set_state(UserAdminStates.waiting_ban_days)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:users:ban_reason:"))
async def admin_users_ban_reason_pick(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    key = (callback.data or "").split(":")[-1]
    if key == "other":
        await _edit_user_prompt_or_answer(
            callback.message,
            state,
            "Введи причину одним сообщением (лучше коротко, 1–2 предложения).",
            reply_markup=None,
        )
        await state.set_state(UserAdminStates.waiting_ban_reason)
        await callback.answer()
        return
    if key not in BAN_REASON_PRESETS:
        await callback.answer("Некорректная причина.", show_alert=True)
        return
    ok = await _apply_admin_ban(
        callback.message,
        state,
        reason_text=BAN_REASON_PRESETS[key],
    )
    await callback.answer("Бан применён." if ok else "Не удалось применить бан.")


@router.message(UserAdminStates.waiting_ban_reason, F.text)
async def admin_users_ban_reason(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass

    reason_raw = (message.text or "").strip() or "Без причины"
    await _apply_admin_ban(message, state, reason_text=reason_raw)


@router.message(UserAdminStates.waiting_ban_reason)
async def admin_users_ban_reason_non_text(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@router.message(UserAdminStates.waiting_ban_days)
async def admin_users_ban_days_non_callback(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    days = data.get("admin_ban_days")
    if days is None:
        await _edit_user_prompt_or_answer(
            message,
            state,
            "Выбери срок ограничения кнопками ниже.",
            reply_markup=_build_ban_days_kb(),
        )
    else:
        await _edit_user_prompt_or_answer(
            message,
            state,
            _ban_reason_text(int(days)),
            reply_markup=_build_ban_reasons_kb(),
        )


@router.callback_query(F.data == "admin:users:hide_active")
async def admin_users_hide_active(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return
    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    if not target_user_id:
        await callback.answer("Сначала найди пользователя.", show_alert=True)
        return
    hidden = await hide_active_photos_for_user(int(target_user_id), new_status="blocked_by_ban")
    payload = await _refresh_selected_user_profile(state, full=False)
    if payload:
        text, kb = payload
        await _edit_user_prompt_or_answer(callback.message, state, text, reply_markup=kb)
    await callback.answer(f"Скрыто фото: {hidden}")


@router.callback_query(F.data == "admin:users:restore_hidden")
async def admin_users_restore_hidden(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return
    data = await state.get_data()
    target_user_id = data.get("selected_user_id")
    if not target_user_id:
        await callback.answer("Сначала найди пользователя.", show_alert=True)
        return
    restored = await restore_photos_from_status(
        int(target_user_id),
        from_status="blocked_by_ban",
        to_status="active",
    )
    payload = await _refresh_selected_user_profile(state, full=False)
    if payload:
        text, kb = payload
        await _edit_user_prompt_or_answer(callback.message, state, text, reply_markup=kb)
    await callback.answer(f"Восстановлено фото: {restored}")


@router.callback_query(F.data == "admin:users:photo_archive")
async def admin_users_photo_archive(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return
    payload = await _refresh_selected_user_profile(state, full=False)
    back_kb = payload[1] if payload else _build_user_admin_profile_kb(is_blocked=False, full=False)
    await _edit_user_prompt_or_answer(
        callback.message,
        state,
        "📚 Архив фото пользователя\n\nРаздел в доработке. Пока доступен просмотр только активного фото.",
        reply_markup=back_kb,
    )
    await callback.answer()
