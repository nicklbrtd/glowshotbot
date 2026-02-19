from __future__ import annotations

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_user_by_username,
    admin_add_credits,
    admin_add_credits_all,
    admin_remove_credits,
    admin_reset_all_credits,
)
from .common import _ensure_admin, edit_or_answer

router = Router(name="admin_credits")


class CreditsAdminStates(StatesGroup):
    waiting_grant_input = State()
    waiting_remove_input = State()
    waiting_grant_all_amount = State()


def _credits_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Отправить", callback_data="admin:credits:grant")
    kb.button(text="🌐 Выдать всем", callback_data="admin:credits:grant_all:ask")
    kb.button(text="📥 Удалить", callback_data="admin:credits:remove")
    kb.button(text="♻️ Сбросить всем", callback_data="admin:credits:reset:ask")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _back_to_credits_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:credits")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def _parse_credit_input(raw: str) -> tuple[str | None, int | None, str | None]:
    s = (raw or "").strip()
    if not s:
        return None, None, "Пустой ввод. Формат: @username 10"

    parts = s.split()
    username = parts[0].strip()
    if not username:
        return None, None, "Укажи @username."
    if username.startswith("@"):
        username = username[1:].strip()
    if not username:
        return None, None, "Укажи корректный @username."

    amount = 1
    if len(parts) > 1:
        try:
            amount = int(parts[1])
        except Exception:
            return None, None, "Количество должно быть числом."

    if amount <= 0:
        return None, None, "Количество должно быть больше нуля."

    return username, amount, None


def _parse_positive_amount(raw: str) -> tuple[int | None, str | None]:
    s = (raw or "").strip()
    if not s:
        return None, "Укажи число больше нуля."
    try:
        amount = int(s)
    except Exception:
        return None, "Количество должно быть целым числом."
    if amount <= 0:
        return None, "Количество должно быть больше нуля."
    return amount, None


def _grant_all_amount_picker_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for amount in (1, 2, 3, 5, 10, 20):
        kb.button(
            text=f"+{amount}",
            callback_data=f"admin:credits:grant_all:preset:{amount}",
        )
    kb.button(text="⬅️ Назад", callback_data="admin:credits")
    kb.adjust(3, 3, 1)
    return kb.as_markup()


def _grant_all_confirm_kb(amount: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Выдать всем +{amount}", callback_data="admin:credits:grant_all:do")
    kb.button(text="✏️ Изменить число", callback_data="admin:credits:grant_all:ask")
    kb.button(text="❌ Отмена", callback_data="admin:credits")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


async def _show_grant_all_confirm(target_message: Message, state: FSMContext, amount: int) -> None:
    await state.update_data(admin_credits_grant_all_amount=int(amount))
    await edit_or_answer(
        target_message,
        state,
        prefix="admin_credits",
        text=(
            "⚠️ Подтверди массовую выдачу\n\n"
            f"Будет начислено: +{int(amount)} credits\n"
            "Кому: всем пользователям в боте\n"
            "Уведомления пользователям отправляться не будут."
        ),
        reply_markup=_grant_all_confirm_kb(int(amount)),
    )


@router.callback_query(F.data == "admin:credits")
async def admin_credits_menu(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    try:
        await state.set_state(None)
    except Exception:
        pass
    text = (
        "💳 Кредиты\n\n"
        "• 📤 Отправить — выдать кредиты пользователю по @username\n"
        "• 🌐 Выдать всем — массово начислить credits всем пользователям\n"
        "• 📥 Удалить — убрать кредиты у пользователя по @username\n"
        "• ♻️ Сбросить всем — обнулить кредиты и show-токены у всех\n\n"
        "Формат ввода: @username 10\n"
        "Если число не указано, по умолчанию используется 1."
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=text,
        reply_markup=_credits_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:credits:grant")
async def admin_credits_grant_start(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    await state.set_state(CreditsAdminStates.waiting_grant_input)
    text = (
        "📤 Выдача кредитов\n\n"
        "Отправь @username и количество через пробел.\n"
        "Пример: @nickname 10\n"
        "Если количество не указать, будет выдан 1 кредит."
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=text,
        reply_markup=_back_to_credits_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:credits:grant_all:ask")
async def admin_credits_grant_all_ask(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    await state.set_state(CreditsAdminStates.waiting_grant_all_amount)
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=(
            "🌐 Выдать всем кредиты\n\n"
            "Выбери готовое значение кнопкой ниже\n"
            "или отправь число сообщением (например: 5).\n\n"
            "Начисление будет выполнено тихо, без уведомлений пользователям."
        ),
        reply_markup=_grant_all_amount_picker_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:credits:grant_all:preset:"))
async def admin_credits_grant_all_preset(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    raw = (callback.data or "").split(":")[-1]
    amount, err = _parse_positive_amount(raw)
    if err:
        await callback.answer("Неверное значение", show_alert=True)
        return
    if callback.message:
        await _show_grant_all_confirm(callback.message, state, int(amount))
    await callback.answer()


@router.callback_query(F.data == "admin:credits:grant_all:do")
async def admin_credits_grant_all_do(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    data = await state.get_data()
    amount = int(data.get("admin_credits_grant_all_amount") or 0)
    if amount <= 0:
        await state.set_state(CreditsAdminStates.waiting_grant_all_amount)
        await edit_or_answer(
            callback.message,
            state,
            prefix="admin_credits",
            text=(
                "⚠️ Сначала выбери количество credits для массовой выдачи.\n"
                "Можно нажать готовую кнопку или отправить число сообщением."
            ),
            reply_markup=_grant_all_amount_picker_kb(),
        )
        await callback.answer("Нет выбранного количества", show_alert=True)
        return

    result = await admin_add_credits_all(int(amount))
    try:
        await state.set_state(None)
    except Exception:
        pass
    await state.update_data(admin_credits_grant_all_amount=None)
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=(
            "✅ Массовая выдача выполнена.\n"
            f"Начислено каждому: +{result['added_per_user']} credits\n"
            f"Затронуто пользователей: {result['affected_users']}\n"
            f"Всего добавлено credits: {result['total_added']}\n"
            "Уведомления пользователям не отправлялись."
        ),
        reply_markup=_credits_menu_kb(),
    )
    await callback.answer("Готово")


@router.callback_query(F.data == "admin:credits:remove")
async def admin_credits_remove_start(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    await state.set_state(CreditsAdminStates.waiting_remove_input)
    text = (
        "📥 Списание кредитов\n\n"
        "Отправь @username и количество через пробел.\n"
        "Пример: @nickname 5\n"
        "Если количество не указать, будет списан 1 кредит.\n"
        "Кредиты не уйдут в минус."
    )
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=text,
        reply_markup=_back_to_credits_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:credits:reset:ask")
async def admin_credits_reset_ask(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сбросить всем", callback_data="admin:credits:reset:do")
    kb.button(text="❌ Отмена", callback_data="admin:credits")
    kb.adjust(1)
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=(
            "⚠️ Подтверди действие\n\n"
            "Будут обнулены кредиты и show-токены у всех пользователей."
        ),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:credits:reset:do")
async def admin_credits_reset_do(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    affected = await admin_reset_all_credits()
    try:
        await state.set_state(None)
    except Exception:
        pass
    await edit_or_answer(
        callback.message,
        state,
        prefix="admin_credits",
        text=f"✅ Сброс выполнен. Затронуто пользователей: {affected}.",
        reply_markup=_credits_menu_kb(),
    )
    await callback.answer("Готово")


@router.message(CreditsAdminStates.waiting_grant_input, F.text)
async def admin_credits_grant_input(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    username, amount, err = _parse_credit_input(raw)
    if err:
        await edit_or_answer(
            message,
            state,
            prefix="admin_credits",
            text=f"⚠️ {err}",
            reply_markup=_back_to_credits_kb(),
        )
        return

    user = await get_user_by_username(str(username))
    if not user:
        await edit_or_answer(
            message,
            state,
            prefix="admin_credits",
            text=f"Пользователь @{username} не найден.",
            reply_markup=_back_to_credits_kb(),
        )
        return

    result = await admin_add_credits(int(user["id"]), int(amount))
    uname = user.get("username")
    label = f"@{uname}" if uname else f"id:{user.get('id')}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Отправить ещё", callback_data="admin:credits:grant")
    kb.button(text="⬅️ В кредиты", callback_data="admin:credits")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await edit_or_answer(
        message,
        state,
        prefix="admin_credits",
        text=(
            f"✅ Выдано: {result['added']} кредит(ов)\n"
            f"Пользователь: {label}\n"
            f"Текущий баланс credits: {result['credits']}\n"
            f"Текущий баланс show_tokens: {result['show_tokens']}"
        ),
        reply_markup=kb.as_markup(),
    )


@router.message(CreditsAdminStates.waiting_remove_input, F.text)
async def admin_credits_remove_input(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    username, amount, err = _parse_credit_input(raw)
    if err:
        await edit_or_answer(
            message,
            state,
            prefix="admin_credits",
            text=f"⚠️ {err}",
            reply_markup=_back_to_credits_kb(),
        )
        return

    user = await get_user_by_username(str(username))
    if not user:
        await edit_or_answer(
            message,
            state,
            prefix="admin_credits",
            text=f"Пользователь @{username} не найден.",
            reply_markup=_back_to_credits_kb(),
        )
        return

    result = await admin_remove_credits(int(user["id"]), int(amount))
    uname = user.get("username")
    label = f"@{uname}" if uname else f"id:{user.get('id')}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Удалить ещё", callback_data="admin:credits:remove")
    kb.button(text="⬅️ В кредиты", callback_data="admin:credits")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await edit_or_answer(
        message,
        state,
        prefix="admin_credits",
        text=(
            f"✅ Списано: {result['removed']} кредит(ов)\n"
            f"Пользователь: {label}\n"
            f"Текущий баланс credits: {result['credits']}\n"
            f"Текущий баланс show_tokens: {result['show_tokens']}"
        ),
        reply_markup=kb.as_markup(),
    )


@router.message(CreditsAdminStates.waiting_grant_all_amount, F.text)
async def admin_credits_grant_all_amount_input(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    amount, err = _parse_positive_amount(raw)
    if err:
        await edit_or_answer(
            message,
            state,
            prefix="admin_credits",
            text=(
                f"⚠️ {err}\n\n"
                "Отправь целое число больше нуля.\n"
                "Пример: 5"
            ),
            reply_markup=_grant_all_amount_picker_kb(),
        )
        return

    await _show_grant_all_confirm(message, state, int(amount))


@router.message(CreditsAdminStates.waiting_grant_input)
@router.message(CreditsAdminStates.waiting_remove_input)
@router.message(CreditsAdminStates.waiting_grant_all_amount)
async def admin_credits_ignore_non_text(message: Message):
    try:
        await message.delete()
    except Exception:
        pass
