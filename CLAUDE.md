# CLAUDE.md — Project Conventions

## What This Is

Outbound AI calling system for medical claim verification. An AI voice agent calls insurance companies via SIP, follows a conversation script, captures claim status, and writes results back to a CSV.

## Tech Stack

- **Backend**: FastAPI + Uvicorn (Python 3.11+)
- **Voice Agent**: LiveKit Agents 1.5 framework
- **LLM**: Groq (Meta Llama 4 Scout 17B) via OpenAI-compatible API
- **STT**: Deepgram Nova-3
- **TTS**: Deepgram Aura-2 (default), ElevenLabs (optional)
- **VAD**: Silero
- **Voice Infra**: LiveKit Cloud + SIP Trunk (Twilio/Telnyx)
- **Frontend**: Vanilla HTML/CSS/JS dashboard (no framework)
- **Deployment**: Railway

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server, REST API, WebSocket, SIP orchestration, call loop |
| `agent_worker.py` | LiveKit voice agent — LLM/STT/TTS pipeline, conversation tools, transcript capture |
| `call_manager.py` | CSV read/write, call state tracking, transcript storage |
| `config.py` | Environment variable loading with startup validation |
| `static/index.html` | Dashboard UI |
| `static/app.js` | Dashboard logic + WebSocket client |

## Architecture

- **Single-process on Railway**: `main.py` starts the agent worker in-process via `AgentServer`. No separate agent process needed.
- **Two-process locally**: Run `python agent_worker.py dev` in one terminal, `python main.py` in another.
- **No database**: All state is in CSV files and JSON result files on disk.
- **Sequential calls**: One call at a time. No parallel calling.

## Important Gotchas

- ElevenLabs WebSocket TTS fails on Railway (Debian/Python 3.13). Always default to `TTS_PROVIDER=deepgram`.
- The SIP participant identity is always `"insurance-rep"` — hardcoded in both `main.py` and `agent_worker.py`.
- Room names include a UUID suffix to prevent collisions: `call-{claim_number}-{hex}`.
- `config.validate()` runs at import time — the app exits immediately if required env vars are missing.
- Phone numbers must be E.164 format (`+1234567890`). Invalid numbers are rejected before SIP dispatch.
- Claim numbers are sanitized (`^[a-zA-Z0-9_.-]+$`) before any filesystem operation.

## Running Commands

```bash
# Install deps
pip install -r requirements.txt

# Local dev (two terminals)
python agent_worker.py dev
python main.py

# Production (single process)
python main.py
```

## Testing

No automated tests yet. Manual testing:
1. Upload CSV via dashboard
2. Start calls, monitor transcript in real-time
3. Verify results in downloaded CSV
4. Check `transcripts/` and `call_results/` directories

## Code Style

- Python: standard library logging, no third-party logging framework
- Async everywhere in FastAPI routes and agent code
- Thread-safe CSV operations via `RLock` in `CallManager`
- All env vars loaded in `config.py`, not scattered across files
