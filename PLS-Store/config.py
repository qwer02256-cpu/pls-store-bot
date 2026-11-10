import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
REVIEWS_URL = os.getenv("REVIEWS_URL", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data/store.db"))
_BASE_DIR = Path(__file__).resolve().parent
_banner_env = os.getenv("BANNER_PATH", "").strip()
BANNER_PATH = Path(_banner_env) if _banner_env else (_BASE_DIR / "assets" / "banner.png")
if not BANNER_PATH.is_absolute():
    BANNER_PATH = _BASE_DIR / BANNER_PATH

PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "").strip()
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "").strip()
PLATEGA_CALLBACK_BASE = os.getenv("PLATEGA_CALLBACK_BASE", "").strip().rstrip("/")
PLATEGA_CALLBACK_PORT = int(os.getenv("PLATEGA_CALLBACK_PORT", "8080"))

ASSETS_DIR = _BASE_DIR / "assets"
BANNERS_DIR = ASSETS_DIR / "banners"
BANNERS_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def require_config():
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    try:
        global ADMIN_ID
        ADMIN_ID = int(ADMIN_ID_RAW)
    except ValueError:
        raise RuntimeError("ADMIN_ID должен быть числовым Telegram ID администратора")


ADMIN_ID = 0
try:
    if ADMIN_ID_RAW:
        ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    pass
