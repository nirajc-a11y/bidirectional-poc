# Outbound AI Calling System — Design Spec

## Context

Medical claims teams need to call insurance companies to verify claim statuses. This is repetitive, time-consuming work: call the insurer, navigate to claims, read out patient/claim details, record the response. This system automates that process with an AI voice agent that makes outbound calls from a CSV of claims, conducts structured conversations with insurance reps, and records results.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Web Dashboard                       │
│         (HTML/JS — call status, transcripts)          │
└──────────────────────┬───────────────────────────────┘
                       │ REST + WebSocket
┌──────────────────────▼───────────────────────────────┐
│              FastAPI Backend (Orchestrator)            │
│  • Reads CSV, queues calls                            │
│  • Creates LiveKit rooms + SIP participants           │
│  • Stores transcripts, updates CSV                    │
│  • Serves dashboard API + WebSocket updates           │
└───────┬──────────────────────────────┬───────────────┘
        │ LiveKit Server SDK           │
┌───────▼──────────┐          ┌───────▼────────────────┐
│  LiveKit Cloud    │          │  LiveKit Agent Worker   │
│  • SIP Trunk      │◄────────►│  • VoicePipelineAgent   │
│  • Room mgmt      │          │  • Claim verification   │
│  • Audio routing   │          │  • DTMF handling        │
│  • SIP Trunk       │          │  • Transcript capture   │
│  • PSTN (Twilio/   │          └────────────────────────┘
│    Telnyx carrier) │
└──────────────────┘
```

### Components

1. **FastAPI Backend** — Orchestrates everything: reads CSV, triggers calls via LiveKit SIP API, serves dashboard, stores results
2. **LiveKit Agent Worker** — Separate Python process running `VoicePipelineAgent` that auto-joins rooms and handles AI conversation
3. **LiveKit Cloud** — Manages rooms, SIP bridge, audio routing, hosted inference (STT/TTS/LLM)
4. **LiveKit SIP Trunk** — Configured with Twilio/Telnyx as carrier for PSTN connectivity (no Plivo needed)
5. **Web Dashboard** — Single-page HTML/JS showing call queue, live transcript, results

## Call Flow

For each CSV row:

1. Backend reads next row (patient, claim #, insurance phone, etc.)
2. Backend creates a LiveKit room: `call-{claim_number}`
3. Backend dispatches SIP call via LiveKit `CreateSIPParticipant` API → routes through Plivo SIP trunk → dials insurance company
4. LiveKit Agent Worker auto-joins the room via agent dispatch
5. Insurance company answers → audio flows through LiveKit room
6. AI Agent follows the conversation script (see below)
7. On call end → transcript saved to file, CSV row updated with results
8. Backend moves to next row

## Agent Conversation Script

### Phase 1 — Introduction
```
"Hello, my name is [Agent Name], calling from [Provider/Practice Name]
 regarding a medical claim. Am I speaking with the claims department?"
```

### Phase 2 — Provide Claim Details
```
"I'd like to check the status of a claim:
 - Patient name: {patient_name}
 - Member ID: {member_id}
 - Claim number: {claim_number}
 - Date of service: {date_of_service}
 - Procedure code: {procedure_code}
 - Provider: {provider_name}, NPI: {npi}"
```

### Phase 3 — Collect Status Information
Ask and record answers for:
- Claim status (approved / denied / pending / in-review)
- If denied — denial reason and appeal deadline
- If approved — approved amount, payment date
- If pending — expected processing date
- Reference number for this inquiry

### Phase 4 — Confirmation
```
"Let me confirm what I have:
 [Reads back collected info]
 Can you please confirm this is correct?
 You can press 1 on your keypad to confirm, or just say yes."
```
- DTMF "1" = confirmed
- Verbal "yes" / "correct" / "confirmed" also accepted

### Phase 5 — Closing
```
"Thank you for your time. Have a good day."
→ End call
```

## DTMF Handling

- LiveKit SIP participants emit DTMF events as SIP INFO or RFC 2833
- The agent listens for these via LiveKit's `participant.on("dtmf_received")` event
- During Phase 4, digit "1" triggers confirmation
- Verbal confirmation is also supported as fallback via STT

## CSV Structure

### Input Columns

| Column | Example | Description |
|--------|---------|-------------|
| `patient_name` | John Smith | Patient's full name |
| `member_id` | MEM-12345 | Insurance member ID |
| `group_number` | GRP-789 | Group/plan number |
| `insurance_phone` | +18005551234 | Insurance company phone (E.164) |
| `claim_number` | CLM-2024-001 | Claim reference number |
| `date_of_service` | 2024-12-15 | Service date |
| `procedure_code` | 99213 | CPT procedure code |
| `diagnosis_code` | J06.9 | ICD-10 diagnosis code |
| `provider_name` | Dr. Jane Doe | Provider name |
| `npi` | 1234567890 | Provider NPI number |
| `billed_amount` | 250.00 | Amount billed |

### Output Columns (added/updated after each call)

| Column | Example | Description |
|--------|---------|-------------|
| `call_status` | completed | pending / in-progress / completed / failed / no-answer |
| `claim_result` | approved | approved / denied / pending / in-review |
| `approved_amount` | 200.00 | If approved |
| `denial_reason` | — | If denied |
| `payment_date` | 2025-01-15 | Expected/actual payment |
| `appeal_deadline` | — | If denied |
| `reference_number` | REF-9876 | Insurance inquiry reference |
| `confirmed` | true | Whether info was confirmed (DTMF or verbal) |
| `call_timestamp` | 2026-04-03T10:30:00 | When the call happened |
| `transcript_file` | transcripts/CLM-2024-001.txt | Path to full transcript |

## Transcript Storage

- Each call transcript saved to `transcripts/{claim_number}.txt`
- Format: timestamped speaker turns
  ```
  [00:00:02] Agent: Hello, my name is...
  [00:00:08] Human: Yes, this is the claims department.
  [00:00:12] Agent: I'd like to check the status...
  ```

## Web Dashboard

Single-page app with 3 panels:

### Call Queue (left)
- Upload CSV button
- List of all rows with status badges (pending / in-progress / completed / failed)
- Start / Pause / Stop buttons
- Progress bar (X of Y completed)

### Live Call (center)
- Active call details (patient name, claim #, insurance company)
- Real-time transcript feed (scrolling)
- Current conversation phase indicator
- Call duration timer

### Results (right)
- Completed calls summary table
- Quick stats: approved / denied / pending counts
- Download updated CSV button
- Links to view individual transcripts

### Tech: HTML + vanilla JS, WebSocket connection to FastAPI for real-time updates

## Tech Stack

- **Python 3.11+**
- **FastAPI** — REST API + WebSocket server
- **livekit** — Python Server SDK for room/SIP management
- **livekit-agents** — Agent framework with VoicePipelineAgent
- **livekit-plugins-openai** — STT (Whisper), TTS (OpenAI), LLM (GPT-4o) via LiveKit inference
- **LiveKit SIP** — built-in SIP trunking with Twilio/Telnyx carrier
- **pandas** — CSV read/write
- **uvicorn** — ASGI server

## Configuration

Environment variables (`.env`):
```
# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-api-secret

# SIP Trunk (configured in LiveKit Cloud dashboard)
LIVEKIT_SIP_TRUNK_ID=your-sip-trunk-id

# Agent
AGENT_NAME=Sarah
PROVIDER_NAME=ABC Medical Group
```

## Verification

1. **Unit test:** Agent script logic with mock claim data
2. **SIP trunk test:** Make a test call to a known number, verify audio flows
3. **End-to-end test:** Upload a 1-row CSV, run a call, verify transcript saved and CSV updated
4. **Dashboard test:** Open dashboard, upload CSV, watch real-time updates during a call
5. **DTMF test:** During a call, press "1" and verify confirmation is captured
