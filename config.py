import os
from dotenv import load_dotenv

# Só carrega .env se tiver num ambiente local (não no Railway/Render)
if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    load_dotenv()

# Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Stripe (plataforma — você recebe todos os pagamentos)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Sua taxa sobre cada venda (em %, ex: 10 = 10% vai pra você)
PLATFORM_FEE_PERCENT = int(os.getenv("PLATFORM_FEE_PERCENT", "10"))

# Você (super admin do bot)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Webhook aiohttp
WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8000")))

# Bot username (preenchido automaticamente no startup)
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# Database path (Render/Railway usam filesystem efêmero — SQLite OK pra MVP)
DB_PATH = os.getenv("DB_PATH", "/tmp/subscriptions.db")
