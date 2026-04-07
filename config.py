import logging
import os
import secrets
import sys

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("config")

# --- LiveKit ---
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")

# --- LLM ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")

# --- STT ---
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

# --- TTS ---
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "deepgram")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
TTS_VOICE_CARTESIA = os.getenv("TTS_VOICE_CARTESIA", "79a125e8-cd45-4c13-8a67-188112f4dd22")

# --- Agent ---
AGENT_NAME = os.getenv("AGENT_NAME", "Sarah")
PROVIDER_NAME = os.getenv("PROVIDER_NAME", "ABC Medical Group")

# --- Dashboard ---
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# --- Server ---
try:
    PORT = int(os.getenv("PORT", "3000"))
except ValueError:
    logger.error("PORT must be an integer, defaulting to 3000")
    PORT = 3000

# --- CORS ---
_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _origins.split(",") if o.strip()]

# --- Paths ---
CSV_PATH = os.getenv("CSV_PATH", "claims.csv")
TRANSCRIPTS_DIR = os.getenv("TRANSCRIPTS_DIR", "transcripts")

# --- Tunable constants ---
ROOM_EMPTY_TIMEOUT = int(os.getenv("ROOM_EMPTY_TIMEOUT", "300"))
CALL_TIMEOUT = int(os.getenv("CALL_TIMEOUT", "600"))
CALL_DELAY = int(os.getenv("CALL_DELAY", "2"))
MAX_CSV_SIZE_MB = int(os.getenv("MAX_CSV_SIZE_MB", "10"))
MIN_CALL_WAIT = int(os.getenv("MIN_CALL_WAIT", "30"))

# --- Rate limiting ---
LOGIN_MAX_ATTEMPTS = 3
LOGIN_WINDOW_SECONDS = 300

# --- Session ---
# WARNING: if SESSION_SECRET is not set, a random secret is generated on each
# startup — any existing browser sessions will be invalidated on every restart.
# Set SESSION_SECRET to a stable random value in production (e.g. `openssl rand -hex 32`).
_session_secret_from_env = os.getenv("SESSION_SECRET", "")
if not _session_secret_from_env and os.getenv("DASHBOARD_PASSWORD"):
    logger.warning(
        "SESSION_SECRET is not set but DASHBOARD_PASSWORD is — sessions will be "
        "invalidated on every restart. Set SESSION_SECRET in your environment."
    )
SESSION_SECRET = _session_secret_from_env or secrets.token_hex(32)

# --- Logging ---
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")  # "text" or "json"

# --- IVR ---
IVR_TIMEOUT_SECONDS = int(os.getenv("IVR_TIMEOUT_SECONDS", "90"))
IVR_MAX_ESCAPE_ATTEMPTS = int(os.getenv("IVR_MAX_ESCAPE_ATTEMPTS", "2"))

# --- SIP Retries ---
SIP_MAX_RETRIES = int(os.getenv("SIP_MAX_RETRIES", "3"))
SIP_RETRY_DELAYS = [5, 15, 30]  # seconds between retry attempts

# --- Startup validation ---
_REQUIRED = {
    "LIVEKIT_URL": LIVEKIT_URL,
    "LIVEKIT_API_KEY": LIVEKIT_API_KEY,
    "LIVEKIT_API_SECRET": LIVEKIT_API_SECRET,
    "LIVEKIT_SIP_TRUNK_ID": LIVEKIT_SIP_TRUNK_ID,
    "GROQ_API_KEY": GROQ_API_KEY,
    "DEEPGRAM_API_KEY": DEEPGRAM_API_KEY,
}


def validate():
    required = dict(_REQUIRED)
    if TTS_PROVIDER == "cartesia":
        required["CARTESIA_API_KEY"] = CARTESIA_API_KEY
    if LLM_PROVIDER == "cerebras":
        required["CEREBRAS_API_KEY"] = CEREBRAS_API_KEY
    missing = [name for name, val in required.items() if not val]
    if missing:
        logger.error(
            "Missing required environment variables: %s. "
            "Copy .env.example to .env and fill in all values.",
            ", ".join(missing),
        )
        sys.exit(1)


validate()
