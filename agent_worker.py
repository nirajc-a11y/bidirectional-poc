import asyncio
import json
import logging
import os
import re
from datetime import datetime

import config

from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    EndpointingOptions,
    InterruptionOptions,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    function_tool,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

logger = logging.getLogger("claim-agent")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)

_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


# --- TTS ---

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


# --- Tools ---

@function_tool(
    name="save_claim_status",
    description="Save the claim verification result. Call after collecting status, amounts, dates from the rep.",
)
async def save_claim_status(
    ctx: RunContext,
    claim_result: str,
    approved_amount: str = "",
    denial_reason: str = "",
    payment_date: str = "",
    appeal_deadline: str = "",
    reference_number: str = "",
    notes: str = "",
):
    _skip = {"", "null", "none", "n/a", "na"}
    result = {k: v for k, v in {
        "claim_result": claim_result,
        "approved_amount": approved_amount,
        "denial_reason": denial_reason,
        "payment_date": payment_date,
        "appeal_deadline": appeal_deadline,
        "reference_number": reference_number,
        "notes": notes,
    }.items() if v and v.strip().lower() not in _skip}
    ctx.session.userdata["claim_results"] = result
    logger.info(f"TOOL save_claim_status: {json.dumps(result)}")
    return "Saved. Confirm details with the rep, then close the call."


@function_tool(
    name="confirm_details",
    description="Record that the rep confirmed the details are correct. Call with no arguments.",
)
async def confirm_details(ctx: RunContext):
    ctx.session.userdata["confirmed"] = True
    logger.info("TOOL confirm_details: True")
    return "Recorded. Thank them and say goodbye."


@function_tool(
    name="mark_unable_to_verify",
    description="Use when the claim can't be verified — wrong dept, can't find claim, etc.",
)
async def mark_unable_to_verify(ctx: RunContext, reason: str):
    ctx.session.userdata["claim_results"] = {"claim_result": "unknown", "notes": reason}
    logger.info(f"TOOL mark_unable_to_verify: {reason}")
    return "Noted. Thank them and say goodbye."


@function_tool(
    name="end_call",
    description="End the phone call. Use when the conversation is complete, the rep asks to hang up or end the call, or the call cannot proceed.",
)
async def end_call(ctx: RunContext, reason: str = ""):
    ctx.session.userdata["call_ended"] = True
    logger.info(f"TOOL end_call: {reason}")
    return "Call ending. Say a brief goodbye."


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
    if ctx.session.userdata.get("mode") == "human":
        return "ok"  # already switched, ignore duplicate call
    ctx.session.userdata["mode"] = "human"
    ctx.session.userdata["ivr_end_time"] = datetime.now().isoformat()
    logger.info("TOOL declare_human_reached: switching to human mode")
    return "ok"


@function_tool(
    name="declare_ivr_failed",
    description="Call this when you cannot navigate the IVR — stuck in a loop, unrecognized system, or cannot reach claims after multiple attempts.",
)
async def declare_ivr_failed(ctx: RunContext, reason: str):
    ctx.session.userdata["claim_results"] = {"claim_result": "ivr-failed", "notes": reason}
    ctx.session.userdata["call_ended"] = True
    logger.info(f"TOOL declare_ivr_failed: {reason}")
    return "IVR navigation failed. Say a brief goodbye and end the call."


# --- Prompt ---

def get_ivr_prompt(claim_data: dict) -> str:
    name = os.getenv("AGENT_NAME", "Sarah")
    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")
    return f"""You are {name} from {org} calling to verify a medical insurance claim.

You just dialed an insurance company. The call has connected. Wait and listen.

STEP 1 — IDENTIFY WHAT YOU HEAR:
- If you hear a HUMAN voice answering (natural speech like "Claims department, how can I help?"), call declare_human_reached() IMMEDIATELY. Do NOT press any digits first.
- If you hear an AUTOMATED voice reading menu options (robotic, listing "press 1 for...", "press 2 for..."), you are in an IVR. Navigate it using send_dtmf.
- If you hear silence or ringing, wait up to 5 seconds before acting.

NAVIGATING AN IVR (only if you hear automated prompts):
- Listen to each prompt fully before pressing anything.
- Press the digit most likely to reach "claims", "claim status", "billing", or "insurance verification".
- If no option matches, press 0 to reach an operator.
- The moment the automated voice stops and a HUMAN answers, call declare_human_reached().
- If the same automated prompt repeats twice, you are stuck — call declare_ivr_failed("stuck in loop").
- Never press a digit you have already tried.

CRITICAL:
- Do NOT press any digits if a human has already answered.
- Do NOT speak or introduce yourself while navigating an IVR.
- Only use send_dtmf when you are certain you are hearing an automated phone menu.
- Call declare_human_reached() ONCE only — do not call it again if already called.
- NEVER narrate what you are doing ("I hear a human...", "I will call..."). Just call the tool silently.

CLAIM (for reference only — do NOT share during IVR):
Patient: {claim_data.get('patient_name', 'N/A')} | Claim#: {claim_data.get('claim_number', 'N/A')}
"""


def get_system_prompt(claim_data: dict) -> str:
    name = os.getenv("AGENT_NAME", "Sarah")
    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {name} from {org}, on a live call with an insurance company verifying a claim. Sound like a real person — calm, brief, professional.

VOICE RULES (strict):
- ONE short sentence per turn. Then stop and wait.
- No narrating: never say "I'll save that", "let me note that", "I've collected the info", etc.
- No filler openers like "Of course!" or "Certainly!". "Got it." or "Sure." only when natural.
- Use contractions. Keep it conversational.
- NEVER repeat what you've already said.
- Avoid starting sentences with "I". Lead with the action or question instead.
- No back-to-back questions. Ask one, wait for a full answer, then ask the next.
- If they give a partial answer (e.g. "it's denied"), follow up tightly: "Got it — and the denial reason?" not a full re-question.

INTERRUPTIONS:
- If they say "No", "Wait", or correct you — stop immediately, address the correction only.
- If they say "okay" or "one moment" after your question — they haven't answered, wait silently or ask once: "Sorry — what was that?"
- If they interrupt mid-sentence — drop whatever you were saying, respond to what they said.

FILLER WORDS:
- "um", "uh", "so", "let me", "one moment", "hold on", "let me check" = the rep is still formulating. Wait silently.
- Only re-ask after 8 full seconds of silence following a filler word.

SILENCE HANDLING:
- 3 seconds of silence after your question → ask once: "Sorry, are you still there?"
- 6 seconds total → say: "I can hear some background — just want to make sure you can hear me."
- 10 seconds total → say: "Having trouble hearing you — I'll try calling back." then call end_call.

DATA RULES:
- NEVER guess or assume. Only use what the rep says.
- Dates: if invalid (e.g. "Feb 29" on a non-leap year), ask: "Sorry, did you mean a different date?"
- Amounts: if unclear or very different from billed amount of ${claim_data.get('billed_amount', 'N/A')}, read it back once to confirm.
- If rep corrects anything during summary — say "Got it" and re-summarize. Don't say goodbye until they confirm.

CLAIM:
Patient: {claim_data.get('patient_name', 'N/A')} | Member ID: {claim_data.get('member_id', 'N/A')}
Claim#: {claim_data.get('claim_number', 'N/A')} | DOS: {claim_data.get('date_of_service', 'N/A')}
CPT: {claim_data.get('procedure_code', 'N/A')} | Billed: ${claim_data.get('billed_amount', 'N/A')}
Provider: {claim_data.get('provider_name', 'N/A')} | NPI: {claim_data.get('npi', 'N/A')}

FLOW — follow this order strictly, one line at a time. Say it naturally, don't read it verbatim:
1. "Hi, this is {name} from {org}. Is this the claims department?"
2. Once confirmed: "Calling about {claim_data.get('patient_name', 'N/A')}, claim {claim_data.get('claim_number', 'N/A')}. Can you pull that up?" — say this ONCE. Never repeat it.
3. Once they have it: "What's the status on that?"
4. Collect based on status — ONE field per turn, "Got it." between each. Never re-ask for info already given:
   - APPROVED → approved amount → payment date → reference number
   - DENIED → denial reason → appeal deadline
   - PENDING → expected timeline → any pending requirements
5. Once you have all fields, call save_claim_status silently. No announcement.
6. "So just to confirm — [status], [key amounts/dates]. Sound right?"
7. If they correct → re-save → re-summarize only the corrected part.
8. Confirmed → call confirm_details → "Great, thanks. Have a good one!"
9. Can't locate / wrong dept → call mark_unable_to_verify → "No problem, thanks anyway!"
10. Rep ends call → call end_call → "Thanks for your time. Bye!"

STRICT RULES:
- Never repeat a question or statement you've already made this call.
- Never re-introduce the patient or claim number after step 2.
- After goodbye — stop completely. Do not speak again.
- Always call a tool before ending: save_claim_status, mark_unable_to_verify, or end_call.
"""


class CallTranscript:
    def __init__(self):
        self.entries: list[str] = []
        self.start_time = datetime.now()

    def add_entry(self, speaker: str, text: str):
        elapsed = (datetime.now() - self.start_time).total_seconds()
        m, s = int(elapsed // 60), int(elapsed % 60)
        self.entries.append(f"[{m:02d}:{s:02d}] {speaker}: {text}")

    def get_full_transcript(self) -> str:
        return "\n".join(self.entries)


async def entrypoint(ctx: JobContext):
    logger.info(f"Joining room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    claim_data = {}
    if ctx.room.metadata:
        try:
            claim_data = json.loads(ctx.room.metadata)
        except json.JSONDecodeError:
            logger.warning(f"Invalid room metadata JSON in {ctx.room.name}")

    claim_number = claim_data.get("claim_number", "unknown")
    call_id = claim_data.get("call_id", "--------")
    call_start = datetime.now()
    ivr_end_time: datetime | None = None
    if not claim_data.get("claim_number") or not claim_data.get("patient_name"):
        logger.warning(f"[{call_id}] Missing required claim fields (claim_number, patient_name) in room {ctx.room.name}")
    logger.info(f"[{call_id}] Claim: {claim_number}")
    transcript = CallTranscript()

    # Groq — fastest model with tool calling
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        logger.error("GROQ_API_KEY not set — agent cannot function")
    llm = openai.LLM(
        model=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        base_url="https://api.groq.com/openai/v1",
        api_key=groq_key,
    )

    agent = Agent(
        instructions=get_ivr_prompt(claim_data),
        stt=deepgram.STT(
            model=os.getenv("STT_MODEL", "nova-3"),  # set STT_MODEL=flux to enable Flux (semantic turn detection)
            language="en",
            no_delay=True,
            smart_format=True,
            punctuate=True,
            keyterm=["claim number", "member ID", "date of service", "CPT code",
                     "approved", "denied", "pending", "appeal deadline",
                     "reference number", "explanation of benefits"],
        ),
        llm=llm,
        tts=get_tts(),
        vad=silero.VAD.load(),
        tools=[send_dtmf, declare_human_reached, declare_ivr_failed, save_claim_status, confirm_details, mark_unable_to_verify, end_call],
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(
                min_delay=0.3,   # faster pickup when rep finishes speaking
                max_delay=1.5,   # insurance reps often pause mid-sentence — give them room
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="adaptive",             # won't fire on filler words ("um", "uh", "hold on")
                min_duration=0.4,
                min_words=1,
                resume_false_interruption=True,
            ),
        ),
        allow_interruptions=True,
    )

    session = AgentSession(
        userdata={"claim_results": {}, "confirmed": False},
    )
    hangup_scheduled = False
    goodbye_said = False
    drop_handled = False
    ivr_prompt_history: list[str] = []
    escape_attempts = 0

    # Auto-hangup: disconnect SIP participant after goodbye
    async def auto_hangup_after_goodbye():
        """Wait for TTS to finish, then disconnect the SIP call."""
        nonlocal hangup_scheduled
        if hangup_scheduled:
            return  # Prevent duplicate hangup tasks
        hangup_scheduled = True

        # Cancel any queued speech after goodbye — we're done talking
        session.interrupt()

        # Wait for all pending TTS speech to finish playing
        try:
            await session.drain()
            logger.info("Speech drained, proceeding with hangup")
        except Exception as e:
            logger.warning(f"Drain failed, falling back to delay: {e}")
            await asyncio.sleep(4)
        logger.info("Auto-hangup: disconnecting SIP participant")
        # Remove SIP participant from room to end the phone call
        try:
            lk_api = api.LiveKitAPI(
                os.getenv("LIVEKIT_URL"),
                os.getenv("LIVEKIT_API_KEY"),
                os.getenv("LIVEKIT_API_SECRET"),
            )
            await lk_api.room.remove_participant(
                api.RoomParticipantIdentity(
                    room=ctx.room.name,
                    identity="insurance-rep",
                )
            )
            await lk_api.aclose()
            logger.info("SIP participant removed - call ended")
        except Exception as e:
            logger.error(f"Failed to remove SIP participant: {e}")
        # Then close the agent session
        await session.aclose()

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

    @session.on("conversation_item_added")
    def on_item(event):
        nonlocal goodbye_said, escape_attempts, ivr_end_time
        item = event.item
        role = getattr(item, "role", "unknown")
        content = ""
        if hasattr(item, "content") and item.content:
            if isinstance(item.content, str):
                content = item.content
            else:
                # content is list[str | ImageContent | AudioContent | Instructions]
                # Only extract non-empty string parts; skip repr noise like "[]", "{}"
                content = " ".join(
                    c for c in item.content
                    if isinstance(c, str) and c.strip() and c.strip() not in ("[]", "{}")
                )
        if not content.strip():
            return
        speaker = "Agent" if role == "assistant" else "Human"
        transcript.add_entry(speaker, content)
        logger.info(f"{speaker}: {content}")

        # Publish transcript line to room for real-time relay to dashboard
        try:
            asyncio.create_task(
                ctx.room.local_participant.publish_data(
                    json.dumps({"speaker": speaker, "text": content}).encode(),
                    topic="transcript",
                )
            )
        except Exception as e:
            logger.warning(f"[{call_id}] Failed to publish transcript data: {e}")

        # IVR loop detection — detect repeated prompts and trigger escape
        if role != "assistant" and session.userdata.get("mode", "ivr") == "ivr":
            normalized = re.sub(r"[^\w\s]", "", content.lower()).strip()
            if normalized and normalized in ivr_prompt_history[-3:]:
                escape_attempts += 1
                logger.warning(f"[{call_id}] IVR loop detected (escape attempt {escape_attempts})")
                if escape_attempts <= config.IVR_MAX_ESCAPE_ATTEMPTS:
                    session.generate_reply(
                        instructions="You are stuck in a loop — the same prompt repeated. Press 0 now using send_dtmf('0'). If that fails, say 'representative' out loud."
                    )
                else:
                    session.generate_reply(
                        instructions="IVR escape failed after multiple attempts. Call declare_ivr_failed with the reason."
                    )
            elif normalized:
                ivr_prompt_history.append(normalized)
                if len(ivr_prompt_history) > 10:
                    ivr_prompt_history.pop(0)

        # Auto-hangup when agent says a closing phrase.
        # Trigger if: call was confirmed, ended via tool, OR any results were saved (covers denied/unknown outcomes).
        if role == "assistant":
            # If goodbye already said, suppress any further agent speech immediately
            if goodbye_said:
                session.interrupt()  # returns a Future, not a coroutine
                return

            goodbye_phrases = [
                "great day", "good day", "have a good", "have a great",
                "bye", "goodbye", "take care", "good one", "good night",
                "thanks for your help", "thank you for your help",
                "thanks for your time", "thank you for your time",
                "no problem, thanks", "thanks anyway",
            ]
            is_concluding = (
                session.userdata.get("confirmed")
                or session.userdata.get("call_ended")
                or bool(session.userdata.get("claim_results"))
            )
            if is_concluding and any(p in content.lower() for p in goodbye_phrases):
                if not goodbye_said:
                    goodbye_said = True
                    asyncio.create_task(auto_hangup_after_goodbye())

        # Mode transition: swap to claim script as soon as human mode is set (any item role)
        if session.userdata.get("mode") == "human":
            if not session.userdata.get("human_mode_initialized"):
                session.userdata["human_mode_initialized"] = True
                ivr_end_str = session.userdata.get("ivr_end_time")
                if ivr_end_str:
                    try:
                        ivr_end_time = datetime.fromisoformat(ivr_end_str)
                    except ValueError:
                        pass
                asyncio.create_task(agent.update_instructions(get_system_prompt(claim_data)))
                session.generate_reply(
                    instructions=f'A human just answered. Say EXACTLY: "Hi, this is {os.getenv("AGENT_NAME", "Sarah")} from {os.getenv("PROVIDER_NAME", "ABC Medical Group")}. Is this the claims department?" — nothing else.'
                )
                logger.info(f"[{call_id}] Switched to human mode — claim script active")

    @ctx.room.on("sip_dtmf_received")
    def on_dtmf(event):
        digit = getattr(event, "digit", str(event))
        if digit == "1":
            session.userdata["confirmed"] = True
            transcript.add_entry("System", "DTMF 1 received")

    @ctx.room.on("participant_attributes_changed")
    def on_attrs(changed, participant):
        if changed.get("sip.dtmf") == "1":
            session.userdata["confirmed"] = True
            transcript.add_entry("System", "DTMF 1 received")

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        if participant.identity == "insurance-rep" and not hangup_scheduled and not drop_handled:
            logger.warning(f"[{call_id}] SIP participant disconnected unexpectedly")
            asyncio.create_task(handle_dropped_call())

    async def ivr_timeout_watchdog():
        """Give up on IVR navigation after IVR_TIMEOUT_SECONDS."""
        await asyncio.sleep(config.IVR_TIMEOUT_SECONDS)
        if session.userdata.get("mode", "ivr") != "ivr":
            return  # Already in human mode
        logger.warning(f"[{call_id}] IVR timeout after {config.IVR_TIMEOUT_SECONDS}s")
        await session.generate_reply(
            instructions=f"{config.IVR_TIMEOUT_SECONDS} seconds have passed. Try pressing 0 or saying 'representative' one final time. If it still doesn't work, call declare_ivr_failed('timeout after {config.IVR_TIMEOUT_SECONDS}s')."
        )
        # Hard timeout: force failure if still in IVR mode after 30 more seconds
        await asyncio.sleep(30)
        if session.userdata.get("mode", "ivr") == "ivr":
            logger.error(f"[{call_id}] IVR hard timeout — forcing failure")
            session.userdata["claim_results"] = {"claim_result": "ivr-failed", "notes": "hard timeout"}
            session.userdata["call_ended"] = True
            await session.aclose()

    session_closed = asyncio.Event()
    watchdog_task: asyncio.Task | None = None
    silence_watchdog_task: asyncio.Task | None = None

    @session.on("close")
    def on_close(*a):
        nonlocal watchdog_task, silence_watchdog_task, hangup_scheduled
        logger.info("Session closed")
        session_closed.set()
        if watchdog_task and not watchdog_task.done():
            watchdog_task.cancel()
        if silence_watchdog_task and not silence_watchdog_task.done():
            silence_watchdog_task.cancel()
        # Failsafe: disconnect SIP participant if auto_hangup was never triggered
        # (e.g., agent said "Thank you" without using end_call tool, or call was dropped)
        if not hangup_scheduled:
            logger.warning("Session closed without scheduled hangup — forcing SIP disconnect")
            hangup_scheduled = True  # Prevent auto_hangup from running separately

            async def _force_disconnect():
                lk_api = api.LiveKitAPI(
                    os.getenv("LIVEKIT_URL"),
                    os.getenv("LIVEKIT_API_KEY"),
                    os.getenv("LIVEKIT_API_SECRET"),
                )
                try:
                    await lk_api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=ctx.room.name,
                            identity="insurance-rep",
                        )
                    )
                except Exception as e:
                    logger.warning(f"[{call_id}] Force disconnect failed (may already be gone): {e}")
                finally:
                    await lk_api.aclose()

            asyncio.create_task(_force_disconnect())

    # Wait for SIP participant to connect and audio to be ready BEFORE starting the session.
    try:
        logger.info("Waiting for SIP participant to connect...")
        participant = await asyncio.wait_for(
            ctx.wait_for_participant(identity="insurance-rep"),
            timeout=60.0,
        )
        logger.info(f"SIP participant connected: {participant.identity}")
        track_count = len(participant.track_publications)
        logger.info(f"SIP participant tracks: audio={track_count}")
        if track_count == 0:
            logger.warning("SIP participant has no audio tracks — waiting up to 3s for audio to arrive")
            deadline = asyncio.get_event_loop().time() + 3.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.25)
                track_count = len(participant.track_publications)
                if track_count > 0:
                    logger.info(f"SIP audio track arrived after short delay")
                    break
            else:
                logger.warning("SIP participant still has no audio tracks after 3s — call may have no audio")

    except asyncio.TimeoutError:
        logger.error("SIP participant did not connect within 60s — call likely not answered")
        await session.aclose()
        return

    await session.start(agent=agent, room=ctx.room, record=False)

    # Focus agent input on the SIP participant's audio — must be after session.start()
    # so that room_io is initialized with a room reference.
    session.room_io.set_participant(participant.identity)

    async def opening_silence_watchdog():
        """If nothing is heard within 4s of connect, assume a human picked up silently
        and trigger the opening greeting. Real IVRs always speak within 1-2s."""
        await asyncio.sleep(4.0)
        if session.userdata.get("mode", "ivr") == "ivr" and not ivr_prompt_history:
            if session_closed.is_set():
                return
            logger.info(f"[{call_id}] No audio heard after 4s — triggering opening greeting")
            session.userdata["mode"] = "human"
            session.userdata["human_mode_initialized"] = True
            await agent.update_instructions(get_system_prompt(claim_data))
            session.generate_reply(
                instructions=f'Nobody has spoken yet. Say EXACTLY: "Hi, this is {os.getenv("AGENT_NAME", "Sarah")} from {os.getenv("PROVIDER_NAME", "ABC Medical Group")}. Is this the claims department?" — nothing else.'
            )

    silence_watchdog_task = asyncio.create_task(opening_silence_watchdog())
    watchdog_task = asyncio.create_task(ivr_timeout_watchdog())
    await session_closed.wait()

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


