import asyncio
import json
import logging
import os
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


# --- TTS ---

def get_tts():
    """TTS selection based on TTS_PROVIDER env var.

    Options:
      elevenlabs - Natural voice with speed control
      deepgram   - Reliable, low latency (default)
    """
    provider = os.getenv("TTS_PROVIDER", "elevenlabs")
    if provider == "elevenlabs" and os.getenv("ELEVEN_API_KEY"):
        return elevenlabs.TTS(
            voice_id=os.getenv("ELEVEN_VOICE_ID", "pFZP5JQG7iQjIQuC4Bku"),
            model="eleven_turbo_v2_5",
            api_key=os.getenv("ELEVEN_API_KEY"),
            voice_settings=elevenlabs.VoiceSettings(
                stability=0.6,          # More natural variation
                similarity_boost=0.8,   # Stay close to original voice
                speed=0.85,             # Slow down 15% for clarity
                use_speaker_boost=True, # Enhance clarity
            ),
            encoding="pcm_24000",       # High quality PCM
        )
    return deepgram.TTS(model=os.getenv("TTS_VOICE", "aura-2-athena-en"))


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


# --- Prompt ---

def get_system_prompt(claim_data: dict) -> str:
    name = os.getenv("AGENT_NAME", "Sarah")
    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {name} from {org} calling an insurance company to verify a medical claim. Be warm, natural, and brief.

RULES:
- Max 1 short sentence per turn. Then STOP and WAIT for the other person to respond.
- Never call any tool until you have actually spoken with the rep and collected real information from them.
- Do NOT assume or make up any claim status, amounts, or dates. You must HEAR them from the rep first.
- Do NOT call confirm_details until the rep verbally confirms your summary is correct.

CLAIM DETAILS:
Patient: {claim_data.get('patient_name', 'N/A')}
Member ID: {claim_data.get('member_id', 'N/A')}
Claim#: {claim_data.get('claim_number', 'N/A')}
Date of Service: {claim_data.get('date_of_service', 'N/A')}
CPT Code: {claim_data.get('procedure_code', 'N/A')}
Provider: {claim_data.get('provider_name', 'N/A')}
NPI: {claim_data.get('npi', 'N/A')}
Billed Amount: ${claim_data.get('billed_amount', 'N/A')}

CONVERSATION FLOW:
1. Introduce yourself and confirm you're speaking with the claims department. Wait for response.
2. Tell them you're calling about a claim for {claim_data.get('patient_name', 'N/A')}, claim number {claim_data.get('claim_number', 'N/A')}. Give more details only if they ask. Wait for response.
3. Ask them to check the claim status. Wait for their answer.
4. Ask follow-up questions ONE at a time: approved amount, payment date, reference number. Wait after each.
5. After collecting all info from the rep, call save_claim_status with what they told you.
6. Summarize back: "So, approved for [amount], payment on [date], is that correct?" Wait for confirmation.
7. Only after they say yes, call confirm_details.
8. Thank them and say goodbye.

If they can't help (wrong department, can't find claim, etc.), call mark_unable_to_verify, thank them, and end the call.
Never say "I'm going to wait" or narrate what you're doing. Just wait silently for their response."""


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
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    claim_data = {}
    if ctx.room.metadata:
        try:
            claim_data = json.loads(ctx.room.metadata)
        except json.JSONDecodeError:
            pass

    claim_number = claim_data.get("claim_number", "unknown")
    logger.info(f"Claim: {claim_number}")
    transcript = CallTranscript()

    # Groq — fastest model with tool calling
    llm = openai.LLM(
        model=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        base_url="https://api.groq.com/openai/v1",
        api_key=os.getenv("GROQ_API_KEY"),
    )

    agent = Agent(
        instructions=get_system_prompt(claim_data),
        stt=deepgram.STT(),
        llm=llm,
        tts=get_tts(),
        vad=silero.VAD.load(),
        tools=[save_claim_status, confirm_details, mark_unable_to_verify],
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",  # VAD-based: faster, responds to voice activity directly
            endpointing=EndpointingOptions(
                min_delay=0.3,
                max_delay=0.8,  # Respond within 800ms max
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="vad",      # VAD-based interruption: immediate response to speech
                min_duration=0.3,  # 300ms of speech = interrupt (very responsive)
                min_words=1,       # Single word can interrupt
                resume_false_interruption=True,
            ),
        ),
        allow_interruptions=True,
    )

    session = AgentSession(
        userdata={"claim_results": {}, "confirmed": False},
    )

    # Auto-hangup: disconnect SIP participant after goodbye
    async def auto_hangup_after_goodbye():
        """Wait 3s after goodbye, then disconnect the SIP call."""
        await asyncio.sleep(3)
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
        except Exception:
            pass

        # Auto-hangup when agent says goodbye
        if role == "assistant" and session.userdata.get("confirmed"):
            goodbye_words = ["great day", "bye", "goodbye", "take care"]
            if any(w in content.lower() for w in goodbye_words):
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

    await session.start(agent=agent, room=ctx.room, record=False)

    # Wait for SIP participant to connect and audio to be ready
    try:
        participant = await ctx.wait_for_participant(identity="insurance-rep")
        logger.info(f"SIP participant connected: {participant.identity}")

        # Explicitly subscribe to the SIP participant's audio
        session.room_io.set_participant(participant.identity)

        # Let the AI generate its greeting
        session.generate_reply(
            instructions="The insurance rep just answered the phone. Start with step 1: introduce yourself and ask if you're speaking with the claims department."
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
    with open(f"call_results/{claim_number}.json", "w") as f:
        json.dump(final, f, indent=2)


