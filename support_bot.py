import asyncio
from datetime import datetime
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
from database import (
    get_support_users,
    get_support_users_full,
    is_user_premium_active,
    get_user_by_tg_id,
    ensure_user_minimal_row,
    get_user_premium_status,
)

# tickets[(user_id, ticket_id)] = информация о тикете (сообщение в чате поддержки и текст пользователя)
tickets: Dict[tuple[int, int], dict] = {}

# pending_replies[agent_id] = (user_id, ticket_id), которому нужно ответить
pending_replies: Dict[int, tuple[int, int]] = {}

# pending_sections[user_id] = выбранный раздел, после которого ждём сообщение/вложение от пользователя
pending_sections: Dict[int, str] = {}
# active dialogue maps
active_ticket_by_user: Dict[int, int] = {}        # user_id -> ticket_id
active_ticket_by_operator: Dict[int, int] = {}    # operator_id -> ticket_id
ticket_operator: Dict[int, int] = {}              # ticket_id -> operator_id
ticket_user: Dict[int, int] = {}                  # ticket_id -> user_id
ticket_support_msg: Dict[int, int] = {}           # ticket_id -> support card message_id in SUPPORT_CHAT_ID


async def main():
    bot = Bot(
        SUPPORT_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    def find_ticket_key_by_id(ticket_id: int) -> tuple[int, int] | None:
        for (uid, tid), _info in tickets.items():
            if int(tid) == int(ticket_id):
                return (uid, tid)
        return None
    
    async def is_support_operator(user_id: int) -> bool:
        """
        Разрешаем /h и /hd в личке бота только саппортам.
        Берём из БД get_support_users_full().
        """
        try:
            su = await get_support_users_full()
        except Exception:
            su = []
        for u in su or []:
            if isinstance(u, dict):
                try:
                    if int(u.get("tg_id")) == int(user_id):
                        return True
                except Exception:
                    continue
            elif isinstance(u, int):
                if int(u) == int(user_id):
                    return True
        return False

    def build_start_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🐞 Баг / ошибка", callback_data="support_section:bug")],
                [InlineKeyboardButton(text="💎 Вопрос по Премиум", callback_data="support_section:premium")],
                [InlineKeyboardButton(text="🔐 Доступ к боту", callback_data="support_section:access")],
                [InlineKeyboardButton(text="🚫 Жалоба", callback_data="support_section:complaint")],
                [InlineKeyboardButton(text="📝 Другое", callback_data="support_section:other")],
            ]
        )

    def section_label(code: str) -> str:
        mapping = {
            "bug": "Баг / ошибка",
            "premium": "Вопрос по Премиум",
            "access": "Доступ к боту",
            "complaint": "Жалоба",
            "other": "Другое",
        }
        return mapping.get(code, code)

    async def _premium_label(tg_id: int) -> str:
        try:
            user = await get_user_by_tg_id(int(tg_id))
            if not user:
                await ensure_user_minimal_row(int(tg_id))
                user = await get_user_by_tg_id(int(tg_id))

            status = await get_user_premium_status(int(tg_id))
            is_flag = bool(status.get("is_premium"))
            until_raw = status.get("premium_until")

            active = False
            if is_flag:
                if until_raw:
                    try:
                        active = datetime.fromisoformat(str(until_raw)) > datetime.now()
                    except Exception:
                        active = True
                else:
                    active = True

            if active:
                return f"💎 Премиум: активен{f' (до {until_raw})' if until_raw else ''}"
            if is_flag:
                return f"💤 Премиум: истёк{f' ({until_raw})' if until_raw else ''}"
            return "💤 Премиум: нет"
        except Exception:
            return "💤 Премиум: нет"

    @dp.message(CommandStart())
    async def start_menu(message: Message):
        # В чате поддержки /start не нужен
        if message.chat.id == SUPPORT_CHAT_ID:
            return

        today = datetime.now().strftime("%d.%m.%Y")
        text = (
            "Привет! на связи поддержка GlowShot, что у вас случилось?\n"
            f"ID: <code>{message.from_user.id}</code>\n"
            f"дата: {today}"
        )
        await message.answer(text, reply_markup=build_start_menu())

    @dp.callback_query(F.data.startswith("support_section:"))
    async def support_section_callback(callback: CallbackQuery):
        # Раздел выбирает только пользователь (не в SUPPORT_CHAT_ID)
        if callback.message and callback.message.chat.id == SUPPORT_CHAT_ID:
            await callback.answer("Эта кнопка не для чата поддержки.", show_alert=True)
            return

        try:
            _, code = callback.data.split(":", 1)
        except Exception:
            await callback.answer("Ошибка данных.", show_alert=True)
            return

        pending_sections[callback.from_user.id] = code

        await callback.message.answer(
            "Супер! опишите поподробнее, что у вас случилось, я отправлю ваш запрос админам и вам ответят в ближайшее время."
        )
        await callback.answer("Ок ✅")

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
                    f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            await message.answer("Не удалось обновить сообщение тикета, но статус помечен локально.")

        ticket["status"] = "resolved"
        await message.answer(f"Тикет #{ticket_id} помечен как решенный ✅")


    @dp.message(Command("h"))
    async def take_ticket_manual(message: Message):
        """Взять тикет в работу: /h <ticket_id>. Включает режим ответа для оператора."""
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /h <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return
        
        # /h в личке разрешаем только операторам
        if message.chat.id != SUPPORT_CHAT_ID:
            if not await is_support_operator(message.from_user.id):
                await message.answer("Команда доступна только операторам поддержки.")
                return

        key = find_ticket_key_by_id(ticket_id)
        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        user_id, tid = key

        # 1) быстрые ответы из группы: старый режим pending_replies
        if message.chat.id == SUPPORT_CHAT_ID:
            pending_replies[message.from_user.id] = (user_id, tid)

        # 2) диалог в личке бота
        active_ticket_by_operator[message.from_user.id] = tid
        active_ticket_by_user[user_id] = tid
        ticket_operator[tid] = message.from_user.id
        ticket_user[tid] = user_id
        try:
            t = tickets.get((user_id, tid))
            if t and t.get("support_msg_id"):
                ticket_support_msg[tid] = int(t.get("support_msg_id"))
        except Exception:
            pass

        # уведомим пользователя, что оператор подключился
        try:
            await message.bot.send_message(
                chat_id=user_id,
                text=(
                    "👤 <b>Оператор подключился!</b>\n"
                    f"Тикет #{ticket_id}\n\n"
                    "Напишите уточнения или дождитесь ответа — я передам оператору."
                ),
            )
        except Exception:
            pass

        if message.chat.id == SUPPORT_CHAT_ID:
            await message.answer(
                f"Ок, взял тикет #{ticket_id}. Для диалога зайди в личку бота и пиши там ✅\nЗакрыть: /hd {ticket_id}",
                reply_markup=ForceReply(selective=True),
            )
        else:
            await message.answer(
                f"Ок, взял тикет #{ticket_id}. Теперь просто пиши сюда — я буду пересылать пользователю.\nЗакрыть: /hd {ticket_id}",
            )


    @dp.message(Command("hd"))
    async def close_ticket_manual(message: Message):
        """Закрыть тикет вручную: /hd <ticket_id> (аналог /resolve)."""
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /hd <номер_тикета>")
            return

        try:
            ticket_id = int(parts[1])
        except ValueError:
            await message.answer("Номер тикета должен быть числом.")
            return
        
        if message.chat.id != SUPPORT_CHAT_ID:
            if not await is_support_operator(message.from_user.id):
                await message.answer("Команда доступна только операторам поддержки.")
                return

        key = find_ticket_key_by_id(ticket_id)
        if key is None:
            await message.answer("Тикет с таким номером не найден (возможно, бот перезапускался).")
            return

        ticket = tickets.get(key)
        if not ticket:
            await message.answer("Тикет не найден (возможно, бот перезапускался).")
            return

        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"

        try:
            await message.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
                    f"Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            pass

        ticket["status"] = "resolved"
        # чистим карты диалога
        try:
            op_id = ticket_operator.get(ticket_id)
            uid = ticket_user.get(ticket_id)
            if op_id:
                active_ticket_by_operator.pop(op_id, None)
            if uid:
                active_ticket_by_user.pop(uid, None)
            ticket_operator.pop(ticket_id, None)
            ticket_user.pop(ticket_id, None)
        except Exception:
            pass

        # Если кто-то держит режим ответа на этот тикет — снимем
        try:
            to_del = [aid for aid, v in pending_replies.items() if v == key]
            for aid in to_del:
                pending_replies.pop(aid, None)
        except Exception:
            pass

        # уведомим пользователя
        try:
            await message.bot.send_message(
                chat_id=int(ticket.get("user_id") or key[0]),
                text=(
                    "✅ <b>Вопрос закрыт поддержкой</b>\n"
                    f"Тикет #{ticket_id}\n\n"
                    "Если проблема появится снова — просто напишите /start и создайте новый тикет."
                ),
            )
        except Exception:
            pass

        try:
            support_msg_id2 = ticket_support_msg.get(ticket_id)
            if support_msg_id2:
                await message.bot.edit_message_reply_markup(
                    chat_id=SUPPORT_CHAT_ID,
                    message_id=int(support_msg_id2),
                    reply_markup=None,
                )
        except Exception:
            pass
        await message.answer(f"Тикет #{ticket_id} закрыт ✅")

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

        section = "—"
        try:
            section = (tickets.get((user_id, ticket_id)) or {}).get("section") or "—"
        except Exception:
            section = "—"

        await message.bot.send_message(
            chat_id=user_id,
            text=(
                "💬 <b>Ответ от поддержки!</b>\n"
                f"Тикет #{ticket_id}\n"
                f"Раздел: <b>{section}</b>\n\n"
                "Сообщение:\n"
                f"{message.text}"
            ),
            reply_markup=feedback_kb,
        )

        # Пишем в чат поддержки, что всё ок
        await message.answer(f"Ответ на тикет #{ticket_id} отправлен ✅")


    @dp.message()
    async def handle_support(message: Message):
        """ 
        Сообщения НЕ из SUPPORT_CHAT_ID:
        1) /start показывает меню разделов.
        2) После выбора раздела следующее сообщение/вложение создаёт тикет.
        """
        if message.chat.id == SUPPORT_CHAT_ID:
            # для чата поддержки есть отдельный хендлер выше
            return

        user = message.from_user

        # Если это оператор в личке бота и он ведёт тикет — пересылаем пользователю
        if user.id in active_ticket_by_operator:
            tid = active_ticket_by_operator[user.id]
            uid = ticket_user.get(tid)
            if not uid:
                await message.answer("Тикет не найден или уже закрыт.")
                active_ticket_by_operator.pop(user.id, None)
                return

            try:
                header = "💬 <b>Сообщение от оператора</b>\n" f"Тикет #{tid}\n\n"
                if message.text:
                    await message.bot.send_message(uid, header + message.text)
                else:
                    await message.bot.send_message(uid, header + "📎 Вложение")
                    await message.forward(uid)
            except Exception:
                pass

            await message.answer(f"Отправлено пользователю по тикету #{tid} ✅")
            return
        # Если по пользователю уже идёт диалог по активному тикету — пересылаем оператору
        if user.id in active_ticket_by_user:
            tid = active_ticket_by_user[user.id]
            op_id = ticket_operator.get(tid)

            if op_id:
                try:
                    prefix = "🗣️ <b>Сообщение от пользователя</b>\n" f"Тикет #{tid}\n\n"
                    if message.text:
                        await message.bot.send_message(op_id, prefix + message.text)
                    else:
                        await message.bot.send_message(op_id, prefix + "📎 Вложение")
                        await message.forward(op_id)
                except Exception:
                    pass

            await message.answer("✅ Сообщение передано оператору по вашему тикету.")
            return

        # Если пользователь ещё не выбрал раздел — показываем меню
        if user.id not in pending_sections:
            today = datetime.now().strftime("%d.%m.%Y")
            text = (
                "Привет! на связи поддержка GlowShot, что у вас случилось?\n"
                f"ID: <code>{user.id}</code>\n"
                f"дата: {today}"
            )
            await message.answer(text, reply_markup=build_start_menu())
            return

        section_code = pending_sections.pop(user.id)
        section = section_label(section_code)

        ticket_id = message.message_id  # используем message_id как номер тикета

        premium_line = await _premium_label(user.id)

        header = (
            "🆘 <b>Новый запрос!</b>\n\n"
            f"Тикет: #{ticket_id}\n"
            f"ID пользователя: <code>{user.id}</code>\n"
            f"Username: @{user.username if user.username else '—'}\n"
            f"{premium_line}\n"
            f"Раздел: <b>{section}</b>\n\n"
            "Сообщение пользователя:\n"
            f"\n<b>Действия:</b> взять — /h {ticket_id} · закрыть — /hd {ticket_id}\n"
        )

        # Админские кнопки: Ответить / Завершить
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✉️ Ответить",
                        callback_data=f"support_reply:{user.id}:{ticket_id}",
                    ),
                    InlineKeyboardButton(
                        text="✅ Завершить",
                        callback_data=f"support_close:{user.id}:{ticket_id}",
                    ),
                ]
            ]
        )

        # 1) Шлём заголовок в чат поддержки
        # Текст выводим здесь, а вложения — отдельным forward'ом ниже
        if message.text:
            body = header + message.text
        else:
            body = header + "📎 Вложение"

        sent = await message.bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            text=body,
            reply_markup=admin_kb,
        )

        # карты для последующего диалога/закрытия
        ticket_user[ticket_id] = user.id
        ticket_support_msg[ticket_id] = sent.message_id

        # 1.1) Если есть вложения — форвардим оригинал (чтобы были фото/файл и т.д.)
        # Для чистого текста не форвардим, чтобы не дублировать.
        if not message.text:
            await message.forward(SUPPORT_CHAT_ID)

        # Сохраняем информацию о тикете в памяти
        tickets[(user.id, ticket_id)] = {
            "support_msg_id": sent.message_id,
            "user_id": user.id,
            "text": message.text or "📎 Вложение",
            "status": "open",
            "section": section,
        }

        # 2) отвечаем юзеру
        await message.answer(
            f"Отлично! ваша заявка #{ticket_id} создана!\nОжидайте ответа от поддержки."
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
                        f"Раздел: <b>{ticket.get('section') or '—'}</b>\n\n"
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

        # статус "не решен" — эскалируем живому оператору поддержки
        # get_support_users() в проекте может возвращать:
        #  - list[int] (tg_id операторов)
        #  - list[dict] (с полями tg_id/username и т.п.)
        try:
            support_users = await get_support_users_full()
        except Exception:
            support_users = []

        # Нормализуем кандидатов -> список словарей {tg_id:int, username:str|None}
        candidates: list[dict] = []
        for u in (support_users or [])[:200]:
            if isinstance(u, int):
                candidates.append({"tg_id": u, "username": None})
                continue
            if isinstance(u, dict):
                tg_id = u.get("tg_id") or u.get("id") or u.get("user_id")
                if tg_id is None:
                    continue
                try:
                    tg_id = int(tg_id)
                except Exception:
                    continue
                candidates.append({"tg_id": tg_id, "username": u.get("username")})

        # Пытаемся написать первому оператору в личку
        operator_tg_id: int | None = None
        operator_username: str | None = None
        for c in candidates:
            tg_id = c.get("tg_id")
            if not tg_id:
                continue
            # не пингуем самого пользователя
            if tg_id == callback.from_user.id:
                continue
            try:
                await callback.bot.send_message(
                    chat_id=int(tg_id),
                    text=(
                        "🆘 <b>Нужна помощь: вопрос не решён</b>\n\n"
                        f"Тикет: #{ticket_id}\n"
                        f"Пользователь ID: <code>{user_id}</code>\n"
                        f"Username: @{callback.from_user.username if callback.from_user.username else '—'}\n"
                        f"Раздел: <b>{(ticket or {}).get('section') or '—'}</b>\n\n"
                        "Пожалуйста, зайдите в чат поддержки и ответьте пользователю по тикету."
                    ),
                )
                operator_tg_id = int(tg_id)
                operator_username = c.get("username")
                break
            except Exception:
                continue

        if operator_tg_id:
            # Пользователю: подтверждаем эскалацию
            if operator_username:
                text = (
                    f"— Вопрос #{ticket_id} не решен.\n"
                    f"Я передал ваш запрос оператору @{operator_username}.\n"
                    f"Ожидайте ответа от поддержки."
                )
            else:
                text = (
                    f"— Вопрос #{ticket_id} не решен.\n"
                    f"Я передал ваш запрос оператору поддержки.\n"
                    f"Ожидайте ответа от поддержки."
                )
        else:
            # Если не получилось никому написать в личку (часто из‑за ограничений Telegram),
            # то эскалируем в общий чат поддержки (SUPPORT_CHAT_ID), где бот писать может всегда.
            mentions = []
            for c in candidates[:10]:
                tg_id = c.get("tg_id")
                uname = c.get("username")
                if uname:
                    mentions.append(f"@{uname}")
                elif tg_id:
                    # упоминание по tg_id работает в группах/супергруппах
                    mentions.append(f'<a href="tg://user?id={int(tg_id)}">оператор</a>')

            ping_line = " ".join(mentions) if mentions else "(операторы не найдены)"

            try:
                await callback.bot.send_message(
                    chat_id=SUPPORT_CHAT_ID,
                    text=(
                        "⚠️ <b>Эскалация: вопрос не решён</b>\n\n"
                        f"Тикет: #{ticket_id}\n"
                        f"Пользователь ID: <code>{user_id}</code>\n"
                        f"Username: @{callback.from_user.username if callback.from_user.username else '—'}\n"
                        f"Раздел: <b>{(ticket or {}).get('section') or '—'}</b>\n\n"
                        "Пользователь отметил, что ответ не решил проблему. Нужен оператор.\n\n"
                        f"Пинг: {ping_line}\n\n"
                        f"<b>Действия:</b> возьмите тикет в личке бота: /h {ticket_id} · закрыть: /hd {ticket_id}"
                    ),
                )
            except Exception:
                pass

            text = (
                f"— Вопрос #{ticket_id} не решен.\n"
                f"Я передал запрос в общий чат поддержки, оператор подключится.\n"
                f"Ожидайте ответа от поддержки."
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

    @dp.callback_query(F.data.startswith("support_close:"))
    async def support_close_callback(callback: CallbackQuery):
        """Закрыть тикет кнопкой из чата поддержки (аналог /resolve)."""
        try:
            _, user_id_str, ticket_id_str = callback.data.split(":")
            user_id = int(user_id_str)
            ticket_id = int(ticket_id_str)
        except (ValueError, AttributeError):
            await callback.answer("Ошибка данных", show_alert=True)
            return

        key = (user_id, ticket_id)
        ticket = tickets.get(key)
        if not ticket:
            await callback.answer("Тикет не найден (возможно, бот перезапускался).", show_alert=True)
            return

        support_msg_id = ticket.get("support_msg_id")
        original_text = ticket.get("text") or "—"
        section = ticket.get("section") or "—"

        try:
            await callback.bot.edit_message_text(
                chat_id=SUPPORT_CHAT_ID,
                message_id=support_msg_id,
                text=(
                    f"✅ Вопрос #{ticket_id} решен\n\n"
                    f"Раздел: <b>{section}</b>\n\n"
                    "Сообщение:\n"
                    f"{original_text}"
                ),
            )
        except Exception:
            pass

        ticket["status"] = "resolved"
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("Тикет закрыт ✅")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
