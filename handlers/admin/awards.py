

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: НАГРАДЫ / АЧИВКИ ==============================
# =============================================================

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_awards_for_user,
    get_award_by_id,
    delete_award_by_id,
    give_achievement_to_user_by_code,
    create_custom_award_for_user,
    get_user_by_tg_id,
    get_user_by_id,
)

from .common import (
    _ensure_admin,
    _ensure_user,
    UserAdminStates,
    UserAwardsStates,
    RoleStates,
    PaymentsStates,
)
from .users import UserAdminStates  # чтобы вернуть FSM назад после создания награды


router = Router()


# =============================================================
# ==== FSM: выдача кастомной награды ===========================
# =============================================================


class UserAwardsStates(StatesGroup):
    """Состояния для выдачи кастомной награды пользователю."""

    waiting_custom_award_text = State()


# =============================================================
# ==== ХЕЛПЕРЫ ================================================
# =============================================================


def _build_awards_list_text(internal_id: int, awards: list[dict], prefix: str | None = None) -> str:
    lines: list[str] = []
    if prefix:
        lines.append(prefix.rstrip())
        lines.append("")

    lines.extend(
        [
            "🏆 <b>Награды пользователя</b>",
            "",
            f"ID в базе: <code>{internal_id}</code>",
        ]
    )

    if not awards:
        lines.append("")
        lines.append("Пока нет ни одной награды. Можно выдать первую ачивку ниже ✨")
        return "\n".join(lines)

    lines.append("")
    for a in awards:
        icon = a.get("icon") or "🏅"
        title = a.get("title") or "Без названия"
        desc = (a.get("description") or "").strip()
        line = f"{icon} <b>{title}</b>"
        if desc:
            line += f"\n   {desc}"
        lines.append(line)

    return "\n".join(lines)


def _build_awards_kb(internal_id: int, awards: list[dict]):
    kb = InlineKeyboardBuilder()

    # кнопки удаления каждой награды (ограничим, чтобы не раздуть клаву)
    for a in awards[:20]:
        aid = a.get("id")
        if aid is None:
            continue
        title = (a.get("title") or "наградa")
        safe_title = title[:22]
        kb.button(text=f"🗑 {safe_title}", callback_data=f"admin:users:award:del:{aid}")

    if awards:
        kb.adjust(1)

    kb.button(text="🎁 Выдать награду/ачивку", callback_data="admin:users:award:create")
    kb.button(text="🏅 Выдать «Бета‑тестер»", callback_data="admin:users:award:beta")
    kb.button(text="👁 Профиль", callback_data="admin:users:profile")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    kb.adjust(1)
    return kb.as_markup()


# =============================================================
# ==== НАГРАДЫ: СПИСОК ========================================
# =============================================================


@router.callback_query(F.data == "admin:users:awards")
async def admin_users_awards(callback: CallbackQuery, state: FSMContext):
    """Экран со списком наград выбранного пользователя."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")

    if not internal_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    awards = await get_awards_for_user(int(internal_id))

    text = _build_awards_list_text(int(internal_id), awards)
    markup = _build_awards_kb(int(internal_id), awards)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# =============================================================
# ==== НАГРАДЫ: УДАЛЕНИЕ ======================================
# =============================================================


@router.callback_query(F.data.startswith("admin:users:award:del:"))
async def admin_users_award_delete(callback: CallbackQuery, state: FSMContext):
    """Удалить конкретную награду пользователя."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")

    if not internal_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Не удалось определить награду.", show_alert=True)
        return

    try:
        award_id = int(parts[4])
    except ValueError:
        await callback.answer("Некорректный идентификатор награды.", show_alert=True)
        return

    award = await get_award_by_id(award_id)
    if not award or int(award.get("user_id", 0) or 0) != int(internal_id):
        await callback.answer("Эта награда не найдена или принадлежит другому пользователю.", show_alert=True)
        return

    await delete_award_by_id(award_id)

    awards = await get_awards_for_user(int(internal_id))
    text = _build_awards_list_text(int(internal_id), awards, prefix="✅ Награда удалена.")
    markup = _build_awards_kb(int(internal_id), awards)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# =============================================================
# ==== НАГРАДЫ: БЕТА-ТЕСТЕР ===================================
# =============================================================


@router.callback_query(F.data == "admin:users:award:beta")
async def admin_users_award_beta(callback: CallbackQuery, state: FSMContext):
    """Выдать фиксированную ачивку «Бета‑тестер бота» по одному нажатию."""
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    data = await state.get_data()
    internal_id = data.get("selected_user_id")
    target_tg_id = data.get("selected_user_tg_id")

    if not internal_id or not target_tg_id:
        await callback.answer("Сначала найди пользователя через раздел «Пользователи».", show_alert=True)
        return

    created = await give_achievement_to_user_by_code(
        user_tg_id=int(target_tg_id),
        code="beta_tester",
        granted_by_tg_id=callback.from_user.id,
    )

    # если выдано впервые — пуш пользователю
    if created and target_tg_id:
        notify_text = (
            "🏆 <b>Новая награда!</b>\n\n"
            "Тебе выдана ачивка: <b>Бета‑тестер бота</b>.\n"
            "Спасибо, что помог(ла) тестировать GlowShot 💚"
        )
        kb_notify = InlineKeyboardBuilder()
        kb_notify.button(text="✅ Просмотрено", callback_data="award:seen")
        kb_notify.adjust(1)
        try:
            await callback.message.bot.send_message(
                chat_id=int(target_tg_id),
                text=notify_text,
                reply_markup=kb_notify.as_markup(),
                disable_notification=False,
            )
        except Exception:
            pass

    awards = await get_awards_for_user(int(internal_id))
    prefix = "✅ Ачивка «Бета‑тестер» выдана." if created else "ℹ️ У пользователя уже есть «Бета‑тестер»."
    text = _build_awards_list_text(int(internal_id), awards, prefix=prefix)
    markup = _build_awards_kb(int(internal_id), awards)

    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)

    await callback.answer()


# =============================================================
# ==== НАГРАДЫ: СОЗДАНИЕ (кастом) ==============================
# =============================================================


@router.callback_query(F.data == "admin:users:award:create")
async def admin_users_award_create(callback: CallbackQuery, state: FSMContext):
    """Попросить админа отправить текст кастомной награды."""
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
    granted_by_user_id = admin_db_user.get("id") if admin_db_user else None

    await create_custom_award_for_user(
        user_id=int(internal_id),
        title=title,
        description=description,
        icon="🏅",
        code=None,
        is_special=False,
        granted_by_user_id=granted_by_user_id,
    )

    # tg_id пользователя для пуша
    target_tg_id = data.get("selected_user_tg_id")
    if not target_tg_id:
        try:
            db_user = await get_user_by_id(int(internal_id))
            if db_user and db_user.get("tg_id"):
                target_tg_id = db_user["tg_id"]
        except Exception:
            target_tg_id = None

    if target_tg_id:
        notify_lines = ["🏆 <b>Новая награда!</b>", "", f"Тебе выдана награда: <b>{title}</b>"]
        if description:
            notify_lines.extend(["", description])
        notify_text = "\n".join(notify_lines)

        kb_notify = InlineKeyboardBuilder()
        kb_notify.button(text="✅ Просмотрено", callback_data="award:seen")
        kb_notify.adjust(1)

        try:
            await message.bot.send_message(
                chat_id=int(target_tg_id),
                text=notify_text,
                reply_markup=kb_notify.as_markup(),
                disable_notification=False,
            )
        except Exception:
            pass

    awards = await get_awards_for_user(int(internal_id))
    text = _build_awards_list_text(int(internal_id), awards, prefix="✅ Награда добавлена.")
    markup = _build_awards_kb(int(internal_id), awards)

    # Возвращаем FSM обратно в пользовательский раздел
    await state.set_state(UserAdminStates.waiting_identifier_for_profile)

    try:
        await message.bot.edit_message_text(
            chat_id=edit_chat_id,
            message_id=edit_msg_id,
            text=text,
            reply_markup=markup,
        )
    except Exception:
        try:
            await message.bot.send_message(
                chat_id=edit_chat_id,
                text=text,
                reply_markup=markup,
                disable_notification=True,
            )
        except Exception:
            pass


# =============================================================
# ==== USER SIDE: «Просмотрено» (если не реализовано) ==========
# =============================================================


@router.callback_query(F.data == "award:seen")
async def award_seen_delete_message(callback: CallbackQuery):
    """Кнопка для пользователя: удалить пуш о награде из чата."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Ок")