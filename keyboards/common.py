from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.time import get_moscow_now
from utils.i18n import t

# Unified navigation labels (GlowShot 2.1 UX)
BACK = "⬅️ Назад"
HOME = "🏠 В меню"
RESULTS = "🏆 Итоги"
RESULTS_ARCHIVE = "📅 Архив итогов"


def build_back_home_kb(
    back_callback: str,
    *,
    home_callback: str = "menu:back",
    back_text: str = BACK,
    home_text: str = HOME,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=back_text, callback_data=back_callback),
                InlineKeyboardButton(text=home_text, callback_data=home_callback),
            ]
        ]
    )


# --- Главное меню ---


def build_main_menu(
    is_admin: bool = False,
    is_moderator: bool = False,
    is_premium: bool = False,
    lang: str = "ru",
    has_photo: bool | None = None,
    has_rate_targets: bool | None = None,
) -> ReplyKeyboardMarkup:
    """Главное меню в виде reply-клавиатуры.

    Кнопки должны появляться только в основном меню, поэтому используем
    `one_time_keyboard=True`, чтобы Telegram скрывал клавиатуру сразу после
    нажатия. Тексты кнопок подтягиваем из i18n с учётом динамических состояний.
    """

    if has_photo is True:
        myphoto_text = t("kb.main.myphoto.filled", lang)
    elif has_photo is False:
        myphoto_text = t("kb.main.myphoto.empty", lang)
    else:
        myphoto_text = t("kb.main.myphoto", lang)

    rate_text = t("kb.main.rate", lang) if has_rate_targets is not False else t("kb.main.rate.empty", lang)

    keyboard = [
        [KeyboardButton(text=myphoto_text), KeyboardButton(text=rate_text)],
        [KeyboardButton(text=t("kb.main.results", lang)), KeyboardButton(text=t("kb.main.profile", lang))],
    ]

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=False,
    )


# --- Кнопки "назад / в меню" ---


def build_back_to_menu_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=HOME, callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()


def build_back_kb(callback_data: str, text: str | None = None, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=text or BACK, callback_data=callback_data),
        InlineKeyboardButton(text=HOME, callback_data="menu:back"),
    )
    return kb.as_markup()


def build_viewed_kb(callback_data: str, text: str | None = None, lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if text is None:
        text = t("kb.viewed", lang)
    kb.button(text=text, callback_data=callback_data)
    kb.adjust(1)
    return kb.as_markup()


# --- Подтверждения / да-нет ---


def build_confirm_kb(
    yes_callback: str,
    no_callback: str,
    yes_text: str | None = None,
    no_text: str | None = None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if yes_text is None:
        yes_text = t("kb.yes", lang)
    if no_text is None:
        no_text = t("kb.cancel", lang)
    kb.button(text=yes_text, callback_data=yes_callback)
    kb.button(text=no_text, callback_data=no_callback)
    kb.adjust(2)
    return kb.as_markup()


# --- Пагинация (стрелочки) ---


def build_pagination_kb(
    prev_callback: str | None,
    next_callback: str | None,
    back_callback: str | None = None,
    lang: str = "ru",
) -> InlineKeyboardMarkup:
    
    rows: list[list[InlineKeyboardButton]] = []

    arrow_row: list[InlineKeyboardButton] = []
    if prev_callback is not None:
        arrow_row.append(InlineKeyboardButton(text="⬅️", callback_data=prev_callback))
    if next_callback is not None:
        arrow_row.append(InlineKeyboardButton(text="➡️", callback_data=next_callback))
    if arrow_row:
        rows.append(arrow_row)

    if back_callback is not None:
        rows.append(
            [InlineKeyboardButton(text=t("kb.back_to_menu", lang), callback_data=back_callback)]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Админ-меню  ---

def build_admin_menu(lang: str = "ru") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    # 1 ряд
    kb.button(text="📊 Статистика сегодня", callback_data="admin:stats")
    kb.button(text="📈 Активность", callback_data="admin:activity")

    # 2 ряд
    kb.button(text=t("kb.admin.roles", lang), callback_data="admin:roles")
    kb.button(text=t("kb.admin.broadcast", lang), callback_data="admin:broadcast")

    # 3 ряд
    kb.button(text=t("kb.admin.users", lang), callback_data="admin:users")
    kb.button(text=t("kb.admin.logs", lang), callback_data="admin:logs")

    # 4 ряд
    kb.button(text=t("kb.admin.premium", lang), callback_data="admin:premium")
    kb.button(text=t("kb.admin.credits", lang), callback_data="admin:credits")

    # 5 ряд
    kb.button(text="📣 Реклама", callback_data="admin:ads")
    kb.button(text="🖼 Фотографии", callback_data="admin:photos")

    # 6 ряд
    kb.button(text=t("kb.admin.settings", lang), callback_data="admin:settings")

    # 7 ряд
    kb.button(text=t("kb.back_to_menu", lang), callback_data="menu:back")

    kb.adjust(2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()
