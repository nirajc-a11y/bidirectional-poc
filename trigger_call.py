"""Trigger a single call directly - bypasses the web server."""
import asyncio
import json
import os
import time

from dotenv import load_dotenv
from livekit import api

load_dotenv()

import config


async def trigger_call():
    claim_data = {
        "patient_name": "Rahul Sharma",
        "member_id": "MEM-10001",
        "group_number": "GRP-500",
        "insurance_phone": "+919422571198",
        "claim_number": "CLM-2025-001",
        "date_of_service": "2025-03-15",
        "procedure_code": "99213",
        "diagnosis_code": "J06.9",
        "provider_name": "Dr. Priya Patel",
        "npi": "1234567890",
        "billed_amount": "15000.00",
    }

    room_name = f"call-CLM-2025-001"
    phone_number = claim_data["insurance_phone"]

    lk_api = api.LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET,
    )

    try:
        # Create room with claim data as metadata
        print(f"[1] Creating room: {room_name}")
        await lk_api.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                metadata=json.dumps(claim_data),
                empty_timeout=300,
            )
        )
        print("  Room created with claim metadata")

        # Wait a moment for agent to join
        print("[2] Waiting 3s for agent to join the room...")
        await asyncio.sleep(3)

        # Check if agent joined
        participants = await lk_api.room.list_participants(
            api.ListParticipantsRequest(room=room_name)
        )
        print(f"  Participants in room: {[p.identity for p in participants.participants]}")

        # Dispatch SIP call
        print(f"[3] Dispatching SIP call to {phone_number}...")
        sip_result = await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=config.LIVEKIT_SIP_TRUNK_ID,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity="insurance-rep",
                participant_name="Insurance Representative",
            )
        )
        print(f"  SIP call dispatched! Call ID: {sip_result.sip_call_id}")
        print(f"\n[4] Call is active! Answer your phone.")
        print("  Monitoring for 120 seconds...\n")

        # Monitor
        for i in range(40):
            await asyncio.sleep(3)
            try:
                participants = await lk_api.room.list_participants(
                    api.ListParticipantsRequest(room=room_name)
                )
                identities = []
                for p in participants.participants:
                    sip_status = p.attributes.get("sip.callStatus", "") if p.attributes else ""
                    identities.append(f"{p.identity}({sip_status})" if sip_status else p.identity)
                print(f"  [{i*3:3d}s] Participants: {identities}")

                # Check if call ended
                has_sip = any("insurance-rep" in p.identity for p in participants.participants)
                if not has_sip and i > 3:
                    print("  SIP participant left - call ended")
                    break
            except Exception as e:
                if "not_found" in str(e):
                    print("  Room closed - call completed")
                    break

        # Check results
        results_path = os.path.join("call_results", "CLM-2025-001.json")
        if os.path.exists(results_path):
            with open(results_path, "r") as f:
                data = json.load(f)
            print(f"\n[5] RESULTS:")
            print(f"  Results: {json.dumps(data.get('results', {}), indent=2)}")
            print(f"\n  Transcript:\n{data.get('transcript', 'No transcript')}")
        else:
            print(f"\n[5] No results file found at {results_path}")

    finally:
        try:
            await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        await lk_api.aclose()


if __name__ == "__main__":
    asyncio.run(trigger_call())
