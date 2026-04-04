import asyncio
import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
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
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

load_dotenv()

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

    return f"""You are {name} from {org} calling insurance about a claim. Be warm, natural, brief.

RULES: Max 1 short sentence per turn. Say "got it" or "okay" then ask next question. Stop and wait after each sentence.

CLAIM: {claim_data.get('patient_name', 'N/A')}, Member {claim_data.get('member_id', 'N/A')}, Claim# {claim_data.get('claim_number', 'N/A')}, DOS {claim_data.get('date_of_service', 'N/A')}, CPT {claim_data.get('procedure_code', 'N/A')}, Provider {claim_data.get('provider_name', 'N/A')}, NPI {claim_data.get('npi', 'N/A')}, Billed ${claim_data.get('billed_amount', 'N/A')}

STEPS:
1. "Hi, this is {name} from {org}, am I speaking with claims?"
2. "I have a claim for {claim_data.get('patient_name', 'N/A')}, claim {claim_data.get('claim_number', 'N/A')}." More details only if asked.
3. "Could you check the status?"
4. One follow-up at a time: amount → date → reference number.
5. Call save_claim_status with all info.
6. "So, approved for [amount], payment [date], correct?"
7. On yes → call confirm_details.
8. "Thanks, have a great day!"

Can't help → call mark_unable_to_verify, thank them, end call. Never say "I'm going to wait". Just wait."""


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

    await session.start(agent=agent, room=ctx.room)

    # Greet when SIP participant connects
    async def greet():
        while True:
            for p in ctx.room.remote_participants.values():
                if p.identity == "insurance-rep":
                    await asyncio.sleep(1)
                    name = os.getenv("AGENT_NAME", "Sarah")
                    org = os.getenv("PROVIDER_NAME", "ABC Medical Group")
                    await session.say(
                        f"Hi, this is {name} from {org}. "
                        f"I'm calling about a claim — is this the claims department?"
                    )
                    return
            await asyncio.sleep(0.5)

    asyncio.create_task(greet())
    await session_closed.wait()

    # Save results
    results = session.userdata.get("claim_results", {})
    results["confirmed"] = str(session.userdata.get("confirmed", False)).lower()
    final = {"claim_number": claim_number, "transcript": transcript.get_full_transcript(), "results": results}
    logger.info(f"Done {claim_number}: {json.dumps(results)}")

    os.makedirs("call_results", exist_ok=True)
    with open(f"call_results/{claim_number}.json", "w") as f:
        json.dump(final, f, indent=2)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
