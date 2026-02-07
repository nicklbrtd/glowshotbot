import os
from pathlib import Path
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
