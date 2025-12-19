

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: РАССЫЛКА ======================================
# =============================================================

import asyncio
from typing import Optional

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .common import _ensure_admin, BroadcastStates
from database import(
    get_moderators,
    get_support_users,
    get_helpers,
    get_all_users_tg_ids,
    get_premium_users
)


router = Router()

# Сколько задержки между отправками (чтобы не словить flood)
_SEND_DELAY_SEC = 0.05


# =============================================================
# ==== ВХОД В РАЗДЕЛ ==========================================
# =============================================================

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_menu(callback: CallbackQuery, state: FSMContext):
    """
    Раздел «Рассылка».
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    # чистим состояние рассылки
    await state.clear()

    text = (
        "<b>Рассылка</b>\n\n"
        "Кому отправляем сообщение?\n\n"
        "• 📢 Всем пользователям\n"
        "• 💎 Только премиум-пользователям\n"
        "• 👥 Составу (модераторы, поддержка, помощники)\n"
        "• 🧪 Тестовая рассылка (только тебе)\n\n"
        "Дальше я попрошу ввести текст и покажу превью перед отправкой."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Всем пользователям", callback_data="admin:broadcast:all")
    kb.button(text="💎 Премиум-пользователям", callback_data="admin:broadcast:premium")
    kb.button(text="👥 Составу", callback_data="admin:broadcast:staff")
    kb.button(text="🧪 Тестовая (мне)", callback_data="admin:broadcast:test")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:staff")
async def admin_broadcast_staff_menu(callback: CallbackQuery, state: FSMContext):
    """
    Подраздел рассылки: кому из состава слать.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    text = (
        "<b>Рассылка составу</b>\n\n"
        "Выбери аудиторию:\n"
        "• Модераторы\n"
        "• Поддержка\n"
        "• Помощники\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🛡 Модераторам", callback_data="admin:broadcast:staff:moderators")
    kb.button(text="👨‍💻 Поддержке", callback_data="admin:broadcast:staff:support")
    kb.button(text="🤝 Помощникам", callback_data="admin:broadcast:staff:helpers")
    kb.button(text="⬅️ Назад в рассылку", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())

    await callback.answer()


@router.callback_query(
    F.data.in_(
        (
            "admin:broadcast:all",
            "admin:broadcast:premium",
            "admin:broadcast:test",
            "admin:broadcast:staff:moderators",
            "admin:broadcast:staff:support",
            "admin:broadcast:staff:helpers",
        )
    )
)
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    """
    Аудитория выбрана — просим текст рассылки.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    data_key = callback.data
    if data_key == "admin:broadcast:all":
        target = "all"
        audience = "всем пользователям"
    elif data_key == "admin:broadcast:premium":
        target = "premium"
        audience = "премиум-пользователям"
    elif data_key == "admin:broadcast:test":
        target = "test"
        audience = "только тебе (тестовая рассылка)"
    elif data_key == "admin:broadcast:staff:moderators":
        target = "moderators"
        audience = "модераторам"
    elif data_key == "admin:broadcast:staff:support":
        target = "support"
        audience = "поддержке"
    else:
        target = "helpers"
        audience = "помощникам"

    text = (
        f"<b>Рассылка {audience}</b>\n\n"
        "Отправь ОДНИМ сообщением текст, который нужно разослать.\n\n"
        "Твой исходный текст я удалю из чата, но использую для пуша.\n"
        "Перед отправкой покажу превью с кнопками «Отправить» и «Отмена».\n\n"
        "Если передумал — нажми «Назад в рассылку»."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад в рассылку", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        prompt = await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        prompt = await callback.message.answer(text, reply_markup=kb.as_markup())

    await state.set_state(BroadcastStates.waiting_text)
    await state.update_data(
        broadcast_target=target,
        broadcast_prompt_chat_id=prompt.chat.id,
        broadcast_prompt_msg_id=prompt.message_id,
    )

    await callback.answer()


@router.message(BroadcastStates.waiting_text, F.text)
async def admin_broadcast_preview(message: Message, state: FSMContext):
    """
    Админ ввёл текст рассылки — показываем превью + кнопки «Отправить» / «Отмена».
    Сообщение админа удаляем.
    """
    data = await state.get_data()
    target = data.get("broadcast_target")

    if not target:
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Сессия рассылки потерялась. Открой раздел «Рассылка» заново.")
        return

    raw_text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(broadcast_text=raw_text)

    if target == "all":
        header = "📢 <b>Сообщение для всех пользователей</b>"
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    preview_text = (
        f"{header}\n\n"
        f"{raw_text}\n\n"
        "Отправить это сообщение выбранной аудитории?"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="admin:broadcast:send")
    kb.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
    kb.adjust(1, 1)

    data = await state.get_data()
    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=preview_text,
                reply_markup=kb.as_markup(),
            )
            return
        except Exception:
            pass

    await message.answer(preview_text, reply_markup=kb.as_markup())


@router.message(BroadcastStates.waiting_text)
async def admin_broadcast_waiting_text_other(message: Message):
    """
    В режиме ввода текста рассылки — удаляем любой не-текст.
    """
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(BroadcastStates.waiting_text, F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    """
    Отмена рассылки после ввода текста — возвращаемся в меню «Рассылка».
    """
    await state.clear()
    await admin_broadcast_menu(callback, state)
    await callback.answer()


@router.callback_query(BroadcastStates.waiting_text, F.data == "admin:broadcast:send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext):
    """
    Подтверждение отправки рассылки.
    Реально шлём сообщение выбранной аудитории с кнопкой «Просмотрено».
    """
    data = await state.get_data()
    target = data.get("broadcast_target")
    text_body = data.get("broadcast_text")

    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if not target or not text_body:
        await state.clear()
        try:
            await callback.message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="Сессия рассылки потерялась. Открой раздел «Рассылка» заново.",
            )
        except Exception:
            await callback.message.answer("Сессия рассылки потерялась. Открой раздел «Рассылка» заново.")
        await callback.answer()
        return

    # Собираем аудиторию
    tg_ids: list[int] = []

    if target == "all":
        tg_ids = await get_all_users_tg_ids()
    elif target == "premium":
        users = await get_premium_users()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "test":
        tg_ids = [callback.from_user.id]
    elif target == "moderators":
        users = await get_moderators()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "support":
        users = await get_support_users()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]
    elif target == "helpers":
        users = await get_helpers()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id")]

    # Убираем дубликаты и нули
    tg_ids = list({uid for uid in tg_ids if uid})

    if target == "all":
        header = "📢 <b>Обновление GlowShot</b>"
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    send_text = f"{header}\n\n{text_body}"

    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
    notif_kb.adjust(1)
    notif_markup = notif_kb.as_markup()

    total = len(tg_ids)
    sent = 0

    for uid in tg_ids:
        try:
            await callback.message.bot.send_message(
                chat_id=uid,
                text=send_text,
                reply_markup=notif_markup,
            )
            sent += 1
        except Exception:
            # заблокировал бота / ограничил сообщения и т.п. — просто пропускаем
            continue

    await state.clear()

    summary = (
        "✅ Рассылка завершена.\n\n"
        f"Всего получателей в выборке: <b>{total}</b>.\n"
        f"Успешно отправлено (по данным Telegram) как минимум <b>{sent}</b> пользователям.\n\n"
        "Можешь вернуться в раздел «Рассылка» или в главное админ-меню."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 В раздел «Рассылка»", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    try:
        if chat_id and msg_id:
            await callback.message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=summary,
                reply_markup=kb.as_markup(),
            )
        else:
            await callback.message.answer(summary, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(summary, reply_markup=kb.as_markup())

    await callback.answer()

