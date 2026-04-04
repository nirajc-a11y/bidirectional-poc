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
    TurnHandlingOptions,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, openai, silero

# Using Deepgram for TTS (native LiveKit plugin, most reliable)

load_dotenv()

logger = logging.getLogger("claim-agent")
logger.setLevel(logging.INFO)


def get_system_prompt(claim_data: dict) -> str:
    agent_name = os.getenv("AGENT_NAME", "Sarah")
    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")

    return f"""You are {agent_name}, a warm and professional medical claims representative calling from {provider_name}. You sound natural, friendly, and confident — like a real person making a work call, not a robot reading a script.

## Your personality
- Speak naturally with contractions ("I'd like", "we're calling", "that's great")
- Use filler words occasionally ("so", "alright", "great", "perfect")
- Keep sentences short — 1-2 sentences max per turn
- Wait for the other person to finish before speaking
- Be patient and polite, never rushed
- If someone sounds confused, slow down and clarify

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

1. GREET: Start with something like "Hi, this is {agent_name} calling from {provider_name}. I'm calling to check on a claim status. Am I speaking with someone in the claims department?"

2. PROVIDE DETAILS: Don't dump all details at once. Start with the key ones:
   "Great, so I have a claim for patient {claim_data.get('patient_name', 'N/A')}, member ID {claim_data.get('member_id', 'N/A')}, claim number {claim_data.get('claim_number', 'N/A')}."
   Then provide date of service, procedure code, provider details only if asked or needed.

3. ASK ABOUT STATUS: "Could you let me know the current status of this claim?"
   Then based on their response, ask follow-up questions naturally:
   - If approved: "That's great. What's the approved amount? And do you have an expected payment date?"
   - If denied: "I see. Could you tell me the denial reason? And is there an appeal deadline?"
   - If pending: "Okay. Any idea when it might be processed?"
   - Always: "And could I get a reference number for this call?"

4. CONFIRM: Read back what you heard naturally: "Alright, so just to make sure I have everything right — the claim is [status], [details]. Does that sound correct? You can press 1 to confirm or just say yes."

5. CLOSE: "Perfect, thank you so much for your help. Have a great day!"

## If they can't find the claim or don't have info
- Be understanding: "No problem, I understand."
- Ask if there's another way to look it up
- If they truly can't help, politely thank them and end the call

## Critical rules
- NEVER speak for more than 2 sentences at a time
- ALWAYS pause and let them respond
- If you hear silence, wait — don't repeat yourself immediately
- If they say "hold on" or "one moment", just say "Sure, take your time" and wait silently
- At the END of the conversation, include this JSON in your final response (the system will extract it):
  {{"claim_result": "approved/denied/pending/in-review/unknown", "approved_amount": "", "denial_reason": "", "payment_date": "", "appeal_deadline": "", "reference_number": ""}}
"""


class CallTranscript:
    def __init__(self):
        self.entries: list[str] = []
        self.start_time = datetime.now()
        self.collected_data: dict = {}

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
    confirmation_received = False

    # Groq LLM — use llama-3.1-8b-instant for speed
    groq_llm = openai.LLM(
        model="llama-3.1-8b-instant",
        base_url="https://api.groq.com/openai/v1",
        api_key=os.getenv("GROQ_API_KEY"),
    )

    agent = Agent(
        instructions=get_system_prompt(claim_data),
        stt=deepgram.STT(),
        llm=groq_llm,
        tts=deepgram.TTS(model="aura-2-andromeda-en"),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            endpointing=EndpointingOptions(
                min_delay=0.6,   # Wait at least 0.6s of silence before responding
                max_delay=1.5,   # Max wait before responding
            ),
            interruption=InterruptionOptions(
                enabled=True,
                mode="vad",       # Use VAD-based interruption (more reliable on phone)
                min_duration=0.8, # Only interrupt if user speaks for 0.8s+
                min_words=3,      # Need at least 3 words to count as interruption
            ),
        ),
        allow_interruptions=True,
        min_endpointing_delay=0.5,
        max_endpointing_delay=1.5,
    )

    session = AgentSession()

    # Track conversation via conversation_item_added
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

            # Extract JSON results from agent messages
            if "{" in content and "claim_result" in content:
                try:
                    json_start = content.index("{")
                    json_end = content.rindex("}") + 1
                    result_data = json.loads(content[json_start:json_end])
                    transcript.collected_data.update(result_data)
                    logger.info(f"Extracted results: {result_data}")
                except (json.JSONDecodeError, ValueError):
                    pass
        elif role == "user":
            transcript.add_entry("Human", content)
            logger.info(f"Human: {content}")

    # DTMF handling
    @ctx.room.on("sip_dtmf_received")
    def on_sip_dtmf(event):
        nonlocal confirmation_received
        digit = event.digit if hasattr(event, "digit") else str(event)
        logger.info(f"DTMF received: {digit}")
        if digit == "1":
            confirmation_received = True
            transcript.add_entry("System", "DTMF confirmation received (digit 1)")

    @ctx.room.on("participant_attributes_changed")
    def on_attributes_changed(changed_attributes: dict[str, str], participant: rtc.Participant):
        nonlocal confirmation_received
        dtmf_digit = changed_attributes.get("sip.dtmf")
        if dtmf_digit == "1":
            confirmation_received = True
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

    # Wait for SIP participant, then greet after Twilio trial message
    async def wait_and_greet():
        while True:
            participants = ctx.room.remote_participants
            for p in participants.values():
                if p.identity == "insurance-rep":
                    logger.info("SIP participant connected, waiting for Twilio trial message...")
                    await asyncio.sleep(8)
                    logger.info("Sending greeting...")
                    agent_name = os.getenv("AGENT_NAME", "Sarah")
                    provider_name = os.getenv("PROVIDER_NAME", "ABC Medical Group")
                    await session.say(
                        f"Hi, this is {agent_name} calling from {provider_name}. "
                        f"I'm calling to check on a claim status. "
                        f"Am I speaking with someone who can help me with that?"
                    )
                    return
            await asyncio.sleep(0.5)

    asyncio.create_task(wait_and_greet())

    # Wait for call to end
    await session_closed.wait()

    # Save results
    results = transcript.collected_data
    results["confirmed"] = str(confirmation_received).lower()

    final_data = {
        "claim_number": claim_number,
        "transcript": transcript.get_full_transcript(),
        "results": results,
    }
    logger.info(f"Call completed for claim {claim_number}: {json.dumps(results, indent=2)}")

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
