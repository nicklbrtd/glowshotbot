# utils/subscription.py

from __future__ import annotations

import os
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "@nyqcreative")


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except TelegramBadRequest:
        return False

    return member.status in ("member", "administrator", "creator")