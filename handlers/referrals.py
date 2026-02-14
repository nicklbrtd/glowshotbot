from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    get_or_create_referral_code,
    get_referral_stats_for_user,
    get_user_by_tg_id,
)
from utils.registration_guard import require_user_name

router = Router(name="referrals")

BOT_USERNAME_CACHE: str | None = None


async def _get_bot_username(obj) -> str | None:
    """
    Получаем username бота один раз и кэшируем.
    obj — это Message или CallbackQuery.
    """
    global BOT_USERNAME_CACHE
    if BOT_USERNAME_CACHE:
        return BOT_USERNAME_CACHE

    bot = obj.bot
    me = await bot.get_me()
    username = me.username or None
    BOT_USERNAME_CACHE = username
    return username


async def _build_ref_main_text(user_tg_id: int, obj) -> str:
    """
    Собирает основной текст реферального экрана:
    условия (3 часа Premium + 2 кредита), ссылка и статистика «ты привёл N друзей».
    """
    code = await get_or_create_referral_code(user_tg_id)
    stats = await get_referral_stats_for_user(user_tg_id)
    invited_qualified = stats.get("invited_qualified") or 0

    bot_username = await _get_bot_username(obj)
    if code and bot_username:
        link = f"https://t.me/{bot_username}?start=ref_{code}"
        link_line = f"<code>{link}</code>"
    else:
        link_line = "Не удалось построить ссылку, попробуй чуть позже."

    text = (
        "🤝 <b>Реферальная программа GlowShot</b>\n\n"
        "Пригласи друга — и вы оба получите бонус: <b>3 часа Premium</b> + <b>2 кредита</b>.\n"
        "Бонус начисляется после того, как друг:\n"
        "   • зарегистрируется в боте;\n"
        "   • оценит хотя бы одну чужую работу.\n\n"
        "Участие в итогах доступно <b>всем</b> — рефералка не нужна.\n\n"
        "Вот твоя реферальная ссылка:\n"
        f"{link_line}\n\n"
        f"По твоей ссылке пришло <b>{invited_qualified}</b> друзей (выполнили условия)."
    )
    return text


def _build_ref_kb() -> InlineKeyboardBuilder:
    """
    Кнопки для раздела рефералки:
    • Дополнительно
    • О премиум
    • Закрыть
    Всё работает через редактирование сообщения.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="ℹ️ Дополнительно", callback_data="ref:more")
    kb.button(text="💎 О премиум", callback_data="ref:premium")
    kb.button(text="✖️ Закрыть", callback_data="ref:close")
    kb.adjust(1)
    return kb


@router.message(Command("ref"))
async def ref_main_command(message: Message):
    """
    Команда /ref — вход в реферальную систему.
    Показываем основной экран с условиями, ссылкой и статистикой.
    """
    user = await get_user_by_tg_id(message.from_user.id)
    if user is None or not (user.get("name") or "").strip():
        if not await require_user_name(message):
            return
        return

    text = await _build_ref_main_text(message.from_user.id, message)
    kb = _build_ref_kb()

    sent = await message.answer(
        text,
        reply_markup=kb.as_markup(),
        disable_notification=True,
    )
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "ref:more")
async def ref_more(callback: CallbackQuery):
    """
    Дополнительная информация про реферальную систему.
    Сообщение не плодим — только редактируем текст и оставляем те же кнопки.
    """
    text = (
        "ℹ️ <b>Дополнительно о реферальной программе</b>\n\n"
        "— Считается только первый аккаунт каждого друга.\n"
        "— Бонус начисляется после того, как друг:\n"
        "   • зарегистрируется в боте;\n"
        "   • оценит хотя бы одну чужую работу.\n\n"
        "— За каждого такого друга вы оба получаете: <b>3 часа Premium</b> + <b>2 кредита</b>.\n"
        "— За нарушения правил (спам, фейковые аккаунты, накрутка) мы можем обнулить реферальный прогресс.\n\n"
        "Делись ссылкой только с теми, кому реально интересна фотография 📸"
    )
    kb = _build_ref_kb()

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "ref:premium")
async def ref_premium_info(callback: CallbackQuery):
    """
    Экран с дополнительной информацией о премиуме,
    открываемый из реферального раздела.
    """
    text = (
        "💎 <b>Что даёт GlowShot Premium</b>\n\n"
        "Премиум-аккаунт открывает дополнительные возможности:\n\n"
        "• Расширенная статистика по твоим кадрам\n"
        "• Дополнительные инструменты продвижения\n"
        "• Возможность указать свой Telegram‑канал в профиле\n"
        "• Приоритетные ответы от поддержки\n"
        "• Доступ к новым экспериментальным функциям раньше остальных\n\n"
        "Часть Premium можно получить не только покупкой, но и через реферальную программу —\n"
        "приглашай друзей и получай <b>3 часа Premium</b> + <b>2 кредита</b> за каждого, кто выполнит условия 💫"
    )
    kb = _build_ref_kb()

    await callback.message.edit_text(
        text,
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "ref:close")
async def ref_close(callback: CallbackQuery):
    """
    Кнопка «Закрыть» — удаляет сообщение бота с реферальным меню.
    """
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "ref:thanks")
async def ref_thanks(callback: CallbackQuery):
    """
    Кнопка «Спасибо!» в пушах реферальки — просто удаляет сообщение.
    """
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
