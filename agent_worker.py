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
from livekit.plugins import deepgram, openai, silero

from sarvam_tts import SarvamTTS

load_dotenv()

logger = logging.getLogger("claim-agent")
logger.setLevel(logging.INFO)

# --- Voice Configuration ---
VOICE_MODE = os.getenv("VOICE_MODE", "indian")  # "indian" or "global"


def get_tts():
    """Get TTS based on VOICE_MODE."""
    if VOICE_MODE == "indian":
        return SarvamTTS(
            api_key=os.getenv("SARVAM_API_KEY"),
            model="bulbul:v3",
            speaker=os.getenv("SARVAM_SPEAKER", "amelia"),
            pace=1.1,
        )
    else:
        return deepgram.TTS(model="aura-2-andromeda-en")


# --- Tool Calling ---
# These tools let the LLM structure data during the conversation

@function_tool(
    name="save_claim_status",
    description="Save the claim verification result after collecting all information from the insurance representative. Call this ONCE you have gathered the claim status and all relevant details.",
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
    """Save claim result — called by the AI during conversation."""
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
    # Store in session userdata for later retrieval
    ctx.session.userdata["claim_results"] = result
    logger.info(f"Tool called - save_claim_status: {json.dumps(result, indent=2)}")
    return f"Claim result saved: {claim_result}. Now please confirm the details with the representative and close the call."


@function_tool(
    name="confirm_details",
    description="Mark that the insurance representative has confirmed the details are correct. Call this after they confirm verbally or press 1.",
)
async def confirm_details(ctx: RunContext, confirmed: bool = True):
    """Mark confirmation received."""
    ctx.session.userdata["confirmed"] = confirmed
    logger.info(f"Tool called - confirm_details: confirmed={confirmed}")
    return "Confirmation recorded. Thank the representative and end the call politely."


@function_tool(
    name="mark_unable_to_verify",
    description="Call this if the insurance representative cannot find the claim, cannot provide information, or the call cannot be completed for any reason.",
)
async def mark_unable_to_verify(ctx: RunContext, reason: str):
    """Mark that verification could not be completed."""
    ctx.session.userdata["claim_results"] = {
        "claim_result": "unknown",
        "notes": reason,
    }
    logger.info(f"Tool called - mark_unable_to_verify: {reason}")
    return "Noted. Thank the representative for their time and end the call politely."


# --- System Prompt ---

def get_system_prompt(claim_data: dict) -> str:
    agent_name = os.getenv("AGENT_NAME", "Sarah")
    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {agent_name}, a warm and professional medical claims representative calling from {provider_name}. You sound natural, friendly, and confident — like a real person making a work call.

## Your personality
- Speak naturally with contractions ("I'd like", "we're calling", "that's great")
- Use conversational fillers occasionally ("so", "alright", "great", "perfect")
- Keep sentences SHORT — 1-2 sentences max per turn. This is critical.
- Wait for the other person to finish before speaking
- Be patient and polite, never rushed

## Claim you're calling about
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

## How the conversation should go

1. GREET: "Hi, this is {agent_name} from {provider_name}. I'm calling to check on a claim status — am I speaking with the claims department?"

2. PROVIDE KEY DETAILS: Don't dump everything at once. Start with:
   "I have a claim for patient {claim_data.get('patient_name', 'N/A')}, member ID {claim_data.get('member_id', 'N/A')}, claim number {claim_data.get('claim_number', 'N/A')}."
   Give more details only if asked.

3. ASK STATUS: "Could you check the current status of this claim?"
   Follow up naturally based on response:
   - Approved → "Great, what's the approved amount and expected payment date?"
   - Denied → "I see. What's the denial reason and appeal deadline?"
   - Pending → "When might it be processed?"
   - Always → "Could I get a reference number for this call?"

4. SAVE RESULTS: Once you have the information, call the `save_claim_status` tool with all the details you collected.

5. CONFIRM: Read back what you heard: "Just to confirm — the claim is [status], [key details]. Is that correct?"
   When they confirm, call the `confirm_details` tool.

6. CLOSE: "Thank you so much for your help. Have a great day!"

## If they can't find the claim
- Be understanding: "No problem at all."
- Ask if there's another way to look it up
- If they truly can't help, call `mark_unable_to_verify` with the reason, thank them, and end the call

## Critical rules
- NEVER speak more than 2 sentences at a time
- ALWAYS pause and let them respond
- Use the tools to save results — do NOT output raw JSON
- If they say "hold on", just say "Sure, take your time" and wait
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

    # LLM — Groq with fast model
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
            endpointing=EndpointingOptions(
                min_delay=0.5,
                max_delay=1.5,
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="vad",
                min_duration=0.8,
                min_words=3,
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
            transcript.add_entry("System", "DTMF confirmation received (digit 1)")

    @ctx.room.on("participant_attributes_changed")
    def on_attributes_changed(changed_attributes: dict[str, str], participant: rtc.Participant):
        dtmf_digit = changed_attributes.get("sip.dtmf")
        if dtmf_digit == "1":
            session.userdata["confirmed"] = True
            transcript.add_entry("System", "DTMF confirmation received (digit 1)")
            logger.info("DTMF confirmation received via SIP attributes")

    # Session close event
    session_closed = asyncio.Event()

    @session.on("close")
    def on_session_close(*args):
        logger.info("Agent session closed")
        session_closed.set()

    # Start session
    await session.start(agent=agent, room=ctx.room)

    # Wait for SIP participant, then greet
    async def wait_and_greet():
        while True:
            participants = ctx.room.remote_participants
            for p in participants.values():
                if p.identity == "insurance-rep":
                    logger.info("SIP participant connected...")
                    await asyncio.sleep(1)
                    logger.info("Sending greeting...")
                    agent_name = os.getenv("AGENT_NAME", "Sarah")
                    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")
                    await session.say(
                        f"Hi, this is {agent_name} from {provider_name}. "
                        f"I'm calling to check on a claim status. "
                        f"Am I speaking with the claims department?"
                    )
                    return
            await asyncio.sleep(0.5)

    asyncio.create_task(wait_and_greet())

    # Wait for call to end
    await session_closed.wait()

    # Gather results from tool calls
    results = session.userdata.get("claim_results", {})
    results["confirmed"] = str(session.userdata.get("confirmed", False)).lower()

    final_data = {
        "claim_number": claim_number,
        "transcript": transcript.get_full_transcript(),
        "results": results,
    }
    logger.info(f"Call completed for claim {claim_number}: {json.dumps(results, indent=2)}")

    # Write results file
    results_dir = "call_results"
    os.makedirs(results_dir, exist_ok=True)
    results_path = os.path.join(results_dir, f"{claim_number}.json")
    with open(results_path, "w") as f:
        json.dump(final_data, f, indent=2)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )
