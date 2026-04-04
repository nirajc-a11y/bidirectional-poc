# Outbound AI Calling System

Automated outbound calling system for medical claim status verification. An AI voice agent calls insurance companies from a CSV, follows a structured script, captures responses and DTMF confirmations, stores transcripts, and updates the CSV with results.

## Prerequisites

- Python 3.11+
- LiveKit Cloud account
- SIP trunk configured in LiveKit (Twilio or Telnyx)
- OpenAI API key

## SIP Trunk Setup (One-Time)

### Option A: Via LiveKit Cloud Dashboard

1. **Get a phone number**: Sign up at [Twilio](https://twilio.com) or [Telnyx](https://telnyx.com) and buy a voice-capable phone number
2. **Create SIP credentials**:
   - **Twilio**: Go to Elastic SIP Trunking > Create trunk > Add origination/termination URIs
   - **Telnyx**: Go to SIP Trunking > Create connection > Note SIP credentials
3. **Configure in LiveKit Cloud**:
   - Go to your [LiveKit Cloud dashboard](https://cloud.livekit.io)
   - Navigate to **SIP** section
   - Click **Create Outbound Trunk**
   - Fill in: Name, SIP Server Address, Auth Username, Auth Password, Phone Numbers
   - Save and copy the **SIP Trunk ID**
4. Add the trunk ID to your `.env` as `LIVEKIT_SIP_TRUNK_ID`

### Option B: Via LiveKit CLI

```bash
# Install LiveKit CLI
# macOS: brew install livekit-cli
# Windows: download from https://github.com/livekit/livekit-cli/releases

# Configure CLI
lk cloud auth

# Create outbound trunk (Twilio example)
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

## Usage

1. Open http://localhost:8000 in your browser
2. Upload a CSV file (see `sample_claims.csv` for format)
3. Click "Start Calls" to begin processing
4. Watch real-time transcripts in the dashboard
5. Download the updated CSV with results when done

## CSV Format

### Input columns (required)
| Column | Description |
|--------|-------------|
| patient_name | Patient's full name |
| member_id | Insurance member ID |
| group_number | Group/plan number |
| insurance_phone | Insurance phone in E.164 format (+1...) |
| claim_number | Claim reference number |
| date_of_service | Date of service (YYYY-MM-DD) |
| procedure_code | CPT procedure code |
| diagnosis_code | ICD-10 diagnosis code |
| provider_name | Provider name |
| npi | Provider NPI |
| billed_amount | Amount billed |

### Output columns (added automatically)
| Column | Description |
|--------|-------------|
| call_status | pending/in-progress/completed/failed/no-answer |
| claim_result | approved/denied/pending/in-review |
| approved_amount | If approved |
| denial_reason | If denied |
| payment_date | Expected/actual payment date |
| reference_number | Insurance inquiry reference |
| confirmed | Whether info was confirmed |
| call_timestamp | When the call was made |
| transcript_file | Path to transcript file |

## Architecture

```
FastAPI Backend (main.py)
  ├── Reads CSV, queues calls
  ├── Creates LiveKit rooms
  ├── Dispatches SIP calls via LiveKit API
  ├── Stores transcripts + updates CSV
  └── Serves dashboard + WebSocket updates

LiveKit Agent Worker (agent_worker.py)
  ├── VoicePipelineAgent with GPT-4o
  ├── 5-phase conversation script
  ├── DTMF handling
  └── Transcript capture

LiveKit Cloud
  ├── Room management
  ├── SIP trunk (Twilio/Telnyx)
  └── Audio routing
```
