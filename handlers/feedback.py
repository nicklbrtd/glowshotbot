from __future__ import annotations

import html
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config import FEEDBACK_CHAT_ID
from database import (
    get_user_by_tg_id,
    create_feedback_idea,
    list_feedback_ideas_by_tg_id,
    set_feedback_status,
    get_feedback_idea_by_id,
)
from utils.antispam import should_throttle
from utils.time import get_moscow_now

router = Router(name="feedback")


class FeedbackStates(StatesGroup):
    waiting_input = State()
    preview = State()


class FeedbackAdminStates(StatesGroup):
    waiting_cancel_reason = State()


STATUS_META = {
    "new": ("⚪️", "Новая"),
    "accepted": ("🟣", "Принято"),
    "in_progress": ("🟠", "Начинаю"),
    "done": ("🟢", "Сделал"),
    "rejected": ("🔴", "Отмена"),
}

ALLOWED_IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".bmp",
    ".tiff",
)


def _status_label(status: str) -> tuple[str, str]:
    return STATUS_META.get(status, ("⚪️", "Новая"))


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def _get_screen_ids(data: dict) -> tuple[int | None, int | None]:
    return data.get("feedback_msg_id"), data.get("feedback_chat_id")


async def _edit_screen(
    bot,
    state: FSMContext,
    text: str,
    kb: InlineKeyboardMarkup,
    parse_mode: str = "HTML",
) -> bool:
    data = await state.get_data()
    msg_id, chat_id = _get_screen_ids(data)
    if msg_id and chat_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=kb,
                parse_mode=parse_mode,
            )
            return True
        except Exception:
            return False
    return False


def _kb_main() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Предложить идею", callback_data="feedback:new")
    kb.button(text="🔄 Обновить", callback_data="feedback:refresh")
    kb.button(text="✖️ Закрыть", callback_data="feedback:close")
    kb.adjust(1)
    return kb.as_markup()


def _kb_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="feedback:home")
    kb.adjust(1)
    return kb.as_markup()


def _kb_preview() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="feedback:send")
    kb.button(text="✏️ Изменить", callback_data="feedback:edit")
    kb.button(text="⬅️ Назад", callback_data="feedback:home")
    kb.adjust(1)
    return kb.as_markup()


def _kb_admin(idea_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🟣 Принято", callback_data=f"feedback:status:{idea_id}:accepted")
    kb.button(text="🟠 Начинаю", callback_data=f"feedback:status:{idea_id}:in_progress")
    kb.button(text="🟢 Сделал", callback_data=f"feedback:status:{idea_id}:done")
    kb.button(text="🔴 Отмена", callback_data=f"feedback:status:{idea_id}:rejected")
    kb.adjust(2, 2)
    return kb.as_markup()


def _format_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value or "")


def _build_main_text(ideas: list[dict]) -> str:
    header = (
        "💡 <b>Идеи для GlowShot</b>\n\n"
        "Предложи улучшение, фичу или изменение — всё читаем.\n"
        "За сильные и реально полезные идеи можем дать премиум на N время (решение админа).\n\n"
    )

    if not ideas:
        return header + "У тебя пока нет идей. Нажми «Предложить идею»."

    lines = ["<b>Твои идеи:</b>"]
    for row in ideas:
        emoji, label = _status_label(str(row.get("status") or "new"))
        code = row.get("idea_code") or "—"
        date_str = _format_date(row.get("created_at"))
        line = f"• {emoji} <b>#{code}</b> — {label}"
        if date_str:
            line += f" ({date_str})"
        lines.append(line)

        reason = (row.get("cancel_reason") or "").strip()
        if str(row.get("status")) == "rejected" and reason:
            lines.append(f"   <i>Причина:</i> {html.escape(reason, quote=False)}")

    return header + "\n".join(lines)


def _is_allowed_document(doc) -> bool:
    mime = (doc.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    name = (doc.file_name or "").lower()
    return any(name.endswith(ext) for ext in ALLOWED_IMAGE_EXTS)


def _extract_text(message: Message) -> str:
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""


def _build_preview_text(text: str, attachment: dict | None) -> str:
    safe_text = html.escape(text or "—", quote=False)
    if attachment:
        kind = attachment.get("type")
        attach_line = "Вложения: 1 фото" if kind == "photo" else "Вложения: 1 файл-изображение"
    else:
        attach_line = "Вложения: нет"
    return (
        "📝 <b>Предпросмотр идеи</b>\n\n"
        f"{safe_text}\n\n"
        f"{attach_line}\n\n"
        "Если всё ок — жми «Отправить»."
    )


def _build_admin_text(idea_code: int, username: str | None, created_at, text: str) -> str:
    date_str = _format_date(created_at) or _format_date(get_moscow_now())
    uname = f"@{username}" if username else "—"
    return (
        f"Номер идеи: #{idea_code}\n"
        f"От кого: {uname}\n"
        f"Дата: {date_str}\n"
        "Текст:\n"
        f"{html.escape(text or '—', quote=False)}"
    )


async def _render_home(*, state: FSMContext, bot, tg_id: int) -> None:
    ideas = await list_feedback_ideas_by_tg_id(int(tg_id), limit=20)
    text = _build_main_text(ideas)
    await _edit_screen(bot, state, text, _kb_main())


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    if should_throttle(message.from_user.id, "feedback:cmd", 1.2):
        return

    user = await get_user_by_tg_id(message.from_user.id)
    if user is None:
        sent = await message.answer(
            "Чтобы предложить идею, сначала зарегистрируйся в боте через /start.",
            disable_notification=True,
        )
        await _safe_delete(message)
        return

    await state.clear()
    ideas = await list_feedback_ideas_by_tg_id(int(message.from_user.id), limit=20)
    text = _build_main_text(ideas)
    sent = await message.answer(
        text,
        reply_markup=_kb_main(),
        parse_mode="HTML",
        disable_notification=True,
    )
    await state.update_data(feedback_msg_id=sent.message_id, feedback_chat_id=sent.chat.id)
    await _safe_delete(message)


@router.callback_query(F.data == "feedback:refresh")
async def feedback_refresh(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "feedback:refresh", 0.8):
        await callback.answer()
        return
    await state.update_data(feedback_msg_id=callback.message.message_id, feedback_chat_id=callback.message.chat.id)
    await _render_home(state=state, bot=callback.message.bot, tg_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "feedback:home")
async def feedback_home(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "feedback:home", 0.8):
        await callback.answer()
        return
    await state.clear()
    await state.update_data(feedback_msg_id=callback.message.message_id, feedback_chat_id=callback.message.chat.id)
    await _render_home(state=state, bot=callback.message.bot, tg_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "feedback:close")
async def feedback_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "feedback:new")
async def feedback_new(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "feedback:new", 0.8):
        await callback.answer()
        return

    await state.set_state(FeedbackStates.waiting_input)
    await state.update_data(
        feedback_text=None,
        feedback_attachment=None,
        feedback_msg_id=callback.message.message_id,
        feedback_chat_id=callback.message.chat.id,
    )
    text = (
        "✍️ Напиши свою идею одним сообщением.\n"
        "Можно прикрепить <b>одно</b> фото/файл-изображение (jpeg/png/heic).\n"
        "После отправки покажу предпросмотр."
    )
    await _edit_screen(callback.message.bot, state, text, _kb_back())
    await callback.answer()


@router.message(FeedbackStates.waiting_input, F.photo)
async def feedback_input_photo(message: Message, state: FSMContext):
    text = _extract_text(message)
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        await _safe_delete(message)
        return
    attachment = {"type": "photo", "file_id": photo.file_id}

    await state.update_data(feedback_text=text or "", feedback_attachment=attachment)
    await state.set_state(FeedbackStates.preview)

    preview = _build_preview_text(text or "", attachment)
    ok = await _edit_screen(message.bot, state, preview, _kb_preview())
    if not ok:
        sent = await message.answer(preview, reply_markup=_kb_preview(), parse_mode="HTML", disable_notification=True)
        await state.update_data(feedback_msg_id=sent.message_id, feedback_chat_id=sent.chat.id)
    await _safe_delete(message)


@router.message(FeedbackStates.waiting_input, F.document)
async def feedback_input_document(message: Message, state: FSMContext):
    doc = message.document
    if doc is None or not _is_allowed_document(doc):
        text = (
            "Нужен файл-изображение (jpeg/png/heic и т.п.).\n"
            "Отправь как файл или прикрепи фото.\n\n"
            "Попробуй ещё раз."
        )
        await _edit_screen(message.bot, state, text, _kb_back())
        await _safe_delete(message)
        return

    text = _extract_text(message)
    attachment = {"type": "document", "file_id": doc.file_id}

    await state.update_data(feedback_text=text or "", feedback_attachment=attachment)
    await state.set_state(FeedbackStates.preview)

    preview = _build_preview_text(text or "", attachment)
    ok = await _edit_screen(message.bot, state, preview, _kb_preview())
    if not ok:
        sent = await message.answer(preview, reply_markup=_kb_preview(), parse_mode="HTML", disable_notification=True)
        await state.update_data(feedback_msg_id=sent.message_id, feedback_chat_id=sent.chat.id)
    await _safe_delete(message)


@router.message(FeedbackStates.waiting_input, F.text)
async def feedback_input_text(message: Message, state: FSMContext):
    text = _extract_text(message)
    if not text:
        await _safe_delete(message)
        return

    await state.update_data(feedback_text=text, feedback_attachment=None)
    await state.set_state(FeedbackStates.preview)

    preview = _build_preview_text(text, None)
    ok = await _edit_screen(message.bot, state, preview, _kb_preview())
    if not ok:
        sent = await message.answer(preview, reply_markup=_kb_preview(), parse_mode="HTML", disable_notification=True)
        await state.update_data(feedback_msg_id=sent.message_id, feedback_chat_id=sent.chat.id)
    await _safe_delete(message)


@router.message(FeedbackStates.waiting_input)
async def feedback_input_other(message: Message):
    await _safe_delete(message)


@router.callback_query(F.data == "feedback:edit")
async def feedback_edit(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "feedback:edit", 0.6):
        await callback.answer()
        return
    await state.set_state(FeedbackStates.waiting_input)
    await state.update_data(feedback_text=None, feedback_attachment=None)
    text = (
        "✍️ Напиши идею заново одним сообщением.\n"
        "Можно прикрепить одно фото/файл-изображение."
    )
    await _edit_screen(callback.message.bot, state, text, _kb_back())
    await callback.answer()


@router.callback_query(F.data == "feedback:send")
async def feedback_send(callback: CallbackQuery, state: FSMContext):
    if should_throttle(callback.from_user.id, "feedback:send", 1.0):
        await callback.answer()
        return

    data = await state.get_data()
    text = (data.get("feedback_text") or "").strip()
    attachment = data.get("feedback_attachment")

    if not text and not attachment:
        await callback.answer("Сначала напиши идею.", show_alert=True)
        return

    # Caption length guard
    if attachment:
        preview_text = _build_admin_text(999999, callback.from_user.username, get_moscow_now(), text)
        if len(preview_text) > 1000:
            await callback.answer("Слишком длинный текст для вложения. Сократи идею.", show_alert=True)
            return

    created = await create_feedback_idea(
        tg_id=int(callback.from_user.id),
        username=callback.from_user.username,
        text=text,
        attachments=[attachment] if attachment else [],
    )
    idea_id = int(created["id"])
    idea_code = int(created["idea_code"])
    created_at = created.get("created_at")

    admin_text = _build_admin_text(idea_code, callback.from_user.username, created_at, text)
    admin_kb = _kb_admin(idea_id)

    if FEEDBACK_CHAT_ID is not None:
        try:
            if attachment:
                if attachment.get("type") == "photo":
                    await callback.message.bot.send_photo(
                        chat_id=FEEDBACK_CHAT_ID,
                        photo=attachment.get("file_id"),
                        caption=admin_text,
                        reply_markup=admin_kb,
                        parse_mode="HTML",
                    )
                else:
                    await callback.message.bot.send_document(
                        chat_id=FEEDBACK_CHAT_ID,
                        document=attachment.get("file_id"),
                        caption=admin_text,
                        reply_markup=admin_kb,
                        parse_mode="HTML",
                    )
            else:
                await callback.message.bot.send_message(
                    chat_id=FEEDBACK_CHAT_ID,
                    text=admin_text,
                    reply_markup=admin_kb,
                    parse_mode="HTML",
                )
        except Exception:
            pass

    await state.clear()
    await state.update_data(feedback_msg_id=callback.message.message_id, feedback_chat_id=callback.message.chat.id)
    await _render_home(state=state, bot=callback.message.bot, tg_id=callback.from_user.id)
    await callback.answer("Идея отправлена!")


@router.callback_query(F.data.startswith("feedback:status:"))
async def feedback_status(callback: CallbackQuery, state: FSMContext):
    if FEEDBACK_CHAT_ID is not None and callback.message.chat.id != FEEDBACK_CHAT_ID:
        await callback.answer()
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer()
        return

    idea_id = int(parts[2])
    status = parts[3]

    if status == "rejected":
        await state.set_state(FeedbackAdminStates.waiting_cancel_reason)
        await state.update_data(feedback_cancel_idea_id=idea_id, feedback_cancel_prompt_id=None)
        idea = await get_feedback_idea_by_id(idea_id)
        code = idea.get("idea_code") if idea else "—"
        prompt = await callback.message.reply(
            f"Напиши причину отказа для идеи #{code}. Сообщение будет удалено после обработки.",
            disable_notification=True,
        )
        await state.update_data(feedback_cancel_prompt_id=prompt.message_id)
        await callback.answer("Жду причину отказа.")
        return

    await set_feedback_status(idea_id, status)
    await callback.answer("Статус обновлён.")


@router.message(FeedbackAdminStates.waiting_cancel_reason)
async def feedback_cancel_reason(message: Message, state: FSMContext):
    if FEEDBACK_CHAT_ID is not None and message.chat.id != FEEDBACK_CHAT_ID:
        await _safe_delete(message)
        return

    data = await state.get_data()
    idea_id = data.get("feedback_cancel_idea_id")
    prompt_id = data.get("feedback_cancel_prompt_id")
    reason = (message.text or "").strip()
    if idea_id and reason:
        await set_feedback_status(int(idea_id), "rejected", reason=reason)

    await state.clear()

    if prompt_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=int(prompt_id))
        except Exception:
            pass
    await _safe_delete(message)
