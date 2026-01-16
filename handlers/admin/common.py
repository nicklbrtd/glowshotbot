


from __future__ import annotations

# =============================================================
# ==== ОБЩЕЕ ДЛЯ АДМИНКИ (helpers / types / states) ===========
# =============================================================

from typing import Optional, Union, Any

from aiogram import Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import MASTER_ADMIN_ID, ADMIN_PASSWORD, BOT_TOKEN
from database import get_user_by_tg_id
from keyboards.common import build_admin_menu, build_back_kb


# =============================================================
# ==== TYPES ===================================================
# =============================================================

UserEvent = Union[Message, CallbackQuery]


# =============================================================
# ==== SAFE UTILS =============================================
# =============================================================

def _safe_int(v: Any) -> int:
    """Безопасно привести значение к int."""
    try:
        return int(v or 0)
    except Exception:
        return 0


# =============================================================
# ==== ACCESS CHECKS ==========================================
# =============================================================

async def _get_from_user(event: UserEvent):
    """Вернуть Telegram user из Message/CallbackQuery."""
    if isinstance(event, CallbackQuery):
        return event.from_user
    return event.from_user


async def _ensure_user(event: UserEvent) -> Optional[dict]:
    """Проверить, что пользователь существует в БД (зарегистрирован)."""
    from_user = await _get_from_user(event)
    user = await get_user_by_tg_id(from_user.id)

    if user is None:
        text = "Сначала нужно зарегистрироваться через /start."
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
            except Exception:
                pass
            try:
                await event.message.answer(text)
            except Exception:
                pass
        else:
            try:
                await event.answer(text)
            except Exception:
                pass
        return None

    return user


async def _ensure_admin(event: UserEvent) -> Optional[dict]:
    """Проверить права администратора (включая MASTER_ADMIN_ID)."""
    user = await _ensure_user(event)
    if user is None:
        return None

    from_user = await _get_from_user(event)

    # MASTER_ADMIN_ID имеет доступ всегда
    if MASTER_ADMIN_ID and from_user.id == MASTER_ADMIN_ID:
        return user

    if not user.get("is_admin"):
        text = "У тебя нет прав администратора."
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(text, show_alert=True)
            except Exception:
                pass
        else:
            try:
                await event.answer(text)
            except Exception:
                pass
        return None

    return user


# =============================================================
# ==== FSM STATES =============================================
# =============================================================

class AdminStates(StatesGroup):
    """Вход в админку по паролю."""
    waiting_password = State()


class RoleStates(StatesGroup):
    """Управление ролями (добавить/удалить/премиум длительность)."""
    waiting_user_for_add = State()
    waiting_user_for_remove = State()
    waiting_premium_duration = State()
    waiting_premium_news = State()


class BroadcastStates(StatesGroup):
    """Рассылка: ждём текст, затем подтверждение."""
    waiting_text = State()


class UserAdminStates(StatesGroup):
    """Раздел «Пользователи»: ожидаем идентификатор."""
    waiting_identifier_for_profile = State()


class UserAwardsStates(StatesGroup):
    """Выдача кастомной награды в разделе пользователей."""
    waiting_custom_award_text = State()


class AchievementStates(StatesGroup):
    """Состояния для работы с ачивками/наградами (если используешь отдельный awards.py)."""
    waiting_user_for_beta = State()
    waiting_custom_user = State()
    waiting_custom_title = State()
    waiting_custom_description = State()
    waiting_custom_icon = State()
    waiting_custom_level = State()
    waiting_manage_user = State()
    waiting_edit_award_text = State()
    waiting_edit_award_icon = State()


# =============================================================
# ==== UI HELPERS =============================================
# =============================================================

async def _get_ctx_ids(state: FSMContext, prefix: str) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get(f"{prefix}_chat_id"), data.get(f"{prefix}_msg_id")


async def _set_ctx_ids(state: FSMContext, prefix: str, chat_id: int, msg_id: int) -> None:
    await state.update_data(**{f"{prefix}_chat_id": chat_id, f"{prefix}_msg_id": msg_id})


async def edit_or_answer(
    message: Message,
    state: FSMContext,
    prefix: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Держим одно «служебное» сообщение для раздела (по prefix).

    Хранит в FSM:
      {prefix}_chat_id
      {prefix}_msg_id

    Пытается edit -> если не вышло, шлёт новое и обновляет ids.
    """
    chat_id, msg_id = await _get_ctx_ids(state, prefix)

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
            # если сообщение старое/не то — попробуем просто продолжить
            pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await _set_ctx_ids(state, prefix, sent.chat.id, sent.message_id)


def kb_one_back(text: str, callback_data: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback_data)
    kb.adjust(1)
    return kb.as_markup()


# =============================================================
# ==== PRIMARY BOT (для отправки уведомлений пользователям) ===
# =============================================================

_PRIMARY_BOT: Bot | None = None


def ensure_primary_bot(bot: Bot) -> Bot:
    """
    Возвращает основной бот (BOT_TOKEN), чтобы уведомления пользователям
    всегда приходили из основного бота, даже если админка открыта в саппорт-боте.
    """
    global _PRIMARY_BOT
    try:
        current_token = bot.token  # type: ignore[attr-defined]
    except Exception:
        current_token = None

    if BOT_TOKEN and current_token != BOT_TOKEN:
        if _PRIMARY_BOT is None:
            _PRIMARY_BOT = Bot(
                BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        return _PRIMARY_BOT
    return bot


__all__ = [
    # constants
    "ADMIN_PASSWORD",
    "MASTER_ADMIN_ID",
    "build_admin_menu",
    "build_back_kb",

    # types & checks
    "UserEvent",
    "_safe_int",
    "_get_from_user",
    "_ensure_user",
    "_ensure_admin",

    # states
    "AdminStates",
    "RoleStates",
    "BroadcastStates",
    "UserAdminStates",
    "UserAwardsStates",
    "AchievementStates",

    # helpers
    "edit_or_answer",
    "kb_one_back",
]
