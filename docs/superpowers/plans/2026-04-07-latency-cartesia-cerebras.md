# Latency Improvement: Cartesia TTS + Cerebras LLM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce per-turn roundtrip latency by replacing Deepgram Aura-2 TTS with Cartesia Sonic and adding Cerebras as an optional LLM provider, both selectable via env vars with existing providers as fallback.

**Architecture:** Both new providers use the OpenAI-compatible API pattern already in use (same `openai.LLM` plugin, different `base_url`/`api_key`). Cartesia uses the `livekit-plugins-cartesia` plugin. All selection is env-var driven — no code path changes required to roll back.

**Tech Stack:** LiveKit Agents 1.5, `livekit-plugins-cartesia`, Cartesia Sonic HTTP streaming TTS, Cerebras Cloud API (OpenAI-compatible), Groq (existing fallback).

---

## File Map

| File | Change |
|------|--------|
| `requirements.txt` | Add `livekit-plugins-cartesia` |
| `config.py` | Add `CARTESIA_API_KEY`, `TTS_VOICE_CARTESIA`, `LLM_PROVIDER`, `CEREBRAS_API_KEY`, `CEREBRAS_MODEL`; update conditional validation |
| `agent_worker.py` | Add `cartesia` branch in `get_tts()`; add LLM provider selection in `entrypoint()` |
| `.env.example` | Document new env vars |
| `.env` | Add new vars (local dev — not committed) |

---

## Task 1: Add Cartesia plugin dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add the Cartesia plugin**

Open `requirements.txt` and add after the elevenlabs line:

```
livekit-plugins-cartesia==1.5.1
```

The full file should look like:
```
fastapi==0.115.0
starlette==0.38.6
uvicorn[standard]==0.30.0
python-dotenv==1.0.1
python-multipart==0.0.9
livekit==1.1.3
livekit-agents==1.5.1
livekit-plugins-openai==1.5.1
livekit-plugins-deepgram==1.5.1
livekit-plugins-silero==1.5.1
livekit-plugins-elevenlabs==1.5.1
livekit-plugins-cartesia==1.5.1
```

- [ ] **Step 2: Install the dependency**

```bash
pip install livekit-plugins-cartesia==1.5.1
```

Expected output: `Successfully installed livekit-plugins-cartesia-1.5.1` (or "already satisfied")

- [ ] **Step 3: Verify import works**

```bash
python -c "from livekit.plugins import cartesia; print('cartesia ok')"
```

Expected: `cartesia ok`

If it fails with `ImportError`, check that the installed version matches — run `pip show livekit-plugins-cartesia` and adjust the version in `requirements.txt` to match what's available.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add livekit-plugins-cartesia dependency"
```

---

## Task 2: Add new config vars and update validation

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add new vars after the TTS section**

In `config.py`, the current TTS section (lines 26–28) reads:
```python
# --- TTS ---
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "deepgram")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku")
```

Replace it with:
```python
# --- TTS ---
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "deepgram")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
TTS_VOICE_CARTESIA = os.getenv("TTS_VOICE_CARTESIA", "79a125e8-cd45-4c13-8a67-188112f4dd22")

# --- LLM ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
```

- [ ] **Step 2: Update the `_REQUIRED` dict and `validate()` to be conditional**

The current `_REQUIRED` dict and `validate()` at the bottom of `config.py` (lines 87–105) are:
```python
_REQUIRED = {
    "LIVEKIT_URL": LIVEKIT_URL,
    "LIVEKIT_API_KEY": LIVEKIT_API_KEY,
    "LIVEKIT_API_SECRET": LIVEKIT_API_SECRET,
    "LIVEKIT_SIP_TRUNK_ID": LIVEKIT_SIP_TRUNK_ID,
    "GROQ_API_KEY": GROQ_API_KEY,
    "DEEPGRAM_API_KEY": DEEPGRAM_API_KEY,
}


def validate():
    missing = [name for name, val in _REQUIRED.items() if not val]
    if missing:
        logger.error(
            "Missing required environment variables: %s. "
            "Copy .env.example to .env and fill in all values.",
            ", ".join(missing),
        )
        sys.exit(1)


validate()
```

Replace with:
```python
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
```

- [ ] **Step 3: Verify config loads without error**

```bash
python -c "import config; print('LLM_PROVIDER:', config.LLM_PROVIDER, '| TTS_PROVIDER:', config.TTS_PROVIDER)"
```

Expected:
```
LLM_PROVIDER: groq | TTS_PROVIDER: deepgram
```

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat: add Cartesia and Cerebras config vars with conditional validation"
```

---

## Task 3: Add Cartesia TTS branch in agent_worker.py

**Files:**
- Modify: `agent_worker.py`

- [ ] **Step 1: Add cartesia import at the top**

In `agent_worker.py`, the current plugins import (line 22) is:
```python
from livekit.plugins import deepgram, elevenlabs, openai, silero
```

Change to:
```python
from livekit.plugins import cartesia, deepgram, elevenlabs, openai, silero
```

- [ ] **Step 2: Add cartesia branch in get_tts()**

The current `get_tts()` function (lines 37–69) is:
```python
def get_tts():
    """TTS selection based on TTS_PROVIDER env var.

    ElevenLabs WebSocket streaming fails on Railway (Debian/Python 3.13)
    so Deepgram is the default for deployed environments.
    Set TTS_PROVIDER=elevenlabs for local development.
    """
    provider = os.getenv("TTS_PROVIDER", "deepgram")
    eleven_key = os.getenv("ELEVEN_API_KEY", "")
    if provider == "elevenlabs" and eleven_key:
        tts = elevenlabs.TTS(
            voice_id=os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku"),
            model="eleven_turbo_v2_5",
            api_key=eleven_key,
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.6,
                similarity_boost=0.8,
                speed=0.85,
                use_speaker_boost=True,
            ),
        )
        logger.info("TTS: ElevenLabs")
        return tts
    # Recommended voices (all aura-2):
    #   asteria-en — warm, natural, best for phone calls (default)
    #   luna-en    — friendly, softer tone
    #   pandora-en — British English, clear articulation
    #   thalia-en  — conversational, younger cadence
    tts = deepgram.TTS(
        model=os.getenv("TTS_VOICE", "aura-2-asteria-en"),
    )
    logger.info("TTS: Deepgram")
    return tts
```

Replace with:
```python
def get_tts():
    """TTS selection based on TTS_PROVIDER env var.

    ElevenLabs WebSocket streaming fails on Railway (Debian/Python 3.13)
    so Deepgram is the default for deployed environments.
    Set TTS_PROVIDER=elevenlabs for local development.
    Set TTS_PROVIDER=cartesia for lowest latency (~75ms TTFB, Railway-compatible).
    """
    provider = os.getenv("TTS_PROVIDER", "deepgram")
    eleven_key = os.getenv("ELEVEN_API_KEY", "")
    cartesia_key = os.getenv("CARTESIA_API_KEY", "")
    if provider == "elevenlabs" and eleven_key:
        tts = elevenlabs.TTS(
            voice_id=os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku"),
            model="eleven_turbo_v2_5",
            api_key=eleven_key,
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.6,
                similarity_boost=0.8,
                speed=0.85,
                use_speaker_boost=True,
            ),
        )
        logger.info("TTS: ElevenLabs")
        return tts
    if provider == "cartesia" and cartesia_key:
        # Recommended Cartesia voices:
        #   79a125e8-cd45-4c13-8a67-188112f4dd22 — British Reading Lady (calm, professional, default)
        #   a0e99841-438c-4a64-b679-ae501e7d6091 — Barbershop Man (warm, natural)
        tts = cartesia.TTS(
            model="sonic-3",
            voice=os.getenv("TTS_VOICE_CARTESIA", "79a125e8-cd45-4c13-8a67-188112f4dd22"),
            api_key=cartesia_key,
        )
        logger.info("TTS: Cartesia Sonic")
        return tts
    # Recommended voices (all aura-2):
    #   asteria-en — warm, natural, best for phone calls (default)
    #   luna-en    — friendly, softer tone
    #   pandora-en — British English, clear articulation
    #   thalia-en  — conversational, younger cadence
    tts = deepgram.TTS(
        model=os.getenv("TTS_VOICE", "aura-2-asteria-en"),
    )
    logger.info("TTS: Deepgram")
    return tts
```

- [ ] **Step 3: Verify syntax**

```bash
python -c "import agent_worker; print('syntax ok')"
```

Expected: `syntax ok`

- [ ] **Step 4: Commit**

```bash
git add agent_worker.py
git commit -m "feat: add Cartesia Sonic TTS option (TTS_PROVIDER=cartesia)"
```

---

## Task 4: Add Cerebras LLM branch in agent_worker.py

**Files:**
- Modify: `agent_worker.py`

- [ ] **Step 1: Replace the LLM instantiation block in entrypoint()**

Find the LLM block in `entrypoint()` (lines 302–310):
```python
    # Groq — fastest model with tool calling
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        logger.error("GROQ_API_KEY not set — agent cannot function")
    llm = openai.LLM(
        model=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        base_url="https://api.groq.com/openai/v1",
        api_key=groq_key,
    )
```

Replace with:
```python
    # LLM selection: Cerebras (faster, ~100-200ms TTFT) or Groq (default)
    llm_provider = os.getenv("LLM_PROVIDER", "groq")
    if llm_provider == "cerebras":
        cerebras_key = os.getenv("CEREBRAS_API_KEY")
        if not cerebras_key:
            logger.error("CEREBRAS_API_KEY not set — agent cannot function")
        llm = openai.LLM(
            model=os.getenv("CEREBRAS_MODEL", "llama-3.3-70b"),
            base_url="https://api.cerebras.ai/v1",
            api_key=cerebras_key,
        )
        logger.info(f"LLM: Cerebras {os.getenv('CEREBRAS_MODEL', 'llama-3.3-70b')}")
    else:
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            logger.error("GROQ_API_KEY not set — agent cannot function")
        llm = openai.LLM(
            model=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
            base_url="https://api.groq.com/openai/v1",
            api_key=groq_key,
        )
        logger.info(f"LLM: Groq {os.getenv('GROQ_MODEL', 'meta-llama/llama-4-scout-17b-16e-instruct')}")
```

- [ ] **Step 2: Verify syntax**

```bash
python -c "import agent_worker; print('syntax ok')"
```

Expected: `syntax ok`

- [ ] **Step 3: Commit**

```bash
git add agent_worker.py
git commit -m "feat: add Cerebras LLM option (LLM_PROVIDER=cerebras)"
```

---

## Task 5: Update .env.example and local .env

**Files:**
- Modify: `.env.example`
- Modify: `.env` (local only, not committed)

- [ ] **Step 1: Update .env.example**

Replace the entire contents of `.env.example` with:

```
# LiveKit Cloud
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=

# LiveKit SIP Trunk (created in LiveKit Cloud dashboard)
LIVEKIT_SIP_TRUNK_ID=

# LLM Provider: "groq" (default) or "cerebras" (lower latency)
LLM_PROVIDER=groq

# Groq — get key at console.groq.com
GROQ_API_KEY=

# Cerebras — get key at cloud.cerebras.ai (required if LLM_PROVIDER=cerebras)
CEREBRAS_API_KEY=
CEREBRAS_MODEL=llama-3.3-70b

# Deepgram (free $200 credits) — get key at console.deepgram.com
DEEPGRAM_API_KEY=

# TTS Provider: "deepgram" (default), "cartesia" (lower latency), or "elevenlabs" (local dev only)
TTS_PROVIDER=deepgram

# ElevenLabs TTS — get key at elevenlabs.io (local dev only, fails on Railway)
ELEVEN_API_KEY=
ELEVEN_VOICE_ID=pFZP5JQG7iQjIQuC4Bku

# Cartesia TTS — get key at cartesia.ai (required if TTS_PROVIDER=cartesia)
CARTESIA_API_KEY=
TTS_VOICE_CARTESIA=79a125e8-cd45-4c13-8a67-188112f4dd22

# Agent Configuration
AGENT_NAME=Sarah
PROVIDER_NAME=ABC Medical Group

# Dashboard password (leave empty for no auth)
DASHBOARD_PASSWORD=
```

- [ ] **Step 2: Add new vars to local .env**

Add these lines to `.env` (append at the end — do not commit this file):

```
LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=<your-cerebras-key>
CEREBRAS_MODEL=llama-3.3-70b
TTS_PROVIDER=cartesia
CARTESIA_API_KEY=<your-cartesia-key>
TTS_VOICE_CARTESIA=79a125e8-cd45-4c13-8a67-188112f4dd22
```

Get keys:
- Cerebras: https://cloud.cerebras.ai → API Keys
- Cartesia: https://cartesia.ai → Dashboard → API Keys

- [ ] **Step 3: Verify config loads with new providers**

```bash
python -c "import config; print('LLM_PROVIDER:', config.LLM_PROVIDER, '| TTS_PROVIDER:', config.TTS_PROVIDER, '| CEREBRAS_MODEL:', config.CEREBRAS_MODEL)"
```

Expected (once keys are set in .env):
```
LLM_PROVIDER: cerebras | TTS_PROVIDER: cartesia | CEREBRAS_MODEL: llama-3.3-70b
```

- [ ] **Step 4: Commit .env.example only**

```bash
git add .env.example
git commit -m "docs: document Cartesia and Cerebras env vars in .env.example"
```

---

## Task 6: End-to-end smoke test

**No files modified — verification only.**

- [ ] **Step 1: Start the server with new providers**

Ensure `.env` has `LLM_PROVIDER=cerebras`, `CEREBRAS_API_KEY`, `TTS_PROVIDER=cartesia`, `CARTESIA_API_KEY` set.

```bash
python main.py
```

Expected in startup logs:
```
INFO:     Application startup complete.
```
No `sys.exit` or missing-key errors.

- [ ] **Step 2: Run a call and verify provider logs**

Upload a CSV and start a call via the dashboard. Watch the logs for:

```
[claim-agent] INFO: STT model: flux, turn_detection: stt
[claim-agent] INFO: TTS: Cartesia Sonic
[claim-agent] INFO: LLM: Cerebras llama-3.3-70b
```

All three lines must appear. If `TTS: Deepgram` appears instead, the `CARTESIA_API_KEY` is missing or `TTS_PROVIDER` is not set to `cartesia`.

- [ ] **Step 3: Verify fallback still works**

Change `.env` temporarily:
```
LLM_PROVIDER=groq
TTS_PROVIDER=deepgram
```

Restart and confirm logs show:
```
[claim-agent] INFO: TTS: Deepgram
[claim-agent] INFO: LLM: Groq meta-llama/llama-4-scout-17b-16e-instruct
```

Restore to `cerebras`/`cartesia` after confirming fallback works.

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "feat: Cartesia TTS + Cerebras LLM latency improvements complete"
```
