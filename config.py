import os
from dotenv import load_dotenv

load_dotenv()

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

AGENT_NAME = os.getenv("AGENT_NAME", "Sarah")
PROVIDER_NAME = os.getenv("PROVIDER_NAME", "ABC Medical Group")

CSV_PATH = os.getenv("CSV_PATH", "claims.csv")
TRANSCRIPTS_DIR = os.getenv("TRANSCRIPTS_DIR", "transcripts")
