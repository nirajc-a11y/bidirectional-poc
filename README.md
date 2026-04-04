# Outbound AI Calling System

Automated outbound calling system for medical claim status verification. An AI voice agent calls insurance companies from a CSV of claims, follows a structured conversation script, captures responses via speech and DTMF, stores transcripts, and updates the CSV with results — all monitored through a real-time web dashboard.

## Architecture

```
┌──────────────────────┐
│   Web Dashboard      │◄──── WebSocket (real-time transcripts, status)
│   (static/*)         │
└──────────┬───────────┘
           │ REST API
┌──────────▼───────────┐       ┌─────────────────────┐
│   FastAPI Server     │       │  LiveKit Agent       │
│   (main.py)          │───────│  (agent_worker.py)   │
│   - CSV management   │       │  - Groq LLM          │
│   - SIP orchestration│       │  - Deepgram STT      │
│   - Call lifecycle   │       │  - Deepgram/11Labs TTS│
└──────────┬───────────┘       │  - Silero VAD        │
           │                   └──────────┬────────────┘
┌──────────▼───────────┐                  │
│   LiveKit Cloud      │◄─────────────────┘
│   - Room management  │
│   - SIP trunk        │───── PSTN ───── Insurance Company
│   - Audio routing    │
└──────────────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM | Groq (Meta Llama 4 Scout 17B) |
| STT | Deepgram Nova-3 |
| TTS | Deepgram Aura-2 (default) / ElevenLabs (optional) |
| VAD | Silero |
| Voice Infra | LiveKit Cloud + SIP Trunk |
| Backend | FastAPI + Uvicorn |
| Frontend | Vanilla HTML/CSS/JS dashboard |
| Deployment | Railway |

## Prerequisites

- Python 3.11+
- [LiveKit Cloud](https://cloud.livekit.io) account
- SIP trunk configured in LiveKit (via [Twilio](https://twilio.com) or [Telnyx](https://telnyx.com))
- [Groq](https://console.groq.com) API key (free)
- [Deepgram](https://console.deepgram.com) API key (free $200 credits)
- (Optional) [ElevenLabs](https://elevenlabs.io) API key for premium TTS

## SIP Trunk Setup (One-Time)

### Option A: Via LiveKit Cloud Dashboard

1. **Get a phone number**: Sign up at [Twilio](https://twilio.com) or [Telnyx](https://telnyx.com) and buy a voice-capable phone number
2. **Create SIP credentials**:
   - **Twilio**: Go to Elastic SIP Trunking > Create trunk > Add origination/termination URIs
   - **Telnyx**: Go to SIP Trunking > Create connection > Note SIP credentials
3. **Configure in LiveKit Cloud**:
   - Go to your [LiveKit Cloud dashboard](https://cloud.livekit.io)
   - Navigate to **SIP** section > **Create Outbound Trunk**
   - Fill in: Name, SIP Server Address, Auth Username, Auth Password, Phone Numbers
   - Save and copy the **SIP Trunk ID**
4. Add the trunk ID to your `.env` as `LIVEKIT_SIP_TRUNK_ID`

### Option B: Via LiveKit CLI

```bash
# Install LiveKit CLI
# macOS: brew install livekit-cli
# Windows: download from https://github.com/livekit/livekit-cli/releases

lk cloud auth

lk sip outbound create \
  --name "Twilio Outbound" \
  --address sip:your-trunk.pstn.twilio.com \
  --username your-sip-username \
  --password your-sip-password \
  --numbers "+1234567890"
```

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your credentials

# 3. Start the LiveKit agent worker (Terminal 1)
python agent_worker.py dev

# 4. Start the FastAPI server (Terminal 2)
python main.py
```

Open http://localhost:3000 in your browser.

> **Note:** On Railway, both processes run in a single command via `python main.py` (the agent worker is started in-process automatically).

## Usage

1. Open the dashboard at http://localhost:3000
2. Upload a CSV file (see format below or use `sample_claims.csv`)
3. Click **Start Calls** to begin processing
4. Watch real-time transcripts in the dashboard
5. Download the updated CSV with results when done

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `LIVEKIT_URL` | LiveKit Cloud WebSocket URL (`wss://...`) |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `LIVEKIT_SIP_TRUNK_ID` | SIP trunk ID from LiveKit dashboard |
| `GROQ_API_KEY` | Groq API key for LLM |
| `DEEPGRAM_API_KEY` | Deepgram API key for STT/TTS |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_PROVIDER` | `deepgram` | TTS engine: `deepgram` or `elevenlabs` |
| `ELEVEN_API_KEY` | — | ElevenLabs API key (if using elevenlabs TTS) |
| `ELEVEN_VOICE_ID` | `pFZP5JQG7iQjIQuC4Bku` | ElevenLabs voice ID |
| `GROQ_MODEL` | `meta-llama/llama-4-scout-17b-16e-instruct` | Groq LLM model |
| `AGENT_NAME` | `Sarah` | AI agent's name in conversations |
| `PROVIDER_NAME` | `ABC Medical Group` | Organization name |
| `DASHBOARD_PASSWORD` | — | Set to enable dashboard authentication |
| `PORT` | `3000` | Server port |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins |
| `CALL_TIMEOUT` | `600` | Max seconds to wait for call completion |
| `ROOM_EMPTY_TIMEOUT` | `300` | LiveKit room empty timeout (seconds) |
| `MAX_CSV_SIZE_MB` | `10` | Maximum CSV upload size |
| `MIN_CALL_WAIT` | `30` | Minimum seconds before checking if call was answered |

## CSV Format

### Input columns (required)

| Column | Description |
|--------|-------------|
| `patient_name` | Patient's full name |
| `member_id` | Insurance member ID |
| `insurance_phone` | Insurance phone in E.164 format (`+1...`) |
| `claim_number` | Claim reference number |

### Input columns (optional)

| Column | Description |
|--------|-------------|
| `group_number` | Group/plan number |
| `date_of_service` | Date of service (YYYY-MM-DD) |
| `procedure_code` | CPT procedure code |
| `diagnosis_code` | ICD-10 diagnosis code |
| `provider_name` | Provider name |
| `npi` | Provider NPI |
| `billed_amount` | Amount billed |

### Output columns (added automatically)

| Column | Description |
|--------|-------------|
| `call_status` | `pending` / `in-progress` / `completed` / `failed` / `no-answer` |
| `claim_result` | `approved` / `denied` / `pending` / `in-review` / `unknown` |
| `approved_amount` | If approved |
| `denial_reason` | If denied |
| `payment_date` | Expected/actual payment date |
| `appeal_deadline` | Appeal deadline if denied |
| `reference_number` | Insurance inquiry reference |
| `confirmed` | Whether info was confirmed by rep |
| `call_timestamp` | When the call was made |
| `transcript_file` | Path to saved transcript |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check (public) |
| `POST` | `/api/upload-csv` | Upload claims CSV |
| `GET` | `/api/claims` | Get all loaded claims |
| `GET` | `/api/stats` | Get call statistics |
| `POST` | `/api/start` | Start/resume call processing |
| `POST` | `/api/pause` | Pause after current call |
| `POST` | `/api/stop` | Stop processing |
| `GET` | `/api/transcript/{claim}` | Get call transcript |
| `GET` | `/api/download-csv` | Download updated CSV |
| `WS` | `/ws` | Real-time event stream |

## Deployment (Railway)

1. Push to a GitHub repository
2. Connect the repo in [Railway](https://railway.app)
3. Set all required environment variables in Railway dashboard
4. Set `DASHBOARD_PASSWORD` for production auth
5. Set `ALLOWED_ORIGINS` to your Railway domain (e.g., `https://your-app.up.railway.app`)
6. Deploy — Railway uses `railway.json` for build/start config

The health check at `/api/health` is used by Railway to verify the service is running.

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| WebSocket 403 on dashboard load | Not logged in yet | Set `DASHBOARD_PASSWORD` and log in first |
| Call immediately marked "no-answer" | Room disappears before `MIN_CALL_WAIT` | Increase `MIN_CALL_WAIT`, check SIP trunk config |
| ElevenLabs TTS fails on Railway | WebSocket streaming incompatible | Use `TTS_PROVIDER=deepgram` (default) |
| "Missing required environment variables" | Empty `.env` values | Fill in all required vars in `.env` |
| Agent doesn't speak | Groq or Deepgram key missing/invalid | Verify API keys in `.env` |
