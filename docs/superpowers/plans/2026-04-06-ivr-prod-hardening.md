# IVR Navigation + Prod-Grade Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add autonomous IVR/DTMF navigation so the agent can navigate insurance phone trees, plus harden the system with retries, trace IDs, structured logging, atomic writes, audit logging, PII redaction, and graceful shutdown.

**Architecture:** The agent gains a two-mode design (IVR → Human) within the existing `AgentSession`. All hardening is layered onto existing files without restructuring. A new `logging_utils.py` provides shared trace/redaction utilities.

**Tech Stack:** Python 3.11+, FastAPI, LiveKit Agents 1.5.1, livekit-agents, asyncio, standard `logging`, `shutil`, `signal`

---

## Pre-Work: Create Branch

- [ ] **Create and switch to feature branch**

```bash
cd c:/Users/Jayant/angel-products/bidirectional-poc
git checkout -b feature/ivr-prod-hardening
git push -u origin feature/ivr-prod-hardening
```

---

## Task 1: Config Additions

**Files:**
- Modify: `config.py`

Add new tunable constants needed by IVR and hardening features.

- [ ] **Step 1: Add new config values to `config.py`**

Open `config.py` and replace the `# --- Rate limiting ---` block and everything after it with:

```python
# --- Rate limiting ---
LOGIN_MAX_ATTEMPTS = 3
LOGIN_WINDOW_SECONDS = 300

# --- Session ---
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# --- Logging ---
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")  # "text" or "json"

# --- IVR ---
IVR_TIMEOUT_SECONDS = int(os.getenv("IVR_TIMEOUT_SECONDS", "90"))
IVR_MAX_ESCAPE_ATTEMPTS = int(os.getenv("IVR_MAX_ESCAPE_ATTEMPTS", "2"))

# --- SIP Retries ---
SIP_MAX_RETRIES = int(os.getenv("SIP_MAX_RETRIES", "3"))
SIP_RETRY_DELAYS = [5, 15, 30]  # seconds between retry attempts
```

Also add `import secrets` at the top of `config.py` (after `import os`).

- [ ] **Step 2: Commit**

```bash
git add config.py
git commit -m "feat: add IVR, retry, logging, session config constants"
```

---

## Task 2: Logging Utilities

**Files:**
- Create: `logging_utils.py`

Shared structured logging and PII redaction used by both `main.py` and `agent_worker.py`.

- [ ] **Step 1: Create `logging_utils.py`**

```python
import json
import logging
import re
from datetime import datetime, timezone

_PHONE_RE = re.compile(r"(\+\d{1,3})\d+(\d{4})")
_PHONE_MASK = r"\1****\2"


def redact_pii(text: str) -> str:
    """Redact phone numbers from log text. E.g. +12345678901 -> +1****8901"""
    return _PHONE_RE.sub(_PHONE_MASK, text)


class _JsonFormatter(logging.Formatter):
    def __init__(self, call_id: str = "", claim_number: str = ""):
        super().__init__()
        self._call_id = call_id
        self._claim_number = claim_number

    def format(self, record: logging.LogRecord) -> str:
        msg = redact_pii(record.getMessage())
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
        }
        if self._call_id:
            entry["call_id"] = self._call_id
        if self._claim_number:
            entry["claim_number"] = self._claim_number
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry)


class _TextFormatter(logging.Formatter):
    def __init__(self, call_id: str = ""):
        fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        if call_id:
            fmt = f"%(asctime)s [{call_id}] [%(name)s] %(levelname)s: %(message)s"
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact_pii(str(record.msg))
        return super().format(record)


def configure_logger(name: str, log_format: str = "text", call_id: str = "", claim_number: str = "") -> logging.Logger:
    """Return a configured logger. Call once per module/call."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        log.handlers.clear()
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(_JsonFormatter(call_id=call_id, claim_number=claim_number))
    else:
        handler.setFormatter(_TextFormatter(call_id=call_id))
    log.addHandler(handler)
    return log


def get_audit_logger() -> logging.Logger:
    """Returns a dedicated audit logger writing to audit.log."""
    audit = logging.getLogger("audit")
    if audit.handlers:
        return audit
    audit.setLevel(logging.INFO)
    audit.propagate = False
    fh = logging.FileHandler("audit.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))
    audit.addHandler(fh)
    return audit
```

- [ ] **Step 2: Commit**

```bash
git add logging_utils.py
git commit -m "feat: add logging_utils with JSON formatter, PII redaction, audit logger"
```

---

## Task 3: Stable Session Secret + Tighter Rate Limit + Audit Logging in main.py

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Replace `SESSION_SECRET` line and add audit logger**

Find and replace the `SESSION_SECRET` line (line 56):
```python
# Session secret (regenerated on restart — fine for a demo)
SESSION_SECRET = secrets.token_hex(32)
```
Replace with:
```python
# Session secret — set SESSION_SECRET env var in prod for stable sessions across restarts
SESSION_SECRET = config.SESSION_SECRET
```

- [ ] **Step 2: Add audit logger import and initialisation**

After the `logger = logging.getLogger("outbound-caller")` line, add:
```python
from logging_utils import get_audit_logger, configure_logger
audit_log = get_audit_logger()
```

- [ ] **Step 3: Add audit events to login routes**

In `login()` (POST /login), replace:
```python
    if password == config.DASHBOARD_PASSWORD:
        logger.info(f"Login success: {ip}")
```
With:
```python
    if password == config.DASHBOARD_PASSWORD:
        logger.info(f"Login success: {ip}")
        audit_log.info(f"LOGIN_SUCCESS ip={ip}")
```

And replace:
```python
    _record_login_attempt(ip)
    logger.warning(f"Login failed: {ip}")
```
With:
```python
    _record_login_attempt(ip)
    attempts_so_far = len(_login_attempts.get(ip, []))
    logger.warning(f"Login failed: {ip}")
    audit_log.info(f"LOGIN_FAILURE ip={ip} attempt={attempts_so_far}/{config.LOGIN_MAX_ATTEMPTS}")
```

- [ ] **Step 4: Add audit events to transcript and CSV download endpoints**

In `get_transcript()`:
```python
    if os.path.exists(filepath):
        audit_log.info(f"TRANSCRIPT_ACCESS claim={claim_number} ip={request.client.host if request.client else 'unknown'}")
        with open(filepath, "r", encoding="utf-8") as f:
```

In `download_csv()`, add `request: Request` parameter and audit line:
```python
@app.get("/api/download-csv")
async def download_csv(request: Request):
    if os.path.exists(config.CSV_PATH):
        audit_log.info(f"CSV_DOWNLOAD ip={request.client.host if request.client else 'unknown'}")
        return FileResponse(config.CSV_PATH, media_type="text/csv", filename="claims_updated.csv")
    return JSONResponse(status_code=404, content={"error": "No CSV loaded"})
```

- [ ] **Step 5: Add `Retry-After` header to rate-limit response**

Replace the rate-limit response in `login()`:
```python
    if not _check_rate_limit(ip):
        logger.warning(f"Login rate-limited: {ip}")
        audit_log.info(f"LOGIN_RATE_LIMITED ip={ip}")
        response = JSONResponse(status_code=429, content={"error": "Too many login attempts. Try again later."})
        response.headers["Retry-After"] = str(config.LOGIN_WINDOW_SECONDS)
        return response
```

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: stable session secret, tighter rate limit, audit logging for login/transcript/csv"
```

---

## Task 4: SIP Call Retry Logic

**Files:**
- Modify: `main.py`

Wrap `make_sip_call()` with retry/backoff. Non-retryable errors (bad phone, missing config) fail immediately.

- [ ] **Step 1: Add `_make_sip_call_with_retry()` after `make_sip_call()`**

Add this function after `make_sip_call()` (after line 456):

```python
async def _make_sip_call_with_retry(claim_data: dict, room_name: str, call_id: str) -> bool:
    """Retry make_sip_call up to SIP_MAX_RETRIES times with backoff."""
    claim_number = claim_data.get("claim_number", "unknown")
    phone = claim_data.get("insurance_phone", "")

    # Non-retryable: validate before any attempt
    if not phone:
        logger.error(f"[{call_id}] No phone number for claim {claim_number}")
        return False
    if not validate_phone(phone):
        logger.error(f"[{call_id}] Invalid phone format for claim {claim_number}")
        return False

    delays = config.SIP_RETRY_DELAYS
    for attempt in range(1, config.SIP_MAX_RETRIES + 1):
        success = await make_sip_call(claim_data, room_name)
        if success:
            if attempt > 1:
                logger.info(f"[{call_id}] SIP call succeeded on attempt {attempt}")
            return True
        if attempt < config.SIP_MAX_RETRIES:
            delay = delays[attempt - 1] if attempt - 1 < len(delays) else delays[-1]
            logger.warning(f"[{call_id}] SIP attempt {attempt}/{config.SIP_MAX_RETRIES} failed, retrying in {delay}s")
            call_mgr.set_call_status(claim_number, "retrying")
            await broadcast({"type": "call_retrying", "claim_number": claim_number, "attempt": attempt})
            await asyncio.sleep(delay)
        else:
            logger.error(f"[{call_id}] All {config.SIP_MAX_RETRIES} SIP attempts failed for {claim_number}")
    return False
```

- [ ] **Step 2: Update `process_single_call()` to use retry wrapper and add trace ID**

Replace the `process_single_call` function signature and first block:

```python
async def process_single_call(claim_data: dict):
    call_id = uuid4().hex[:8]
    claim_number = str(claim_data.get("claim_number", "unknown"))
    safe_claim = sanitize_claim_number(claim_number)
    if not safe_claim:
        logger.error(f"[{call_id}] Invalid claim number format: {claim_number}")
        return

    # Inject call_id into claim_data so agent can log with it
    claim_data = {**claim_data, "call_id": call_id}

    # Unique room name to avoid collisions
    room_name = f"call-{safe_claim}-{uuid4().hex[:6]}"
    call_start = time.time()

    logger.info(f"[{call_id}] Starting call for claim {claim_number}")
    await broadcast({
        "type": "call_started",
        "claim_number": claim_number,
        "claim_data": {k: str(v) for k, v in claim_data.items()},
    })
    call_mgr.set_call_status(claim_number, "in-progress")

    # Remove stale results (race-safe)
    stale_results = os.path.join("call_results", f"{safe_claim}.json")
    try:
        os.remove(stale_results)
    except FileNotFoundError:
        pass

    success = await _make_sip_call_with_retry(claim_data, room_name, call_id)
    if not success:
        call_mgr.set_call_status(claim_number, "failed")
        await broadcast({"type": "call_failed", "claim_number": claim_number, "reason": "SIP call failed after retries"})
        return

    relay_task = asyncio.create_task(relay_transcripts(room_name, claim_number))

    result = await wait_for_call_completion(safe_claim, room_name)

    relay_task.cancel()
    try:
        await relay_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"[{call_id}] Relay task error: {e}", exc_info=True)

    total_duration = time.time() - call_start

    if result:
        transcript_text = result.get("transcript", "")
        if transcript_text:
            call_mgr.save_transcript(claim_number, transcript_text, config.TRANSCRIPTS_DIR)

        call_results = result.get("results", {})
        call_results["call_status"] = "completed"
        call_mgr.update_row(claim_number, call_results)

        ivr_duration = result.get("ivr_duration", 0)
        human_duration = result.get("human_duration", 0)
        logger.info(
            f"[{call_id}] Call {claim_number} complete: "
            f"total={total_duration:.1f}s ivr={ivr_duration:.1f}s human={human_duration:.1f}s "
            f"result={call_results.get('claim_result', 'unknown')}"
        )

        await broadcast({
            "type": "call_completed",
            "claim_number": claim_number,
            "results": call_results,
            "stats": call_mgr.get_stats(),
        })
    else:
        call_mgr.set_call_status(claim_number, "no-answer")
        logger.warning(f"[{call_id}] Call {claim_number} no-answer after {total_duration:.1f}s")
        await broadcast({"type": "call_no_answer", "claim_number": claim_number, "stats": call_mgr.get_stats()})

    # Room cleanup
    lk_api = api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
    try:
        await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception as e:
        logger.warning(f"[{call_id}] Room cleanup failed for {room_name}: {e}")
    finally:
        await lk_api.aclose()
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: SIP call retry with backoff, trace IDs per call, relay task error propagation"
```

---

## Task 5: Graceful Shutdown (SIGTERM) + CSV Backup

**Files:**
- Modify: `main.py`
- Modify: `call_manager.py`

- [ ] **Step 1: Add SIGTERM handler to `main.py`**

Add `import signal` to the imports at the top of `main.py`.

Then add this block right after the global variables (after `start_time = time.time()`):

```python
def _handle_sigterm(*_):
    global is_stopped
    logger.info("SIGTERM received — will stop after current call completes")
    is_stopped = True

signal.signal(signal.SIGTERM, _handle_sigterm)
```

- [ ] **Step 2: Fix lifespan shutdown to wait for current call**

Replace the `# Graceful shutdown` block in `lifespan()`:
```python
    # Graceful shutdown — wait up to 30s for current call to finish
    if call_loop_task and not call_loop_task.done():
        logger.info("Waiting up to 30s for current call to finish...")
        try:
            await asyncio.wait_for(call_loop_task, timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Call did not finish in 30s, cancelling")
            call_loop_task.cancel()
        except asyncio.CancelledError:
            pass
    try:
        await agent_server.aclose()
    except Exception:
        pass
    agent_task.cancel()
    logger.info("Shutdown complete")
```

- [ ] **Step 3: Add `.bak` backup to `CallManager._save()` in `call_manager.py`**

Add `import shutil` at the top of `call_manager.py`.

Replace the `_save()` method:
```python
    def _save(self):
        if not self.rows or not self.fieldnames:
            return
        dir_name = os.path.dirname(os.path.abspath(self.csv_path))
        tmp_path = None
        try:
            # Write backup before modifying
            bak_path = self.csv_path + ".bak"
            if os.path.exists(self.csv_path):
                shutil.copy2(self.csv_path, bak_path)

            fd, tmp_path = tempfile.mkstemp(suffix=".csv", dir=dir_name)
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()
                writer.writerows(self.rows)
            os.replace(tmp_path, self.csv_path)
        except Exception as e:
            logger.error(f"Failed to save CSV: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
```

- [ ] **Step 4: Add `.bak` recovery to `CallManager.load_csv()`**

Replace `load_csv()`:
```python
    def load_csv(self, path: str | None = None):
        target = path or self.csv_path
        bak_path = target + ".bak"
        # Recover from backup if primary is missing but backup exists
        if not os.path.exists(target) and os.path.exists(bak_path):
            logger.warning(f"CSV missing, recovering from backup: {bak_path}")
            shutil.copy2(bak_path, target)
        with self._lock:
            with open(target, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.fieldnames = list(reader.fieldnames or [])
                self.rows = list(reader)

            for col, default in OUTPUT_COLUMNS.items():
                if col not in self.fieldnames:
                    self.fieldnames.append(col)
                for row in self.rows:
                    if col not in row or not row[col]:
                        row[col] = default

            self.csv_path = target
            self._save()
            logger.info(f"Loaded CSV: {len(self.rows)} claims from {target}")
```

- [ ] **Step 5: Add new status values to `get_stats()`**

In `get_stats()`, add after the existing `"no_answer"` line:
```python
            "retrying": sum(1 for r in self.rows if r.get("call_status") == "retrying"),
            "ivr_failed": sum(1 for r in self.rows if r.get("call_status") == "ivr-failed"),
            "dropped": sum(1 for r in self.rows if r.get("call_status") == "dropped"),
```

- [ ] **Step 6: Commit**

```bash
git add main.py call_manager.py
git commit -m "feat: graceful SIGTERM shutdown, CSV .bak backup/recovery, new call statuses"
```

---

## Task 6: Dropped Call Detection + Atomic Results Write in agent_worker.py

**Files:**
- Modify: `agent_worker.py`

- [ ] **Step 1: Add call_id and timing to `entrypoint()`**

After `claim_number = claim_data.get("claim_number", "unknown")` add:
```python
    call_id = claim_data.get("call_id", "--------")
    call_start = datetime.now()
    ivr_end_time: datetime | None = None
```

Update the existing `logger.info(f"Claim: {claim_number}")` line:
```python
    logger.info(f"[{call_id}] Claim: {claim_number}")
```

- [ ] **Step 2: Add dropped call handler**

After `hangup_scheduled = False` and `goodbye_said = False`, add:
```python
    drop_handled = False
```

Add this new inner function after `auto_hangup_after_goodbye()`:
```python
    async def handle_dropped_call():
        nonlocal drop_handled
        if drop_handled:
            return
        drop_handled = True
        logger.warning(f"[{call_id}] SIP participant dropped — saving partial results")
        partial = session.userdata.get("claim_results", {})
        partial["claim_result"] = partial.get("claim_result", "dropped")
        partial["notes"] = partial.get("notes", "") + " | call dropped mid-conversation"
        session.userdata["claim_results"] = partial
        await session.aclose()
```

- [ ] **Step 3: Add participant_disconnected event handler**

After the `on_attrs` handler, add:
```python
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        if participant.identity == "insurance-rep" and not hangup_scheduled and not drop_handled:
            logger.warning(f"[{call_id}] SIP participant disconnected unexpectedly")
            asyncio.create_task(handle_dropped_call())
```

- [ ] **Step 4: Make results JSON write atomic**

Replace the results save block at the bottom of `entrypoint()`:
```python
    # Save results atomically (write .tmp then rename to prevent partial reads)
    results = session.userdata.get("claim_results", {})
    results["confirmed"] = str(session.userdata.get("confirmed", False)).lower()

    # Duration metrics
    call_end = datetime.now()
    total_seconds = (call_end - call_start).total_seconds()
    ivr_seconds = (ivr_end_time - call_start).total_seconds() if ivr_end_time else 0
    human_seconds = (call_end - ivr_end_time).total_seconds() if ivr_end_time else total_seconds

    final = {
        "claim_number": claim_number,
        "call_id": call_id,
        "transcript": transcript.get_full_transcript(),
        "results": results,
        "ivr_duration": round(ivr_seconds, 1),
        "human_duration": round(human_seconds, 1),
        "total_duration": round(total_seconds, 1),
    }
    logger.info(f"[{call_id}] Done {claim_number}: {json.dumps(results)}")

    os.makedirs("call_results", exist_ok=True)
    safe_name = claim_number if _SAFE_FILENAME_RE.match(claim_number) else "unknown"
    tmp_path = f"call_results/{safe_name}.tmp.json"
    final_path = f"call_results/{safe_name}.json"
    with open(tmp_path, "w") as f:
        json.dump(final, f, indent=2)
    os.replace(tmp_path, final_path)
```

- [ ] **Step 5: Elevate silent transcript publish failure to warning**

Find:
```python
        except Exception as e:
            logger.debug(f"Failed to publish transcript data: {e}")
```
Replace with:
```python
        except Exception as e:
            logger.warning(f"[{call_id}] Failed to publish transcript data: {e}")
```

- [ ] **Step 6: Commit**

```bash
git add agent_worker.py
git commit -m "feat: dropped call detection, atomic results JSON, call duration metrics, trace IDs in agent"
```

---

## Task 7: IVR Mode — New Tools + Prompt

**Files:**
- Modify: `agent_worker.py`

This is the core IVR feature. The agent starts in IVR mode and transitions to human mode when `declare_human_reached()` is called.

- [ ] **Step 1: Add IVR tools after the existing tools section**

After the `end_call` function tool (after line ~120), add:

```python
@function_tool(
    name="send_dtmf",
    description="Press a phone keypad digit (DTMF tone). Use to navigate IVR menus. digit must be 0-9, *, or #.",
)
async def send_dtmf(ctx: RunContext, digit: str):
    if digit not in "0123456789*#":
        return f"Invalid digit '{digit}'. Must be 0-9, *, or #."
    try:
        await ctx.room.local_participant.publish_dtmf(digit)
        logger.info(f"DTMF sent: {digit}")
        return f"Pressed {digit}. Wait 1-2 seconds then listen for the next prompt."
    except Exception as e:
        logger.error(f"DTMF send failed: {e}")
        return f"Failed to press {digit}: {e}"


@function_tool(
    name="declare_human_reached",
    description="Call this the moment you hear a real human (not an automated voice) on the line. This switches you from IVR navigation mode to the claim verification script.",
)
async def declare_human_reached(ctx: RunContext):
    ctx.session.userdata["mode"] = "human"
    ctx.session.userdata["ivr_end_time"] = datetime.now().isoformat()
    logger.info("TOOL declare_human_reached: switching to human mode")
    return "HUMAN_MODE_ACTIVE. Now follow the claim verification script. Greet the rep."


@function_tool(
    name="declare_ivr_failed",
    description="Call this when you cannot navigate the IVR — stuck in a loop, unrecognized system, or cannot reach claims after multiple attempts.",
)
async def declare_ivr_failed(ctx: RunContext, reason: str):
    ctx.session.userdata["claim_results"] = {"claim_result": "ivr-failed", "notes": reason}
    ctx.session.userdata["call_ended"] = True
    logger.info(f"TOOL declare_ivr_failed: {reason}")
    return "IVR navigation failed. Say a brief goodbye and end the call."
```

- [ ] **Step 2: Add `get_ivr_prompt()` function before `get_system_prompt()`**

```python
def get_ivr_prompt(claim_data: dict) -> str:
    name = os.getenv("AGENT_NAME", "Sarah")
    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")
    return f"""You are {name} from {org} calling to verify a medical insurance claim.

You have just dialed an insurance company and are currently navigating their automated phone system (IVR).

YOUR ONLY GOAL RIGHT NOW: reach a live human agent in the claims department.

RULES:
- Listen to each automated prompt fully before acting.
- Use send_dtmf to press a digit when a menu offers options. Pick the option most likely to reach "claims", "claim status", "billing", or "insurance verification".
- If no option clearly matches, press 0 or use send_dtmf("0") to reach an operator.
- If the IVR asks you to speak (voice-activated), use the say_phrase tool to say "claims department" or "representative".
- The moment you hear a real human voice (natural speech, not robotic), immediately call declare_human_reached().
- If you get stuck in a loop (same prompt twice) or cannot proceed after 2 escape attempts, call declare_ivr_failed("reason").
- Never repeat a digit sequence you've already tried.
- Do NOT introduce yourself or mention the claim while in IVR mode.
- Do NOT say anything out loud unless using say_phrase — the IVR cannot understand you.

CLAIM (for reference only — do NOT share during IVR):
Patient: {claim_data.get('patient_name', 'N/A')} | Claim#: {claim_data.get('claim_number', 'N/A')}
"""
```

- [ ] **Step 3: Update `Agent` instantiation to use IVR tools and IVR prompt initially**

Find the `agent = Agent(...)` block and update `instructions` and `tools`:

```python
    agent = Agent(
        instructions=get_ivr_prompt(claim_data),
        stt=deepgram.STT(model="nova-3", language="en", no_delay=True, smart_format=True, punctuate=True),
        llm=llm,
        tts=get_tts(),
        vad=silero.VAD.load(),
        tools=[send_dtmf, declare_human_reached, declare_ivr_failed, save_claim_status, confirm_details, mark_unable_to_verify, end_call],
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(
                min_delay=0.5,
                max_delay=1.5,
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="vad",
                min_duration=0.5,
                min_words=1,
                resume_false_interruption=True,
            ),
        ),
        allow_interruptions=True,
    )
```

- [ ] **Step 4: Initialise IVR tracking state**

After `goodbye_said = False` add:
```python
    ivr_prompt_history: list[str] = []
    escape_attempts = 0
```

- [ ] **Step 5: Commit**

```bash
git add agent_worker.py
git commit -m "feat: IVR tools (send_dtmf, declare_human_reached, declare_ivr_failed) and IVR prompt"
```

---

## Task 8: IVR Loop Detection + Timeout Watchdog

**Files:**
- Modify: `agent_worker.py`

- [ ] **Step 1: Add loop detection to `on_item` handler**

In the `on_item` function, after the transcript publish block and before the goodbye detection block, add:

```python
        # IVR loop detection — detect repeated prompts and trigger escape
        if role != "assistant" and session.userdata.get("mode", "ivr") == "ivr":
            import unicodedata
            normalized = re.sub(r"[^\w\s]", "", content.lower()).strip()
            if normalized and normalized in ivr_prompt_history[-3:]:
                nonlocal escape_attempts
                escape_attempts += 1
                logger.warning(f"[{call_id}] IVR loop detected (escape attempt {escape_attempts})")
                if escape_attempts <= config.IVR_MAX_ESCAPE_ATTEMPTS:
                    asyncio.create_task(session.generate_reply(
                        instructions="You are stuck in a loop — the same prompt repeated. Press 0 now using send_dtmf('0'). If that fails, say 'representative' using say_phrase."
                    ))
                else:
                    asyncio.create_task(session.generate_reply(
                        instructions="IVR escape failed after multiple attempts. Call declare_ivr_failed with the reason."
                    ))
            elif normalized:
                ivr_prompt_history.append(normalized)
                if len(ivr_prompt_history) > 10:
                    ivr_prompt_history.pop(0)
```

- [ ] **Step 2: Add IVR timeout watchdog**

After `session_closed = asyncio.Event()`, add:

```python
    async def ivr_timeout_watchdog():
        """Give up on IVR navigation after IVR_TIMEOUT_SECONDS."""
        await asyncio.sleep(config.IVR_TIMEOUT_SECONDS)
        if session.userdata.get("mode", "ivr") != "ivr":
            return  # Already in human mode, nothing to do
        logger.warning(f"[{call_id}] IVR timeout after {config.IVR_TIMEOUT_SECONDS}s")
        # Give the agent one last chance to press 0
        await session.generate_reply(
            instructions=f"{config.IVR_TIMEOUT_SECONDS} seconds have passed. Try pressing 0 or saying 'representative' one final time. If it still doesn't work, call declare_ivr_failed('timeout after {config.IVR_TIMEOUT_SECONDS}s')."
        )
        # Hard timeout: if still in IVR mode after 30 more seconds, force failure
        await asyncio.sleep(30)
        if session.userdata.get("mode", "ivr") == "ivr":
            logger.error(f"[{call_id}] IVR hard timeout — forcing failure")
            session.userdata["claim_results"] = {"claim_result": "ivr-failed", "notes": "hard timeout"}
            session.userdata["call_ended"] = True
            await session.aclose()

    asyncio.create_task(ivr_timeout_watchdog())
```

Place the `asyncio.create_task(ivr_timeout_watchdog())` line right before `await session_closed.wait()`.

- [ ] **Step 3: Handle mode transition — swap instructions when human reached**

In `on_item`, after the `if role == "assistant":` block for goodbye detection, add:

```python
        # Mode transition: when declare_human_reached fires, update agent instructions
        if role == "assistant" and session.userdata.get("mode") == "human":
            if not session.userdata.get("human_mode_initialized"):
                session.userdata["human_mode_initialized"] = True
                ivr_end_str = session.userdata.get("ivr_end_time")
                if ivr_end_str:
                    nonlocal ivr_end_time
                    try:
                        ivr_end_time = datetime.fromisoformat(ivr_end_str)
                    except ValueError:
                        pass
                agent.instructions = get_system_prompt(claim_data)
                logger.info(f"[{call_id}] Switched to human mode — claim script active")
```

- [ ] **Step 4: Commit**

```bash
git add agent_worker.py
git commit -m "feat: IVR loop detection, 90s timeout watchdog, human mode instruction swap"
```

---

## Task 9: Update Agent Greeting to Start in IVR Mode

**Files:**
- Modify: `agent_worker.py`

- [ ] **Step 1: Update the `generate_reply` greeting instruction**

Find:
```python
        session.generate_reply(
            instructions="Someone just picked up the phone. Greet them naturally and ask if you've reached the claims department."
        )
```

Replace with:
```python
        session.generate_reply(
            instructions="The call just connected. Listen carefully. If you hear an automated IVR system, begin navigating it using send_dtmf. If a human immediately answers, call declare_human_reached() then greet them."
        )
```

- [ ] **Step 2: Commit**

```bash
git add agent_worker.py
git commit -m "feat: start call in IVR-aware mode, listen before acting on pickup"
```

---

## Task 10: Push Branch

- [ ] **Step 1: Push all commits to remote**

```bash
git push origin feature/ivr-prod-hardening
```

- [ ] **Step 2: Verify branch on remote**

```bash
git log --oneline origin/feature/ivr-prod-hardening | head -15
```

Expected: all commits from Tasks 1-9 visible.

---

## Self-Review Checklist

### Spec Coverage
- [x] IVR tools: `send_dtmf`, `declare_human_reached`, `declare_ivr_failed` — Task 7
- [x] IVR prompt `get_ivr_prompt()` — Task 7
- [x] Loop detection with rolling window — Task 8
- [x] 90s timeout watchdog — Task 8
- [x] Human mode instruction swap via `agent.instructions` — Task 8
- [x] SIP retry with backoff — Task 4
- [x] Trace ID `call_id` per call — Tasks 4, 6
- [x] Structured logging + PII redaction — Task 2
- [x] Dropped call detection — Task 6
- [x] Atomic results JSON — Task 6
- [x] CSV `.bak` backup + recovery — Task 5
- [x] Audit log — Tasks 2, 3
- [x] Stable `SESSION_SECRET` — Tasks 1, 3
- [x] Rate limit reduced to 3 + `Retry-After` — Tasks 1, 3
- [x] New statuses (`retrying`, `ivr-failed`, `dropped`) — Tasks 4, 5, 7
- [x] Graceful SIGTERM — Task 5
- [x] Duration metrics — Tasks 6
- [x] Feature branch — Pre-Work, Task 10

### Type Consistency
- `call_id` is always `str` (8-char hex), passed via `claim_data["call_id"]`
- `ivr_end_time` is `datetime | None`, set from ISO string in `session.userdata["ivr_end_time"]`
- `escape_attempts` is `int`, incremented in `on_item`
- `ivr_prompt_history` is `list[str]` of normalized strings
- All new tool functions follow `async def fn(ctx: RunContext, ...)` pattern matching existing tools
