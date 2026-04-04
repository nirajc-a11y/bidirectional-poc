import os
from dotenv import load_dotenv

load_dotenv()

# LiveKit
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
LIVEKIT_SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")

# LLM
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# STT
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

# TTS
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "elevenlabs")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_SPEAKER = os.getenv("SARVAM_SPEAKER", "amelia")

# Agent
AGENT_NAME = os.getenv("AGENT_NAME", "Sarah")
PROVIDER_NAME = os.getenv("PROVIDER_NAME", "ABC Medical Group")

# Paths
CSV_PATH = os.getenv("CSV_PATH", "claims.csv")
TRANSCRIPTS_DIR = os.getenv("TRANSCRIPTS_DIR", "transcripts")
