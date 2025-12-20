from __future__ import annotations

# =============================================================
# ==== АДМИНКА: РОЛИ ==========================================
# =============================================================
# Здесь живёт весь раздел управления ролями:
# • модераторы
# • помощники
# • поддержка
# • премиум
#
# Файл самодостаточный: не зависит от admin.py.

from datetime import datetime, timedelta
from typing import Optional, Union

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.time import get_moscow_now
from config import MASTER_ADMIN_ID

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_user_by_username,
    get_moderators,
    get_helpers,
    get_support_users,
    get_premium_users,
    set_user_moderator_by_tg_id,
    set_user_helper_by_tg_id,
    set_user_support_by_tg_id,
    set_user_premium_role_by_tg_id,
    set_user_premium_status,
)

from .common import (
    _ensure_admin,
    _ensure_user,
    RoleStates,
)

router = Router()


# =============================================================
# ==== ТИПЫ / FSM ==============================================
# =============================================================

UserEvent = Union[Message, CallbackQuery]


class RoleStates(StatesGroup):
    """FSM для управления ролями."""
    waiting_user_for_add = State()
    waiting_user_for_remove = State()


# =============================================================
# ==== ENSURE ADMIN ============================================
# =============================================================

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


# =============================================================
# ==== КЛАВИАТУРЫ / ХЕЛПЕРЫ ===================================
# =============================================================

def build_roles_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Модераторы", callback_data="admin:roles:moderator")
    kb.button(text="🤝 Помощники", callback_data="admin:roles:helper")
    kb.button(text="👨‍💻 Поддержка", callback_data="admin:roles:support")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


async def _find_user_by_identifier(identifier: str) -> dict | None:
    """ID / tg_id / @username → user dict или None."""
    ident = (identifier or "").strip()
    if not ident:
        return None

    if ident.isdigit():
        # сначала tg_id
        try:
            tg_id = int(ident)
            u = await get_user_by_tg_id(tg_id)
            if u:
                return u
        except Exception:
            pass

        # потом internal id
        try:
            internal_id = int(ident)
            u = await get_user_by_id(internal_id)
            if u:
                return u
        except Exception:
            pass

        return None

    username = ident[1:] if ident.startswith("@") else ident
    username = username.strip()
    if not username:
        return None

    try:
        return await get_user_by_username(username)
    except Exception:
        return None


async def _users_from_tg_ids(tg_ids: list[int]) -> list[dict]:
    """Подтянуть юзеров по tg_id, чтобы красиво отрисовать список."""
    out: list[dict] = []
    for tg_id in tg_ids[:200]:
        try:
            u = await get_user_by_tg_id(int(tg_id))
            if u:
                out.append(u)
        except Exception:
            continue
    return out


async def _edit_role_prompt_or_answer(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Держим одно служебное сообщение в разделе ролей."""
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
            try:
                await message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(role_prompt_chat_id=sent.chat.id, role_prompt_msg_id=sent.message_id)


def _fmt_user_line(u: dict) -> str:
    name = u.get("name") or "Без имени"
    tg_id = u.get("tg_id")
    username = u.get("username")
    label = f"@{username}" if username else name
    return f"• {label} — <code>{tg_id}</code>"


def _parse_premium_until(raw: str) -> str | None:
    """
    Принимаем:
    - '30' (дней)
    - '31.12.2025' (дата)
    - пусто -> None (бессрочно)
    Возвращаем ISO-строку или None.
    """
    s = (raw or "").strip()
    if not s:
        return None

    if s.isdigit():
        days = int(s)
        if days <= 0:
            return None
        until = get_moscow_now() + timedelta(days=days)
        return until.isoformat()

    try:
        dt = datetime.strptime(s, "%d.%m.%Y").replace(hour=23, minute=59, second=59)
        return dt.isoformat()
    except Exception:
        return None


# =============================================================
# ==== КОНФИГ РОЛЕЙ ===========================================
# =============================================================

ROLE_CONFIG = {
    "moderator": {
        "title": "Модераторы",
        "name_single": "модератора",
        "get_list": get_moderators,  # -> list[int] tg_ids
        "set_func": set_user_moderator_by_tg_id,
    },
    "helper": {
        "title": "Помощники",
        "name_single": "помощника",
        "get_list": get_helpers,  # -> list[int]
        "set_func": set_user_helper_by_tg_id,
    },
    "support": {
        "title": "Поддержка",
        "name_single": "поддержки",
        "get_list": get_support_users,  # -> list[int]
        "set_func": set_user_support_by_tg_id,
    },
}


# =============================================================
# ==== ВХОД В РАЗДЕЛ ===========================================
# =============================================================

@router.callback_query(F.data == "admin:roles")
async def admin_roles_menu(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    await state.clear()

    text = (
        "<b>Роли</b>\n\n"
        "Здесь можно управлять командами проекта и особыми статусами:\n"
        "• 🛡 Модераторы — следят за контентом и жалобами\n"
        "• 🤝 Помощники — помогают с ручными задачами, тестами и проверками\n"
        "• 👨‍💻 Поддержка — отвечают пользователям в саппорт-боте\n"
        "Выбери роль ниже."
    )

    try:
        msg = await callback.message.edit_text(text, reply_markup=build_roles_menu_kb())
    except Exception:
        msg = await callback.message.answer(text, reply_markup=build_roles_menu_kb())

    await state.update_data(role_prompt_chat_id=msg.chat.id, role_prompt_msg_id=msg.message_id)
    await callback.answer()


# =============================================================
# ==== РОУТЕР ДЕЙСТВИЙ ПО РОЛЯМ ================================
# =============================================================

@router.callback_query(F.data.startswith("admin:roles:"))
async def admin_roles_router(callback: CallbackQuery, state: FSMContext):
    admin_user = await _ensure_admin(callback)
    if admin_user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    role_code = parts[2]
    cfg = ROLE_CONFIG.get(role_code)
    if not cfg:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    # admin:roles:<role> — меню роли
    if len(parts) == 3:
        await state.clear()

        text = (
            f"<b>Роль: {cfg['title']}</b>\n\n"
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
            msg = await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            msg = await callback.message.answer(text, reply_markup=kb.as_markup())

        await state.update_data(role_prompt_chat_id=msg.chat.id, role_prompt_msg_id=msg.message_id)
        await callback.answer()
        return

    action = parts[3]

    # ======================= LIST =======================
    if action == "list":
        if role_code == "premium":
            users = await get_premium_users()
            if not users:
                text = "Сейчас нет ни одного премиум-пользователя."
            else:
                now_date = get_moscow_now().date()
                lines: list[str] = ["<b>Премиум-пользователи</b>", ""]
                for u in users[:200]:
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
                            duration = str(premium_until)
                    else:
                        duration = "бессрочно"

                    lines.append(f"• {label} — ({duration})")

                text = "\n".join(lines)
        else:
            tg_ids = await cfg["get_list"]()
            users = await _users_from_tg_ids([int(x) for x in tg_ids])
            if not users:
                text = f"Сейчас нет ни одного {cfg['name_single']}."
            else:
                lines = [f"<b>{cfg['title']}</b>", ""]
                for u in users:
                    lines.append(_fmt_user_line(u))
                text = "\n".join(lines)

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.button(text="⬅️ В роли", callback_data="admin:roles")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1)

        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

        await callback.answer()
        return

    # ======================= ADD / REMOVE =======================
    if action in ("add", "remove"):
        await state.clear()
        await state.update_data(role_code=role_code, action=action, pending_user=None)

        if action == "add":
            await state.set_state(RoleStates.waiting_user_for_add)
        else:
            await state.set_state(RoleStates.waiting_user_for_remove)

        text = (
            f"Введи ID или @username пользователя, которому нужно "
            f"{'выдать' if action == 'add' else 'снять'} роль <b>{cfg['name_single']}</b>.\n\n"
            "Пример: <code>123456789</code> или <code>@username</code>."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1)

        try:
            msg = await callback.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            msg = await callback.message.answer(text, reply_markup=kb.as_markup())

        await state.update_data(role_prompt_chat_id=msg.chat.id, role_prompt_msg_id=msg.message_id)
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


# =============================================================
# ==== FSM: ADD/REMOVE USER ====================================
# =============================================================

@router.message(RoleStates.waiting_user_for_add, F.text)
async def roles_add_user(message: Message, state: FSMContext):
    admin_user = await _ensure_admin(message)
    if admin_user is None:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    role_code = data.get("role_code")
    cfg = ROLE_CONFIG.get(role_code or "")
    if not cfg:
        await state.clear()
        await message.answer("Роль не выбрана. Открой раздел «Роли» заново.")
        return

    u = await _find_user_by_identifier(ident)
    if not u:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data=f"admin:roles:{role_code}:add")
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.adjust(1)
        await _edit_role_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    tg_id = int(u.get("tg_id"))

    # Премиум — спросим срок
    if role_code == "premium":
        await state.update_data(pending_user=u)
        await state.set_state(RoleStates.waiting_premium_until)

        text = (
            "💎 <b>Премиум</b>\n\n"
            "Введи срок премиума:\n"
            "• число дней (например <code>30</code>)\n"
            "или\n"
            "• дату окончания в формате <code>31.12.2025</code>\n\n"
            "Если отправишь пусто — сделаю бессрочно."
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="admin:roles:premium")
        kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
        kb.adjust(1)

        await _edit_role_prompt_or_answer(message, state, text, kb.as_markup())
        return

    # остальные роли
    try:
        await cfg["set_func"](tg_id, True)
    except Exception:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.adjust(1)
        await _edit_role_prompt_or_answer(message, state, "Не удалось обновить роль (ошибка БД).", kb.as_markup())
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data=f"admin:roles:{role_code}:list")
    kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
    kb.adjust(1)

    await state.clear()
    await _edit_role_prompt_or_answer(
        message,
        state,
        f"✅ Роль <b>{cfg['name_single']}</b> выдана пользователю:\n{_fmt_user_line(u)}",
        kb.as_markup(),
    )


@router.message(RoleStates.waiting_premium_until, F.text)
async def roles_premium_until(message: Message, state: FSMContext):
    admin_user = await _ensure_admin(message)
    if admin_user is None:
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    u = data.get("pending_user")
    if not u:
        await state.clear()
        await message.answer("Сессия премиума сбилась. Открой «Роли → Премиум» заново.")
        return

    tg_id = int(u.get("tg_id"))

    premium_until = _parse_premium_until(raw)

    try:
        set_user_premium_role_by_tg_id(tg_id, True)
        set_user_premium_status(tg_id, True, premium_until=premium_until)
    except Exception:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="admin:roles:premium")
        kb.adjust(1)
        await _edit_role_prompt_or_answer(message, state, "Не удалось выдать премиум (ошибка БД).", kb.as_markup())
        return

    until_text = "бессрочно"
    if premium_until:
        try:
            until_text = "до " + datetime.fromisoformat(premium_until).strftime("%d.%m.%Y")
        except Exception:
            until_text = str(premium_until)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:roles:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:roles:premium")
    kb.adjust(1)

    await state.clear()
    await _edit_role_prompt_or_answer(
        message,
        state,
        f"✅ Премиум выдан пользователю:\n{_fmt_user_line(u)}\nСрок: <b>{until_text}</b>",
        kb.as_markup(),
    )


@router.message(RoleStates.waiting_user_for_remove, F.text)
async def roles_remove_user(message: Message, state: FSMContext):
    admin_user = await _ensure_admin(message)
    if admin_user is None:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    role_code = data.get("role_code")
    cfg = ROLE_CONFIG.get(role_code or "")
    if not cfg:
        await state.clear()
        await message.answer("Роль не выбрана. Открой раздел «Роли» заново.")
        return

    u = await _find_user_by_identifier(ident)
    if not u:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data=f"admin:roles:{role_code}:remove")
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.adjust(1)
        await _edit_role_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    tg_id = int(u.get("tg_id"))

    try:
        if role_code == "premium":
            set_user_premium_role_by_tg_id(tg_id, False)
            set_user_premium_status(tg_id, False, premium_until=None)
        else:
            await cfg["set_func"](tg_id, False)
    except Exception:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
        kb.adjust(1)
        await _edit_role_prompt_or_answer(message, state, "Не удалось снять роль (ошибка БД).", kb.as_markup())
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data=f"admin:roles:{role_code}:list")
    kb.button(text="⬅️ Назад", callback_data=f"admin:roles:{role_code}")
    kb.adjust(1)

    await state.clear()
    await _edit_role_prompt_or_answer(
        message,
        state,
        f"✅ Роль снята: <b>{cfg['name_single']}</b>\nПользователь: {_fmt_user_line(u)}",
        kb.as_markup(),
    )


@router.message(RoleStates.waiting_user_for_add)
@router.message(RoleStates.waiting_user_for_remove)
@router.message(RoleStates.waiting_premium_until)
async def roles_ignore_non_text(message: Message):
    try:
        await message.delete()
    except Exception:
        pass