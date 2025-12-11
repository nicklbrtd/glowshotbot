from __future__ import annotations

from typing import Optional, Union

from datetime import timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardMarkup
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

from database import (
    get_user_by_tg_id,
    set_user_admin_by_tg_id,
    get_total_users,
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
)

from keyboards.common import build_admin_menu, build_back_kb
from utils.time import get_moscow_now
from config import ADMIN_PASSWORD, MASTER_ADMIN_ID

router = Router()

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


UserEvent = Union[Message, CallbackQuery]


async def _get_admin_context(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get("admin_chat_id"), data.get("admin_msg_id")


# ================= HELPER: Edit last role prompt or answer =================
async def _edit_role_prompt_or_answer(message: Message, state: FSMContext, text: str):
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
            )
            return
        except Exception:
            pass

    await message.answer(text)


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


# ====== Поиск пользователя по ID или username ======
async def _find_user_by_identifier(identifier: str) -> Optional[dict]:
    """
    Пытается найти пользователя по числовому tg_id или по @username.
    identifier может быть '123456789' или '@username'.
    """
    if not identifier:
        return None

    identifier = identifier.strip()

    # По username
    if identifier.startswith("@"):
        username = identifier[1:].strip()
        if not username:
            return None
        user = await get_user_by_username(username)
        return user

    # По ID
    if identifier.isdigit():
        user = await get_user_by_tg_id(int(identifier))
        return user

    return None


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

    await callback.answer()


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
async def admin_roles_menu(callback: CallbackQuery):
    """
    Раздел управления ролями:
    - модераторы
    - помощники
    - поддержка.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

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
            text = f"Сейчас нет ни одного {cfg['name_single']}."
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
            "Пример: <code>123456789</code> или <code>@username</code>."
        )

        try:
            prompt = await callback.message.edit_text(text)
        except Exception:
            prompt = await callback.message.answer(text)

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
    user = await _find_user_by_identifier(identifier)

    if not user:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username."
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
        await _edit_role_prompt_or_answer(
            message,
            state,
            f"Выдаём премиум-подписку пользователю {name} — ID <code>{tg_id}</code>{extra}.\n\n"
            "На какой срок выдать премиум?\n"
            "• Напиши количество дней (например: <code>7</code> или <code>30</code>);\n"
            "• или отправь <b>навсегда</b>, чтобы выдать бессрочный премиум."
        )
        return

    # Все остальные роли — как раньше
    await cfg["set_func"](tg_id, True)
    extra = f" (@{username})" if username else ""

    await _edit_role_prompt_or_answer(
        message,
        state,
        f"Роль {cfg['name_single']} выдана пользователю {name} — ID <code>{tg_id}</code>{extra} ✅"
    )

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
            "Данные о пользователе потерялись. Попробуй выдать премиум ещё раз."
        )
        return

    raw = (message.text or "").strip().lower()

    # Бессрочный премиум
    if raw in ("навсегда", "бессрочно", "навечно", "forever", "∞"):
        await set_user_premium_status(tg_id, True, premium_until=None)

        extra = f" (@{username})" if username else ""
        await _edit_role_prompt_or_answer(
            message,
            state,
            f"Премиум-подписка выдана пользователю {name} — ID <code>{tg_id}</code>{extra} "
            f"на <b>бессрочный</b> период ✅"
        )

        await state.clear()

        # Уведомление пользователю о бессрочном премиуме
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        notif_kb = InlineKeyboardBuilder()
        notif_kb.button(text="✅ Прочитано", callback_data="profile:premium_notif_read")
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
            "или отправь <b>навсегда</b>."
        )
        return

    if days <= 0:
        await _edit_role_prompt_or_answer(
            message,
            state,
            "Срок должен быть больше нуля. Попробуй ещё раз."
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
        f"на <b>{days}</b> дн. (до {human_until}) ✅"
    )

    await state.clear()

    # Уведомление пользователю о премиуме с конкретным сроком
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Прочитано", callback_data="profile:premium_notif_read")
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
    user = await _find_user_by_identifier(identifier)

    if not user:
        await message.answer(
            "Пользователь не найден.\n\n"
            "Убедись, что он уже запускал бота, и попробуй ещё раз.\n"
            "Можно ввести числовой ID или @username."
        )
        return

    tg_id = user.get("tg_id")
    username = user.get("username")
    name = user.get("name") or "Без имени"

    await cfg["set_func"](tg_id, False)
    await state.clear()

    extra = f" (@{username})" if username else ""
    await message.answer(
        f"Роль {cfg['name_single']} снята с пользователя {name} — ID <code>{tg_id}</code>{extra} ✅"
    )


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
        "• 👥 Кол-во пользователей\n"
        "• 📈 Активные за сегодня / неделю\n"
        "• ⏱ Онлайн сейчас\n"
        "• 📬 Сколько сообщений бот обработал\n"
        "• ➕ Новые за сегодня / вчера / неделю\n\n"
        "А также выборки по пользователям:"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Кол-во пользователей", callback_data="admin:stats:total_users")
    kb.button(text="📈 Активные (скоро)", callback_data="admin:stub:stats_active")
    kb.button(text="⏱ Онлайн сейчас (скоро)", callback_data="admin:stub:stats_online")
    kb.button(text="📬 Сообщения (скоро)", callback_data="admin:stub:stats_messages")
    kb.button(text="➕ Новые (скоро)", callback_data="admin:stub:stats_new")
    kb.button(text="💎 Премиум-пользователи (скоро)", callback_data="admin:stub:stats_premium")
    kb.button(text="⛔️ В бане (скоро)", callback_data="admin:stub:stats_banned")
    kb.button(text="🏆 С неоднократными победами (скоро)", callback_data="admin:stub:stats_top")
    kb.button(text="📋 Все пользователи (скоро)", callback_data="admin:stub:stats_all")
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

    text = (
        "<b>Статистика → Кол-во пользователей</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n\n"
        "В будущем здесь появится более детальная разбивка: по дням, неделям и активности."
    )

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


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_menu(callback: CallbackQuery):
    """
    Раздел «Рассылка».
    Позже здесь появится функционал отправки сообщений всем пользователям.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Рассылка</b>\n\n"
        "Здесь будет раздел для массовых уведомлений:\n"
        "• отправка текста/фото всем пользователям;\n"
        "• тестовая рассылка самому себе;\n"
        "• выбор сегментов аудитории.\n\n"
        "Пока это заглушка, раздел в разработке."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Новая рассылка (скоро)", callback_data="admin:stub:broadcast_new")
    kb.button(text="🧪 Тестовая рассылка себе (скоро)", callback_data="admin:stub:broadcast_test")
    kb.button(text="🎯 Сегменты аудитории (скоро)", callback_data="admin:stub:broadcast_segments")
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