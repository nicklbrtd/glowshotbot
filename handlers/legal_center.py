from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder


import config

HELP_TELEGRAPH_URL = getattr(
    config,
    "HELP_TELEGRAPH_URL",
    "https://telegra.ph/GlowShot---Informaciya-dlya-polzovatelej-12-16",
)
SUPPORT_URL = getattr(config, "SUPPORT_URL", "https://t.me/supofglowshotbot")

router = Router(name="help_center")

# ===== ТЕКСТЫ  =====

RULES_QUOTE = (
    "🚫 В GS запрещается:\n"
    "• загружать чужие фотографии;\n"
    "• 18+/шок-контент;\n"
    "• загружать селфи/портреты с изображением вас самих\n"
    "• насилие, жестокость, кровь, ненависть;\n"
    "• оскорбления, травля, угрозы;\n"
    "• накрутка оценок, мультиаккаунты;\n"
    "• спам и реклама.\n\n"
    "Администрация может удалить контент и ограничить вам\n"
    "доступ к боту при нарушениях."
)

TERMS_QUOTE = (
    "GlowShot — Telegram-бот для любителей творчества.\n"
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
    "• проблемы с Premium\n"
    "• ошибка/баг/что-то работает странно;\n"
    "• есть вопрос по любому поводу связанный с ботом\n\n"
    "Желательно сразу указать:\n"
    "• причину/что случилось\n"
    "• Другие данные в зависимости от ситуации (скрин, ник и т.п.)\n\n"
)

FAQ_QUOTE = (
    "<b>Q:</b> Где взять кредиты?\n"
    "<b>A:</b> Кредиты начисляются за активность: \n"
    "• каждый день — базовый бонус;\n"
    "• за публикацию фотографии;\n"
    "• за оценки чужих фотографий.\n\n"
    "Если кредитов мало — оценивай больше работ других авторов: это напрямую увеличивает показы твоих фото."
)

HELP_TEXT = (
    "<b>🆘 Help / Поддержка GlowShot</b>\n\n"
    "Тут должно быть всё, что может понадобиться:\n"
    "• правила и условия\n"
    "• приватность\n"
    "• помощь, FAQ и поддержка\n\n"
    "Если нужно официальное описание — открой Информация."
)

def quote_block(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"

def page(title: str, quote: str, extra: str = "") -> str:
    t = f"<b>{title}</b>\n\n{quote_block(quote)}"
    if extra:
        t += f"\n\n{extra}"
    return t

# ===== КНОПКИ =====

def kb_help():
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Информация", url=HELP_TELEGRAPH_URL)
    kb.button(text="📄 Условия", callback_data="help:open:terms")
    kb.button(text="🔐 Конфид.", callback_data="help:open:privacy")
    kb.button(text="📄 Правила", callback_data="help:open:rules")
    kb.button(text="❓ FAQ", callback_data="help:open:faq")
    kb.button(text="💬 Поддержка", callback_data="help:open:support")
    kb.button(text="🗑 Закрыть", callback_data="help:delete")
    kb.adjust(1, 2, 2, 2, 1)
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
        text = page("📄 Условия", TERMS_QUOTE, "Если нужна полная официальная версия — кнопка Информация  есть в /help.")
        kb = kb_back()
    elif kind == "privacy":
        text = page("🔐 Конфиденциальность", PRIVACY_QUOTE, "Если нужна полная официальная версия — кнопка Информация есть в /help.")
        kb = kb_back()
    elif kind == "rules":
        text = page("📄 Правила", RULES_QUOTE)
        kb = kb_back()
    elif kind == "faq":
        text = page("❓ FAQ", FAQ_QUOTE)
        kb = kb_back()
    elif kind == "support":
        text = page("💬 Поддержка", SUPPORT_QUOTE)
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