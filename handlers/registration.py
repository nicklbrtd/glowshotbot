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
    waiting_gender = State()
    waiting_age = State()
    waiting_bio = State()
    # язык выбираем до имени, но остаёмся в waiting_name


async def _get_reg_context(state: FSMContext) -> tuple[int | None, int | None]:
    data = await state.get_data()
    return data.get("reg_chat_id"), data.get("reg_msg_id")


async def _finish_registration_message(*, bot, chat_id: int, msg_id: int) -> None:
    """Показывает финальное сообщение регистрации с кнопкой «В меню», не удаляя исходное."""
    try:
        day = get_moscow_today()
        try:
            reg_date = datetime.fromisoformat(day).strftime("%d.%m.%Y")
        except Exception:
            reg_date = day
    except Exception:
        reg_date = ""

    lines = [
        "Регистрация завершена 🎉",
        "",
        f"Дата регистрации: {reg_date}" if reg_date else "Дата регистрации: —",
        "Теперь ты можешь пользоваться этим ботом.",
        "Все данные — имя, пол, возраст и описание — можно менять позже в разделе «Профиль».",
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
    """Удаляем клавиатуру и подсказку, отправляем меню отдельным сообщением."""
    try:
        text = (callback.message.text or "").replace("Жми «В меню»", "").strip()
        await callback.message.edit_text(text)
    except Exception:
        pass

    try:
        await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Вот главное меню:",
            reply_markup=build_main_menu(),
        )
    except Exception:
        pass

    await callback.answer("Открываю меню")




@router.callback_query(F.data == "auth:start")
async def registration_start(callback: CallbackQuery, state: FSMContext):

    existing = await get_user_by_tg_id(callback.from_user.id)
    if existing is not None and (existing.get("name") or "").strip():
        await callback.answer("Ты уже зарегистрирован.", show_alert=True)
        try:
            await callback.message.edit_text(
                "Ты уже в системе. Вот главное меню:",
                reply_markup=build_main_menu(),
            )
        except TelegramBadRequest:
            # If it's a photo message, edit caption instead
            await callback.message.edit_caption(
                caption="Ты уже в системе. Вот главное меню:",
                reply_markup=build_main_menu(),
            )
        return

    await state.set_state(RegistrationStates.waiting_name)

    # If registration starts from a photo message (e.g., link rating result), delete it so the photo disappears
    # and continue registration in a fresh text message.
    if callback.message.photo:
        try:
            await callback.message.delete()
        except Exception:
            pass
        msg = await callback.message.bot.send_message(
            chat_id=callback.message.chat.id,
            text="Выбери язык для регистрации и дальнейшей работы:",
            reply_markup=_build_lang_kb(),
        )
        await state.update_data(reg_msg_id=msg.message_id, reg_chat_id=msg.chat.id, reg_lang="ru")
    else:
        await state.update_data(
            reg_msg_id=callback.message.message_id,
            reg_chat_id=callback.message.chat.id,
            reg_lang="ru",
        )
        await callback.message.edit_text(
            "Выбери язык для регистрации и дальнейшей работы:",
            reply_markup=_build_lang_kb(),
        )

    await callback.answer()


def _build_lang_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Русский", callback_data="reg:lang:ru")
    kb.button(text="English", callback_data="reg:lang:en")
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(RegistrationStates.waiting_name, F.data.startswith("reg:lang:"))
async def registration_lang(callback: CallbackQuery, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await callback.answer("Сессия регистрации сбилась. Нажми /start и попробуй снова.", show_alert=True)
        return

    lang = (callback.data or "reg:lang:ru").split(":")[-1]
    if lang not in ("ru", "en"):
        lang = "ru"
    await state.update_data(reg_lang=lang)

    await callback.message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Некоторые вопросы для статистики, почти всё можно пропустить.\n\n"
            "Как тебя указывать? Имя или псевдоним — его увидят другие пользователи.\n\n"
            "Осталось всего пару шагов."
        ),
    )
    await callback.answer("Язык выбран")


@router.message(RegistrationStates.waiting_name, F.text)
async def registration_name(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и нажми «Зарегистрироваться», чтобы начать заново.",
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
                    "Имя не может быть пустым.\n\n"
                    "Напиши имя или свой псевдоним!"
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await state.update_data(name=name)

    kb = InlineKeyboardBuilder()
    kb.button(text="Парень 🚹", callback_data="gender:male")
    kb.button(text="Девушка 🚺", callback_data="gender:female")
    kb.button(text="Не важно", callback_data="gender:na")
    kb.adjust(2, 1)

    await state.set_state(RegistrationStates.waiting_gender)
    await message.delete()
    await message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Выбери свой пол.\n"
            "Если не хочешь уточнять — жми «Не важно». Уже почти готово."
        ),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(RegistrationStates.waiting_gender, F.data.startswith("gender:"))
async def registration_gender(callback: CallbackQuery, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await callback.answer(
            "Сессия регистрации сбилась. Нажми /start и попробуй ещё раз.",
            show_alert=True,
        )
        return

    gender_code = callback.data.split(":", 1)[1]
    mapping = {
        "male": "Парень",
        "female": "Девушка",
        "na": "Не важно",
    }
    gender = mapping.get(gender_code, "Не важно")
    await state.update_data(gender=gender, gender_code=gender_code)

    await state.set_state(RegistrationStates.waiting_age)

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="age:skip")
    kb.adjust(1)

    await callback.message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Сколько тебе лет?\n"
            "Напиши число (только цифры) или нажми «Пропустить».\n"
            "Последний шаг будет совсем коротким."
        ),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(RegistrationStates.waiting_age, F.data == "age:skip")
async def registration_age_skip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(age=None)
    await state.set_state(RegistrationStates.waiting_bio)

    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await callback.answer(
            "Сессия регистрации сбилась. Нажми /start и попробуй ещё раз.",
            show_alert=True,
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="bio:skip")
    kb.adjust(1)

    await callback.message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Теперь можешь добавить описание.\n"
            "Напиши это <b>одним</b> сообщением или нажми «Пропустить»."
        ),
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.message(RegistrationStates.waiting_age, F.text)
async def registration_age_value(message: Message, state: FSMContext):
    reg_chat_id, reg_msg_id = await _get_reg_context(state)
    if not reg_chat_id or not reg_msg_id:
        await state.clear()
        await message.answer(
            "Сессия регистрации сбилась.\n\n"
            "Отправь /start и нажми «Зарегистрироваться», чтобы начать заново.",
        )
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=(
                    "Возраст должен быть числом.\n\n"
                    "Напиши только цифры, например: <code>18</code>.\n"
                    "Или нажми кнопку «Пропустить»."
                ),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    age = int(text)
    if age < 6  or age > 90:
        await message.delete()
        data = await state.get_data()
        gender_code = data.get("gender_code", "na")
        if gender_code == "male":
            unsure = "Ты уверен, что это твой реальный возраст?"
        elif gender_code == "female":
            unsure = "Ты уверена, что это твой реальный возраст?"
        else:
            unsure = "Похоже, возраст указан необычно. Проверь, не опечатался?"
        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=f"{unsure}\nНапиши реальный возраст или нажми «Пропустить».",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    await state.update_data(age=age)
    await state.set_state(RegistrationStates.waiting_bio)
    await message.delete()

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="bio:skip")
    kb.adjust(1)

    await message.bot.edit_message_text(
        chat_id=reg_chat_id,
        message_id=reg_msg_id,
        text=(
            "Последний шаг: можешь добавить описание для профиля.\n"
            "Напиши это одним сообщением или нажми «Пропустить»."
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
            "Отправь /start и нажми «Зарегистрироваться», чтобы начать заново.",
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
                    "Добавь описание для профиля одним сообщением\n"
                    "или нажми «Пропустить»."
                ),
                reply_markup=kb.as_markup(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="bio:skip")
    kb.adjust(1)

    if not bio:
        await message.delete()
        try:
            await message.bot.edit_message_text(
                chat_id=reg_chat_id,
                message_id=reg_msg_id,
                text=(
                    "Описание пустое. Напиши хотя бы пару слов про себя\n"
                    "или нажми «Пропустить»."
                ),
                reply_markup=kb.as_markup(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise
        return

    data = await state.get_data()
    name = data.get("name")
    gender = data.get("gender")
    age = data.get("age")

    await state.clear()

    tg_user = message.from_user

    await upsert_user_profile(
        tg_id=tg_user.id,
        username=tg_user.username,
        name=name,
        gender=gender,
        age=age,
        bio=bio,
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
    gender = data.get("gender")
    age = data.get("age")

    await state.clear()

    tg_user = callback.from_user

    await upsert_user_profile(
        tg_id=tg_user.id,
        username=tg_user.username,
        name=name,
        gender=gender,
        age=age,
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


@router.message(RegistrationStates.waiting_age, ~F.text)
async def registration_age_non_text(message: Message, state: FSMContext):
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
