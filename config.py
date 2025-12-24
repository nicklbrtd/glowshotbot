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
AD_CHANNEL_LINK = os.getenv("https://t.me/glowshorchanel")

# ===== Manual RUB (card transfer) =====
# Toggle manual RUB flow (card transfer + user sends receipt)
MANUAL_RUB_ENABLED = os.getenv("MANUAL_RUB_ENABLED", "1").strip().lower() in ("1", "true", "yes")

# Card details shown to user
MANUAL_CARD_NUMBER = os.getenv("MANUAL_CARD_NUMBER", "").strip()
MANUAL_RECIPIENT = os.getenv("MANUAL_RECIPIENT", "").strip()
MANUAL_BANK_HINT = os.getenv("MANUAL_BANK_HINT", "").strip() or "Любой банк"

# Optional support contact shown in instructions
MANUAL_CONTACT = os.getenv("MANUAL_CONTACT", "").strip() or os.getenv("SUPPORT_URL", "") or "@your_username"

TB_PASSWORD = os.getenv("TB_PASSWORD", "")
TB_TERMINAL_KEY = os.getenv("TB_TERMINAL_KEY", "")