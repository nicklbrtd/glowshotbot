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
    get_active_photos_for_user,
    get_or_create_share_link_code,
    refresh_share_link_code,
    get_owner_tg_id_by_share_code,
    ensure_user_minimal_row,
    add_rating_by_tg_id,
    get_user_rating_value,
    get_link_ratings_count_for_photo,
    get_ratings_count_for_photo,
    is_user_premium_active,
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
    photo_id: int,
    idx: int,
    rated_value: int | None,
    is_registered: bool,
    has_next: bool,
    is_rateable: bool,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if is_rateable and rated_value is None:
        kb.row(
            *[
                InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{photo_id}:{idx}:{i}:{code}")
                for i in range(1, 6)
            ]
        )
        kb.row(
            *[
                InlineKeyboardButton(text=str(i), callback_data=f"lr:set:{owner_tg_id}:{photo_id}:{idx}:{i}:{code}")
                for i in range(6, 11)
            ]
        )
        return kb.as_markup()

    # Уже оценено или нельзя оценивать — даём кнопку дальше/регистрации/меню
    if has_next:
        kb.row(InlineKeyboardButton(text="➡️ Далее", callback_data=f"lr:next:{owner_tg_id}:{idx+1}:{code}"))
    else:
        if is_registered:
            kb.row(InlineKeyboardButton(text="🏠 В меню", callback_data="menu:back"))
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


async def _get_active_photos_for_share(owner_tg_id: int) -> list[dict]:
    """Вернёт список активных фото автора (до 2 шт.), отсортированных по дате создания (старая -> новая)."""
    owner = await get_user_by_tg_id(int(owner_tg_id))
    if not owner:
        return []
    premium_active = False
    try:
        premium_active = await is_user_premium_active(int(owner_tg_id))
    except Exception:
        premium_active = False
    try:
        photos = await get_active_photos_for_user(int(owner["id"]), limit=2)
        photos = sorted(photos, key=lambda p: (p.get("created_at") or ""))
    except Exception:
        photos = []
    photos = photos[:2]
    if premium_active:
        return photos
    # Без премиума — оставляем только одну (самую раннюю в списке)
    return photos[:1]

async def _render_link_photo(target: Message | CallbackQuery, owner_tg_id: int, code: str, idx: int = 0):
    viewer_tg_id = target.from_user.id
    await ensure_user_minimal_row(viewer_tg_id, username=target.from_user.username)
    viewer_full = await get_user_by_tg_id(int(viewer_tg_id))
    is_reg = _is_registered(viewer_full)

    photos = await _get_active_photos_for_share(owner_tg_id)
    if not photos:
        txt = "❌ У автора сейчас нет активных фотографий."
        if isinstance(target, Message):
            await target.answer(txt, disable_notification=True)
        else:
            await target.answer(txt, show_alert=True)
        return

    idx = max(0, min(idx, len(photos) - 1))
    photo = photos[idx]
    has_next = idx < len(photos) - 1

    owner_user = await get_user_by_id(int(photo["user_id"]))
    owner_username = (owner_user or {}).get("username")

    rated_value = None
    viewer_user_id = viewer_full.get("id") if viewer_full else None
    if viewer_user_id:
        rated_value = await get_user_rating_value(int(photo["id"]), int(viewer_user_id))

    title = (photo.get("title") or "Фотография").strip()
    pub = _fmt_pub_date(photo)
    pub_inline = f"  <i>{pub}</i>" if pub else ""

    is_rateable = bool(photo.get("ratings_enabled", True))
    if not is_rateable:
        title_line = f"<b>\"{title}\"</b>{pub_inline}"
        author_line = (f"Автор: @{owner_username}\n" if owner_username else "Автор: —\n")
        text = (
            "🔗⭐️ <b>Оценка по ссылке</b>\n\n"
            f"{title_line}\n"
            f"{author_line}"
            "\n🚫 Эта фотография недоступна для оценок.\n"
        )
    elif rated_value is None:
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

    kb = _rate_kb(
        owner_tg_id=owner_tg_id,
        code=code,
        photo_id=int(photo["id"]),
        idx=idx,
        rated_value=rated_value,
        is_registered=is_reg,
        has_next=has_next,
        is_rateable=is_rateable,
    )

    if not is_rateable:
        # Показать фото с подписью (без оценки), либо текст если не можем отредактировать
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

    if rated_value is None and is_rateable:
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

    # AFTER rating или если оценка запрещена: удаляем фото и показываем компактный текст
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
    # Если фото заблокировано из-за отсутствия премиума (вторая активная) — не даём делиться
    premium_active = False
    try:
        premium_active = await is_user_premium_active(int(callback.from_user.id))
    except Exception:
        premium_active = False
    if not premium_active:
        try:
            owner_photos = await get_active_photos_for_user(int(owner_user["id"]), limit=2)
            owner_photos = sorted(owner_photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
            if len(owner_photos) > 1 and int(photo_id) != int(owner_photos[0]["id"]):
                await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
                return
        except Exception:
            pass

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

    owner_user = await get_user_by_tg_id(int(callback.from_user.id))
    premium_active = False
    try:
        premium_active = await is_user_premium_active(int(callback.from_user.id))
    except Exception:
        premium_active = False
    if not premium_active and owner_user:
        try:
            owner_photos = await get_active_photos_for_user(int(owner_user["id"]), limit=2)
            owner_photos = sorted(owner_photos, key=lambda p: (p.get("created_at") or ""), reverse=True)
            if len(owner_photos) > 1 and int(photo_id) != int(owner_photos[0]["id"]):
                await callback.answer("Доступно с GlowShot Premium 💎.", show_alert=True)
                return
        except Exception:
            pass

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

    await _render_link_photo(message, int(owner_tg_id), code, idx=0)

@router.callback_query(F.data.startswith("lr:set:"))
async def lr_set(callback: CallbackQuery):
    _, _, owner_tg_id_s, photo_id_s, idx_s, value_s, code = (callback.data or "").split(":", 6)
    owner_tg_id = int(owner_tg_id_s)
    photo_id = int(photo_id_s)
    idx = int(idx_s)
    value = int(value_s)

    owner_user = await get_user_by_tg_id(owner_tg_id)
    photo = await get_photo_by_id(photo_id)
    if not photo or photo.get("is_deleted") or photo.get("moderation_status") != "active":
        await callback.answer("❌ Фото недоступно.", show_alert=True)
        return
    if owner_user and int(photo.get("user_id") or 0) != int(owner_user.get("id") or 0):
        await callback.answer("❌ Фото недоступно.", show_alert=True)
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
    # Показать эту же карточку с итогом/кнопкой далее
    await _render_link_photo(callback, owner_tg_id, code, idx=idx)


@router.callback_query(F.data.startswith("lr:next:"))
async def lr_next(callback: CallbackQuery):
    _, _, owner_tg_id_s, idx_s, code = (callback.data or "").split(":", 4)
    owner_tg_id = int(owner_tg_id_s)
    idx = int(idx_s)
    await _render_link_photo(callback, owner_tg_id, code, idx=idx)

@router.callback_query(F.data == "lr:done")
async def lr_done(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
