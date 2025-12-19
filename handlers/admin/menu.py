from __future__ import annotations

# =============================================================
# ==== АДМИНКА: МЕНЮ / ВХОД ПО /admin =========================
# =============================================================

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_PASSWORD, MASTER_ADMIN_ID
from keyboards.common import build_admin_menu, build_back_kb
from utils.time import get_moscow_now

from database import (
    get_user_by_tg_id,
    set_user_admin_by_tg_id,
)

from .common import (
    _ensure_admin,
    _ensure_user,
    UserAdminStates,
    UserAwardsStates,
    RoleStates,
    PaymentsStates,
)


router = Router()


# =============================================================
# ==== FSM СТЕЙТЫ (вход в админку) ============================
# =============================================================


class AdminStates(StatesGroup):
    waiting_password = State()


# =============================================================
# ==== ТЕКСТЫ / ХЕЛПЕРЫ =======================================
# =============================================================


def _build_admin_panel_text(user_tg_id: int) -> str:
    now = get_moscow_now()
    today = now.strftime("%d.%m.%Y")
    return (
        "⚙️ <b>Админ-панель GlowShot</b>\n"
        f"ID: <code>{user_tg_id}</code>\n"
        f"сегодня: <b>{today}</b>\n\n"
        "Выбирай раздел ниже 👇"
    )


async def _get_admin_context(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get("admin_chat_id"), data.get("admin_msg_id")


# =============================================================
# ==== ГЛАВНОЕ АДМИН-МЕНЮ =====================================
# =============================================================


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext):
    """Главный экран админ-панели."""
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = _build_admin_panel_text(callback.from_user.id)

    try:
        await callback.message.edit_text(text, reply_markup=build_admin_menu())
    except Exception:
        await callback.message.answer(text, reply_markup=build_admin_menu())

    await callback.answer()


# =============================================================
# ==== ВХОД В АДМИНКУ (/admin) =================================
# =============================================================


@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    user = await _ensure_user(message)
    if user is None:
        return

    # MASTER_ADMIN_ID попадает в админку без пароля
    if MASTER_ADMIN_ID and message.from_user.id == MASTER_ADMIN_ID:
        await set_user_admin_by_tg_id(message.from_user.id, True)
        await state.clear()
        await message.answer(
            _build_admin_panel_text(message.from_user.id),
            reply_markup=build_admin_menu(),
        )
        return

    # Уже админ — сразу в панель
    if user.get("is_admin"):
        await state.clear()
        await message.answer(
            _build_admin_panel_text(message.from_user.id),
            reply_markup=build_admin_menu(),
        )
        return

    # Иначе — просим пароль
    await state.clear()
    await state.set_state(AdminStates.waiting_password)
    await state.update_data(admin_attempts=0)

    prompt = await message.answer(
        "Введи пароль администратора:\n\n"
        "Если передумал — нажми «Отмена».",
        reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
    )

    # Запоминаем сообщение, которое будем редактировать при вводе пароля
    await state.update_data(
        admin_chat_id=prompt.chat.id,
        admin_msg_id=prompt.message_id,
    )


@router.callback_query(AdminStates.waiting_password, F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    chat_id, msg_id = await _get_admin_context(state)
    await state.clear()

    text = "Вход в админ-панель отменён."

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


@router.message(AdminStates.waiting_password, F.text)
async def admin_check_password(message: Message, state: FSMContext):
    chat_id, msg_id = await _get_admin_context(state)

    # Если контекст потерялся — сбрасываем
    if not chat_id or not msg_id:
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Сессия ввода пароля сбилась. Напиши /admin ещё раз.")
        return

    data = await state.get_data()
    attempts = int(data.get("admin_attempts", 0))

    text_in = (message.text or "").strip()

    # удаляем пароль из чата
    try:
        await message.delete()
    except Exception:
        pass

    # Неверный пароль
    if text_in != ADMIN_PASSWORD:
        attempts += 1
        await state.update_data(admin_attempts=attempts)

        if attempts >= 3:
            await state.clear()
            fail_text = (
                "Пароль несколько раз введён неверно.\n"
                "Режим входа закрыт. Напиши /admin, чтобы попробовать снова."
            )
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=fail_text,
                )
            except Exception:
                await message.answer(fail_text)
            return

        warn_text = (
            "Неверный пароль. Попробуй ещё раз.\n\n"
            f"Осталось попыток: <b>{3 - attempts}</b>"
        )
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=warn_text,
                reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
            )
        except Exception:
            await message.answer(
                warn_text,
                reply_markup=build_back_kb(callback_data="admin:cancel", text="❌ Отмена"),
            )
        return

    # Верный пароль → делаем админом
    await set_user_admin_by_tg_id(message.from_user.id, True)
    await state.clear()

    panel_text = _build_admin_panel_text(message.from_user.id)
    try:
        await message.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=panel_text,
            reply_markup=build_admin_menu(),
        )
    except Exception:
        await message.answer(panel_text, reply_markup=build_admin_menu())


@router.message(AdminStates.waiting_password)
async def admin_waiting_password_non_text(message: Message):
    # Любое не-текстовое сообщение в режиме ввода пароля — удаляем
    try:
        await message.delete()
    except Exception:
        pass