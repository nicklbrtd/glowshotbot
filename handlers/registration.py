from aiogram import Router, F
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from database import upsert_user_profile, get_user_by_tg_id
from keyboards.common import build_main_menu

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


async def _finish_registration_message(*, bot, chat_id: int, msg_id: int) -> None:
    """Показывает финальное сообщение регистрации с кнопкой «В меню», не удаляя исходное."""
    try:
        day = get_moscow_today()
        try:
            reg_date = datetime.fromisoformat(day).strftime("%d.%m.%Y %H:%M")
        except Exception:
            reg_date = day
    except Exception:
        reg_date = ""

    lines = [
        "Готово! 🎉",
        "",
        f"Дата регистрации: {reg_date}" if reg_date else "Дата регистрации: —",
        "",
        "Дальше можно:",
        "— «Загрузить» фотографию",
        "— «Оценивать» других",
        "— Посмотреть «Профиль»",
        "",
        "Жми «В меню»",
    ]

    kb = InlineKeyboardBuilder()
    kb.button(text="В меню", callback_data="afterreg:menu")
    kb.adjust(1)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text="\n".join(lines),
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "afterreg:menu")
async def after_registration_menu(callback: CallbackQuery, state: FSMContext):
    """Оставляем финальный экран и отправляем меню отдельным сообщением."""
    try:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Вот главное меню:",
            reply_markup=build_main_menu(),
        )
    except Exception:
        # если сообщение не отправилось — ничего не ломаем
        pass

    try:
        await callback.answer("Открываю меню")
    except Exception:
        pass




@router.callback_query(F.data == "auth:start")
async def registration_start(callback: CallbackQuery, state: FSMContext):

    existing = await get_user_by_tg_id(callback.from_user.id)
    if existing is not None and (existing.get("name") or "").strip():
        await callback.answer("Ты уже зарегистрирован.", show_alert=True)
        try:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text="Ты уже в системе. Вот главное меню:",
                reply_markup=build_main_menu(),
                disable_notification=True,
            )
        except Exception:
            pass
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    await state.set_state(RegistrationStates.waiting_name)

    prompt = (
        "Как тебя здесь показывать?\n"
        "Имя или псевдоним — его увидят другие."
    )

    # If registration starts from a photo message (e.g., link rating result), delete it so the photo disappears
    # and continue registration in a fresh text message.
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        msg = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text=prompt,
        )
        await state.update_data(reg_msg_id=msg.message_id, reg_chat_id=msg.chat.id)
    else:
        await state.update_data(
            reg_msg_id=callback.message.message_id,
            reg_chat_id=callback.message.chat.id,
        )
        await callback.message.edit_text(prompt)

    await callback.answer()


@router.message(RegistrationStates.waiting_name, F.text)
async def registration_name(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и нажми «Сыыыыр», чтобы начать заново.",
        )
        return

    name = (message.text or "").strip()

    if has_links_or_usernames(name) or has_promo_channel_invite(name):
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=(
                    "В имени нельзя оставлять @username, ссылки на Telegram, соцсети или сайты.\n\n"
                    "Напиши имя или свой псевдоним <b>без контактов</b>."
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    if not name:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=(
                    "Напиши имя или псевдоним."
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await state.update_data(name=name)

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="bio:skip")
    kb.adjust(1)

    await state.set_state(RegistrationStates.waiting_bio)
    await message.delete()
    await message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Хочешь — добавь пару слов о себе (одним сообщением).\n"
            "Можно пропустить."
        ),
        reply_markup=kb.as_markup(),
    )


@router.message(RegistrationStates.waiting_bio, F.text)
async def registration_bio(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и нажми «Сыыыыр», чтобы начать заново.",
            disable_notification=True,
        )
        return

    bio = (message.text or "").strip()

    if has_links_or_usernames(bio) or has_promo_channel_invite(bio):
        await message.delete()

        kb = InlineKeyboardBuilder()
        kb.button(text="Пропустить", callback_data="bio:skip")
        kb.adjust(1)

        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=(
                    "Описание без ссылок и @.\n"
                    "Или нажми «Пропустить»."
                ),
                reply_markup=kb.as_markup(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
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

    await message.delete()
    await _finish_registration_message(
        bot=message.bot,
        chat_id=reg_chat_id,
        msg_id=reg_msg_id,
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
