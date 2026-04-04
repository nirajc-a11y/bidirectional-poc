import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from livekit import api

import config
from call_manager import CallManager

# State
call_mgr = CallManager(config.CSV_PATH)
connected_websockets: list[WebSocket] = []
call_loop_task: asyncio.Task | None = None
is_paused = False
is_stopped = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(config.TRANSCRIPTS_DIR, exist_ok=True)
    os.makedirs("call_results", exist_ok=True)
    yield


app = FastAPI(title="Outbound AI Calling System", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- WebSocket broadcast ---

async def broadcast(event: dict):
    message = json.dumps(event)
    disconnected = []
    for ws in connected_websockets:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_websockets.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_websockets.append(ws)
    try:
        while True:
            await ws.receive_text()  # Keep connection alive
    except WebSocketDisconnect:
        if ws in connected_websockets:
            connected_websockets.remove(ws)


# --- REST API ---

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    filepath = config.CSV_PATH
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)
    call_mgr.load_csv(filepath)
    rows = call_mgr.get_all_rows()
    await broadcast({"type": "csv_loaded", "count": len(rows), "rows": rows})
    return {"message": f"CSV loaded with {len(rows)} claims", "count": len(rows)}


@app.get("/api/claims")
async def get_claims():
    return call_mgr.get_all_rows()


@app.get("/api/stats")
async def get_stats():
    return call_mgr.get_stats()


@app.post("/api/start")
async def start_calls():
    global call_loop_task, is_paused, is_stopped
    if call_loop_task and not call_loop_task.done():
        if is_paused:
            is_paused = False
            await broadcast({"type": "status", "message": "Resumed"})
            return {"message": "Resumed"}
        return {"message": "Already running"}
    is_paused = False
    is_stopped = False
    call_loop_task = asyncio.create_task(call_processing_loop())
    await broadcast({"type": "status", "message": "Started"})
    return {"message": "Call processing started"}


@app.post("/api/pause")
async def pause_calls():
    global is_paused
    is_paused = True
    await broadcast({"type": "status", "message": "Paused (will pause after current call)"})
    return {"message": "Will pause after current call completes"}


@app.post("/api/stop")
async def stop_calls():
    global is_stopped
    is_stopped = True
    await broadcast({"type": "status", "message": "Stopped"})
    return {"message": "Stopped"}


@app.get("/api/transcript/{claim_number}")
async def get_transcript(claim_number: str):
    filepath = os.path.join(config.TRANSCRIPTS_DIR, f"{claim_number}.txt")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return {"claim_number": claim_number, "transcript": f.read()}
    return JSONResponse(status_code=404, content={"error": "Transcript not found"})


@app.get("/api/download-csv")
async def download_csv():
    if os.path.exists(config.CSV_PATH):
        return FileResponse(
            config.CSV_PATH,
            media_type="text/csv",
            filename="claims_updated.csv",
        )
    return JSONResponse(status_code=404, content={"error": "No CSV loaded"})


# --- Call Processing Loop ---

async def make_sip_call(claim_data: dict, room_name: str) -> bool:
    """Create a LiveKit room and dispatch an outbound SIP call."""
    phone_number = claim_data.get("insurance_phone", "")
    if not phone_number:
        return False

    lk_api = api.LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET,
    )

    try:
        # Create room with claim data as metadata
        await lk_api.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                metadata=json.dumps(claim_data),
                empty_timeout=300,  # 5 min timeout
            )
        )

        # Create outbound SIP participant (this dials the phone number)
        await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=config.LIVEKIT_SIP_TRUNK_ID,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity="insurance-rep",
                participant_name="Insurance Representative",
            )
        )
        return True
    except Exception as e:
        print(f"Error creating SIP call: {e}")
        return False
    finally:
        await lk_api.aclose()


async def wait_for_call_completion(claim_number: str, room_name: str, timeout: int = 600):
    """Wait for the agent to finish the call and write results."""
    results_path = os.path.join("call_results", f"{claim_number}.json")
    elapsed = 0
    poll_interval = 3

    lk_api = api.LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET,
    )

    try:
        while elapsed < timeout:
            # Check if results file was written by agent
            if os.path.exists(results_path):
                with open(results_path, "r") as f:
                    data = json.load(f)
                os.remove(results_path)
                return data

            # Check if room still exists (call still active)
            try:
                rooms = await lk_api.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
                if not rooms.rooms:
                    # Room gone, call ended - wait for agent to write results
                    await asyncio.sleep(10)
                    if os.path.exists(results_path):
                        with open(results_path, "r") as f:
                            data = json.load(f)
                        os.remove(results_path)
                        return data
                    return None
            except Exception:
                pass

            # Broadcast that call is still active
            await broadcast({
                "type": "call_active",
                "claim_number": claim_number,
                "elapsed": elapsed,
            })

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
    finally:
        await lk_api.aclose()

    return None


async def process_single_call(claim_data: dict):
    """Process a single outbound call for a claim."""
    claim_number = str(claim_data.get("claim_number", "unknown"))
    room_name = f"call-{claim_number}"

    await broadcast({
        "type": "call_started",
        "claim_number": claim_number,
        "claim_data": {k: str(v) for k, v in claim_data.items()},
    })

    # Mark as in-progress
    call_mgr.set_call_status(claim_number, "in-progress")

    # Make the SIP call
    success = await make_sip_call(claim_data, room_name)
    if not success:
        call_mgr.set_call_status(claim_number, "failed")
        await broadcast({
            "type": "call_failed",
            "claim_number": claim_number,
            "reason": "Failed to create SIP call",
        })
        return

    # Wait for call completion
    result = await wait_for_call_completion(claim_number, room_name)

    if result:
        # Save transcript
        transcript_text = result.get("transcript", "")
        if transcript_text:
            call_mgr.save_transcript(claim_number, transcript_text, config.TRANSCRIPTS_DIR)

        # Update CSV with results
        call_results = result.get("results", {})
        call_results["call_status"] = "completed"
        call_mgr.update_row(claim_number, call_results)

        await broadcast({
            "type": "call_completed",
            "claim_number": claim_number,
            "results": call_results,
            "stats": call_mgr.get_stats(),
        })
    else:
        call_mgr.set_call_status(claim_number, "no-answer")
        await broadcast({
            "type": "call_no_answer",
            "claim_number": claim_number,
            "stats": call_mgr.get_stats(),
        })

    # Clean up room
    try:
        lk_api = api.LiveKitAPI(
            config.LIVEKIT_URL,
            config.LIVEKIT_API_KEY,
            config.LIVEKIT_API_SECRET,
        )
        await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        await lk_api.aclose()
    except Exception:
        pass


async def call_processing_loop():
    """Main loop that processes calls one by one from the CSV."""
    global is_paused, is_stopped

    await broadcast({"type": "status", "message": "Processing calls..."})

    while not is_stopped:
        if is_paused:
            await asyncio.sleep(1)
            continue

        claim_data = call_mgr.get_next_pending()
        if not claim_data:
            await broadcast({
                "type": "status",
                "message": "All calls completed!",
                "stats": call_mgr.get_stats(),
            })
            break

        await process_single_call(claim_data)

        # Brief pause between calls
        await asyncio.sleep(2)

    await broadcast({
        "type": "status",
        "message": "Call processing finished",
        "stats": call_mgr.get_stats(),
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
