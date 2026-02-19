from __future__ import annotations

# =============================================================
# ==== АДМИНКА: ГЛАВНОЕ МЕНЮ ==================================
# =============================================================

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from utils.time import get_moscow_now

from keyboards.common import build_admin_menu, build_back_kb

from .common import (
    _ensure_user,
    _ensure_admin,
    AdminStates,
    edit_or_answer,
    ADMIN_PASSWORD,
)

# опционально: если есть функция в БД — сделаем админку постоянной
try:
    from database import set_user_admin  # type: ignore
except Exception:  # pragma: no cover
    set_user_admin = None  # type: ignore


router = Router()


async def _reset_fsm_state_only(state: FSMContext) -> None:
    """Сбрасываем только FSM-состояние, не трогая data (чтобы не терять admin_chat_id/admin_msg_id)."""
    try:
        await state.set_state(None)
    except Exception:
        # fallback (на всякий случай)
        pass


# =============================================================
# ==== UI TEXT =================================================
# =============================================================

def _build_admin_panel_text(tg_id: int) -> str:
    now = get_moscow_now()
    today = now.strftime("%d.%m.%Y")
    return (
        "<b>Админ-панель GlowShot</b>\n"
        f"ID: <code>{tg_id}</code>\n"
        f"сегодня: {today}"
    )


# =============================================================
# ==== ENTRY: /admin ==========================================
# =============================================================

@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    # всегда сбрасываем, чтобы ничего не залипало
    await _reset_fsm_state_only(state)

    user = await _ensure_user(message)
    if user is None:
        return

    # уже админ → открываем меню
    if await _ensure_admin(message):
        await edit_or_answer(
            message,
            state,
            prefix="admin",
            text=_build_admin_panel_text(message.from_user.id),
            reply_markup=build_admin_menu(),
        )
        return

    # не админ → просим пароль
    await state.set_state(AdminStates.waiting_password)
    await edit_or_answer(
        message,
        state,
        prefix="admin",
        text=(
            "🔒 <b>Доступ в админку</b>\n\n"
            "Введи пароль администратора.\n\n"
            "Если передумал — нажми «Отмена»."
        ),
        reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
    )


# =============================================================
# ==== MAIN MENU: callback ====================================
# =============================================================

@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    # ВАЖНО: при входе в главное админ-меню всегда сбрасываем состояния
    await _reset_fsm_state_only(state)
    # Чистим служебные сообщения секций, чтобы не оставлять хвосты.
    try:
        data = await state.get_data()
        current_chat_id = int(callback.message.chat.id) if callback.message and callback.message.chat else None
        current_msg_id = int(callback.message.message_id) if callback.message and callback.message.message_id else None
        section_pairs = [
            ("admin_activity_chat_id", "admin_activity_msg_id"),
            ("role_prompt_chat_id", "role_prompt_msg_id"),
            ("user_prompt_chat_id", "user_prompt_msg_id"),
            ("premium_prompt_chat_id", "premium_prompt_msg_id"),
            ("broadcast_prompt_chat_id", "broadcast_prompt_msg_id"),
            ("admin_credits_chat_id", "admin_credits_msg_id"),
            ("admin_settings_chat_id", "admin_settings_msg_id"),
            ("admin_photos_chat_id", "admin_photos_msg_id"),
        ]
        updates: dict[str, None] = {}
        for chat_key, msg_key in section_pairs:
            chat_id = data.get(chat_key)
            msg_id = data.get(msg_key)
            if not chat_id or not msg_id:
                continue
            try:
                c_chat = int(chat_id)
                c_msg = int(msg_id)
            except Exception:
                updates[chat_key] = None
                updates[msg_key] = None
                continue
            if current_chat_id == c_chat and current_msg_id == c_msg:
                continue
            try:
                await callback.message.bot.delete_message(
                    chat_id=c_chat,
                    message_id=c_msg,
                )
            except Exception:
                pass
            updates[chat_key] = None
            updates[msg_key] = None
        if updates:
            await state.update_data(**updates)
    except Exception:
        pass

    await edit_or_answer(
        callback.message,
        state,
        prefix="admin",
        text=_build_admin_panel_text(callback.from_user.id),
        reply_markup=build_admin_menu(),
    )

    await callback.answer()


# =============================================================
# ==== CANCEL PASSWORD ========================================
# =============================================================

@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    await _reset_fsm_state_only(state)
    # возвращаем в обычное меню бота (или просто закрываем админку)
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_text("Окей, отменил вход в админку.")
        except Exception:
            pass

    await callback.answer("Отменено")


# =============================================================
# ==== CHECK PASSWORD =========================================
# =============================================================

@router.message(AdminStates.waiting_password)
async def admin_check_password(message: Message, state: FSMContext):
    user = await _ensure_user(message)
    if user is None:
        await _reset_fsm_state_only(state)
        return

    pwd = (message.text or "").strip()

    # удаляем пароль из чата
    try:
        await message.delete()
    except Exception:
        pass

    if not pwd or pwd != (ADMIN_PASSWORD or ""):
        await edit_or_answer(
            message,
            state,
            prefix="admin",
            text=(
                "❌ Неверный пароль.\n\n"
                "Попробуй ещё раз или нажми «Отмена»."
            ),
            reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
        )
        return

    # пароль верный → делаем админом (если есть функция в БД)
    if set_user_admin is not None:
        try:
            await set_user_admin(message.from_user.id, True)
        except Exception:
            # если в проекте другая схема ролей — просто продолжим (доступ будет в рамках текущей сессии)
            pass

    await _reset_fsm_state_only(state)

    await edit_or_answer(
        message,
        state,
        prefix="admin",
        text=_build_admin_panel_text(message.from_user.id),
        reply_markup=build_admin_menu(),
    )
