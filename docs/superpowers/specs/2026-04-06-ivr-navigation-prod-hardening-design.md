# Design: IVR Navigation + Prod-Grade Hardening

**Date:** 2026-04-06  
**Branch:** `feature/ivr-prod-hardening`  
**Approach:** Option A — incremental in-place extension of existing files

---

## Context

The system is a working MVP that makes outbound SIP calls via LiveKit to verify medical claims with insurance companies. Two gaps need closing:

1. **IVR Navigation**: Insurance company calls hit automated phone trees. The agent currently has no ability to navigate them — it waits for audio and assumes a human answers. In practice, most calls hit an IVR first.

2. **Production Hardening**: The codebase has silent failures, no retry logic, no trace IDs, unsafe shutdown, and data integrity risks that make it unsuitable for production use.

---

## Phase 1: IVR/DTMF Navigation

### Overview

The agent operates in two sequential modes per call:

- **IVR mode** — autonomous navigation of automated phone trees using LLM judgment + DTMF tools
- **Human mode** — existing claim verification script (unchanged in behavior)

Mode is tracked in `session.userdata["mode"]` (`"ivr"` → `"human"`). The transition is triggered by the agent calling `declare_human_reached()`.

### New Tools (agent_worker.py)

**`send_dtmf(digit: str)`**  
Sends a DTMF tone via LiveKit SIP API (`room.local_participant.publish_dtmf(digit)`). Returns confirmation. The LLM is instructed to wait ~1.5s after pressing before listening for the next prompt.

**`say_phrase(phrase: str)`**  
Sends TTS speech into the call (for IVRs that accept voice commands like "claims" or "representative"). Thin wrapper around `session.say()`.

**`declare_human_reached()`**  
Signals that a live human has been detected. Triggers:
1. `session.userdata["mode"] = "human"`
2. `agent.instructions = get_system_prompt(claim_data)` — swaps in the full claim script (LiveKit Agents 1.5 supports live instruction updates on the `Agent` object)
3. `session.generate_reply()` triggers the claim greeting

**`declare_ivr_failed(reason: str)`**  
Called when IVR navigation is hopeless (loop detected, timeout, unrecognized system). Sets `call_status = "ivr-failed"`, saves result, triggers hangup.

### IVR System Prompt (`get_ivr_prompt()`)

Separate from `get_system_prompt()`. Key rules:
- You are navigating an automated phone system to reach the claims department
- Listen to each prompt fully before acting
- Press the digit most likely to reach "claims", "billing", or "insurance verification"
- If no clear option exists, press 0 or say "representative" / "agent"
- Call `declare_human_reached()` the moment you hear a real person speaking naturally
- Call `declare_ivr_failed("reason")` if you cannot proceed
- Never repeat a sequence you've already tried

### Loop Detection

Implemented in `on_item` event handler:

```
ivr_prompt_history: list[str] = []  # rolling window of last 5 IVR transcript entries

On each Human (IVR audio) entry in IVR mode:
  normalized = normalize(content)  # lowercase, strip punctuation
  if normalized in ivr_prompt_history[-3:]:
      # Same prompt heard again — trigger escape
      escape_attempts += 1
      if escape_attempts <= 2:
          session.generate_reply(instructions="You're in a loop. Press 0 or say 'representative' now.")
      else:
          # Give up
          session.generate_reply(instructions="Navigation failed. Call declare_ivr_failed.")
  else:
      ivr_prompt_history.append(normalized)
```

### IVR Timeout

90-second timer starts when agent joins room. If `mode` is still `"ivr"` after 90s:
```python
asyncio.create_task(ivr_timeout_watchdog())  # started after session.start()
```
Watchdog fires `session.generate_reply(instructions="90 seconds elapsed. Press 0 or say representative. If still stuck, call declare_ivr_failed.")`.

After 2 more failed attempts (30s each), force `declare_ivr_failed("timeout")`.

### Human Mode Transition

When `declare_human_reached()` is called:
1. `session.userdata["mode"] = "human"`
2. `session.update_agent(instructions=get_system_prompt(claim_data))` — swaps in the full claim script
3. `session.generate_reply(instructions="A human just answered. Greet them and ask if you've reached the claims department.")` — triggers natural greeting

IVR navigation duration is logged: `logger.info(f"[{call_id}] IVR navigation complete in {elapsed:.1f}s")`

### DTMF Sending Implementation

LiveKit SIP supports DTMF via `room.local_participant.publish_dtmf(digit)` (available in `livekit-agents` 1.5+). The `send_dtmf` tool calls this directly. No separate SIP signaling needed.

---

## Phase 2: Production Hardening

### A) Reliability

**SIP Call Retries (main.py)**

`make_sip_call()` wrapped in `_make_sip_call_with_retry()`:
- Max 3 attempts
- Backoff: 5s, 15s, 30s
- Retryable: `aiohttp.ClientError`, `asyncio.TimeoutError`, LiveKit 5xx responses
- Non-retryable: `ValueError` (invalid phone/config), LiveKit 4xx (bad request)
- Between attempts: `call_mgr.set_call_status(claim_number, "retrying")`
- After all attempts exhausted: status = `"failed"`, reason logged

**Dropped Call Detection (agent_worker.py)**

New event handler:
```python
@ctx.room.on("participant_disconnected")
def on_participant_disconnected(participant):
    if participant.identity == "insurance-rep" and not hangup_scheduled:
        logger.warning(f"[{call_id}] SIP participant dropped unexpectedly")
        session.userdata["drop_detected"] = True
        asyncio.create_task(handle_dropped_call())
```

`handle_dropped_call()`: saves whatever partial results exist, marks call `"dropped"`, closes session.

**Silent Failure Elimination**

- All `logger.debug(f"Failed to publish transcript...")` → `logger.warning(...)` with call_id
- Relay task exceptions caught and logged with `exc_info=True`
- `auto_hangup_after_goodbye()` failures retry LiveKit API removal once before giving up

### B) Observability

**Trace IDs**

`call_id = str(uuid.uuid4())[:8]` generated in `process_single_call()` (main.py). Passed to agent via room metadata (`claim_data["call_id"] = call_id`). All log lines in `agent_worker.py` prefixed `[{call_id}]`.

**Structured Logging**

New `configure_logging(call_id=None)` function in a shared `logging_utils.py`:
- Default: existing human-readable format
- `LOG_FORMAT=json` env var: emits JSON lines with fields `timestamp`, `level`, `call_id`, `claim_number`, `message`
- Applied to both `main.py` and `agent_worker.py` loggers

**Duration Metrics**

Logged at call completion:
```
[a1b2c3d4] Call CLM-2025-001 complete: total=127.3s ivr=34.1s human=93.2s result=completed
```

**New Call Statuses**

Added to `CallManager` and CSV:
- `retrying` — between SIP retry attempts
- `ivr-failed` — IVR navigation gave up
- `dropped` — SIP participant disconnected mid-call

### C) Data Safety

**Atomic CSV Writes (call_manager.py)**

Before each CSV update, write `.bak` backup:
```python
shutil.copy2(self.csv_path, self.csv_path + ".bak")
# then proceed with tempfile + os.replace()
```
On startup, if `csv_path` missing but `.bak` exists, auto-recover from backup.

**Atomic Results JSON**

In `agent_worker.py`, results written to `{safe_name}.tmp.json` first, then `os.replace()` to final path. Prevents partial reads from `wait_for_call_completion()`.

**Duplicate Call Prevention**

In `process_single_call()`, before dispatching:
```python
result_file = f"call_results/{safe_claim}.json"
if os.path.exists(result_file):
    existing = json.load(open(result_file))
    if existing.get("results", {}).get("claim_result"):
        logger.info(f"Skipping {claim_number} — results already exist")
        call_mgr.set_call_status(claim_number, "completed")
        continue
```

### D) Security

**Stable Session Secret**

`config.py`: `SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))`  
Documented in CLAUDE.md: set `SESSION_SECRET` in Railway env for persistent sessions across deploys.

**Tighter Rate Limiting**

`LOGIN_MAX_ATTEMPTS = 3` (down from 5). Window stays 300s. After lockout, response includes `Retry-After` header.

**Audit Log**

New `audit_logger` in `main.py` writing to `audit.log`:
```
2026-04-06T07:28:01Z LOGIN_SUCCESS ip=100.64.0.5
2026-04-06T07:28:45Z TRANSCRIPT_ACCESS claim=CLM-2025-001 ip=100.64.0.5
2026-04-06T07:29:10Z CSV_DOWNLOAD ip=100.64.0.5
2026-04-06T07:31:00Z LOGIN_FAILURE ip=100.64.0.9 attempt=1/3
```
Written with `logging.FileHandler("audit.log")` on a dedicated logger. No buffering.

**PII Redaction in Logs**

Utility `redact_pii(text: str) -> str` in `logging_utils.py`:
- Phone numbers: `+1234567890` → `+1****7890`
- Patient names: passed through CLAIM_INFO block only, not echoed in logs
- Applied to agent log lines containing patient name or phone

### E) Graceful Shutdown

SIGTERM handler in `main.py`:
```python
import signal

def handle_sigterm(*_):
    logger.info("SIGTERM received — waiting for current call to finish")
    global is_stopped
    is_stopped = True

signal.signal(signal.SIGTERM, handle_sigterm)
```

Call loop already checks `is_stopped` between calls. Current in-progress call runs to completion (or 30s max wait). After loop exits, all WebSocket connections closed cleanly.

---

## Files Modified

| File | Changes |
|------|---------|
| `agent_worker.py` | IVR mode, new tools, loop detection, watchdog, dropped call handler, trace IDs, atomic results write, PII redaction |
| `main.py` | SIP retry logic, trace IDs, audit logging, graceful shutdown, duplicate prevention, new statuses |
| `call_manager.py` | `.bak` backup on write, startup recovery, new status values |
| `config.py` | `SESSION_SECRET`, `LOG_FORMAT`, `IVR_TIMEOUT_SECONDS=90`, `SIP_MAX_RETRIES=3` |
| `logging_utils.py` | New file — structured logging, PII redaction utilities |

---

## Verification

### IVR Navigation
1. Call a number with a known IVR tree — verify agent presses correct digits
2. Simulate a loop — verify escape sequence fires (press 0 / say "representative")
3. Let IVR timeout — verify `ivr-failed` status set, call hung up cleanly
4. Verify IVR duration logged at transition

### Reliability
1. Kill LiveKit API mid-call — verify retry with backoff, status goes `retrying` → `completed/failed`
2. Drop SIP participant mid-conversation — verify `dropped` status, partial results saved
3. Send SIGTERM during active call — verify call completes before process exits

### Data Safety
1. Kill process mid-CSV-write — verify `.bak` recovery on restart
2. Start same claim twice — verify second run skips, doesn't overwrite results
3. Corrupt results JSON — verify call loop marks `failed` gracefully

### Observability
1. Verify all log lines for a call share same `call_id` prefix
2. Set `LOG_FORMAT=json`, verify JSON output parseable
3. Check `audit.log` after login, transcript access, CSV download

### Security
1. Attempt 4 logins — verify lockout after 3
2. Restart server — verify sessions still valid (with `SESSION_SECRET` set)
3. Check logs — verify no full phone numbers or patient names in output
