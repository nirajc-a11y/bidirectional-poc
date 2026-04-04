import asyncio
import json
import logging
import os
import re
from datetime import datetime

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
    tts = deepgram.TTS(model=os.getenv("TTS_VOICE", "aura-2-athena-en"))
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
    result = {k: v for k, v in {
        "claim_result": claim_result,
        "approved_amount": approved_amount,
        "denial_reason": denial_reason,
        "payment_date": payment_date,
        "appeal_deadline": appeal_deadline,
        "reference_number": reference_number,
        "notes": notes,
    }.items() if v}
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


# --- Prompt ---

def get_system_prompt(claim_data: dict) -> str:
    name = os.getenv("AGENT_NAME", "Sarah")
    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {name}, a friendly and professional representative from {org}. You are on a live phone call with an insurance company to check on a medical claim. Sound natural — like a real person, not a script reader.

IDENTITY (NEVER BREAK):
- You ARE on a real phone call right now. This is not a simulation.
- NEVER say you "can't make calls" or "can't interact with the outside world."
- NEVER break character or refer to yourself as an AI, language model, or assistant.
- If the other person asks to end the call, says "hang up", or wants to stop, use the end_call tool immediately.

HANDLING INTERRUPTIONS (CRITICAL):
- If you are mid-sentence and hear background noise, a one-word response, or partial speech, DO NOT restart your sentence from the beginning.
- If interrupted with a short response like "okay" or "one moment", just continue from where you left off.
- Only say "Sorry, go ahead?" if it seems like they genuinely want to ask you something (more than 3 words, a clear question).
- NEVER repeat your full intro message ("I'm calling to check on a claim for...") more than once. If you've already stated the patient name and claim number, do not repeat the entire phrase — just answer the rep's question directly.

VOICE & TONE:
- Speak naturally, use contractions (I'm, we've, that's).
- Keep responses short — 1 sentence max per turn, then wait.
- Use filler words sparingly but naturally: "Great", "Got it", "Perfect".
- Never narrate your actions or say "I'm going to wait."

CRITICAL RULES:
- NEVER make up or guess any information. Only use what the rep tells you.
- NEVER call any tool until you've actually heard the information from the rep.
- NEVER call confirm_details until the rep explicitly says "yes" or "correct" to your summary.

DATA ACCURACY:
- Dates: If you hear something ambiguous like "twenty nine thirty", ask "Sorry, could you give me the month and day for that?" Dates must be real calendar dates. Always read back as "April 29th" not "4/29".
- Amounts: Read back dollar amounts clearly. If the approved amount is very different from the billed amount of ${claim_data.get('billed_amount', 'N/A')}, ask "Just to confirm, the approved amount is [X]?"
- If the rep corrects ANY detail during your summary, say "Got it, let me update that" — then re-summarize with the fix. Do NOT say goodbye until they confirm.

CLAIM INFO:
Patient: {claim_data.get('patient_name', 'N/A')} | Member ID: {claim_data.get('member_id', 'N/A')}
Claim#: {claim_data.get('claim_number', 'N/A')} | DOS: {claim_data.get('date_of_service', 'N/A')}
CPT: {claim_data.get('procedure_code', 'N/A')} | Billed: ${claim_data.get('billed_amount', 'N/A')}
Provider: {claim_data.get('provider_name', 'N/A')} | NPI: {claim_data.get('npi', 'N/A')}

CALL FLOW:
1. "Hi, this is {name} from {org}. Am I speaking with the claims department?"
2. "I'm calling to check on a claim for {claim_data.get('patient_name', 'N/A')}, claim number {claim_data.get('claim_number', 'N/A')}." Only give more details if asked.
3. "Could you pull up the status on that for me?"
4. Based on the status they give you:
   - APPROVED: ask approved amount → payment date → reference number (one at a time, say "Got it" between each).
   - DENIED/NOT APPROVED: ask denial reason → appeal deadline (one at a time). Do NOT ask for approved amount.
   - PENDING: ask expected resolution timeline and any pending requirements.
5. Once you have the relevant info, IMMEDIATELY call save_claim_status with all collected fields.
6. Then summarize what you've got: "So just to confirm — [status], [key detail], [key detail]. Does that all sound right?"
7. If they correct anything → update via save_claim_status → re-summarize.
8. After they confirm → call confirm_details → "Thank you so much for your help. Have a great day!"
9. If they cannot locate the claim, transferred incorrectly, or cannot help → call mark_unable_to_verify → "No problem, thanks anyway. Have a good one!"
10. If they ask to hang up, end the call, or say goodbye → call end_call → "No problem, thanks for your time. Goodbye!"

IMPORTANT — YOU MUST ALWAYS CALL A TOOL BEFORE ENDING THE CALL:
- If you collected ANY information about the claim → call save_claim_status.
- If the call was completely unhelpful → call mark_unable_to_verify.
- If ending at rep's request → call end_call.
- Never say goodbye without first calling one of these tools.
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
    if not claim_data.get("claim_number") or not claim_data.get("patient_name"):
        logger.warning(f"Missing required claim fields (claim_number, patient_name) in room {ctx.room.name}")
    logger.info(f"Claim: {claim_number}")
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
        instructions=get_system_prompt(claim_data),
        stt=deepgram.STT(model="nova-3", language="en", no_delay=True, smart_format=True, punctuate=True),
        llm=llm,
        tts=get_tts(),
        vad=silero.VAD.load(),
        tools=[save_claim_status, confirm_details, mark_unable_to_verify, end_call],
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing=EndpointingOptions(
                min_delay=0.5,
                max_delay=1.5,
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="vad",
                min_duration=0.8,   # Short enough to catch 2-word questions like "Claim for?"
                min_words=2,        # 2+ words = real interruption (handles "Claim for", "Can you repeat")
                resume_false_interruption=True,  # Single-word noise ("One", "Okay") resumes instead of restarting
            ),
        ),
        allow_interruptions=True,
    )

    session = AgentSession(
        userdata={"claim_results": {}, "confirmed": False},
    )
    hangup_scheduled = False

    # Auto-hangup: disconnect SIP participant after goodbye
    async def auto_hangup_after_goodbye():
        """Wait for TTS to finish, then disconnect the SIP call."""
        nonlocal hangup_scheduled
        if hangup_scheduled:
            return  # Prevent duplicate hangup tasks
        hangup_scheduled = True

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

    @session.on("conversation_item_added")
    def on_item(event):
        item = event.item
        role = getattr(item, "role", "unknown")
        content = ""
        if hasattr(item, "content") and item.content:
            content = item.content if isinstance(item.content, str) else " ".join(str(c) for c in item.content)
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
            logger.debug(f"Failed to publish transcript data: {e}")

        # Auto-hangup when agent says a closing phrase.
        # Trigger if: call was confirmed, ended via tool, OR any results were saved (covers denied/unknown outcomes).
        if role == "assistant":
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
                asyncio.create_task(auto_hangup_after_goodbye())

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

    session_closed = asyncio.Event()

    @session.on("close")
    def on_close(*a):
        logger.info("Session closed")
        session_closed.set()
        # Failsafe: disconnect SIP participant if auto_hangup was never triggered
        # (e.g., agent said "Thank you" without using end_call tool, or call was dropped)
        if not hangup_scheduled:
            logger.warning("Session closed without scheduled hangup — forcing SIP disconnect")
            asyncio.create_task(auto_hangup_after_goodbye())

    await session.start(agent=agent, room=ctx.room, record=False)

    # Wait for SIP participant to connect and audio to be ready
    try:
        participant = await ctx.wait_for_participant(identity="insurance-rep")
        logger.info(f"SIP participant connected: {participant.identity}")
        logger.info(f"SIP participant tracks: audio={len(participant.track_publications)}")

        # Focus agent input on the SIP participant's audio
        session.room_io.set_participant(participant.identity)

        # Let the AI generate its greeting
        session.generate_reply(
            instructions="Someone just picked up the phone. Greet them naturally and ask if you've reached the claims department."
        )
    except RuntimeError:
        logger.warning("Session closed before greeting could be sent")

    await session_closed.wait()

    # Save results
    results = session.userdata.get("claim_results", {})
    results["confirmed"] = str(session.userdata.get("confirmed", False)).lower()
    final = {"claim_number": claim_number, "transcript": transcript.get_full_transcript(), "results": results}
    logger.info(f"Done {claim_number}: {json.dumps(results)}")

    os.makedirs("call_results", exist_ok=True)
    safe_name = claim_number if _SAFE_FILENAME_RE.match(claim_number) else "unknown"
    with open(f"call_results/{safe_name}.json", "w") as f:
        json.dump(final, f, indent=2)


