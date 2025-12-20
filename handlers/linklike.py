from __future__ import annotations

from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import SkipHandler, TelegramBadRequest

from utils.validation import has_links_or_usernames, has_promo_channel_invite

from database import (
    get_user_by_tg_id,
    get_user_by_id,
    get_photo_by_id,
    get_or_create_share_link_code,
    refresh_share_link_code,
    get_owner_tg_id_by_share_code,
    get_active_photo_for_owner_tg_id,
    ensure_user_minimal_row,
    add_rating_by_tg_id,
    has_user_commented,          # если нет — сделай как SELECT 1 из comments
    get_user_rating_value,       # если нет — сделай как SELECT value из ratings
    create_comment,
)

router = Router()

class LinkCommentStates(StatesGroup):
    waiting_text = State()

_BOT_USERNAME: str | None = None

async def _get_bot_username(obj: Message | CallbackQuery) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    bot = obj.bot if isinstance(obj, Message) else obj.message.bot
    me = await bot.get_me()
    _BOT_USERNAME = me.username or ""
    return _BOT_USERNAME

def _is_registered(u: dict | None) -> bool:
    if not u:
        return False
    return bool((u.get("name") or "").strip()) and (u.get("age") is not None)

def _share_kb(photo_id: int, link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={quote(link)}"))
    kb.row(InlineKeyboardButton(text="♻️ Обновить ссылку", callback_data=f"myphoto:share_refresh:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    return kb.as_markup()

def _rate_kb(owner_tg_id: int, code: str, rated_value: int | None, commented: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if rated_value is None:
        kb.row(*[InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{i}:{code}") for i in range(1, 6)])
        kb.row(*[InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{i}:{code}") for i in range(6, 11)])
    else:
        kb.row(InlineKeyboardButton(text=f"✅ Твоя оценка: {rated_value}", callback_data="lr:noop"))

    if commented:
        kb.row(InlineKeyboardButton(text="💬 Комментарий отправлен", callback_data="lr:noop"))
    else:
        kb.row(InlineKeyboardButton(text="💬 Комментарий", callback_data=f"lr:comment:{owner_tg_id}:{code}"))

    return kb.as_markup()

async def _render_link_ui(target: Message | CallbackQuery, owner_tg_id: int, code: str):
    viewer_tg_id = target.from_user.id if isinstance(target, Message) else target.from_user.id
    viewer = await ensure_user_minimal_row(viewer_tg_id, username=target.from_user.username)

    photo = await get_active_photo_for_owner_tg_id(owner_tg_id)
    if not photo:
        txt = "❌ У автора сейчас нет активной фотографии."
        if isinstance(target, Message):
            await target.answer(txt, disable_notification=True)
        else:
            await target.answer(txt, show_alert=True)
        return

    owner_user = await get_user_by_id(int(photo["user_id"]))
    owner_username = (owner_user or {}).get("username")

    rated_value = None
    commented = False
    if viewer and viewer.get("id"):
        rated_value = await get_user_rating_value(int(photo["id"]), int(viewer["id"]))
        commented = await has_user_commented(int(photo["id"]), int(viewer["id"]))

    title = (photo.get("title") or "Фотография").strip()
    caption = (
        "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
        f"<b>\"{title}\"</b>\n"
        + (f"Автор: @{owner_username}\n" if owner_username else "")
        + "\nПоставь оценку от 1 до 10 👇"
    )

    kb = _rate_kb(owner_tg_id, code, rated_value, commented)

    if isinstance(target, Message):
        await target.bot.send_photo(
            chat_id=target.chat.id,
            photo=photo["file_id"],
            caption=caption,
            reply_markup=kb,
            disable_notification=True,
        )
    else:
        try:
            if target.message.photo:
                await target.message.edit_caption(caption=caption, reply_markup=kb, parse_mode="HTML")
            else:
                await target.message.edit_text(caption, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await target.message.bot.send_photo(
                chat_id=target.message.chat.id,
                photo=photo["file_id"],
                caption=caption,
                reply_markup=kb,
                disable_notification=True,
            )

@router.callback_query(F.data.startswith("myphoto:share:"))
async def myphoto_share(callback: CallbackQuery, state: FSMContext):
    photo_id = int(callback.data.split(":")[2])
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted"):
        await callback.answer("Фотография не найдена", show_alert=True)
        return

    owner_user = await get_user_by_id(int(photo["user_id"]))
    if not owner_user or int(owner_user.get("tg_id") or 0) != int(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    code = await get_or_create_share_link_code(int(callback.from_user.id))
    bot_username = await _get_bot_username(callback)
    link = f"https://t.me/{bot_username}?start=rate_{code}"

    text = (
        "🔗 <b>Поделиться фотографией</b>\n\n"
        "Хочешь больше оценок на фотографии?\n"
        "Не хватает оценок до проходного?\n\n"
        "Ты можешь поделиться ссылкой и отправить друзьям.\n"
        "Но учти: там тоже можно поставить <b>1</b> балл 😅\n\n"
        "<b>Вот твоя ссылка — скопируй:</b>\n"
        f"<code>{link}</code>"
    )

    kb = _share_kb(photo_id, link)

    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise

    await callback.answer()

@router.callback_query(F.data.startswith("myphoto:share_refresh:"))
async def myphoto_share_refresh(callback: CallbackQuery, state: FSMContext):
    photo_id = int(callback.data.split(":")[2])
    await refresh_share_link_code(int(callback.from_user.id))
    callback.data = f"myphoto:share:{photo_id}"
    await myphoto_share(callback, state)

@router.message(CommandStart())
async def start_rate_link(message: Message, command: CommandObject, state: FSMContext):
    args = (command.args or "").strip()
    if not args.startswith("rate_"):
        raise SkipHandler

    code = args.replace("rate_", "", 1).strip()
    owner_tg_id = await get_owner_tg_id_by_share_code(code)
    if not owner_tg_id:
        await message.answer("❌ Эта ссылка не активна или устарела.", disable_notification=True)
        return

    await _render_link_ui(message, int(owner_tg_id), code)

@router.callback_query(F.data == "lr:noop")
async def lr_noop(callback: CallbackQuery):
    await callback.answer()

@router.callback_query(F.data.startswith("lr:set:"))
async def lr_set(callback: CallbackQuery, state: FSMContext):
    _, _, owner_tg_id_s, value_s, code = (callback.data or "").split(":", 4)
    owner_tg_id = int(owner_tg_id_s)
    value = int(value_s)

    photo = await get_active_photo_for_owner_tg_id(owner_tg_id)
    if not photo:
        await callback.answer("❌ У автора сейчас нет активной фотографии.", show_alert=True)
        return

    owner_user = await get_user_by_id(int(photo["user_id"]))
    if owner_user and int(owner_user.get("tg_id") or 0) == int(callback.from_user.id):
        await callback.answer("Нельзя оценивать свою фотографию.", show_alert=True)
        return

    await ensure_user_minimal_row(int(callback.from_user.id), username=callback.from_user.username)

    ok = await add_rating_by_tg_id(
        photo_id=int(photo["id"]),
        rater_tg_id=int(callback.from_user.id),
        value=value,
        source="link",
        source_code=code,
    )

    await callback.answer("✅ Оценка учтена!" if ok else "Ты уже оценивал(а) этот кадр.", show_alert=not ok)
    await _render_link_ui(callback, owner_tg_id, code)

@router.callback_query(F.data.startswith("lr:comment:"))
async def lr_comment(callback: CallbackQuery, state: FSMContext):
    _, _, owner_tg_id_s, code = (callback.data or "").split(":", 3)
    owner_tg_id = int(owner_tg_id_s)

    viewer = await ensure_user_minimal_row(int(callback.from_user.id), username=callback.from_user.username)
    if not _is_registered(viewer):
        await callback.answer("Чтобы комментировать, нужно зарегистрироваться (заполнить профиль) — /start.", show_alert=True)
        return

    photo = await get_active_photo_for_owner_tg_id(owner_tg_id)
    if not photo:
        await callback.answer("❌ У автора сейчас нет активной фотографии.", show_alert=True)
        return

    owner_user = await get_user_by_id(int(photo["user_id"]))
    if owner_user and int(owner_user.get("tg_id") or 0) == int(callback.from_user.id):
        await callback.answer("Нельзя комментировать свою фотографию.", show_alert=True)
        return

    if await has_user_commented(int(photo["id"]), int(viewer["id"])):
        await callback.answer("Ты уже оставлял(а) комментарий.", show_alert=True)
        return

    await state.set_state(LinkCommentStates.waiting_text)
    await state.update_data(lr_owner_tg_id=owner_tg_id, lr_code=code)

    await callback.answer()
    await callback.message.answer("💬 Напиши комментарий одним сообщением (без ссылок и @username).", disable_notification=True)

@router.message(LinkCommentStates.waiting_text, F.text)
async def lr_comment_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    owner_tg_id = int(data.get("lr_owner_tg_id") or 0)
    code = str(data.get("lr_code") or "")

    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not text:
        return

    if has_links_or_usernames(text) or has_promo_channel_invite(text):
        await message.bot.send_message(message.chat.id, "❌ В комментарии нельзя ссылки/@username. Попробуй ещё раз.")
        return

    viewer = await get_user_by_tg_id(int(message.from_user.id))
    if not _is_registered(viewer):
        await state.clear()
        await message.bot.send_message(message.chat.id, "❌ Чтобы комментировать, нужно зарегистрироваться — /start.")
        return

    photo = await get_active_photo_for_owner_tg_id(owner_tg_id)
    if not photo:
        await state.clear()
        await message.bot.send_message(message.chat.id, "❌ У автора сейчас нет активной фотографии.")
        return

    if await has_user_commented(int(photo["id"]), int(viewer["id"])):
        await state.clear()
        await message.bot.send_message(message.chat.id, "Ты уже оставлял(а) комментарий.")
        return

    await create_comment(int(viewer["id"]), int(photo["id"]), text, is_public=True)
    await state.clear()

    await message.bot.send_message(message.chat.id, "✅ Комментарий отправлен!", disable_notification=True)
    await _render_link_ui(message, owner_tg_id, code)