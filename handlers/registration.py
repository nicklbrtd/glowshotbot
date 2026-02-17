from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import upsert_user_profile, get_user_by_tg_id

from utils.validation import has_links_or_usernames, has_promo_channel_invite
from utils.time import get_moscow_today
from datetime import datetime

router = Router()


class RegistrationStates(StatesGroup):
    waiting_name = State()
    waiting_bio = State()
    # waiting_name -> waiting_bio -> finish


async def _get_reg_context(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get("reg_chat_id"), data.get("reg_msg_id")


async def _delete_message_safe(bot, chat_id: int | None, message_id: int | None) -> None:
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
    except Exception:
        pass


async def _render_reg_screen(
    *,
    bot,
    state: FSMContext,
    chat_id: int,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    fallback_msg_id: int | None = None,
) -> int | None:
    """
    Пытается обновить текущее сообщение регистрации.
    Если редактировать нельзя — отправляет новое, удаляет старое и сохраняет новый id в FSM.
    """
    data = await state.get_data()
    reg_msg_id = data.get("reg_msg_id")
    reg_chat_id = data.get("reg_chat_id") or int(chat_id)
    if reg_msg_id is None and fallback_msg_id is not None:
        reg_msg_id = int(fallback_msg_id)
        data["reg_msg_id"] = int(fallback_msg_id)
        data["reg_chat_id"] = int(chat_id)
        await state.set_data(data)

    old_msg_id = int(reg_msg_id) if reg_msg_id else None
    used_msg_id: int | None = None

    if old_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=int(reg_chat_id),
                message_id=int(old_msg_id),
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            used_msg_id = int(old_msg_id)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                used_msg_id = int(old_msg_id)
            elif (
                "message to edit not found" in msg
                or "message can't be edited" in msg
                or "message_id invalid" in msg
            ):
                used_msg_id = None
            else:
                used_msg_id = None
        except Exception:
            used_msg_id = None

    if used_msg_id is None:
        sent = await bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_notification=True,
        )
        used_msg_id = int(sent.message_id)
        if old_msg_id and old_msg_id != used_msg_id:
            await _delete_message_safe(bot, int(reg_chat_id), int(old_msg_id))

    data["reg_msg_id"] = int(used_msg_id)
    data["reg_chat_id"] = int(chat_id)
    await state.set_data(data)
    return used_msg_id


async def _finish_registration_message(*, bot, chat_id: int, msg_id: int, state: FSMContext, name: str) -> None:
    """Показывает финальный экран регистрации в том же сообщении."""
    try:
        day = get_moscow_today()
        try:
            reg_date = datetime.fromisoformat(day).strftime("%d.%m.%Y")
        except Exception:
            reg_date = day
    except Exception:
        reg_date = ""

    final_text = (
        "Регистрация завершена.\n\n"
        f"Добро пожаловать в GlowShot, {name}.\n\n"
        f"🗓 Дата регистрации: {reg_date or '—'}\n\n"
        "Теперь ты можешь:\n"
        "• Публиковать фотографии\n"
        "• Оценивать других\n"
        "• Участвовать в ежедневных итогах\n\n"
        "Совет: оцени несколько фотографий — и твои начнут крутиться чаще.\n\n"
        "Жми /start, чтобы перейти в главное меню"
    )
    await _render_reg_screen(
        bot=bot,
        state=state,
        chat_id=int(chat_id),
        text=final_text,
        reply_markup=None,
        fallback_msg_id=int(msg_id),
    )


@router.callback_query(F.data == "afterreg:menu")
async def after_registration_menu(callback: CallbackQuery, state: FSMContext):
    """Legacy callback для старых финальных сообщений с кнопкой."""
    try:
        await callback.answer("Жми /start, чтобы перейти в главное меню", show_alert=True)
    except Exception:
        pass




@router.callback_query(F.data == "auth:start")
async def registration_start(callback: CallbackQuery, state: FSMContext):

    existing = await get_user_by_tg_id(callback.from_user.id)
    if existing is not None and (existing.get("name") or "").strip():
        await callback.answer("Ты уже зарегистрирован.", show_alert=True)
        return

    await state.set_state(RegistrationStates.waiting_name)
    prev_data = await state.get_data()
    prev_reg_chat_id = prev_data.get("reg_chat_id")
    prev_reg_msg_id = prev_data.get("reg_msg_id")

    prompt = (
        "Как тебя будут видеть в GlowShot?\n\n"
        "Это имя будет отображаться под твоими фотографиями\n"
        "и в результатах партий.\n\n"
        "Можно использовать настоящее имя\n"
        "или творческий псевдоним.\n\n"
        "Напиши имя ниже 👇"
    )

    # If registration starts from a photo message (e.g., link rating result), delete it so the photo disappears
    # and continue registration in a fresh text message.
    if callback.message.photo:
        if prev_reg_chat_id and prev_reg_msg_id:
            await _delete_message_safe(callback.message.bot, int(prev_reg_chat_id), int(prev_reg_msg_id))
        try:
            await callback.message.delete()
        except Exception:
            pass
        msg = await callback.message.bot.send_message(chat_id=callback.message.chat.id, text=prompt)
        await state.update_data(reg_msg_id=msg.message_id, reg_chat_id=msg.chat.id)
    else:
        await state.update_data(
            reg_msg_id=int(callback.message.message_id),
            reg_chat_id=int(callback.message.chat.id),
        )
        await _render_reg_screen(
            bot=callback.message.bot,
            state=state,
            chat_id=int(callback.message.chat.id),
            text=prompt,
            reply_markup=None,
            fallback_msg_id=int(callback.message.message_id),
        )

    await callback.answer()


@router.message(RegistrationStates.waiting_name, F.text)
async def registration_name(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и начни регистрацию заново.",
        )
        return

    name = (message.text or "").strip()

    if has_links_or_usernames(name) or has_promo_channel_invite(name):
        try:
            await message.delete()
        except Exception:
            pass
        await _render_reg_screen(
            bot=message.bot,
            state=state,
            chat_id=int(reg_chat_id),
            text=(
                "В имени нельзя оставлять @username, ссылки на Telegram, соцсети или сайты.\n\n"
                "Напиши имя или свой псевдоним без контактов."
            ),
            reply_markup=None,
            fallback_msg_id=int(reg_msg_id),
        )
        return

    if not name:
        try:
            await message.delete()
        except Exception:
            pass
        await _render_reg_screen(
            bot=message.bot,
            state=state,
            chat_id=int(reg_chat_id),
            text="Напиши имя или псевдоним.",
            reply_markup=None,
            fallback_msg_id=int(reg_msg_id),
        )
        return

    await state.update_data(name=name)

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="bio:skip")
    kb.adjust(1)

    await state.set_state(RegistrationStates.waiting_bio)
    try:
        await message.delete()
    except Exception:
        pass
    await _render_reg_screen(
        bot=message.bot,
        state=state,
        chat_id=int(reg_chat_id),
        text=(
            "Расскажи немного о себе.\n\n"
            "Что ты снимаешь?\n"
            "На что снимаешь?\n"
            "Что тебе ближе — свет, люди или улица?\n\n"
            "Описание поможет другим понять тебя как автора.\n"
            "Если не хочется — можно пропустить."
        ),
        reply_markup=kb.as_markup(),
        fallback_msg_id=int(reg_msg_id),
    )


@router.message(RegistrationStates.waiting_bio, F.text)
async def registration_bio(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и начни регистрацию заново.",
            disable_notification=True,
        )
        return

    bio = (message.text or "").strip()

    if has_links_or_usernames(bio) or has_promo_channel_invite(bio):
        try:
            await message.delete()
        except Exception:
            pass

        kb = InlineKeyboardBuilder()
        kb.button(text="Пропустить", callback_data="bio:skip")
        kb.adjust(1)

        await _render_reg_screen(
            bot=message.bot,
            state=state,
            chat_id=int(reg_chat_id),
            text=(
                "Описание без ссылок и @.\n"
                "Или нажми «Пропустить»."
            ),
            reply_markup=kb.as_markup(),
            fallback_msg_id=int(reg_msg_id),
        )
        return

    data = await state.get_data()
    name = data.get("name")

    await state.clear()

    tg_user = message.from_user

    await upsert_user_profile(
        tg_id=tg_user.id,
        username=tg_user.username,
        name=name,
        gender=None,
        age=None,
        bio=bio or None,
    )

    try:
        await message.delete()
    except Exception:
        pass
    await _finish_registration_message(
        bot=message.bot,
        chat_id=reg_chat_id,
        msg_id=reg_msg_id,
        state=state,
        name=str(name or "друг"),
    )


@router.callback_query(RegistrationStates.waiting_bio, F.data == "bio:skip")
async def registration_bio_skip(callback: CallbackQuery, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await callback.answer(
            "Сессия регистрации сбилась. Нажми /start и попробуй ещё раз.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    name = data.get("name")

    await state.clear()

    tg_user = callback.from_user

    await upsert_user_profile(
        tg_id=tg_user.id,
        username=tg_user.username,
        name=name,
        gender=None,
        age=None,
        bio=None,
    )

    await _finish_registration_message(
        bot=callback.message.bot,
        chat_id=reg_chat_id,
        msg_id=reg_msg_id,
        state=state,
        name=str(name or "друг"),
    )
    await callback.answer()


# --- Удаляем не-текстовые сообщения во время регистрации ---


@router.message(RegistrationStates.waiting_name, ~F.text)
async def registration_name_non_text(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass


@router.message(RegistrationStates.waiting_bio, ~F.text)
async def registration_bio_non_text(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
