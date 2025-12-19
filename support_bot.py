import asyncio
import json
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
)

from config import SUPPORT_BOT_TOKEN, SUPPORT_CHAT_ID
from database import get_support_users


# =============================================================
# ==== ХРАНЕНИЕ СОСТОЯНИЯ (чтобы не терять тикеты при рестарте)
# =============================================================

STATE_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_PATH = os.path.join(STATE_DIR, "support_state.json")


def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _load_state_sync() -> dict:
    _ensure_state_dir()
    if not os.path.exists(STATE_PATH):
        return {"tickets": {}, "counter": 0}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"tickets": {}, "counter": 0}
    except Exception:
        return {"tickets": {}, "counter": 0}


def _save_state_sync(state: dict) -> None:
    _ensure_state_dir()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


async def load_state() -> dict:
    return await asyncio.to_thread(_load_state_sync)


async def save_state(state: dict) -> None:
    await asyncio.to_thread(_save_state_sync, state)


# =============================================================
# ==== МОДЕЛИ / ПАМЯТЬ ========================================
# =============================================================

# tickets[(user_id, ticket_id)] = информация о тикете
TicketsDict = Dict[Tuple[int, int], dict]

# pending_replies[agent_id] = (user_id, ticket_id)
PendingRepliesDict = Dict[int, Tuple[int, int]]


tickets: TicketsDict = {}
pending_replies: PendingRepliesDict = {}

# автоинкремент тикетов
_ticket_counter: int = 0


# =============================================================
# ==== КЭШ СПИСКА СОТРУДНИКОВ ПОДДЕРЖКИ =======================
# =============================================================

_support_cache: dict = {"ts": 0.0, "ids": set()}


async def get_support_agent_ids() -> set[int]:
    """Достаём список tg_id поддержки (кэш 30 секунд)."""
    now = asyncio.get_running_loop().time()
    if _support_cache["ids"] and (now - _support_cache["ts"]) < 30:
        return _support_cache["ids"]

    try:
        rows = await get_support_users()
    except Exception:
        rows = []

    ids = set()
    for u in rows:
        tg_id = u.get("tg_id")
        if tg_id:
            try:
                ids.add(int(tg_id))
            except Exception:
                pass

    _support_cache["ts"] = now
    _support_cache["ids"] = ids
    return ids


# =============================================================
# ==== ТИКЕТЫ: ХЕЛПЕРЫ ========================================
# =============================================================

async def next_ticket_id() -> int:
    global _ticket_counter
    _ticket_counter += 1

    # сохраняем счётчик
    st = await load_state()
    st["counter"] = _ticket_counter
    # tickets в файле храним без ключей-ту플ов: отдельным списком
    st["tickets"] = list(tickets.values())
    await save_state(st)

    return _ticket_counter


async def persist_tickets() -> None:
    st = await load_state()
    st["counter"] = _ticket_counter
    st["tickets"] = list(tickets.values())
    await save_state(st)


async def restore_tickets() -> None:
    global _ticket_counter

    st = await load_state()
    _ticket_counter = int(st.get("counter") or 0)

    restored = {}
    for item in (st.get("tickets") or []):
        try:
            uid = int(item.get("user_id"))
            tid = int(item.get("ticket_id"))
        except Exception:
            continue
        restored[(uid, tid)] = item

    tickets.clear()
    tickets.update(restored)


def build_feedback_kb(user_id: int, ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Вопрос решен",
                    callback_data=f"ticket_feedback:{user_id}:{ticket_id}:resolved",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Вопрос не решен",
                    callback_data=f"ticket_feedback:{user_id}:{ticket_id}:unresolved",
                )
            ],
        ]
    )


# =============================================================
# ==== MAIN ====================================================
# =============================================================

async def main():
    await restore_tickets()

    bot = Bot(
        SUPPORT_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # ---------------------------------------------------------
    # Команда: /chatid
    # ---------------------------------------------------------
    @dp.message(Command("chatid"))
    async def get_chat_id(message: Message):
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")

    # ---------------------------------------------------------
    # Ручной resolve в чате поддержки: /resolve <ticket_id>
    # ---------------------------------------------------------
    @dp.message(F.chat.id == SUPPORT_CHAT_ID, Command("resolve"))
    async def resolve_ticket_manual(message: Message):
        support_ids = await get_support_agent_ids()
        if message.from_user.id not in support_ids:
            return

        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /resolve <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return

        key = None
        for (uid, tid), info in tickets.items():
            if tid == ticket_id:
                key = (uid, tid)
                break

        if key is None:
            await message.answer("Тикет не найден (возможно, он очень старый или был очищен).")
            return

        ticket = tickets[key]
        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"

        try:
            await message.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=int(support_msg_id),
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            await message.answer("Не удалось обновить сообщение тикета, но статус помечен.")

        ticket["status"] = "resolved"
        await persist_tickets()
        await message.answer(f"Тикет #{ticket_id} помечен как решенный ✅")

    # ---------------------------------------------------------
    # Сообщения в SUPPORT_CHAT_ID: ответы поддержки пользователю
    # ---------------------------------------------------------
    @dp.message(F.chat.id == SUPPORT_CHAT_ID)
    async def handle_support_chat_message(message: Message):
        support_ids = await get_support_agent_ids()
        agent_id = message.from_user.id

        if agent_id not in support_ids:
            return

        if agent_id not in pending_replies:
            return

        user_id, ticket_id = pending_replies.pop(agent_id)

        # 1) копируем контент ответа пользователю (текст/медиа/что угодно)
        try:
            await message.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except Exception:
            await message.answer("Не смог переслать сообщение пользователю. Попробуй ещё раз.")
            pending_replies[agent_id] = (user_id, ticket_id)
            return

        # 2) отдельным сообщением — кнопки фидбэка
        try:
            await message.bot.send_message(
                chat_id=user_id,
                text=f"💬 <b>Оцени ответ поддержки</b> (тикет #{ticket_id})",
                reply_markup=build_feedback_kb(user_id, ticket_id),
            )
        except Exception:
            pass

        await message.answer(f"Ответ на тикет #{ticket_id} отправлен ✅")

    # ---------------------------------------------------------
    # Новый тикет от пользователя (личка с ботом)
    # ---------------------------------------------------------
    @dp.message()
    async def handle_support(message: Message):
        if message.chat.id == SUPPORT_CHAT_ID:
            return

        user = message.from_user
        ticket_id = await next_ticket_id()

        header = (
            "🆘 <b>Новый запрос!</b>\n\n"
            f"Тикет: #{ticket_id}\n"
            f"ID пользователя: <code>{user.id}</code>\n"
            f"Username: @{user.username if user.username else '—'}\n"
            "Сообщение:\n"
        )

        body_text = message.text if message.text else "📎 Вложение"
        body = header + body_text

        reply_button = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✉️ Ответить",
                        callback_data=f"support_reply:{user.id}:{ticket_id}",
                    )
                ]
            ]
        )

        sent = await message.bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            text=body,
            reply_markup=reply_button,
        )

        # если это не текст — копируем оригинал следом (надёжнее, чем forward)
        if not message.text:
            try:
                await message.bot.copy_message(
                    chat_id=SUPPORT_CHAT_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            except Exception:
                # fallback
                try:
                    await message.forward(SUPPORT_CHAT_ID)
                except Exception:
                    pass

        tickets[(user.id, ticket_id)] = {
            "support_msg_id": sent.message_id,
            "user_id": user.id,
            "ticket_id": ticket_id,
            "text": body_text,
            "status": "open",
        }

        await persist_tickets()

        await message.answer(
            f"Спасибо! Твой номер тикета <b>#{ticket_id}</b>. Жди ответа от поддержки 💌"
        )

    # ---------------------------------------------------------
    # Пользователь: оценка ответа
    # ---------------------------------------------------------
    @dp.callback_query(F.data.startswith("ticket_feedback:"))
    async def ticket_feedback_callback(callback: CallbackQuery):
        try:
            _, user_id_str, ticket_id_str, status = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных.", show_alert=True)
            return

        # удаляем клаву
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        key = (user_id, ticket_id)
        ticket = tickets.get(key)

        if status == "resolved":
            if ticket:
                ticket["status"] = "resolved"
                await persist_tickets()

                # обновляем сообщение в чате поддержки
                support_msg_id = ticket.get("support_msg_id")
                original_text = ticket.get("text") or "—"
                try:
                    await callback.bot.edit_message_text(
                        chat_id=SUPPORT_CHAT_ID,
                        message_id=int(support_msg_id),
                        text=(
                            f"✅ Вопрос #{ticket_id} решен\n\n"
                            f"Сообщение:\n"
                            f"{original_text}"
                        ),
                    )
                except Exception:
                    pass

            await callback.answer("Спасибо, отметили вопрос как решенный ✅")
            return

        # unresolved
        await callback.answer("Понял, не решено ⚠️")
        await callback.message.answer(
            "Окей. Опиши, пожалуйста, что именно осталось непонятно — я передам поддержке."
        )

    # ---------------------------------------------------------
    # Callback: "Ответить" в чате поддержки
    # ---------------------------------------------------------
    @dp.callback_query(F.data.startswith("support_reply:"))
    async def support_reply_callback(callback: CallbackQuery):
        support_ids = await get_support_agent_ids()
        if callback.from_user.id not in support_ids:
            await callback.answer("Нет доступа.", show_alert=True)
            return

        try:
            _, user_id_str, ticket_id_str = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных", show_alert=True)
            return

        pending_replies[callback.from_user.id] = (user_id, ticket_id)

        await callback.message.answer(
            f"Отвечаем пользователю по тикету #{ticket_id}. Следующее твоё сообщение будет отправлено ему.",
            reply_markup=ForceReply(selective=True),
        )

        await callback.answer("Режим ответа включен ✅")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())