from __future__ import annotations

from urllib.parse import quote

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler

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
    get_user_rating_value,
    get_link_ratings_count_for_photo, 
    get_ratings_count_for_photo,
)

router = Router()

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
    # Minimal rows may exist (link rating). Registered = profile name is filled.
    if not u:
        return False
    return bool((u.get("name") or "").strip())

def _share_kb(photo_id: int, link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={quote(link)}"))
    kb.row(InlineKeyboardButton(text="♻️ Обновить ссылку", callback_data=f"myphoto:share_refresh:{photo_id}"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"myphoto:back:{photo_id}"))
    return kb.as_markup()

async def _get_share_counts(photo_id: int) -> tuple[int | None, int | None]:
    """Return (link_ratings_count, total_ratings_count). Uses best-effort DB helpers."""
    try:
        # Prefer dedicated helpers if they exist

        link_cnt = await get_link_ratings_count_for_photo(int(photo_id))
        total_cnt = await get_ratings_count_for_photo(int(photo_id))
        return int(link_cnt or 0), int(total_cnt or 0)
    except Exception:
        # Fallback: do not break share UI if counts are unavailable
        return None, None

def _rate_kb(
    *,
    owner_tg_id: int,
    code: str,
    rated_value: int | None,
    is_registered: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if rated_value is None:
        kb.row(*[InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{i}:{code}") for i in range(1, 6)])
        kb.row(*[InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{i}:{code}") for i in range(6, 11)])
        return kb.as_markup()

    if is_registered:
        kb.row(InlineKeyboardButton(text="✅ Отлично", callback_data="lr:done"))
    else:
        kb.row(InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="auth:start"))

    return kb.as_markup()


def _fmt_pub_date(photo: dict) -> str:
    raw = (photo.get("created_at") or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime

        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        if "T" in raw:
            return raw.split("T", 1)[0]
        return raw

async def _render_link_ui(target: Message | CallbackQuery, owner_tg_id: int, code: str):
    viewer_tg_id = target.from_user.id
    # minimal row for uniqueness tracking (even if not registered)
    await ensure_user_minimal_row(viewer_tg_id, username=target.from_user.username)
    viewer_full = await get_user_by_tg_id(int(viewer_tg_id))
    is_reg = _is_registered(viewer_full)

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
    if viewer_full and viewer_full.get("id"):
        rated_value = await get_user_rating_value(int(photo["id"]), int(viewer_full["id"]))

    title = (photo.get("title") or "Фотография").strip()
    pub = _fmt_pub_date(photo)
    pub_inline = f"  <i>{pub}</i>" if pub else ""

    if rated_value is None:
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"<b>\"{title}\"</b>{pub_inline}\n"
            + (f"Автор: @{owner_username}\n" if owner_username else "Автор: —\n")
            + "\nПоставь оценку от 1 до 10 👇\n"
            + "<i>Регистрация не нужна, чтобы оценить.</i>"
        )
    else:
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"<b>\"{title}\"</b>{pub_inline}\n"
            + (f"Автор: @{owner_username}\n" if owner_username else "Автор: —\n")
            + f"\n<b>Твоя оценка:</b> {rated_value}"
        )

    kb = _rate_kb(owner_tg_id=owner_tg_id, code=code, rated_value=rated_value, is_registered=is_reg)

    if rated_value is None:
        # BEFORE rating: show photo + rating keyboard
        if isinstance(target, Message):
            await target.bot.send_photo(
                chat_id=target.chat.id,
                photo=photo["file_id"],
                caption=text,
                reply_markup=kb,
                disable_notification=True,
                parse_mode="HTML",
            )
        else:
            try:
                if target.message.photo:
                    await target.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
                else:
                    await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await target.message.bot.send_photo(
                    chat_id=target.message.chat.id,
                    photo=photo["file_id"],
                    caption=text,
                    reply_markup=kb,
                    disable_notification=True,
                    parse_mode="HTML",
                )
        return

    # AFTER rating: remove the photo message (if any) and show compact TEXT result
    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except Exception:
            pass
        await target.message.bot.send_message(
            chat_id=target.message.chat.id,
            text=text,
            reply_markup=kb,
            disable_notification=True,
            parse_mode="HTML",
        )
    else:
        await target.bot.send_message(
            chat_id=target.chat.id,
            text=text,
            reply_markup=kb,
            disable_notification=True,
            parse_mode="HTML",
        )

@router.callback_query(F.data.startswith("myphoto:share:"))
async def myphoto_share(callback: CallbackQuery):
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
        "Не хватает оценок до проходного?\n\n"
        "Ты можешь поделиться ссылкой:\n"
        "Например: в профиле, в тгк или с друзьями.\n"
        "Но учти: там тоже можно поставить <b>плохую</b> оценку!\n\n"
        "✨ <b>Твоя ссылка — скопируй или жми Поделиться</b>\n"
        f"<code>{link}</code>"
    )

    link_cnt, total_cnt = await _get_share_counts(photo_id)
    if link_cnt is not None and total_cnt is not None:
        text += (
            "\n\n"
            f"🔗⭐️ Кол-во оценок по ссылке: <b>{link_cnt}</b>\n"
            f"⭐️ Всего оценок: <b>{total_cnt}</b>"
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
async def myphoto_share_refresh(callback: CallbackQuery):
    photo_id = int(callback.data.split(":")[2])

    # Issue a new active code and render it immediately in the same message
    code = await refresh_share_link_code(int(callback.from_user.id))
    bot_username = await _get_bot_username(callback)
    link = f"https://t.me/{bot_username}?start=rate_{code}"

    text = (
        "🔗 <b>Поделиться фотографией</b>\n\n"
        "Не хватает оценок до проходного?\n\n"
        "Ты можешь поделиться ссылкой:\n"
        "Например: в профиле, в тгк или с друзьями.\n"
        "Но учти: там тоже можно поставить <b>плохую</b> оценку!\n\n"
        "✨ <b>Твоя ссылка — скопируй или жми Поделиться</b>\n"
        f"<code>{link}</code>"
    )

    link_cnt, total_cnt = await _get_share_counts(photo_id)
    if link_cnt is not None and total_cnt is not None:
        text += (
            "\n\n"
            f"🔗⭐️ Кол-во оценок по ссылке: <b>{link_cnt}</b>\n"
            f"⭐️ Всего оценок: <b>{total_cnt}</b>"
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

    await callback.answer("♻️ Ссылка обновлена!")

@router.message(CommandStart())
async def start_rate_link(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args.startswith("rate_"):
        raise SkipHandler

    code = args.replace("rate_", "", 1).strip()
    owner_tg_id = await get_owner_tg_id_by_share_code(code)
    if not owner_tg_id:
        await message.answer("❌ Эта ссылка не активна или устарела.", disable_notification=True)
        return

    await _render_link_ui(message, int(owner_tg_id), code)

@router.callback_query(F.data.startswith("lr:set:"))
async def lr_set(callback: CallbackQuery):
    _, _, owner_tg_id_s, value_s, code = (callback.data or "").split(":", 4)
    owner_tg_id = int(owner_tg_id_s)
    value = int(value_s)

    photo = await get_active_photo_for_owner_tg_id(owner_tg_id)
    if not photo:
        await callback.answer("❌ У автора сейчас нет активной фотографии.", show_alert=True)
        return

    if not bool(photo.get("ratings_enabled", True)):
        await callback.answer("Автор отключил оценки для этого фото.", show_alert=True)
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

@router.callback_query(F.data == "lr:done")
async def lr_done(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
