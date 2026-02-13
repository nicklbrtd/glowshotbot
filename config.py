import os
from pathlib import Path
from datetime import time
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
MASTER_ADMIN_ID = int(os.getenv("MASTER_ADMIN_ID"))
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN")
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID"))
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/supofglowshotbot")

REQUIRED_CHANNEL_ID = os.getenv("@nyqcreative")
REQUIRED_CHANNEL_LINK = os.getenv("https://t.me/nyqcreative")
AD_CHANNEL_LINK = os.getenv("https://t.me/glowshotchannel")

# ===== Moderation notifications =====
# Optional: if set, all report notifications and threshold cards will be sent to this chat (group/supergroup).
# If not set, the bot will fallback to sending DMs to each moderator from get_moderators().
_MODERATION_CHAT_ID_RAW = (os.getenv("MODERATION_CHAT_ID") or "").strip()
MODERATION_CHAT_ID = None
if _MODERATION_CHAT_ID_RAW:
    try:
        MODERATION_CHAT_ID = int(_MODERATION_CHAT_ID_RAW)
    except ValueError:
        MODERATION_CHAT_ID = None

# ===== Author verification submissions =====
# Optional: separate group chat for author verification requests. If empty, bot falls back to MODERATION_CHAT_ID.
_AUTHOR_APPLICATIONS_CHAT_ID_RAW = (os.getenv("AUTHOR_APPLICATIONS_CHAT_ID") or "").strip()
AUTHOR_APPLICATIONS_CHAT_ID = None
if _AUTHOR_APPLICATIONS_CHAT_ID_RAW:
    try:
        AUTHOR_APPLICATIONS_CHAT_ID = int(_AUTHOR_APPLICATIONS_CHAT_ID_RAW)
    except ValueError:
        AUTHOR_APPLICATIONS_CHAT_ID = None
else:
    # Default group for author verification requests (fallback if .env is not set)
    AUTHOR_APPLICATIONS_CHAT_ID = -1003728717861

# ===== Feedback / Ideas =====
_FEEDBACK_CHAT_ID_RAW = (os.getenv("FEEDBACK_CHAT_ID") or "").strip()
FEEDBACK_CHAT_ID = None
if _FEEDBACK_CHAT_ID_RAW:
    try:
        FEEDBACK_CHAT_ID = int(_FEEDBACK_CHAT_ID_RAW)
    except ValueError:
        FEEDBACK_CHAT_ID = None
else:
    FEEDBACK_CHAT_ID = -1003726130918

# ===== Rating tutorial photo =====
RATE_TUTORIAL_PHOTO_FILE_ID = os.getenv(
    "RATE_TUTORIAL_PHOTO_FILE_ID",
    "AgACAgIAAxkBAAIg2GmDn0m5yO122K7pVB2_9j_sGOegAAK2DWsbRt4hSBEpPEq_thR_AQADAgADdwADOAQ",
)

# ===== Rating feed tuning =====
RATE_POPULAR_MIN_RATINGS = int(os.getenv("RATE_POPULAR_MIN_RATINGS", "10"))
RATE_LOW_RATINGS_MAX = int(os.getenv("RATE_LOW_RATINGS_MAX", "2"))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Moscow")


def _parse_time(raw: str | None, default: str) -> time:
    val = (raw or default).strip()
    parts = val.split(":")
    try:
        h = int(parts[0]) if parts and parts[0] else 0
        m = int(parts[1]) if len(parts) > 1 else 0
        return time(hour=max(0, min(23, h)), minute=max(0, min(59, m)))
    except Exception:
        return _parse_time(default, default) if raw != default else time(15, 0)


HAPPY_HOUR_START = _parse_time(os.getenv("HAPPY_HOUR_START"), "15:00")
HAPPY_HOUR_END = _parse_time(os.getenv("HAPPY_HOUR_END"), "16:00")
CREDIT_SHOWS_BASE = int(os.getenv("CREDIT_SHOWS_BASE", "2"))
CREDIT_SHOWS_HAPPY = int(os.getenv("CREDIT_SHOWS_HAPPY", "4"))
MIN_VOTES_FOR_TOP = int(os.getenv("MIN_VOTES_FOR_TOP", "7"))
ANTI_ABUSE_MAX_VOTES_PER_AUTHOR_PER_DAY = int(os.getenv("ANTI_ABUSE_MAX_VOTES_PER_AUTHOR_PER_DAY", "5"))
PORTFOLIO_TOP_N = int(os.getenv("PORTFOLIO_TOP_N", "9"))
TAIL_PROBABILITY = float(os.getenv("TAIL_PROBABILITY", "0.05"))
MIN_VOTES_FOR_NORMAL_FEED = int(os.getenv("MIN_VOTES_FOR_NORMAL_FEED", "5"))

# ===== Manual RUB (card transfer) =====
# Toggle manual RUB flow (card transfer + user sends receipt)
MANUAL_RUB_ENABLED = os.getenv("MANUAL_RUB_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# Card details shown to user
MANUAL_CARD_NUMBER = os.getenv("MANUAL_CARD_NUMBER", "").strip()
MANUAL_RECIPIENT = os.getenv("MANUAL_RECIPIENT", "").strip()
MANUAL_BANK_HINT = os.getenv("MANUAL_BANK_HINT", "").strip() or "Любой банк"

# Optional support contact shown in instructions
MANUAL_CONTACT = os.getenv("MANUAL_CONTACT", "").strip() or os.getenv("SUPPORT_URL", "") or "@nyqlbrtd"

TB_PASSWORD = os.getenv("TB_PASSWORD", "")
TB_TERMINAL_KEY = os.getenv("TB_TERMINAL_KEY", "")
