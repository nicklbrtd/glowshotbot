from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.time import get_moscow_now

# --- Главное меню ---


def build_main_menu(
    is_admin: bool = False,
    is_moderator: bool = False,
    is_premium: bool = False,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()


    # Базовые пользовательские кнопки
    kb.button(text="📸 Моя фотография", callback_data="myphoto:open")
    kb.button(text="🔥 Оценивать", callback_data="rate:start")
    kb.button(text="🏆 Итоги дня", callback_data="results:day")
    kb.button(text="👤 Профиль", callback_data="profile:open")

    now = get_moscow_now()
    is_sunday = now.weekday() == 6  # воскресенье
    if is_sunday:
        kb.button(text="🌟 Итоги недели", callback_data="results:week")

    if is_moderator:
        kb.button(text="🛡 Модератор", callback_data="moderator:menu")

    if is_admin:
        kb.button(text="⚙️ Админ-панель", callback_data="admin:menu")

    kb.adjust(2, 2, 2)

    return kb.as_markup()


# --- Кнопки "назад / в меню" ---


def build_back_to_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(1)
    return kb.as_markup()


def build_back_kb(callback_data: str, text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback_data)
    kb.adjust(1)
    return kb.as_markup()


def build_viewed_kb(callback_data: str, text: str = "✅ Просмотрено") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback_data)
    kb.adjust(1)
    return kb.as_markup()


# --- Подтверждения / да-нет ---


def build_confirm_kb(
    yes_callback: str,
    no_callback: str,
    yes_text: str = "✅ Да",
    no_text: str = "❌ Отмена",
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=yes_text, callback_data=yes_callback)
    kb.button(text=no_text, callback_data=no_callback)
    kb.adjust(2)
    return kb.as_markup()


# --- Пагинация (стрелочки) ---


def build_pagination_kb(
    prev_callback: str | None,
    next_callback: str | None,
    back_callback: str | None = None,
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
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=back_callback)]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- Админ-меню  ---

def build_admin_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    # 1 ряд
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="👥 Роли", callback_data="admin:roles")

    # 2 ряд
    kb.button(text="📣 Рассылка", callback_data="admin:broadcast")
    kb.button(text="🙍‍♂️ Пользователи", callback_data="admin:users")

    # 3 ряд
    kb.button(text="🧾 Логи / ошибки", callback_data="admin:logs:page:1")

    # 4 ряд
    kb.button(text="⚙️ Настройки", callback_data="admin:settings")
    kb.button(text="⬅️ В меню", callback_data="menu:back")

    kb.adjust(2, 2, 1, 2)
    return kb.as_markup()
