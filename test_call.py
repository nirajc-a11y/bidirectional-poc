"""
Diagnostic script to test SIP outbound calling.
Run: python test_call.py +919422571198
"""
import asyncio
import json
import sys
import time

from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv()

import config


async def monitor_room_events(room_name: str, token: str):
    """Connect to the room and monitor events in real-time."""
    room = rtc.Room()

    @room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        print(f"  EVENT: Participant connected: {participant.identity} (sid={participant.sid})")

    @room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        print(f"  EVENT: Participant disconnected: {participant.identity}")
        # Check for SIP disconnect reason
        attrs = participant.attributes
        if attrs:
            print(f"  EVENT: Participant attributes: {attrs}")

    @room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant):
        print(f"  EVENT: Track subscribed: {track.kind} from {participant.identity}")

    @room.on("data_received")
    def on_data(data: rtc.DataPacket):
        print(f"  EVENT: Data received: {data.data[:200]}")

    @room.on("participant_attributes_changed")
    def on_attrs_changed(changed: dict, participant: rtc.Participant):
        print(f"  EVENT: Attributes changed for {participant.identity}: {changed}")

    try:
        await room.connect(config.LIVEKIT_URL, token)
        print(f"  Monitor connected to room: {room_name}")
        # Keep running
        while room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    except Exception as e:
        print(f"  Monitor error: {e}")
    finally:
        await room.disconnect()


async def test_sip_call(phone_number: str):
    print("=" * 60)
    print("SIP OUTBOUND CALL DIAGNOSTIC v2")
    print("=" * 60)

    # 1. Validate config
    print("\n[1] Checking configuration...")
    print(f"  LIVEKIT_URL: {config.LIVEKIT_URL}")
    print(f"  LIVEKIT_API_KEY: {config.LIVEKIT_API_KEY[:8]}...")
    print(f"  LIVEKIT_SIP_TRUNK_ID: {config.LIVEKIT_SIP_TRUNK_ID}")
    print(f"  Phone number: {phone_number}")

    lk_api = api.LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET,
    )

    room_name = f"test-call-{int(time.time())}"

    try:
        # 2. Check SIP trunks
        print("\n[2] Checking SIP trunks...")
        try:
            trunks = await lk_api.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            if trunks.items:
                for trunk in trunks.items:
                    print(f"  Trunk: id={trunk.sip_trunk_id}")
                    print(f"    Name: {trunk.name}")
                    print(f"    Address: {trunk.address}")
                    print(f"    Numbers: {trunk.numbers}")
                    print(f"    Transport: {trunk.transport}")
                    if trunk.auth_username:
                        print(f"    Auth: username={trunk.auth_username}")
                    else:
                        print(f"    Auth: NO CREDENTIALS SET (this may be the problem!)")
            else:
                print("  ERROR: No outbound SIP trunks found!")
                return
        except Exception as e:
            print(f"  Error: {e}")

        # 3. Create room
        print(f"\n[3] Creating room: {room_name}")
        room_info = await lk_api.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=120,
                metadata=json.dumps({"test": True}),
            )
        )
        print(f"  Room created: sid={room_info.sid}")

        # 4. Generate token and start room monitor
        print(f"\n[4] Starting room monitor...")
        token = (
            api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
            .with_identity("monitor")
            .with_name("Call Monitor")
            .with_grants(api.VideoGrants(
                room_join=True,
                room=room_name,
            ))
            .to_jwt()
        )
        monitor_task = asyncio.create_task(monitor_room_events(room_name, token))
        await asyncio.sleep(2)  # Let monitor connect

        # 5. Dispatch SIP call
        print(f"\n[5] Dispatching SIP call to {phone_number}...")
        try:
            sip_result = await lk_api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=config.LIVEKIT_SIP_TRUNK_ID,
                    sip_call_to=phone_number,
                    room_name=room_name,
                    participant_identity="callee",
                    participant_name="Test Callee",
                )
            )
            print(f"  SIP call dispatched!")
            print(f"  Participant ID: {sip_result.participant_id}")
            print(f"  SIP Call ID: {sip_result.sip_call_id}")
        except Exception as e:
            print(f"  ERROR: {e}")
            error_msg = str(e)
            if "geo" in error_msg.lower() or "permission" in error_msg.lower():
                print("\n  Twilio may not have geo-permissions enabled for India (+91)")
                print("  Go to Twilio Console -> Voice -> Settings -> Geo Permissions")
                print("  Enable 'India' in the list")
            return

        # 6. Wait and monitor
        print(f"\n[6] Waiting 45 seconds for call events...")
        print("  (Answer your phone when it rings!)\n")
        await asyncio.sleep(45)

        # 7. Check final state
        print(f"\n[7] Checking final room state...")
        try:
            participants = await lk_api.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            if participants.participants:
                for p in participants.participants:
                    print(f"  Participant: {p.identity}, state={p.state}")
                    if p.attributes:
                        print(f"    Attributes: {dict(p.attributes)}")
            else:
                print("  No participants remaining")
        except Exception as e:
            print(f"  Room may have been cleaned up: {e}")

        monitor_task.cancel()
        print("\n[8] Test complete.")
        print("\nIf the call didn't ring, check:")
        print("  1. Twilio Console -> Voice -> Settings -> Geo Permissions -> Enable India")
        print("  2. Twilio Console -> your SIP Trunk -> Termination -> Authentication is set")
        print("  3. Twilio trial: number must be verified at Phone Numbers -> Verified Caller IDs")
        print("  4. Twilio Console -> Debugger for any error logs")

    finally:
        try:
            await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            pass
        await lk_api.aclose()


if __name__ == "__main__":
    phone = sys.argv[1] if len(sys.argv) > 1 else "+919422571198"
    asyncio.run(test_sip_call(phone))
