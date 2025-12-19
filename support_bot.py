import asyncio
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
)

from config import SUPPORT_BOT_TOKEN, SUPPORT_CHAT_ID
from database import get_support_users


# tickets[(user_id, ticket_id)] = информация о тикете (сообщение в чате поддержки и текст пользователя)
tickets: Dict[tuple[int, int], dict] = {}

# pending_replies[agent_id] = (user_id, ticket_id), которому нужно ответить
pending_replies: Dict[int, tuple[int, int]] = {}


async def main():
    bot = Bot(
        SUPPORT_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    @dp.message(Command("chatid"))
    async def get_chat_id(message: Message):
        """
        Вспомогательная команда для получения ID текущего чата.
        Используй её в чате поддержки, чтобы узнать SUPPORT_CHAT_ID.
        """
        await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")

    @dp.message(F.chat.id == SUPPORT_CHAT_ID, Command("resolve"))
    async def resolve_ticket_manual(message: Message):
        """
        Ручная команда в чате поддержки: /resolve <ticket_id>
        Помечает тикет как решенный и обновляет сообщение.
        """
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /resolve <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return

        # Ищем любой тикет с таким номером
        key = None
        for (uid, tid), info in tickets.items():
            if tid == ticket_id:
                key = (uid, tid)
                break

        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        ticket = tickets[key]
        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"

        try:
            await message.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            await message.answer("Не удалось обновить сообщение тикета, но статус помечен локально.")

        ticket["status"] = "resolved"
        await message.answer(f"Тикет #{ticket_id} помечен как решенный ✅")

    # --- Обработка сообщений из чата поддержки (SUPPORT_CHAT_ID) ---

    @dp.message(F.chat.id == SUPPORT_CHAT_ID)
    async def handle_support_chat_message(message: Message):
        """
        Сообщения в чате поддержки.
        Если у отправителя (сотрудника поддержки) есть незакрытый pending_replies,
        считаем это ответом пользователю.
        """
        agent_id = message.from_user.id

        # Если нет активного "режима ответа" для этого агента — игнорим
        if agent_id not in pending_replies:
            return

        user_id, ticket_id = pending_replies.pop(agent_id)

        if not message.text:
            await message.answer("Отправь, пожалуйста, ответ текстом, я перешлю его пользователю.")
            return

        # Кнопки для оценки ответа
        feedback_kb = InlineKeyboardMarkup(
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

        # Шлём ответ юзеру
        await message.bot.send_message(
            chat_id=user_id,
            text=(
                f"💬 <b>Ответ от поддержки на тикет #{ticket_id}</b>\n\n"
                f"{message.text}"
            ),
            reply_markup=feedback_kb,
        )

        # Пишем в чат поддержки, что всё ок
        await message.answer(f"Ответ на тикет #{ticket_id} отправлен ✅")

    # --- Новый тикет от пользователя (личка с ботом) ---

    @dp.message()
    async def handle_support(message: Message):
        """
        Любое сообщение НЕ из SUPPORT_CHAT_ID считаем тикетом от пользователя.
        """
        if message.chat.id == SUPPORT_CHAT_ID:
            # для чата поддержки есть отдельный хендлер выше
            return

        user = message.from_user

        ticket_id = message.message_id  # используем message_id как номер тикета

        header = (
            "🆘 <b>Новый запрос!</b>\n\n"
            f"Тикет: #{ticket_id}\n"
            f"ID пользователя: <code>{user.id}</code>\n"
            f"Username: @{user.username if user.username else '—'}\n"
            "Сообщение:\n"
        )

        # 1) шлём текст в чат поддержки + кнопка "Ответить"
        if message.text:
            body = header + message.text
        else:
            body = header + "📎 Вложение"

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

        # Сохраняем информацию о тикете в памяти
        tickets[(user.id, ticket_id)] = {
            "support_msg_id": sent.message_id,
            "user_id": user.id,
            "text": message.text or "📎 Вложение",
            "status": "open",
        }

        # если это не текст, можно форварднуть оригинальное сообщение следом
        if not message.text:
            await message.forward(SUPPORT_CHAT_ID)

        # 2) отвечаем юзеру
        await message.answer(
            f"Спасибо, твой номер тикета #{ticket_id}, жди ответа от поддержки 💌"
        )

    @dp.callback_query(F.data.startswith("ticket_feedback:"))
    async def ticket_feedback_callback(callback: CallbackQuery):
        """
        Обработка нажатий пользователя: вопрос решен / не решен.
        """
        try:
            _, user_id_str, ticket_id_str, status = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных.", show_alert=True)
            return

        key = (user_id, ticket_id)
        ticket = tickets.get(key)

        # В любом случае убираем клавиатуру, чтобы нельзя было тыкать бесконечно
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        if status == "resolved":
            if not ticket:
                await callback.answer("Тикет не найден (возможно, бот перезапускался).", show_alert=True)
                return

            support_msg_id = ticket.get("support_msg_id")
            original_text = ticket.get("text") or "—"

            # Обновляем сообщение в чате поддержки
            try:
                await callback.bot.edit_message_text(
                    chat_id=SUPPORT_CHAT_ID,
                    message_id=support_msg_id,
                    text=(
                        f"✅ Вопрос #{ticket_id} решен\n\n"
                        f"Сообщение:\n"
                        f"{original_text}"
                    ),
                )
            except Exception:
                # Если не удалось отредактировать, просто игнорим
                pass

            ticket["status"] = "resolved"
            await callback.answer("Спасибо, отметили вопрос как решенный ✅", show_alert=False)
            return

        # статус "не решен" — предлагаем написать живому человеку
        # пытаемся найти кого-то из поддержки
        try:
            support_users = await get_support_users()
        except Exception:
            support_users = []

        target_username = None
        for u in support_users:
            uname = u.get("username")
            if uname:
                target_username = uname
                break

        if target_username:
            text = (
                f"— Вопрос #{ticket_id} не решен.\n"
                f"Напишите, пожалуйста, @{target_username} для дальнейшей помощи.\n\n"
                f"Не забудь переслать ему сообщение с тикетом, чтобы он понимал, о чем речь."
            )
        else:
            text = (
                f"— Вопрос #{ticket_id} не решен.\n"
                f"Сейчас нет свободного оператора с указанным @username.\n"
                f"Пожалуйста, напишите в общий чат поддержки ещё раз."
            )

        await callback.message.answer(text)
        await callback.answer("Сообщил, что вопрос не решен ⚠️", show_alert=False)

    # --- Callback на кнопку "Ответить" в чате поддержки ---

    @dp.callback_query(F.data.startswith("support_reply:"))
    async def support_reply_callback(callback: CallbackQuery):
        """
        Ставит для агента "режим ответа": следующее его сообщение в чате поддержки
        будет отправлено пользователю.
        """
        try:
            _, user_id_str, ticket_id_str = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных", show_alert=True)
            return

        agent_id = callback.from_user.id
        pending_replies[agent_id] = (user_id, ticket_id)

        await callback.message.answer(
            (
                f"Отвечаем пользователю по тикету #{ticket_id}.\n"
            ),
            reply_markup=ForceReply(selective=True),
        )

        await callback.answer("Режим ответа включен ✅")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())