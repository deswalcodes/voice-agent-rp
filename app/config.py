"""Environment-driven configuration. Nothing here is secret-bearing by default."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


VAPI_API_BASE = "https://api.vapi.ai"
VAPI_PRIVATE_KEY = env("VAPI_PRIVATE_KEY")
VAPI_PUBLIC_KEY = env("VAPI_PUBLIC_KEY")
VAPI_ASSISTANT_ID = env("VAPI_ASSISTANT_ID")
VAPI_PHONE_NUMBER_ID = env("VAPI_PHONE_NUMBER_ID")
VAPI_WEBHOOK_SECRET = env("VAPI_WEBHOOK_SECRET")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")

VAPI_MODEL_PROVIDER = env("VAPI_MODEL_PROVIDER", "openai")
VAPI_MODEL = env("VAPI_MODEL", "gpt-4.1-mini")
VAPI_VOICE_PROVIDER = env("VAPI_VOICE_PROVIDER", "vapi")
VAPI_VOICE_ID = env("VAPI_VOICE_ID", "Naina")

MERCHANT_NAME = env("MERCHANT_NAME", "StreamBox")
AGENT_NAME = env("AGENT_NAME", "Asha")

CALL_WINDOW_START = env("CALL_WINDOW_START", "09:00")
CALL_WINDOW_END = env("CALL_WINDOW_END", "20:00")
CALL_TIMEZONE = env("CALL_TIMEZONE", "Asia/Kolkata")
MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "3"))

DATA_DIR = ROOT / "data"
CUSTOMERS_FILE = DATA_DIR / "customers.json"
DB_FILE = Path(env("DB_FILE", str(DATA_DIR / "state.db")))
