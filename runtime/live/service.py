import asyncio
import json
import os
import copy
import uuid
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .normalizer import EventNormalizer, load_bridge
from .adapter import current_persona
from .audit import Audit
from .router import LiveInteractionRouter

ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).with_name("config.json")
cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
(ROOT / "logs").mkdir(exist_ok=True)
audit = Audit(ROOT / "logs/live_interaction.log", cfg.get("log_level", "INFO"))
bridge_path = Path(cfg["bridge_path"])
bridge = load_bridge(bridge_path if bridge_path.is_absolute() else ROOT / bridge_path)
router = LiveInteractionRouter(cfg, EventNormalizer(bridge), audit)
source_task = None
stop = asyncio.Event()
connected = False
connection_reason = "OFF"
collector_packets_received = 0
client_seen = {}
ATTEMPTED = Path(__file__).with_name("attempted.json")
attempted = []
if ATTEMPTED.exists():
    try:
        attempted = json.loads(ATTEMPTED.read_text(encoding="utf-8"))[-cfg["dedupe_size"]:]
        for entry in attempted:
            if router.clock() - entry["at"] <= cfg["dedupe_ttl"]:
                router.dedupe.seen[tuple(entry["key"])] = entry["at"]
    except (OSError, ValueError, TypeError, KeyError):
        audit("attempted_state_invalid")


def persist_attempts(rows):
    tmp = ATTEMPTED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    tmp.replace(ATTEMPTED)


def connection(value, reason):
    global connected, connection_reason
    connected, connection_reason = value, reason
    audit("collector_connection", connected=value, reason=reason)


def receive_collector(raw):
    global collector_packets_received
    collector_packets_received += 1
    router.ingest(raw)


async def source_enabled(enabled):
    global source_task, stop
    router.set_enabled(enabled)
    if source_task:
        stop.set()
        await source_task
        source_task = None
    if enabled:
        stop = asyncio.Event()
        source_task = asyncio.create_task(bridge.stream_comments(cfg["ws_url"], receive_collector, connection, stop,
                              cfg["auto_reconnect"], cfg["reconnect_min"], cfg["reconnect_max"]))


@asynccontextmanager
async def lifespan(app):
    if router.enabled:
        await source_enabled(True)
    yield
    await source_enabled(False)
    audit.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:7860", "http://localhost:7860"],
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    from starlette.responses import JSONResponse
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin not in ("http://127.0.0.1:7860", "http://localhost:7860", "http://127.0.0.1:8781"):
            return JSONResponse({"error": "ORIGIN_DENIED"}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"error": "INVALID_CONTENT_LENGTH"}, status_code=400)
        if length > 65536:
            return JSONResponse({"error": "TOO_LARGE"}, status_code=413)
    return await call_next(request)


class Control(BaseModel):
    enabled: bool


class Claim(BaseModel):
    client_id: str
    ready: bool = False
    private_busy: bool = True
    s2s_session_id: str = ""


class Phase(BaseModel):
    client_id: str
    ticket: str
    phase: str
    data: dict = {}


@app.get("/health")
@app.get("/status")
async def status():
    router.expire(router.clock())
    router.session.prune(router.clock())
    return {"ok": True, "pid": os.getpid(), **router.status(), "collector_connected": connected,
            "connection_reason": connection_reason, "collector_packets_received": collector_packets_received,
            "log_dropped": audit.dropped,
            "frontend_poll_ms": cfg["frontend_poll_ms"], "dispatch_timeout": cfg["dispatch_timeout"]}


@app.post("/control")
async def control(value: Control):
    await source_enabled(value.enabled)
    # Persist OFF/ON atomically; no source is connected while OFF.
    # Preserve edits made on disk while the running instance uses its snapshot.
    disk_cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    disk_cfg["enabled"] = value.enabled
    cfg["enabled"] = value.enabled
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(disk_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)
    return await status()


@app.post("/claim")
async def claim(value: Claim):
    global attempted
    if len(value.client_id) > 80:
        raise HTTPException(400, "CLIENT_ID_TOO_LONG")
    client_seen[value.client_id] = router.clock()
    for key, at in list(client_seen.items()):
        if router.clock() - at > 120 or len(client_seen) > 32:
            del client_seen[key]
    if not value.ready or value.private_busy:
        return {"job": None}
    if not router.enabled or (not router.queue and not router.inflight):
        return {"job": None}
    # The installed session.created omits session IDs. Read the authoritative
    # existing pool instead of inventing an ID or editing its protocol.
    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
        try:
            pool = (await client.get("http://127.0.0.1:8765/v1/pool")).json()
            active_ids = [unit["session_id"] for unit in pool["units"] if unit["state"] == "active"]
            if len(active_ids) != 1:
                return {"job": None, "reason": "ONE_ACTIVE_SESSION_REQUIRED"}
            s2s_id = active_ids[0]
            job = router.inflight
            if job and job.get("s2s_session_id") and job["s2s_session_id"] not in [u.get("session_id") for u in pool["units"]]:
                router.phase(job["client_id"], job["ticket"], "failed", {"reason": "owner_session_released"})
        except (httpx.HTTPError, ValueError, KeyError):
            return {"job": None, "reason": "SESSION_OWNERSHIP_UNKNOWN"}
    try:
        persona, ident = await asyncio.to_thread(current_persona, ROOT)
        job = router.claim(value.client_id, persona)
        if job:
            router.inflight["s2s_session_id"] = s2s_id
            event = router.inflight["event"]
            entry = {"key": list(router.dedupe.key(event)), "at": router.clock()}
            attempted = [one for one in attempted if router.clock() - one["at"] <= cfg["dedupe_ttl"]]
            attempted = (attempted + [entry])[-cfg["dedupe_size"]:]
            try:
                await asyncio.to_thread(persist_attempts, list(attempted))
            except OSError:
                router.phase(value.client_id, job["ticket"], "failed", {"reason": "attempt_persist_failed"})
                return {"job": None, "reason": "ATTEMPT_PERSIST_FAILED"}
        return {"job": job, "character_id": ident}
    except (ValueError, OSError) as exc:
        audit("adapter_unready", reason=type(exc).__name__)
        return {"job": None, "reason": type(exc).__name__}


@app.post("/phase")
async def phase(value: Phase):
    allowed = {"llm_start", "llm_first_token", "llm_first_text_block", "llm_done", "tts_start", "audio_generated", "router_accepted", "audio_start", "completed", "failed", "cancelled"}
    if value.phase not in allowed:
        raise HTTPException(400, "UNKNOWN_PHASE")
    return {"ok": router.phase(value.client_id, value.ticket, value.phase, value.data)}


@app.post("/replay")
async def replay(scenario: str = "default"):
    # Fixed sanitized fixture only; cannot read arbitrary filesystem paths.
    if not router.enabled:
        return {"ok": False, "reason": "OFF"}
    names = {"default": "fictional_live_comments.json", "context": "fictional_live_context.json", "memory": "fictional_live_memory.json", "priority": "fictional_live_priority.json", "smoke": "fictional_live_smoke.json"}
    if scenario not in names:
        raise HTTPException(400, "UNKNOWN_SCENARIO")
    path = ROOT / "config" / names[scenario]
    rows = json.loads(path.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:12]
    for row in rows:
        payload = fixture_packet(row['payload'], run_id)
        router.ingest(payload)
        await asyncio.sleep(float(row.get("delay", 0)))
    return {"ok": True, "count": len(rows), "run_id": run_id, "source": "simulation"}


def fixture_packet(original, run_id):
    payload = copy.deepcopy(original)
    inner = bridge.parse_inner(payload.get('Data'))
    if not isinstance(inner, dict):
        raise ValueError('INVALID_FIXTURE_DATA')
    if inner.get('MsgId'):
        inner['MsgId'] = str(inner['MsgId']) + ':' + run_id
    inner['RoomId'] = str(inner.get('RoomId') or 'fixture') + ':test:' + run_id
    payload['Data'] = inner
    payload['_test_fixture'] = run_id
    return payload
