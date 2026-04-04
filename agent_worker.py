import asyncio
import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from livekit import rtc
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
from livekit.plugins import deepgram, openai, silero, turn_detector

from sarvam_tts import SarvamTTS

load_dotenv()

logger = logging.getLogger("claim-agent")
logger.setLevel(logging.INFO)

# --- Voice Configuration ---
VOICE_MODE = os.getenv("VOICE_MODE", "indian")  # "indian" or "global"


def get_tts():
    """Get TTS based on VOICE_MODE env var.

    Deepgram aura-2 voices:
    - aura-2-andromeda-en: Clear, professional female (default global)
    - aura-2-athena-en: Warm, natural female
    - aura-2-luna-en: Friendly female
    - aura-2-stella-en: Confident female
    - aura-2-orion-en: Professional male
    - aura-2-arcas-en: Deep male
    """
    voice = os.getenv("TTS_VOICE", "aura-2-andromeda-en")
    return deepgram.TTS(model=voice)


# --- Tool Calling ---

@function_tool(
    name="save_claim_status",
    description="Save the verified claim status and details collected from the insurance representative. Call this ONCE after gathering all claim information.",
)
async def save_claim_status(
    ctx: RunContext,
    claim_result: str,
    approved_amount: str = "",
    denial_reason: str = "",
    payment_date: str = "",
    appeal_deadline: str = "",
    reference_number: str = "",
    processing_date: str = "",
    notes: str = "",
):
    result = {
        "claim_result": claim_result,
        "approved_amount": approved_amount,
        "denial_reason": denial_reason,
        "payment_date": payment_date,
        "appeal_deadline": appeal_deadline,
        "reference_number": reference_number,
        "processing_date": processing_date,
        "notes": notes,
    }
    ctx.session.userdata["claim_results"] = result
    logger.info(f"TOOL: save_claim_status -> {json.dumps(result)}")
    return "Saved. Now confirm the details with the representative and close the call."


@function_tool(
    name="confirm_details",
    description="Record that the insurance representative confirmed the details are correct.",
)
async def confirm_details(ctx: RunContext, confirmed: bool = True):
    ctx.session.userdata["confirmed"] = confirmed
    logger.info(f"TOOL: confirm_details -> {confirmed}")
    return "Confirmation recorded. Thank them and end the call."


@function_tool(
    name="mark_unable_to_verify",
    description="Call this when claim verification cannot be completed — rep can't find claim, wrong department, etc.",
)
async def mark_unable_to_verify(ctx: RunContext, reason: str):
    ctx.session.userdata["claim_results"] = {
        "claim_result": "unknown",
        "notes": reason,
    }
    logger.info(f"TOOL: mark_unable_to_verify -> {reason}")
    return "Noted. Thank them and end the call."


# --- System Prompt ---

def get_system_prompt(claim_data: dict) -> str:
    agent_name = os.getenv("AGENT_NAME", "Sarah")
    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {agent_name}, a warm and professional medical claims representative calling from {provider_name}.

## How you sound
- Natural, conversational — like a real person on a phone call
- Use contractions: "I'd", "we're", "that's", "I'll"
- Use fillers naturally: "so", "alright", "great", "perfect", "okay"
- SHORT responses — max 1-2 sentences per turn. Never monologue.
- Pause after each response. Wait for them. Do not keep talking.

## The claim
- Patient: {claim_data.get('patient_name', 'N/A')}
- Member ID: {claim_data.get('member_id', 'N/A')}
- Group: {claim_data.get('group_number', 'N/A')}
- Claim #: {claim_data.get('claim_number', 'N/A')}
- Date of Service: {claim_data.get('date_of_service', 'N/A')}
- Procedure: {claim_data.get('procedure_code', 'N/A')}
- Diagnosis: {claim_data.get('diagnosis_code', 'N/A')}
- Provider: {claim_data.get('provider_name', 'N/A')}
- NPI: {claim_data.get('npi', 'N/A')}
- Billed: ${claim_data.get('billed_amount', 'N/A')}

## Conversation flow

1. GREET — "Hi, this is {agent_name} from {provider_name}. I'm calling about a claim. Am I speaking with the claims department?"

2. GIVE KEY INFO — "I have a claim for {claim_data.get('patient_name', 'N/A')}, member ID {claim_data.get('member_id', 'N/A')}, claim number {claim_data.get('claim_number', 'N/A')}." Only give more details if they ask.

3. ASK STATUS — "Could you check the status of this claim for me?"

4. FOLLOW UP based on what they say:
   - Approved → "Great! What's the approved amount and payment date?"
   - Denied → "I see. What's the denial reason? And the appeal deadline?"
   - Pending → "When do you expect it to be processed?"
   - Always ask → "Could I get a reference number for this call?"

5. USE TOOLS — Once you have the info, call `save_claim_status` with all details.

6. CONFIRM — "Just to make sure I have it right — [read back key info]. Does that sound correct?" When they say yes, call `confirm_details`.

7. CLOSE — "Perfect, thank you so much. Have a great day!"

## If they can't help
- "No problem. Is there another way to look it up?"
- If stuck, call `mark_unable_to_verify`, thank them, end call.

## Rules
- NEVER more than 2 sentences at a time
- ALWAYS wait for their response
- If they say "hold on" → "Sure, take your time" then stay quiet
- Use tools to save results. Do not output JSON directly.
- Do not repeat yourself if there's silence — wait patiently.
"""


class CallTranscript:
    def __init__(self):
        self.entries: list[str] = []
        self.start_time = datetime.now()

    def add_entry(self, speaker: str, text: str):
        elapsed = (datetime.now() - self.start_time).total_seconds()
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        self.entries.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {text}")

    def get_full_transcript(self) -> str:
        return "\n".join(self.entries)


async def entrypoint(ctx: JobContext):
    logger.info(f"Agent joining room: {ctx.room.name}")

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Get claim data from room metadata
    claim_data = {}
    metadata_str = ctx.room.metadata
    if metadata_str:
        try:
            claim_data = json.loads(metadata_str)
        except json.JSONDecodeError:
            logger.warning("Could not parse room metadata as JSON")

    claim_number = claim_data.get("claim_number", "unknown")
    logger.info(f"Processing claim: {claim_number}")

    transcript = CallTranscript()

    # LLM — Groq llama-3.3-70b for tool calling support
    groq_llm = openai.LLM(
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        api_key=os.getenv("GROQ_API_KEY"),
    )

    agent = Agent(
        instructions=get_system_prompt(claim_data),
        stt=deepgram.STT(),
        llm=groq_llm,
        tts=get_tts(),
        vad=silero.VAD.load(),
        tools=[save_claim_status, confirm_details, mark_unable_to_verify],
        turn_handling=TurnHandlingOptions(
            # Semantic turn detection: uses ML model to detect when user is done
            # Falls back to VAD endpointing as secondary signal
            turn_detection="stt",
            endpointing=EndpointingOptions(
                min_delay=0.5,  # Wait at least 500ms of silence
                max_delay=3.0,  # Max 3s before assuming turn is done
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="adaptive",  # ML-based: detects real interruptions vs "mm-hmm"
                min_duration=0.5,  # 500ms of speech to count as interruption
                min_words=2,       # At least 2 words to interrupt
                resume_false_interruption=True,  # Resume from where it left off
            ),
        ),
        allow_interruptions=True,
    )

    session = AgentSession(
        userdata={"claim_results": {}, "confirmed": False},
    )

    # Track conversation
    @session.on("conversation_item_added")
    def on_conversation_item(event):
        item = event.item
        role = item.role if hasattr(item, "role") else "unknown"
        content = ""
        if hasattr(item, "content") and item.content:
            if isinstance(item.content, str):
                content = item.content
            elif isinstance(item.content, list):
                content = " ".join(str(c) for c in item.content)
            else:
                content = str(item.content)

        if not content.strip():
            return

        if role == "assistant":
            transcript.add_entry("Agent", content)
            logger.info(f"Agent: {content}")
        elif role == "user":
            transcript.add_entry("Human", content)
            logger.info(f"Human: {content}")

    # DTMF handling
    @ctx.room.on("sip_dtmf_received")
    def on_sip_dtmf(event):
        digit = event.digit if hasattr(event, "digit") else str(event)
        logger.info(f"DTMF received: {digit}")
        if digit == "1":
            session.userdata["confirmed"] = True
            transcript.add_entry("System", "DTMF confirmation (digit 1)")

    @ctx.room.on("participant_attributes_changed")
    def on_attributes_changed(changed_attributes: dict[str, str], participant: rtc.Participant):
        dtmf_digit = changed_attributes.get("sip.dtmf")
        if dtmf_digit == "1":
            session.userdata["confirmed"] = True
            transcript.add_entry("System", "DTMF confirmation (digit 1)")
            logger.info("DTMF confirmation via SIP attributes")

    # Session close
    session_closed = asyncio.Event()

    @session.on("close")
    def on_session_close(*args):
        logger.info("Agent session closed")
        session_closed.set()

    # Start session
    await session.start(agent=agent, room=ctx.room)

    # Greet when SIP participant connects
    async def wait_and_greet():
        while True:
            for p in ctx.room.remote_participants.values():
                if p.identity == "insurance-rep":
                    logger.info("SIP participant connected, greeting...")
                    await asyncio.sleep(1)
                    agent_name = os.getenv("AGENT_NAME", "Sarah")
                    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")
                    await session.say(
                        f"Hi, this is {agent_name} from {provider_name}. "
                        f"I'm calling about a claim. Am I speaking with the claims department?"
                    )
                    return
            await asyncio.sleep(0.5)

    asyncio.create_task(wait_and_greet())

    # Wait for call to end
    await session_closed.wait()

    # Gather results
    results = session.userdata.get("claim_results", {})
    results["confirmed"] = str(session.userdata.get("confirmed", False)).lower()

    final_data = {
        "claim_number": claim_number,
        "transcript": transcript.get_full_transcript(),
        "results": results,
    }
    logger.info(f"Call completed for {claim_number}: {json.dumps(results, indent=2)}")

    # Write results file
    results_dir = "call_results"
    os.makedirs(results_dir, exist_ok=True)
    with open(os.path.join(results_dir, f"{claim_number}.json"), "w") as f:
        json.dump(final_data, f, indent=2)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
