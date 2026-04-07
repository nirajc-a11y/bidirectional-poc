# Design: Latency Improvement — Cartesia TTS + Cerebras LLM

**Date:** 2026-04-07  
**Branch:** feature/ivr-prod-hardening  
**Goal:** Reduce overall roundtrip latency per conversation turn by replacing Deepgram Aura-2 TTS with Cartesia Sonic and adding Cerebras as an optional LLM provider.

---

## Problem

The current pipeline (Deepgram flux STT → Groq Llama 4 Scout 17B → Deepgram Aura-2 TTS) has a typical roundtrip of 650ms–1.3s per turn. The two biggest opportunities:

- **TTS TTFB**: Deepgram Aura-2 ~150–300ms → Cartesia Sonic ~75ms
- **LLM TTFT**: Groq Llama 4 Scout ~300–600ms → Cerebras Llama 3.3 70B ~100–200ms (Cerebras runs ~2,000 tok/s throughput)

STT (Deepgram flux) is already optimal — no change.

---

## Architecture

No structural changes. Both new providers use the OpenAI-compatible API pattern already in use for Groq. New providers are selected via env vars; all existing defaults are preserved.

```
SIP Audio
  └─► Deepgram flux STT (unchanged)
        └─► LLM: Groq (default) | Cerebras (LLM_PROVIDER=cerebras)
              └─► TTS: Deepgram Aura-2 (default) | Cartesia Sonic (TTS_PROVIDER=cartesia)
                    └─► SIP Audio Out
```

---

## Components

### 1. TTS — Cartesia Sonic

**File:** `agent_worker.py` → `get_tts()`

Add a `cartesia` branch:
- Plugin: `livekit-plugins-cartesia`
- Model: `sonic-2` (Cartesia's latest, lowest latency)
- Voice: configurable via `TTS_VOICE_CARTESIA` env var
- Default voice ID: `79a125e8-cd45-4c13-8a67-188112f4dd22` (calm, professional)
- Transport: HTTP streaming — no WebSocket, Railway-compatible

Selection order in `get_tts()`:
1. `TTS_PROVIDER=elevenlabs` + key set → ElevenLabs (existing)
2. `TTS_PROVIDER=cartesia` + key set → Cartesia Sonic (new)
3. Default → Deepgram Aura-2 (existing)

### 2. LLM — Cerebras

**File:** `agent_worker.py` → `entrypoint()`

Add LLM provider selection:
- `LLM_PROVIDER=groq` (default) → existing Groq path, unchanged
- `LLM_PROVIDER=cerebras` → `openai.LLM` with Cerebras base URL + `CEREBRAS_API_KEY`
- Base URL: `https://api.cerebras.ai/v1`
- Default model: `llama-3.3-70b` (configurable via `CEREBRAS_MODEL`)

Uses `openai.LLM` — same LiveKit plugin, just different `base_url`/`api_key`. No new plugin needed.

### 3. Config — `config.py`

New vars:
```
CARTESIA_API_KEY  = os.getenv("CARTESIA_API_KEY", "")
TTS_VOICE_CARTESIA = os.getenv("TTS_VOICE_CARTESIA", "79a125e8-cd45-4c13-8a67-188112f4dd22")
LLM_PROVIDER      = os.getenv("LLM_PROVIDER", "groq")
CEREBRAS_API_KEY  = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL    = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
```

Validation: conditional — only require Cartesia key if `TTS_PROVIDER=cartesia`; only require Cerebras key if `LLM_PROVIDER=cerebras`. Groq key stays required regardless.

---

## New Environment Variables

| Var | Default | Required |
|-----|---------|----------|
| `TTS_PROVIDER` | `deepgram` | No — existing var, new value `cartesia` |
| `CARTESIA_API_KEY` | — | Yes, if `TTS_PROVIDER=cartesia` |
| `TTS_VOICE_CARTESIA` | `79a125e8-cd45-4c13-8a67-188112f4dd22` | No |
| `LLM_PROVIDER` | `groq` | No |
| `CEREBRAS_API_KEY` | — | Yes, if `LLM_PROVIDER=cerebras` |
| `CEREBRAS_MODEL` | `llama-3.3-70b` | No |

---

## Dependencies

Add to `requirements.txt`:
```
livekit-plugins-cartesia
```

Cerebras uses the existing `openai` plugin — no new package needed. Optionally add `cerebras-cloud-sdk` for typing, but not required at runtime.

---

## Error Handling

- If `TTS_PROVIDER=cartesia` but `CARTESIA_API_KEY` is missing → `config.validate()` exits at startup with a clear error message.
- If `LLM_PROVIDER=cerebras` but `CEREBRAS_API_KEY` is missing → same.
- No runtime fallback between providers — fail fast at startup.

---

## Rollback

All changes are env-var driven. To revert:
- Set `TTS_PROVIDER=deepgram` → back to Aura-2
- Set `LLM_PROVIDER=groq` → back to Groq Llama 4 Scout
- No code changes needed.

---

## Out of Scope

- STT changes (Deepgram flux is already optimal)
- Endpointing timing tweaks (Option C — defer to follow-on)
- Parallel calling or concurrency changes
