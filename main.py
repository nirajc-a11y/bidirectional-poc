import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from livekit import api, rtc
from livekit.agents import AgentServer

import config
from agent_worker import entrypoint
from call_manager import CallManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("outbound-caller")

call_mgr = CallManager(config.CSV_PATH)
connected_websockets: list[WebSocket] = []
call_loop_task: asyncio.Task | None = None
is_paused = False
is_stopped = False
start_time = time.time()

# Session secret (regenerated on restart — fine for a demo)
SESSION_SECRET = secrets.token_hex(32)


def make_session_token() -> str:
    return hmac.new(
        SESSION_SECRET.encode(), config.DASHBOARD_PASSWORD.encode(), hashlib.sha256
    ).hexdigest()


def verify_session(request: Request) -> bool:
    if not config.DASHBOARD_PASSWORD:
        return True
    token = request.cookies.get("session")
    if not token:
        return False
    expected = make_session_token()
    return hmac.compare_digest(token, expected)


# --- Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    global call_loop_task
    os.makedirs(config.TRANSCRIPTS_DIR, exist_ok=True)
    os.makedirs("call_results", exist_ok=True)

    # Start LiveKit agent worker in-process
    agent_server = AgentServer(
        ws_url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
        port=0,
        num_idle_processes=0,
    )
    agent_server.rtc_session(entrypoint)
    is_dev = os.getenv("RAILWAY_ENVIRONMENT") is None
    agent_task = asyncio.create_task(agent_server.run(devmode=is_dev))
    logger.info("LiveKit agent worker started in-process")

    yield

    # Graceful shutdown
    if call_loop_task and not call_loop_task.done():
        call_loop_task.cancel()
    try:
        await agent_server.aclose()
    except Exception:
        pass
    agent_task.cancel()
    logger.info("Shutdown complete")


app = FastAPI(title="Outbound AI Calling System", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Auth Middleware ---

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = {"/login", "/api/health", "/favicon.ico"}
    if path in public_paths or path.startswith("/static/"):
        return await call_next(request)
    if not verify_session(request):
        if path.startswith("/api/") or path.startswith("/ws"):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return RedirectResponse("/login")
    return await call_next(request)


# --- Auth Routes ---

@app.get("/login")
async def login_page():
    if not config.DASHBOARD_PASSWORD:
        return RedirectResponse("/")
    return FileResponse("static/login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if password == config.DASHBOARD_PASSWORD:
        response = RedirectResponse("/", status_code=303)
        is_https = os.getenv("RAILWAY_ENVIRONMENT") is not None
        response.set_cookie("session", make_session_token(), httponly=True, samesite="lax", secure=is_https)
        return response
    return FileResponse("static/login.html", status_code=401)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login")
    response.delete_cookie("session")
    return response


# --- WebSocket ---

async def broadcast(event: dict):
    message = json.dumps(event)
    disconnected = []
    for ws in list(connected_websockets):
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_websockets.remove(ws)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Check auth for WebSocket
    if config.DASHBOARD_PASSWORD:
        token = ws.cookies.get("session")
        expected = make_session_token()
        if not token or not hmac.compare_digest(token, expected):
            await ws.close(code=4001, reason="Unauthorized")
            return
    await ws.accept()
    connected_websockets.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in connected_websockets:
            connected_websockets.remove(ws)


# --- REST API ---

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/api/health")
async def health():
    uptime = int(time.time() - start_time)
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "claims_loaded": len(call_mgr.rows),
    }


REQUIRED_COLUMNS = {"patient_name", "member_id", "insurance_phone", "claim_number"}


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename or not file.filename.endswith(".csv"):
        return JSONResponse(status_code=422, content={"error": "File must be a .csv"})

    filepath = config.CSV_PATH
    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    missing = call_mgr.validate_csv(filepath)
    if missing:
        os.remove(filepath)
        return JSONResponse(
            status_code=422,
            content={"error": f"Missing required columns: {', '.join(missing)}"},
        )

    call_mgr.load_csv(filepath)
    rows = call_mgr.get_all_rows()
    await broadcast({"type": "csv_loaded", "count": len(rows), "rows": rows})
    logger.info(f"CSV uploaded: {len(rows)} claims")
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
    await broadcast({"type": "status", "message": "Paused after current call"})
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
        return FileResponse(config.CSV_PATH, media_type="text/csv", filename="claims_updated.csv")
    return JSONResponse(status_code=404, content={"error": "No CSV loaded"})


# --- Transcript Relay ---

async def relay_transcripts(room_name: str, claim_number: str):
    token = (
        api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity(f"monitor-{room_name}")
        .with_name("Transcript Monitor")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    room = rtc.Room()

    @room.on("data_received")
    def on_data(data: rtc.DataPacket):
        if data.topic == "transcript":
            try:
                payload = json.loads(data.data.decode())
                asyncio.create_task(broadcast({
                    "type": "transcript_line",
                    "claim_number": claim_number,
                    "speaker": payload.get("speaker", ""),
                    "text": payload.get("text", ""),
                }))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    try:
        await room.connect(config.LIVEKIT_URL, token)
        logger.info(f"Transcript monitor connected to room {room_name}")
        while room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    except Exception as e:
        logger.warning(f"Transcript relay error for {room_name}: {e}")
    finally:
        await room.disconnect()


# --- Call Processing ---

async def make_sip_call(claim_data: dict, room_name: str) -> bool:
    phone_number = claim_data.get("insurance_phone", "")
    if not phone_number:
        logger.error(f"No phone number for claim {claim_data.get('claim_number')}")
        return False

    lk_api = api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
    try:
        await lk_api.room.create_room(
            api.CreateRoomRequest(name=room_name, metadata=json.dumps(claim_data), empty_timeout=300)
        )
        await lk_api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=config.LIVEKIT_SIP_TRUNK_ID,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity="insurance-rep",
                participant_name="Insurance Representative",
                krisp_enabled=True,
                wait_until_answered=True,
            )
        )
        logger.info(f"SIP call dispatched to {phone_number} in room {room_name}")
        return True
    except Exception as e:
        logger.error(f"SIP call failed for {room_name}: {e}")
        return False
    finally:
        await lk_api.aclose()


def _read_results_file(results_path: str):
    """Read and remove a results JSON file. Returns None if missing or corrupt."""
    if not os.path.exists(results_path):
        return None
    try:
        with open(results_path, "r") as f:
            data = json.load(f)
        os.remove(results_path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read results file {results_path}: {e}")
        return None


async def wait_for_call_completion(claim_number: str, room_name: str, timeout: int = 600):
    results_path = os.path.join("call_results", f"{claim_number}.json")
    elapsed = 0
    poll_interval = 3

    lk_api = api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
    try:
        while elapsed < timeout:
            data = _read_results_file(results_path)
            if data:
                return data

            try:
                rooms = await lk_api.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
                if not rooms.rooms:
                    await asyncio.sleep(10)
                    data = _read_results_file(results_path)
                    if data:
                        return data
                    return None
            except Exception as e:
                logger.warning(f"Room check failed for {room_name}: {e}")

            await broadcast({"type": "call_active", "claim_number": claim_number, "elapsed": elapsed})
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
    finally:
        await lk_api.aclose()
    return None


async def process_single_call(claim_data: dict):
    claim_number = str(claim_data.get("claim_number", "unknown"))
    room_name = f"call-{claim_number}"

    await broadcast({
        "type": "call_started",
        "claim_number": claim_number,
        "claim_data": {k: str(v) for k, v in claim_data.items()},
    })
    call_mgr.set_call_status(claim_number, "in-progress")

    stale_results = os.path.join("call_results", f"{claim_number}.json")
    if os.path.exists(stale_results):
        os.remove(stale_results)

    success = await make_sip_call(claim_data, room_name)
    if not success:
        call_mgr.set_call_status(claim_number, "failed")
        await broadcast({"type": "call_failed", "claim_number": claim_number, "reason": "SIP call failed"})
        return

    relay_task = asyncio.create_task(relay_transcripts(room_name, claim_number))

    result = await wait_for_call_completion(claim_number, room_name)

    relay_task.cancel()

    if result:
        transcript_text = result.get("transcript", "")
        if transcript_text:
            call_mgr.save_transcript(claim_number, transcript_text, config.TRANSCRIPTS_DIR)

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
        await broadcast({"type": "call_no_answer", "claim_number": claim_number, "stats": call_mgr.get_stats()})

    try:
        lk_api = api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        await lk_api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        await lk_api.aclose()
    except Exception as e:
        logger.warning(f"Room cleanup failed for {room_name}: {e}")


async def call_processing_loop():
    global is_paused, is_stopped
    await broadcast({"type": "status", "message": "Processing calls..."})

    while not is_stopped:
        if is_paused:
            await asyncio.sleep(1)
            continue

        claim_data = call_mgr.get_next_pending()
        if not claim_data:
            await broadcast({"type": "status", "message": "All calls completed!", "stats": call_mgr.get_stats()})
            break

        try:
            await process_single_call(claim_data)
        except Exception as e:
            claim_number = claim_data.get("claim_number", "unknown")
            logger.error(f"Error processing call {claim_number}: {e}", exc_info=True)
            call_mgr.set_call_status(str(claim_number), "failed")
            await broadcast({
                "type": "call_failed",
                "claim_number": str(claim_number),
                "reason": f"Unexpected error: {e}",
            })

        await asyncio.sleep(2)

    await broadcast({"type": "status", "message": "Call processing finished", "stats": call_mgr.get_stats()})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
