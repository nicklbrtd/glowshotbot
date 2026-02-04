

from __future__ import annotations

# =============================================================
# ==== АДМИНКА: РАССЫЛКА ======================================
# =============================================================

import asyncio
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message
from datetime import datetime
from utils.time import get_moscow_now
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .common import _ensure_admin, BroadcastStates
from database import (
    get_moderators,
    get_support_users,
    get_helpers,
    get_all_users_tg_ids,
    get_premium_users,
    get_user_by_tg_id_any,
    create_scheduled_broadcast,
    list_scheduled_broadcasts,
    cancel_scheduled_broadcast,
)
from config import BOT_TOKEN

router = Router()

# Сколько задержки между отправками (чтобы не словить flood)
_SEND_DELAY_SEC = 0.05
_PRIMARY_BROADCAST_BOT: Bot | None = None
_SCHEDULED_PAGE_LIMIT = 10


def _parse_schedule_datetime(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None

    now = get_moscow_now()

    # HH:MM -> сегодня
    if len(s) <= 5 and ":" in s:
        try:
            hh, mm = s.split(":", 1)
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            return None

    # DD.MM.YYYY HH:MM
    try:
        return datetime.strptime(s, "%d.%m.%Y %H:%M")
    except Exception:
        pass

    # YYYY-MM-DD HH:MM
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        pass

    # DD.MM HH:MM (текущий год)
    try:
        dt = datetime.strptime(s, "%d.%m %H:%M")
        return dt.replace(year=now.year)
    except Exception:
        pass

    return None


def _get_send_bot(current_bot: Bot) -> Bot:
    """
    В саппорт-боте отправляем рассылку через основной бот (BOT_TOKEN),
    чтобы сообщения приходили от него. В обычном режиме возвращаем текущий.
    """
    global _PRIMARY_BROADCAST_BOT
    try:
        current_token = current_bot.token  # type: ignore[attr-defined]
    except Exception:
        current_token = None

    if BOT_TOKEN and current_token != BOT_TOKEN:
        if _PRIMARY_BROADCAST_BOT is None:
            _PRIMARY_BROADCAST_BOT = Bot(
                BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        return _PRIMARY_BROADCAST_BOT
    return current_bot


def _audience_label(target: str) -> str:
    if target == "all":
        return "всем пользователям"
    if target == "premium":
        return "премиум-пользователям"
    if target == "test":
        return "только тебе (тестовая рассылка)"
    if target == "moderators":
        return "модераторам"
    if target == "support":
        return "поддержке"
    return "помощникам"


def _shorten(text: str, limit: int = 80) -> str:
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


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
        "Дальше я попрошу ввести текст и покажу превью перед отправкой.\n\n"
        "Также можно запланировать рассылку на дату и время."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Всем пользователям", callback_data="admin:broadcast:all")
    kb.button(text="💎 Премиум-пользователям", callback_data="admin:broadcast:premium")
    kb.button(text="👥 Составу", callback_data="admin:broadcast:staff")
    kb.button(text="🧪 Тестовая (мне)", callback_data="admin:broadcast:test")
    kb.button(text="⏰ Отложенная рассылка", callback_data="admin:broadcast:schedule")
    kb.button(text="🗓 Список запланированных", callback_data="admin:broadcast:scheduled:list:1")
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


@router.callback_query(F.data == "admin:broadcast:schedule")
async def admin_broadcast_schedule_menu(callback: CallbackQuery, state: FSMContext):
    """
    Старт отложенной рассылки: выбираем аудиторию.
    """
    user = await _ensure_admin(callback)
    if user is None:
        return

    await state.clear()
    await state.update_data(broadcast_mode="scheduled")

    text = (
        "<b>Отложенная рассылка</b>\n\n"
        "Выбери аудиторию:\n"
        "• 📢 Всем пользователям\n"
        "• 💎 Премиум-пользователям\n"
        "• 👥 Составу (модераторы, поддержка, помощники)\n"
        "• 🧪 Тестовая (только тебе)\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Всем пользователям", callback_data="admin:broadcast:all")
    kb.button(text="💎 Премиум-пользователям", callback_data="admin:broadcast:premium")
    kb.button(text="👥 Составу", callback_data="admin:broadcast:staff")
    kb.button(text="🧪 Тестовая (мне)", callback_data="admin:broadcast:test")
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
    data = await state.get_data()
    mode = data.get("broadcast_mode") or "instant"
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

    header = "Отложенная рассылка" if mode == "scheduled" else "Рассылка"
    text = (
        f"<b>{header} {audience}</b>\n\n"
        "Отправь ОДНИМ сообщением текст, который нужно разослать.\n\n"
        "Твой исходный текст я удалю из чата, но использую для пуша.\n"
        "Перед отправкой покажу превью.\n\n"
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
        broadcast_mode=mode,
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
    mode = data.get("broadcast_mode") or "instant"

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

    if mode == "scheduled":
        prompt_text = (
            f"<b>Отложенная рассылка {_audience_label(target)}</b>\n\n"
            "Теперь отправь дату и время в формате:\n"
            "• <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (например, 05.02.2026 19:30)\n"
            "или просто <b>ЧЧ:ММ</b> (тогда сегодня).\n\n"
            "Часовой пояс: МСК."
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
        kb.adjust(1)

        data = await state.get_data()
        chat_id = data.get("broadcast_prompt_chat_id")
        msg_id = data.get("broadcast_prompt_msg_id")
        if chat_id and msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=prompt_text,
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                await message.answer(prompt_text, reply_markup=kb.as_markup())
        else:
            await message.answer(prompt_text, reply_markup=kb.as_markup())

        await state.set_state(BroadcastStates.waiting_schedule_datetime)
        return

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


@router.message(BroadcastStates.waiting_schedule_datetime, F.text)
async def admin_broadcast_schedule_datetime(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("broadcast_target")
    text_body = data.get("broadcast_text")
    if not target or not text_body:
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("Сессия рассылки потерялась. Открой раздел «Рассылка» заново.")
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    dt = _parse_schedule_datetime(raw)
    if dt is None:
        try:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text="Не понял дату/время. Формат: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> или <b>ЧЧ:ММ</b>.",
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception:
            pass
        return

    now = get_moscow_now()
    if dt <= now:
        try:
            await message.bot.send_message(
                chat_id=message.chat.id,
                text="Время должно быть в будущем. Укажи корректную дату/время.",
                parse_mode="HTML",
                disable_notification=True,
            )
        except Exception:
            pass
        return

    await state.update_data(
        broadcast_schedule_iso=dt.isoformat(),
        broadcast_schedule_human=dt.strftime("%d.%m.%Y %H:%M"),
    )

    if target == "all":
        header = ""
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    send_text = text_body if not header else f"{header}\n\n{text_body}"
    human = dt.strftime("%d.%m.%Y %H:%M")
    preview_text = (
        f"<b>Отложенная рассылка {_audience_label(target)}</b>\n"
        f"Отправка: <b>{human}</b> (МСК)\n\n"
        f"{send_text}\n\n"
        "Запланировать это сообщение?"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запланировать", callback_data="admin:broadcast:schedule:confirm")
    kb.button(text="❌ Отмена", callback_data="admin:broadcast:cancel")
    kb.adjust(1, 1)

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
        except Exception:
            await message.answer(preview_text, reply_markup=kb.as_markup())
    else:
        await message.answer(preview_text, reply_markup=kb.as_markup())

    await state.set_state(BroadcastStates.waiting_schedule_confirm)


@router.callback_query(BroadcastStates.waiting_schedule_confirm, F.data == "admin:broadcast:schedule:confirm")
async def admin_broadcast_schedule_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target = data.get("broadcast_target")
    text_body = data.get("broadcast_text")
    scheduled_iso = data.get("broadcast_schedule_iso")
    scheduled_human = data.get("broadcast_schedule_human")
    chat_id = data.get("broadcast_prompt_chat_id")
    msg_id = data.get("broadcast_prompt_msg_id")

    if not target or not text_body or not scheduled_iso:
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

    await create_scheduled_broadcast(
        target=target,
        text=text_body,
        scheduled_at_iso=scheduled_iso,
        created_by_tg_id=callback.from_user.id,
    )

    await state.clear()

    summary = (
        "✅ Рассылка запланирована.\n\n"
        f"Аудитория: <b>{_audience_label(target)}</b>\n"
        f"Время: <b>{scheduled_human or scheduled_iso}</b> (МСК)\n\n"
        "Сообщение будет отправлено автоматически."
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


async def _render_scheduled_list(callback: CallbackQuery, state: FSMContext, page: int) -> None:
    offset = (page - 1) * _SCHEDULED_PAGE_LIMIT

    total, rows = await list_scheduled_broadcasts(status="pending", limit=_SCHEDULED_PAGE_LIMIT, offset=offset)
    total_pages = max(1, (int(total or 0) + _SCHEDULED_PAGE_LIMIT - 1) // _SCHEDULED_PAGE_LIMIT)
    if page > total_pages:
        page = total_pages

    lines: list[str] = [
        "<b>Запланированные рассылки</b>",
        f"Всего: <b>{int(total or 0)}</b>",
        f"Страница: <b>{page}</b>/<b>{total_pages}</b>",
        "",
    ]

    if not rows:
        lines.append("Пока нет запланированных рассылок.")
    else:
        for i, r in enumerate(rows, start=offset + 1):
            target = _audience_label(str(r.get("target") or ""))
            scheduled_at = str(r.get("scheduled_at") or "")
            try:
                dt = datetime.fromisoformat(scheduled_at)
                scheduled_at = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass
            text_preview = _shorten(str(r.get("text") or ""))
            lines.append(f"{i}. <b>{scheduled_at}</b> · {target} · {text_preview}")

    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"❌ Отменить #{r['id']}", callback_data=f"admin:broadcast:scheduled:cancel:{r['id']}:{page}")

    if page > 1:
        kb.button(text="⬅️", callback_data=f"admin:broadcast:scheduled:list:{page-1}")
    if page < total_pages:
        kb.button(text="➡️", callback_data=f"admin:broadcast:scheduled:list:{page+1}")

    kb.button(text="⬅️ Назад в рассылку", callback_data="admin:broadcast")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")

    if rows:
        kb.adjust(1)
    if page > 1 or page < total_pages:
        kb.adjust(1, 2, 1, 1)
    else:
        kb.adjust(1, 1)

    text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("admin:broadcast:scheduled:list:"))
async def admin_broadcast_scheduled_list(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 5:
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    try:
        page = int(parts[4])
    except Exception:
        page = 1
    page = max(1, page)

    await _render_scheduled_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:broadcast:scheduled:cancel:"))
async def admin_broadcast_scheduled_cancel(callback: CallbackQuery, state: FSMContext):
    user = await _ensure_admin(callback)
    if user is None:
        return

    parts = (callback.data or "").split(":")
    if len(parts) < 6:
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    try:
        sched_id = int(parts[4])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    try:
        back_page = int(parts[5])
    except Exception:
        back_page = 1

    await cancel_scheduled_broadcast(sched_id)
    await callback.answer("Отменено")

    await _render_scheduled_list(callback, state, back_page)


@router.callback_query(BroadcastStates.waiting_text, F.data == "admin:broadcast:cancel")
@router.callback_query(BroadcastStates.waiting_schedule_datetime, F.data == "admin:broadcast:cancel")
@router.callback_query(BroadcastStates.waiting_schedule_confirm, F.data == "admin:broadcast:cancel")
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

    def _is_valid_user(u: dict) -> bool:
        # Фильтр: активный (is_deleted=0), не заблокирован, есть имя
        if not u:
            return False
        if u.get("is_deleted"):
            return False
        if u.get("is_blocked"):
            return False
        name = (u.get("name") or "").strip()
        if not name:
            return False
        return True

    if target == "all":
        # get_all_users_tg_ids уже фильтрует is_deleted/is_blocked/empty name
        tg_ids = await get_all_users_tg_ids()
    elif target == "premium":
        users = await get_premium_users()
        tg_ids = [int(u["tg_id"]) for u in users if u.get("tg_id") and _is_valid_user(u)]
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
        header = ""
    elif target == "premium":
        header = "💎 <b>Сообщение для GlowShot Premium</b>"
    elif target == "test":
        header = "🧪 <b>Тестовая рассылка</b>"
    else:
        header = "👥 <b>Сообщение для команды GlowShot</b>"

    send_text = text_body if not header else f"{header}\n\n{text_body}"

    notif_kb = InlineKeyboardBuilder()
    notif_kb.button(text="✅ Просмотрено", callback_data="admin:notif_read")
    notif_kb.adjust(1)
    notif_markup = notif_kb.as_markup()

    total = len(tg_ids)
    sent = 0
    send_bot = _get_send_bot(callback.message.bot)

    for uid in tg_ids:
        try:
            await send_bot.send_message(
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


@router.callback_query(F.data == "admin:notif_read")
async def admin_broadcast_seen(callback: CallbackQuery):
    """
    Получатель рассылки нажал «Просмотрено» — удаляем уведомление.
    """
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
