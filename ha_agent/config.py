import os
from dotenv import load_dotenv

load_dotenv()

HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x.strip()}

_missing = [name for name, val in [
    ("HA_URL", HA_URL), ("HA_TOKEN", HA_TOKEN),
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY), ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
] if not val]
if _missing:
    raise ValueError(f"Missing required environment variables: {', '.join(_missing)}. See .env.example")
