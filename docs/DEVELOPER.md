# Developer Guide

## Project Structure

```
bidirectional-poc/
├── main.py                 # FastAPI server, REST API, WebSocket, call orchestration
├── agent_worker.py         # LiveKit voice agent (LLM + STT + TTS + VAD pipeline)
├── call_manager.py         # CSV state management, transcript storage
├── config.py               # Environment variables + startup validation
├── requirements.txt        # Python dependencies
├── railway.json            # Railway deployment config
├── Procfile                # Heroku-compatible process definition
├── .env.example            # Environment variable template
├── .gitignore              # Git exclusions
├── CLAUDE.md               # AI assistant conventions
├── sample_claims.csv       # Example CSV format
├── static/
│   ├── index.html          # Dashboard UI (3-panel layout)
│   ├── login.html          # Password login page
│   ├── app.js              # Dashboard logic + WebSocket client
│   └── styles.css          # Dashboard styling
├── docs/
│   ├── CALL_FLOW.md        # Call flow documentation
│   └── DEVELOPER.md        # This file
├── transcripts/            # Saved call transcripts (gitignored)
└── call_results/           # Interim JSON results (gitignored)
```

## Local Development Setup

### Prerequisites

- Python 3.11+
- LiveKit Cloud account with SIP trunk configured
- API keys: Groq, Deepgram, (optional) ElevenLabs

### Quick Start

```bash
# Clone and install
git clone <repo-url>
cd bidirectional-poc
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Fill in all required values in .env

# Start agent worker (Terminal 1)
python agent_worker.py dev

# Start FastAPI server (Terminal 2)
python main.py

# Open dashboard
# http://localhost:3000
```

### Why Two Terminals Locally?

In local development (`devmode=True`), the LiveKit agent worker runs separately so it can auto-reload on code changes. On Railway, both run in a single process — `main.py` starts the agent worker in-process via `AgentServer`.

## Key Concepts

### Call Lifecycle

1. **CSV Upload** → `CallManager.load_csv()` stores rows in memory
2. **Start Calls** → `call_processing_loop()` iterates pending claims
3. **Per Call**: Create LiveKit room → SIP call → Agent joins → Conversation → Results
4. **Results**: Agent writes `call_results/{claim}.json` → `main.py` reads it → Updates CSV
5. **Cleanup**: Room deleted, transcript saved

### Agent Tools

The AI agent has 3 function tools it can call during conversation:

- `save_claim_status` — Store verified claim data
- `confirm_details` — Mark that rep confirmed the summary
- `mark_unable_to_verify` — Handle cases where claim can't be verified

Tools are defined as decorated async functions in `agent_worker.py` and passed to the `Agent` constructor.

### WebSocket Events

The dashboard connects via WebSocket at `/ws` and receives these event types:

| Event | When | Payload |
|-------|------|---------|
| `csv_loaded` | CSV uploaded | `count`, `rows` |
| `status` | State change | `message` |
| `call_started` | Call begins | `claim_number`, `claim_data` |
| `transcript_line` | During call | `speaker`, `text` |
| `call_active` | Heartbeat | `claim_number`, `elapsed` |
| `call_completed` | Call done | `claim_number`, `results`, `stats` |
| `call_failed` | Call error | `claim_number`, `reason` |
| `call_no_answer` | No answer | `claim_number`, `stats` |

## Common Modifications

### Adding a New Agent Tool

1. Define the tool in `agent_worker.py`:

```python
@function_tool(
    name="my_tool",
    description="What this tool does — the LLM reads this.",
)
async def my_tool(ctx: RunContext, param1: str, param2: str = ""):
    # Access session data
    ctx.session.userdata["my_key"] = param1
    logger.info(f"TOOL my_tool: {param1}")
    # Return instruction for the agent
    return "Done. Now do the next thing."
```

2. Add it to the `Agent` tools list:

```python
agent = Agent(
    ...
    tools=[save_claim_status, confirm_details, mark_unable_to_verify, my_tool],
)
```

3. Update the system prompt in `get_system_prompt()` if the agent needs instructions on when to use it.

### Modifying the Conversation Prompt

Edit `get_system_prompt()` in `agent_worker.py`. The prompt is structured as:

- **VOICE & TONE**: How to speak
- **CRITICAL RULES**: What never to do
- **DATA ACCURACY**: Validation rules for dates/amounts
- **CLAIM INFO**: Dynamic claim data (injected from CSV)
- **CALL FLOW**: Step-by-step conversation script

### Adding a New Environment Variable

1. Add to `.env.example` with a comment
2. Load in `config.py` with a sensible default
3. If required, add to `_REQUIRED` dict in `config.py`
4. Use as `config.MY_VAR` in other files

### Adding a New API Endpoint

Add to `main.py`. The auth middleware protects all routes except those in `public_paths`. Example:

```python
@app.post("/api/my-endpoint")
async def my_endpoint():
    # This is automatically protected by auth middleware
    return {"result": "ok"}
```

## Testing with Your Own Phone

1. Set `insurance_phone` in your CSV to your own phone number in E.164 format (e.g., `+14155551234`)
2. Upload the CSV and start calls
3. Your phone will ring — answer and play the role of the insurance rep
4. The AI agent will follow its script and attempt to collect claim info from you

## Railway Deployment

### First Deploy

1. Push code to GitHub
2. Create a new project in [Railway](https://railway.app)
3. Connect your GitHub repo
4. Add all required environment variables in Settings > Variables
5. Deploy

### Configuration

`railway.json` defines:
- **Builder**: RAILPACK (auto-detects Python)
- **Start command**: `python main.py`
- **Health check**: `GET /api/health` (30s timeout)
- **Restart policy**: ON_FAILURE, max 3 retries

### Production Checklist

- [ ] Set `DASHBOARD_PASSWORD` to a strong password
- [ ] Set `ALLOWED_ORIGINS` to your Railway domain
- [ ] Verify all 6 required env vars are set
- [ ] Test SIP trunk connectivity with a test call
- [ ] Monitor logs for startup validation errors

## Monitoring & Debugging

### Logs

- `[outbound-caller]` — FastAPI server, call orchestration
- `[claim-agent]` — Agent worker, conversation, tool calls
- `[call-manager]` — CSV operations, state changes
- `[livekit.agents]` — LiveKit agent framework internals
- `[config]` — Startup validation

### Useful Log Patterns

```bash
# See all tool calls
grep "TOOL " logs.txt

# See call outcomes
grep "Updated claim" logs.txt

# See SIP call dispatches
grep "SIP call" logs.txt

# See agent conversation
grep "Agent:\|Human:" logs.txt
```

### Files to Check

- `call_results/{claim}.json` — Interim results (deleted after processing)
- `transcripts/{claim}.txt` — Full call transcripts
- `claims.csv` — Updated CSV with all results

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| App exits on startup | Missing required env var | Check error message, fill in `.env` |
| 403 on WebSocket | Not logged in | Log in at `/login` first |
| "Invalid phone number format" | Phone not in E.164 | Use `+` prefix with country code |
| Call marked "no-answer" too fast | SIP not connecting | Check SIP trunk config, increase `MIN_CALL_WAIT` |
| ElevenLabs TTS silent | WS fails on Railway | Use `TTS_PROVIDER=deepgram` |
| Agent doesn't respond | Groq key invalid | Verify `GROQ_API_KEY` at console.groq.com |
| CSV upload rejected | Missing required columns | Need: patient_name, member_id, insurance_phone, claim_number |
| "File too large" on upload | CSV exceeds size limit | Increase `MAX_CSV_SIZE_MB` or split CSV |
