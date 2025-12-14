from datetime import datetime, timedelta
from utils.time import get_moscow_now

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InputMediaPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from keyboards.common import build_back_to_menu_kb

from database import (
    get_moscow_today,
    get_weekly_best_photo,
    get_daily_top_photos,
)

router = Router()

# ========== Итоги дня ==========
def build_day_nav_kb(day_key: str, step: int) -> InlineKeyboardMarkup:
    """
    Построить навигацию по шагам итогов дня.

    step 0: заставка — кнопки «Вперёд», «В меню»
    step 1–3: 3 / 2 / 1 место — кнопки «Назад», «Вперёд»
    step 4: топ-10 — кнопки «Назад», «В меню»
    """
    if step <= 0:
        # Стартовый экран: только вперёд + в меню
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏠 В меню",
                        callback_data=f"menu:back",
                    ),
                    InlineKeyboardButton(
                        text="➡️ Вперёд",
                        callback_data=f"results:day:{day_key}:1",
                    ),
                ]
            ]
        )

    if 1 <= step <= 3:
        prev_step = step - 1
        next_step = step + 1
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"results:day:{day_key}:{prev_step}",
                    ),
                    InlineKeyboardButton(
                        text="➡️ Вперёд",
                        callback_data=f"results:day:{day_key}:{next_step}",
                    ),
                ]
            ]
        )

    # step >= 4 — экран топ-10: назад и в меню
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"results:day:{day_key}:3",
                ),
                InlineKeyboardButton(
                    text="🏠 В меню",
                    callback_data="menu:back",
                ),
            ]
        ]
    )


async def _show_text_result(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """
    UX-правило:
    1) пытаемся отредактировать текущее сообщение;
    2) если не вышло — удаляем его и отправляем новое.
    """
    msg = callback.message

    # 1) Пробуем отредактировать
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await msg.edit_text(text, reply_markup=reply_markup)
        return
    except Exception:
        pass

    # 2) Фоллбек: удаляем и отправляем новое
    try:
        await msg.delete()
    except Exception:
        pass

    try:
        await msg.bot.send_message(
            chat_id=msg.chat.id,
            text=text,
            reply_markup=reply_markup,
            disable_notification=True,
        )
    except Exception:
        # прям самый последний шанс
        try:
            await msg.answer(text, reply_markup=reply_markup)
        except Exception:
            pass
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
            )
        else:
            await callback.message.edit_text(
                text,
                reply_markup=reply_markup,
            )
    except Exception:
        try:
            await callback.message.bot.send_message(
                chat_id=callback.message.chat.id,
                text=text,
                reply_markup=reply_markup,
                disable_notification=True,
            )
        except Exception:
            try:
                await callback.message.answer(
                    text,
                    reply_markup=reply_markup,
                )
            except Exception:
                pass


async def _show_photo_result(
    callback: CallbackQuery,
    file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=file_id,
                caption=caption,
            ),
            reply_markup=reply_markup,
        )
        return
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass

        try:
            await callback.message.bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=file_id,
                caption=caption,
                reply_markup=reply_markup,
                disable_notification=True,
            )
        except Exception:
            await _show_text_result(callback, caption, reply_markup)


def _label_for_day(day_key: str) -> str:
    now = get_moscow_now()
    today = get_moscow_today()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    if day_key == today:
        return "сегодняшнего дня"
    if day_key == yesterday:
        return "вчерашнего дня"
    return f"дня {day_key}"


async def _render_results_day(callback: CallbackQuery, day_key: str, step: int) -> None:
    label = _label_for_day(day_key)
    # Клавиатура «назад в меню» на случай, если вообще нет фоток
    kb_back_menu = build_back_to_menu_kb()

    top = await get_daily_top_photos(day_key, limit=10)

    if not top:
        text = (
            f"📭 За {label} пока нет ни одной фотографии с оценками.\n\n"
            "Итоги появятся, когда пользователи начнут оценивать работы."
        )
        await _show_text_result(callback, text, kb_back_menu)
        await callback.answer()
        return

    # Навигация для текущего шага
    nav_kb = build_day_nav_kb(day_key, step)

    # ---------- ШАГ 0: заставка ----------
    if step <= 0:
        # Преобразуем ключ дня в человекочитаемую дату
        try:
            day_dt = datetime.fromisoformat(day_key)
            day_str = day_dt.strftime("%d.%m.%Y")
        except Exception:
            day_str = day_key

        today_dt = get_moscow_now().date()
        today_str = today_dt.strftime("%d.%m.%Y")

        text = (
            f"📅 <b>Итоги дня ({day_str})</b>\n"
            f"Сегодня: {today_str}\n\n"
            "Нажимай «Вперёд», чтобы увидеть:\n"
            "• 🥉 3 место дня\n"
            "• 🥈 2 место дня\n"
            "• 🥇 1 место дня\n"
            "• 📊 Топ-10 фотографий дня."
        )
        await _show_text_result(callback, text, nav_kb)
        await callback.answer()
        return

    total = len(top)

    # ---------- ШАГИ 1–3: 3 / 2 / 1 место ----------
    if step in (1, 2, 3):
        # Определяем, какое место показываем и какой индекс в списке top
        if step == 1:
            place_num = 3
            if total < 3:
                msg = (
                    f"ℹ️ За {label} недостаточно работ, чтобы показать "
                    "3 место."
                )
                await _show_text_result(callback, msg, nav_kb)
                await callback.answer()
                return
            item = top[2]
        elif step == 2:
            place_num = 2
            if total < 2:
                msg = (
                    f"ℹ️ За {label} недостаточно работ, чтобы показать "
                    "2 место."
                )
                await _show_text_result(callback, msg, nav_kb)
                await callback.answer()
                return
            item = top[1]
        else:  # step == 3
            place_num = 1
            # здесь total >= 1 гарантированно, так как top не пустой
            item = top[0]

        # Оформляем автора: имя в кликабельных скобках, если есть username
        author_name = item.get("user_name") or ""
        username = item.get("user_username")
        if username:
            link_text = author_name or f"@{username}"
            # Имя или @username, кликабельная ссылка на профиль
            author_display = f'<a href="https://t.me/{username}">{link_text}</a>'
        elif author_name:
            author_display = author_name
        else:
            author_display = "Неизвестный автор"

        avg = item.get("avg_rating")
        if avg is not None:
            avg_str = f"{avg:.2f}".rstrip("0").rstrip(".")
        else:
            avg_str = "—"

        medal_map = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medal_map.get(place_num, "🏅")

        caption_lines = [
            f"{medal} <b>{place_num} место {label}</b>",
            "",
            f"<code>\"{item['title']}\"</code>",
            f"Автор: {author_display}",
            "",
            f"Рейтинг: <b>{avg_str}</b>",
        ]
        caption = "\n".join(caption_lines)

        await _show_photo_result(
            callback=callback,
            file_id=item["file_id"],
            caption=caption,
            reply_markup=nav_kb,
        )
        await callback.answer()
        return

    # ---------- ШАГ 4: текстовый топ-10 ----------
    lines: list[str] = [f"📊 <b>Топ-10 фотографий {label}</b>", ""]

    for i, item in enumerate(top, start=1):
        avg = item.get("avg_rating")
        if avg is not None:
            avg_str = f"{avg:.2f}".rstrip("0").rstrip(".")
        else:
            avg_str = "—"

        medal_map = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medal_map.get(i, "▪️")

        # В топ-10 не показываем авторов: только места и названия.
        # Для 1–3 места не дублируем среднюю оценку.
        if i <= 3:
            lines.append(
                f"{medal} {i} место - <b>\"{item['title']}\"</b>"
            )
        else:
            lines.append(
                f"{medal} {i} место - <b>\"{item['title']}\"</b>"
            )
            lines.append(
                f"рейтинг: <b>{avg_str}</b>"
            )

        # После первых трёх мест добавляем пустую строку, чтобы отделить их от остальных
        if i == 3 and len(top) > 3:
            lines.append("")

    text = "\n".join(lines)
    # Для топ-10 step всегда считается как 4, но навигацию на всякий случай
    nav = build_day_nav_kb(day_key, step=4)

    await _show_text_result(callback, text, nav)
    await callback.answer()


@router.callback_query(F.data == "results:day")
async def results_day(callback: CallbackQuery):
    """
    Итоги дня всегда считаем за вчерашний календарный день по Москве,
    НО показываем их только после 07:00 по московскому времени.
    """
    now = get_moscow_now()

    # До 07:00 по МСК итоги за вчера ещё «готовятся»
    if now.hour < 7:
        kb = build_back_to_menu_kb()
        text = (
            "⏰ Итоги дня появляются каждый день после <b>07:00 по МСК</b>.\n\n"
            f"Сейчас: <b>{now.strftime('%H:%M')}</b>.\n"
            "Загляни чуть позже, когда мы полностью подсчитаем оценки за вчерашний день."
        )
        await _show_text_result(callback, text, kb)
        await callback.answer()
        return

    # После 07:00 считаем итоги за вчерашний календарный день
    day_key = (now.date() - timedelta(days=1)).isoformat()
    await _render_results_day(callback, day_key, step=0)


@router.callback_query(F.data.startswith("results:day:"))
async def results_day_nav(callback: CallbackQuery):
    try:
        _, _, day_key, step_str = callback.data.split(":", 3)
        step = int(step_str)
    except Exception:
        await callback.answer()
        return

    if step < 0:
        step = 0
    if step > 4:
        step = 4

    await _render_results_day(callback, day_key, step)

# ========== Итоги недели ==========

@router.callback_query(F.data == "results:week")
async def results_week(callback: CallbackQuery):
    now = get_moscow_now()

    if now.weekday() != 6 or (now.hour, now.minute) < (21, 0):
        kb = build_back_to_menu_kb()
        text = "Итоги недели доступны каждое воскресенье после 21:00 по МСК."
        await _show_text_result(callback, text, kb)
        await callback.answer()
        return

    today = now.date()
    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()

    winner = await get_weekly_best_photo(start, end)

    kb = build_back_to_menu_kb()

    if winner is None:
        text = (
            "На этой неделе не нашлось ни одной работы\n"
            "со средней оценкой <b>9.0</b> и выше.\n\n"
            "Неделя без абсолютного фаворита."
        )
        await _show_text_result(callback, text, kb)
        await callback.answer()
        return

    author_name = winner.get("user_name") or ""
    username = winner.get("user_username")
    if username:
        if author_name:
            author_display = f"{author_name} (@{username})"
        else:
            author_display = f"@{username}"
    elif author_name:
        author_display = author_name
    else:
        author_display = "Неизвестный автор"

    avg = winner.get("avg_rating")
    count = winner.get("ratings_count") or 0

    caption_lines = [
        "🌟 <b>Фотография недели</b>",
        "",
        f"<b>\"{winner['title']}\"</b>",
        f"Автор: {author_display}",
    ]
    if avg is not None:
        avg_str = f"{avg:.2f}".rstrip("0").rstrip(".")
        caption_lines.append(f"Средняя оценка: <b>{avg_str}</b> ({count} голосов)")
    caption = "\n".join(caption_lines)

    await _show_photo_result(
        callback=callback,
        file_id=winner["file_id"],
        caption=caption,
        reply_markup=kb,
    )
    await callback.answer()