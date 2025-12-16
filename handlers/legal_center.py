from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import HELP_TELEGRAPH_URL, SUPPORT_URL

router = Router(name="help_center")

# ===== ТЕКСТЫ (внутри бота, без Telegraph) =====

RULES_QUOTE = (
    "🚫 Запрещается:\n"
    "• загружать чужие фотографии без разрешения;\n"
    "• порнография/эротика/шок-контент;\n"
    "• загружать селфи/портреты с изображением вас самих\n"
    "• насилие, жестокость, кровь, ненависть;\n"
    "• оскорбления, травля, угрозы;\n"
    "• накрутка оценок, мультиаккаунты;\n"
    "• спам и реклама.\n\n"
    "Администрация может удалить контент и ограничить вам\n"
    "доступ к боту при нарушениях."
)

TERMS_QUOTE = (
    "GlowShot — Telegram-бот для любителей фотографии.\n"
    "Покупая Premium, ты получаешь доступ к доп. функциям на срок тарифа.\n\n"
    "Важно:\n"
    "• Платёж подтверждается платёжной системой.\n"
    "• В случае сбоев можно обратиться в поддержку.\n"
    "• Админ вправе менять функциональность и правила."
)

PRIVACY_QUOTE = (
    "Какие данные могут обрабатываться:\n"
    "• Telegram ID (обязателен для работы бота);\n"
    "• username/имя (если есть в Telegram);\n"
    "• данные подписки Premium (срок, статус);\n"
    "• технические данные, необходимые для работы функций.\n\n"
    "Бот не требует паспорт/карту/пароли Telegram.\n"
    "По вопросам — пиши в поддержку."
)

SUPPORT_QUOTE = (
    "Пиши, если:\n"
    "• оплатил Premium, но не активировался;\n"
    "• ошибка/баг/что-то работает странно;\n"
    "• есть вопрос по тарифам.\n\n"
    "Желательно сразу указать:\n"
    "• причину/что случилось\n"
    "• время оплаты (если было)"
)

HELP_TEXT = (
    "<b>🆘 Help / Поддержка GlowShot</b>\n\n"
    "Тут всё, что может понадобиться:\n"
    "• правила и условия\n"
    "• приватность\n"
    "• помощь и поддержка\n\n"
    "Если нужно официальное описание — открой Информация."
)

def quote_block(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"

def page(title: str, quote: str, extra: str = "") -> str:
    t = f"<b>{title}</b>\n\n{quote_block(quote)}"
    if extra:
        t += f"\n\n{extra}"
    return t

# ===== КЛАВЫ =====

def kb_help():
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Информация)", url=HELP_TELEGRAPH_URL)
    kb.button(text="📄 Terms", callback_data="help:open:terms")
    kb.button(text="🔐 Privacy", callback_data="help:open:privacy")
    kb.button(text="📄 Rules", callback_data="help:open:rules")
    kb.button(text="💬 Support", callback_data="help:open:support")
    kb.button(text="🗑 Закрыть", callback_data="help:delete")
    kb.adjust(1, 2, 2, 1)
    return kb.as_markup()

def kb_back():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад в Help", callback_data="help:home")
    kb.button(text="🗑 Закрыть", callback_data="help:delete")
    kb.adjust(1)
    return kb.as_markup()

def kb_support_back():
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать в поддержку", url=SUPPORT_URL)
    kb.button(text="⬅️ Назад в Help", callback_data="help:home")
    kb.button(text="🗑 Закрыть", callback_data="help:delete")
    kb.adjust(1)
    return kb.as_markup()

# ===== ХЕНДЛЕРЫ =====

@router.message(Command("help"))
async def cmd_help(message: Message):
    # удаляем /help пользователя
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        HELP_TEXT,
        reply_markup=kb_help(),
        parse_mode="HTML",
        disable_notification=True,
    )

@router.callback_query(F.data == "help:home")
async def help_home(callback: CallbackQuery):
    try:
        await callback.message.edit_text(HELP_TEXT, reply_markup=kb_help(), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("help:open:"))
async def help_open(callback: CallbackQuery):
    kind = (callback.data or "").split(":")[-1]

    if kind == "terms":
        text = page("📄 Terms (Условия)", TERMS_QUOTE, "Если нужна полная официальная версия — кнопка Информация  есть в /help.")
        kb = kb_back()
    elif kind == "privacy":
        text = page("🔐 Privacy (Конфиденциальность)", PRIVACY_QUOTE, "Если нужна полная официальная версия — кнопка Информация есть в /help.")
        kb = kb_back()
    elif kind == "rules":
        text = page("📄 Rules (Правила)", RULES_QUOTE)
        kb = kb_back()
    elif kind == "support":
        text = page("💬 Support (Поддержка)", SUPPORT_QUOTE)
        kb = kb_support_back()
    else:
        await callback.answer()
        return

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer()

@router.callback_query(F.data == "help:delete")
async def help_delete(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await callback.answer()