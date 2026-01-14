from __future__ import annotations

import html
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    add_premium_benefit,
    add_premium_news,
    get_all_users_tg_ids,
    get_premium_benefits,
    get_premium_users,
    get_user_by_id,
    get_user_by_tg_id,
    get_user_by_username,
    set_user_premium_status,
    swap_premium_benefits,
    update_premium_benefit,
)
from utils.time import get_moscow_now
from .common import _ensure_admin

router = Router(name="admin_premium")


class PremiumAdminStates(StatesGroup):
    waiting_identifier_for_grant = State()
    waiting_premium_until = State()
    waiting_identifier_for_revoke = State()
    waiting_fest_name = State()
    waiting_fest_text = State()
    waiting_fest_days = State()
    waiting_fest_notify = State()
    waiting_premium_news = State()
    waiting_benefit_add = State()
    waiting_benefit_edit = State()
    waiting_benefit_edit_num = State()
    waiting_benefit_swap = State()


def build_premium_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="➕ Выдать", callback_data="admin:premium:grant")
    kb.button(text="➖ Убрать", callback_data="admin:premium:revoke")
    kb.button(text="🆕 Добавить изменения", callback_data="admin:premium:news")
    kb.button(text="🧩 Преимущества", callback_data="admin:premium:benefits")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _parse_premium_until(raw: str) -> str | None:
    """
    Accept:
    - days: '30'
    - date: '31.12.2025'
    - forever: 'навсегда' / 'без срока' / 'бессрочно' / 'forever' / '∞' / '-'
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty")

    s_low = s.lower()
    forever_tokens = {
        "навсегда",
        "без срока",
        "безсрока",
        "бессрочно",
        "forever",
        "infinite",
        "∞",
        "-",
        "0",
    }
    if s_low in forever_tokens:
        return None

    if s.isdigit():
        days = int(s)
        if days <= 0:
            return None
        until = datetime.now() + timedelta(days=days)
        return until.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    try:
        dt = datetime.strptime(s, "%d.%m.%Y")
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
        return dt.isoformat()
    except Exception:
        raise ValueError("invalid")


def _extend_until(current_until: str | None, days: int, *, now: datetime) -> str:
    if days <= 0:
        raise ValueError("days must be positive")

    if current_until is None:
        return None

    base = now
    try:
        dt = datetime.fromisoformat(str(current_until))
        if dt > now:
            base = dt
    except Exception:
        base = now

    new_dt = base + timedelta(days=days)
    new_dt = new_dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return new_dt.isoformat()


async def _edit_premium_prompt_or_answer(
    message: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Keep one service message for Premium section to prevent spam."""
    data = await state.get_data()
    chat_id = data.get("premium_prompt_chat_id")
    msg_id = data.get("premium_prompt_msg_id")

    target_message = message.message if isinstance(message, CallbackQuery) else message

    if chat_id and msg_id:
        try:
            await target_message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            try:
                await target_message.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    try:
        sent = await target_message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        await state.update_data(premium_prompt_chat_id=sent.chat.id, premium_prompt_msg_id=sent.message_id)
    except Exception:
        pass


async def _premium_soft_clear(state: FSMContext):
    """Clear FSM state/data but keep IDs of the premium service message to avoid message spam."""
    data = await state.get_data()
    chat_id = data.get("premium_prompt_chat_id")
    msg_id = data.get("premium_prompt_msg_id")
    await state.clear()
    if chat_id and msg_id:
        await state.update_data(premium_prompt_chat_id=chat_id, premium_prompt_msg_id=msg_id)


def build_premium_notice_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Просмотрено", callback_data="user:premium:seen")
    kb.adjust(1)
    return kb.as_markup()


async def _notify_user_premium_change(
    bot,
    tg_id: int,
    *,
    is_enabled: bool,
    until_iso: str | None,
    admin_label: str,
):
    """Best-effort notification to user about premium changes."""

    def _fmt_until(iso: str | None) -> str:
        if not iso:
            return "бессрочно"
        try:
            return "до " + datetime.fromisoformat(iso).strftime("%d.%m.%Y")
        except Exception:
            return "до " + str(iso)

    admin_label_safe = html.escape(admin_label or "админа", quote=False)

    if is_enabled:
        until_text = _fmt_until(until_iso)
        text = (
            "💎 <b>GlowShot Premium активирован</b>\n\n"
            f"Вы получили Premium от {admin_label_safe}.\n"
            f"Срок: <b>{html.escape(until_text, quote=False)}</b>\n\n"
            "Теперь вам доступны премиум‑функции в боте ✨"
        )
    else:
        text = (
            "💤 <b>GlowShot Premium отключён</b>\n\n"
            f"Premium был отключён админом {admin_label_safe}.\n"
            "Если это ошибка — напишите в поддержку."
        )

    try:
        await bot.send_message(
            chat_id=int(tg_id),
            text=text,
            reply_markup=build_premium_notice_kb(),
            parse_mode="HTML",
            disable_notification=True,
        )
    except Exception:
        return


async def _find_user_by_identifier(identifier: str) -> dict | None:
    """Найти пользователя по tg_id / внутреннему id / @username."""
    ident = (identifier or "").strip()
    if not ident:
        return None

    if ident.isdigit():
        try:
            tg_id = int(ident)
        except ValueError:
            tg_id = None

        if tg_id is not None:
            try:
                u = await get_user_by_tg_id(tg_id)
            except Exception:
                u = None
            if u:
                return u

        try:
            internal_id = int(ident)
        except ValueError:
            internal_id = None

        if internal_id is not None:
            try:
                u = await get_user_by_id(internal_id)
            except Exception:
                u = None
            if u:
                return u

    if ident.startswith("@"):
        ident = ident[1:]

    try:
        u = await get_user_by_username(ident)
    except Exception:
        u = None
    return u


def _split_benefit_text(raw: str) -> tuple[str, str]:
    s = (raw or "").strip()
    if not s:
        return "", ""
    for sep in (" — ", " - ", " —", " -", "—", "-"):
        if sep in s:
            parts = s.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return s, ""


async def _render_benefits_admin(state: FSMContext, message_or_cb, *, error: str | None = None):
    benefits = await get_premium_benefits()
    lines: list[str] = ["🧩 <b>Преимущества Premium</b>", ""]
    if benefits:
        for i, b in enumerate(benefits, start=1):
            title = html.escape(str(b.get("title") or ""), quote=False)
            desc = html.escape(str(b.get("description") or ""), quote=False)
            lines.append(f"{i}) {title}")
            if desc:
                lines.append(desc)
            lines.append("")
        lines.append("Выбери пункт для редактирования или добавь новый.")
    else:
        lines.append("Пока нет записей. Нажми «➕ Добавить», чтобы создать первый пункт.")
    if error:
        lines.append("")
        lines.append(f"⚠️ {error}")

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="admin:premium:benefits:editnum")
    kb.button(text="🔀 Поменять местами", callback_data="admin:premium:benefits:swap")
    kb.button(text="➕ Добавить", callback_data="admin:premium:benefits:add")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(message_or_cb, state, "\n".join(lines), kb.as_markup())


@router.callback_query(F.data == "admin:premium")
async def admin_premium_menu(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)

    text = (
        "<b>Премиум</b>\n\n"
        "• 📋 Список — текущие премиум-пользователи\n"
        "• ➕ Выдать — по @username/ID и сроку\n"
        "• ➖ Убрать — снять премиум\n"
        "• 🆕 Добавить изменения — текст в блок «Новое в Premium» за неделю\n"
    )

    try:
        msg = await callback.message.edit_text(text, reply_markup=build_premium_menu_kb(), parse_mode="HTML")
    except Exception:
        msg = await callback.message.answer(text, reply_markup=build_premium_menu_kb(), parse_mode="HTML")

    await state.update_data(premium_prompt_chat_id=msg.chat.id, premium_prompt_msg_id=msg.message_id)
    await callback.answer()


@router.callback_query(F.data == "admin:premium:news")
async def admin_premium_news(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_premium_news)

    text = (
        "🆕 <b>Добавить изменения в Premium</b>\n\n"
        "Отправь текст одним сообщением.\n"
        "Пример:\n"
        "• Новый фильтр 🔥\n"
        "• Улучшили выдачу фото\n\n"
        "Эти пункты попадут в «Новое в Premium за последнюю неделю» в профиле."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_premium_news, F.text)
async def admin_premium_news_save(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = (message.text or "").strip()
    await message.delete()
    if not raw:
        await _edit_premium_prompt_or_answer(
            message,
            state,
            "Текст пустой. Отправь описание изменения или вернись назад.",
            build_premium_menu_kb(),
        )
        return

    try:
        await add_premium_news(raw)
    except Exception:
        await _edit_premium_prompt_or_answer(
            message,
            state,
            "Не удалось сохранить. Попробуй позже.",
            build_premium_menu_kb(),
        )
        return

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(
        message,
        state,
        "✅ Добавлено! Запись появится в блоке «Новое в Premium» (последняя неделя).",
        build_premium_menu_kb(),
    )


@router.callback_query(F.data == "admin:premium:benefits")
async def admin_premium_benefits(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await _render_benefits_admin(state, callback)
    await callback.answer()


@router.callback_query(F.data == "admin:premium:benefits:add")
async def admin_premium_benefits_add(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await state.set_state(PremiumAdminStates.waiting_benefit_add)
    await _edit_premium_prompt_or_answer(
        callback.message,
        state,
        "➕ <b>Новый пункт преимуществ</b>\n\nОтправь слоган и описание в одном сообщении.\nПример:\n<i>🚀 Быстрый старт — приоритет в очереди загрузок</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:premium:benefits")]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:premium:benefits:editnum")
async def admin_premium_benefits_editnum(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    benefits = await get_premium_benefits()
    if not benefits:
        await _render_benefits_admin(state, callback, error="Пока нет пунктов для редактирования.")
        await callback.answer()
        return

    await state.set_state(PremiumAdminStates.waiting_benefit_edit_num)
    kb = InlineKeyboardBuilder()
    for idx, b in enumerate(benefits, start=1):
        title = str(b.get("title") or "")
        kb.button(text=f"{idx}. {title[:28] or '—'}", callback_data=f"admin:premium:benefits:edit:{b['id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:premium:benefits")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(
        callback.message,
        state,
        "✏️ <b>Редактирование</b>\n\nВыбери пункт кнопкой или отправь его номер.\nПосле выбора пришли новый слоган и описание одним сообщением.",
        kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:premium:benefits:swap")
async def admin_premium_benefits_swap(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await state.set_state(PremiumAdminStates.waiting_benefit_swap)
    await _edit_premium_prompt_or_answer(
        callback.message,
        state,
        "🔀 <b>Поменять местами</b>\n\nОтправь два номера через пробел, например: <code>1 3</code>.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:premium:benefits")]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin:premium:benefits:edit:(\d+)$"))
async def admin_premium_benefits_edit(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    try:
        bid = int((callback.data or "").split(":")[-1])
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return
    await state.set_state(PremiumAdminStates.waiting_benefit_edit)
    await state.update_data(premium_benefit_id=bid)
    await _edit_premium_prompt_or_answer(
        callback.message,
        state,
        "✏️ <b>Редактирование пункта</b>\n\nОтправь новый слоган и описание в одном сообщении.\nПример:\n<i>🚀 Быстрый старт — приоритет в очереди загрузок</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:premium:benefits")]
            ]
        ),
    )
    await callback.answer()


@router.message(PremiumAdminStates.waiting_benefit_add, F.text)
async def admin_premium_benefit_add_save(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return
    raw = (message.text or "").strip()
    await message.delete()
    title, desc = _split_benefit_text(raw)
    if not title:
        await _render_benefits_admin(state, message, error="Пустой слоган. Отправь слоган и описание.")
        return
    try:
        await add_premium_benefit(title, desc)
    except Exception:
        await _render_benefits_admin(state, message, error="Не удалось сохранить. Попробуй ещё раз.")
        return
    await _premium_soft_clear(state)
    await _render_benefits_admin(state, message, error="Добавлено!")


@router.message(PremiumAdminStates.waiting_benefit_edit_num, F.text)
async def admin_premium_benefit_pick_number(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return
    raw = (message.text or "").strip()
    await message.delete()
    try:
        num = int(raw)
    except Exception:
        await _render_benefits_admin(state, message, error="Номер должен быть числом.")
        return
    benefits = await get_premium_benefits()
    if num <= 0 or num > len(benefits):
        await _render_benefits_admin(state, message, error="Нет преимущества с таким номером.")
        return
    benefit = benefits[num - 1]
    await state.set_state(PremiumAdminStates.waiting_benefit_edit)
    await state.update_data(premium_benefit_id=int(benefit.get("id")))
    await _edit_premium_prompt_or_answer(
        message,
        state,
        "✏️ <b>Редактирование пункта</b>\n\nОтправь новый слоган и описание в одном сообщении.\nПример:\n<i>🚀 Быстрый старт — приоритет в очереди загрузок</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:premium:benefits")]
            ]
        ),
    )


@router.message(PremiumAdminStates.waiting_benefit_edit, F.text)
async def admin_premium_benefit_edit_save(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return
    data = await state.get_data()
    bid = int(data.get("premium_benefit_id") or 0)
    raw = (message.text or "").strip()
    await message.delete()
    title, desc = _split_benefit_text(raw)
    if not title or not bid:
        await _render_benefits_admin(state, message, error="Пустой слоган или неверный пункт.")
        return
    ok = False
    try:
        ok = await update_premium_benefit(bid, title, desc)
    except Exception:
        ok = False
    await _premium_soft_clear(state)
    if not ok:
        await _render_benefits_admin(state, message, error="Не удалось обновить.")
        return
    await _render_benefits_admin(state, message, error="Обновлено!")


@router.message(PremiumAdminStates.waiting_benefit_swap, F.text)
async def admin_premium_benefit_swap_save(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return
    raw = (message.text or "").strip()
    await message.delete()
    parts = raw.replace(",", " ").split()
    if len(parts) != 2:
        await _render_benefits_admin(state, message, error="Нужно два номера через пробел.")
        return
    try:
        a, b = int(parts[0]), int(parts[1])
    except Exception:
        await _render_benefits_admin(state, message, error="Номера должны быть числами.")
        return
    ok = await swap_premium_benefits(a, b)
    await _premium_soft_clear(state)
    if not ok:
        await _render_benefits_admin(state, message, error="Не удалось поменять местами. Проверь номера.")
        return
    await _render_benefits_admin(state, message, error="Порядок обновлён!")


@router.callback_query(F.data == "admin:premium:list")
async def admin_premium_list(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    users = await get_premium_users()
    if not users:
        text = "Сейчас нет ни одного премиум-пользователя."
    else:
        lines = ["<b>Премиум-пользователи</b>", ""]
        now = datetime.utcnow()
        shown = 0
        for u in users[:400]:
            uname = u.get("username")
            label = f"@{uname}" if uname else (u.get("name") or "Без имени")
            label = html.escape(str(label), quote=False)

            until = u.get("premium_until")
            if until:
                try:
                    until_dt = datetime.fromisoformat(until)
                except Exception:
                    until_dt = None

                if until_dt and until_dt <= now:
                    continue

                if until_dt:
                    days_left = max(1, int((until_dt - now).total_seconds() // 86400 + 1))
                    until_str = until_dt.strftime("%d.%m.%Y")
                    lines.append(f"• {label} — до {until_str} ({days_left} дн.)")
                else:
                    lines.append(f"• {label} — до {html.escape(str(until), quote=False)}")
            else:
                lines.append(f"• {label} — бессрочно")

            shown += 1
            if shown >= 200:
                break

        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:premium:grant")
async def admin_premium_grant(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)

    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Всем", callback_data="admin:premium:grant:festive")
    kb.button(text="🎯 Выборочно", callback_data="admin:premium:grant:selective")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1, 1, 1)

    text = (
        "➕ <b>Выдать премиум</b>\n\n"
        "Выбери вариант:\n"
        "• 🎁 Премиум всем — задать название/текст/срок, выдать всем активным пользователям;\n"
        "• 🎯 Выборочно — по @username / ID и сроку.\n\n"
        "⚠️ На следующих шагах можно выбрать режим уведомления: «с уведомлением» или «тихо»."
    )

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:premium:grant:selective")
async def admin_premium_grant_selective(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_identifier_for_grant)

    text = (
        "🎯 <b>Выборочная выдача</b>\n\n"
        "Отправь Telegram ID или @username пользователя.\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:premium:grant:festive")
async def admin_premium_grant_festive(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_fest_name)

    text = (
        "🎁 <b>Премиум для всех</b>\n\n"
        "1) Введи название (например, «Новый год» или «Извинения»).\n"
        "2) Затем я попрошу текст сообщения для пользователей.\n"
        "3) Потом задам срок в днях и выдам премиум всем активным пользователям с твоим текстом."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium:grant")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1, 1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_identifier_for_grant, F.text)
async def admin_premium_grant_get_user(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    u = await _find_user_by_identifier(ident)
    if not u:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data="admin:premium:grant")
        kb.button(text="⬅️ Назад", callback_data="admin:premium")
        kb.adjust(1)
        await _edit_premium_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    await state.update_data(pending_premium_user=u)
    await state.set_state(PremiumAdminStates.waiting_premium_until)

    text = (
        "💎 <b>Срок премиума</b>\n\n"
        "Выбери вариант:\n"
        "• число дней (например <code>30</code>)\n"
        "• дату окончания <code>31.12.2025</code>\n"
        "• или нажми кнопку <b>♾ Бессрочно</b>\n\n"
        "Можно также написать словом: <code>навсегда</code> / <code>без срока</code> / <code>бессрочно</code>.\n\n"
        "💡 Если указать число дней, они прибавятся к текущему премиуму, а не обнулят срок."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="♾ Бессрочно", callback_data="admin:premium:grant:forever")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(message, state, text, kb.as_markup())


@router.callback_query(F.data == "admin:premium:grant:forever")
async def admin_premium_grant_forever(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return

    data = await state.get_data()
    u = data.get("pending_premium_user")
    if not u or not u.get("tg_id"):
        await callback.answer("Сначала выбери пользователя.", show_alert=True)
        return

    tg_id = int(u["tg_id"])

    await set_user_premium_status(tg_id, True, premium_until=None)

    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        callback.message.bot,
        tg_id,
        is_enabled=True,
        until_iso=None,
        admin_label=admin_label,
    )

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(callback.message, state, f"✅ Премиум выдан: <b>{label}</b>\nСрок: <b>бессрочно</b>", kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_premium_until, F.text)
async def admin_premium_grant_set_until(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    u = data.get("pending_premium_user")
    if not u or not u.get("tg_id"):
        await _premium_soft_clear(state)
        await _edit_premium_prompt_or_answer(message, state, "Сессия сбилась. Открой «Премиум» заново.", build_premium_menu_kb())
        return

    tg_id = int(u["tg_id"])

    premium_until: str | None
    now = datetime.now()
    if raw.isdigit():
        days = int(raw)
        if days <= 0:
            kb = InlineKeyboardBuilder()
            kb.button(text="♾ Бессрочно", callback_data="admin:premium:grant:forever")
            kb.button(text="⬅️ Назад", callback_data="admin:premium")
            kb.adjust(1)
            await _edit_premium_prompt_or_answer(
                message,
                state,
                "❌ Срок должен быть больше 0 дней.\n\nПопробуй ещё раз или нажми <b>♾ Бессрочно</b>.",
                kb.as_markup(),
            )
            return
        current_until = u.get("premium_until")
        try:
            premium_until = _extend_until(current_until, days, now=now)
        except Exception:
            premium_until = _extend_until(None, days, now=now)
    else:
        try:
            premium_until = _parse_premium_until(raw)
        except ValueError:
            kb = InlineKeyboardBuilder()
            kb.button(text="♾ Бессрочно", callback_data="admin:premium:grant:forever")
            kb.button(text="⬅️ Назад", callback_data="admin:premium")
            kb.adjust(1)

            await _edit_premium_prompt_or_answer(
                message,
                state,
                "❌ Не понял срок.\n\nВведи <code>30</code> (дней) или <code>31.12.2025</code>, либо нажми <b>♾ Бессрочно</b>.",
                kb.as_markup(),
            )
            return

    await set_user_premium_status(tg_id, True, premium_until=premium_until)

    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        message.bot,
        tg_id,
        is_enabled=True,
        until_iso=premium_until,
        admin_label=admin_label,
    )

    until_text = "бессрочно"
    if premium_until:
        try:
            until_text = "до " + datetime.fromisoformat(premium_until).strftime("%d.%m.%Y")
        except Exception:
            until_text = html.escape(str(premium_until), quote=False)

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(message, state, f"✅ Премиум выдан: <b>{label}</b>\nСрок: <b>{until_text}</b>", kb.as_markup())


@router.callback_query(F.data == "admin:premium:revoke")
async def admin_premium_revoke(callback: CallbackQuery, state: FSMContext):
    admin = await _ensure_admin(callback)
    if not admin:
        return
    await _premium_soft_clear(state)
    await state.set_state(PremiumAdminStates.waiting_identifier_for_revoke)

    text = (
        "➖ <b>Снять премиум</b>\n\n"
        "Отправь Telegram ID или @username пользователя.\n"
        "Пример: <code>123456789</code> или <code>@username</code>."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="⬅️ В админ-меню", callback_data="admin:menu")
    kb.adjust(1)

    await _edit_premium_prompt_or_answer(callback.message, state, text, kb.as_markup())
    await callback.answer()


@router.message(PremiumAdminStates.waiting_identifier_for_revoke, F.text)
async def admin_premium_revoke_do(message: Message, state: FSMContext):
    admin = await _ensure_admin(message)
    if not admin:
        return

    ident = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    u = await _find_user_by_identifier(ident)
    if not u or not u.get("tg_id"):
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Попробовать снова", callback_data="admin:premium:revoke")
        kb.button(text="⬅️ Назад", callback_data="admin:premium")
        kb.adjust(1)
        await _edit_premium_prompt_or_answer(message, state, "Пользователь не найден.", kb.as_markup())
        return

    tg_id = int(u["tg_id"])

    await set_user_premium_status(tg_id, False, premium_until=None)

    admin_label = "админа"
    try:
        a = admin
        if isinstance(a, dict):
            if a.get("username"):
                admin_label = "@" + str(a.get("username"))
            elif a.get("name"):
                admin_label = str(a.get("name"))
    except Exception:
        admin_label = "админа"

    await _notify_user_premium_change(
        message.bot,
        tg_id,
        is_enabled=False,
        until_iso=None,
        admin_label=admin_label,
    )

    label = f"@{u.get('username')}" if u.get("username") else (u.get("name") or "Без имени")
    label = html.escape(str(label), quote=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:premium")
    kb.button(text="📋 Список", callback_data="admin:premium:list")
    kb.adjust(1)

    await _premium_soft_clear(state)
    await _edit_premium_prompt_or_answer(message, state, f"✅ Премиум снят: <b>{label}</b>", kb.as_markup())


@router.callback_query(F.data == "user:premium:seen")
async def user_premium_notice_seen(callback: CallbackQuery):
    try:
        if callback.message:
            await callback.message.delete()
    except Exception:
        pass

    try:
        await callback.answer("Ок")
    except Exception:
        pass

