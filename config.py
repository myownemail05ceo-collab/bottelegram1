import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:5000")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
PRICE_ID = os.getenv("PRICE_ID", "")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "0"))
