import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .catalog import Catalog
from .errors import Fault
from .runtime import Runtime
from .store import Store
from .plans import prepare_plan
from .snapshots import Snapshots
from .worker import PlanWorker, personal_actor

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1/agent"
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
Message = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Context(Strict):
    object_type: Identifier | None = None
    object_ids: list[Identifier] = Field(default_factory=list, max_length=64)


class SessionInput(Strict):
    business_id: Identifier
    context: Context = Field(default_factory=Context)


class ChatInput(SessionInput):
    session_id: Identifier | None = None
    message: Message
    mode: Literal["offline", "openai"] = "offline"
    response_mode: Literal["blocking", "streaming"] = "blocking"
    execution_mode: Literal["reactive", "durable_plan"] = "reactive"
    budget: dict[str, int] | None = None


class DiscoverInput(Strict):
    run_context_id: Identifier
    intent: str = Field(default="", max_length=2000)


class RetrieveInput(Strict):
    run_context_id: Identifier
    query: Message
    top_k: int = Field(default=5, ge=1, le=10)
    knowledge_scope: Identifier | None = None


class ExecuteInput(Strict):
    run_context_id: Identifier
    capability_id: Identifier
    arguments: dict
    call_id: Identifier


def fail(request_id, fault):
    return JSONResponse({"request_id": request_id, "error": fault.as_dict()}, status_code=fault.status)


class RequestLimits:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks = bytearray()
        while True:
            packet = await receive()
            if packet["type"] == "http.disconnect":
                return
            chunks.extend(packet.get("body", b""))
            if len(chunks) > 96000:
                return await fail(uuid4().hex, Fault("INVALID_REQUEST", "请求过大", 400))(scope, receive, send)
            if not packet.get("more_body"):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(*, scenarios=None, database=None, token=None, model_settings=None, principals=None,
               embedded_worker=None):
    model = model_settings if model_settings is not None else {
        "base_url": os.getenv("AGENT_MODEL_BASE_URL", "").strip(),
        "model": os.getenv("AGENT_MODEL_NAME", "").strip(),
        "api_key": os.getenv("AGENT_MODEL_API_KEY", ""),
    }
    configured_token = token or os.getenv("AGENT_ACCESS_TOKEN")
    local_token = configured_token or secrets.token_urlsafe(32)
    service_token = os.getenv("AGENT_SERVICE_TOKEN")
    model_ready = bool(model.get("base_url") and model.get("model"))
    bursts = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        catalog = Catalog(Path(scenarios or os.getenv("AGENT_SCENARIOS") or ROOT / "scenarios"))
        db_path = Path(database or os.getenv("AGENT_DATABASE") or ROOT / "var" / "agent.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        worker_mode = os.getenv("AGENT_PLAN_WORKER", "embedded")
        if worker_mode not in ("embedded", "external"):
            raise ValueError("AGENT_PLAN_WORKER must be embedded or external")
        run_here = worker_mode == "embedded" if embedded_worker is None else embedded_worker
        if principals is not None and not run_here:
            raise ValueError("Use the embedded worker for custom in-process principals.")
        app.state.snapshots = Snapshots(db_path, catalog)
        store = Store(db_path)
        actors = principals if principals is not None else {local_token: personal_actor(catalog)}
        app.state.runtime = Runtime(catalog, store, actors, model)
        dispatcher = None
        try:
            if run_here:
                worker = PlanWorker(app.state.runtime, app.state.snapshots)
                dispatcher = asyncio.create_task(worker.run())
            yield
        finally:
            workers = list(app.state.runtime.workers.values())
            if dispatcher:
                workers.append(dispatcher)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            store.close()

    app = FastAPI(title="Universal Agent", version="0.2.0", lifespan=lifespan)
    app.add_middleware(RequestLimits)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.exception_handler(Fault)
    async def known_error(request, error):
        return fail(uuid4().hex, error)

    @app.exception_handler(RequestValidationError)
    async def malformed(request, error):
        return fail(uuid4().hex, Fault("INVALID_REQUEST", "请求字段、类型或长度无效"))

    def runtime():
        return app.state.runtime

    def bearer(request):
        kind, _, value = request.headers.get("authorization", "").partition(" ")
        return value if kind.lower() == "bearer" else ""

    def identify(request: Request):
        presented = bearer(request)
        for registered, actor in runtime().principals.items():
            if hmac.compare_digest(registered.encode(), presented.encode()):
                return actor
        raise Fault("UNAUTHORIZED", "请使用本地访问令牌", 401)

    def service(request: Request):
        if not service_token or not hmac.compare_digest(service_token.encode(), bearer(request).encode()):
            raise Fault("UNAUTHORIZED", "需要服务身份", 401)

    def allowed_task(actor, task_id):
        task = runtime().store.task(actor, task_id)
        runtime().authorize_task(actor, task["business_id"], task_id)
        return task

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(ROOT / "static" / "index.html", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        })

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.post("/local/access", include_in_schema=False)
    async def access(request: Request):
        if (configured_token or principals is not None or not request.client
                or request.client.host not in ("127.0.0.1", "::1")
                or request.headers.get("X-Local-UI") != "1"
                or request.headers.get("Origin", str(request.base_url).rstrip("/")) != str(request.base_url).rstrip("/")):
            raise Fault("PERMISSION_DENIED", "请手动输入本地访问令牌", 403)
        return JSONResponse({"token": local_token}, headers={"Cache-Control": "no-store"})

    @app.get(API + "/scenarios")
    async def scene_list(actor=Depends(identify)):
        rt = runtime()
        return {"scenarios": [{**{k: scene.get(k) for k in ("id", "name", "description", "version", "example")},
                               "durable_plan": sid in app.state.snapshots.current}
                              for sid, scene in rt.catalog.scenarios.items() if rt.grant(actor, sid)],
                "model_available": model_ready, "model_name": model.get("model", "")}

    @app.post(API + "/sessions", status_code=201)
    async def create_session(body: SessionInput, actor=Depends(identify)):
        context = body.context.model_dump(exclude_none=True)
        runtime().authorize(actor, body.business_id, context)
        return runtime().store.create_session(actor, body.business_id, context)

    @app.get(API + "/sessions/{session_id}")
    async def get_session(session_id: Identifier, actor=Depends(identify)):
        session = runtime().store.session(actor, session_id)
        runtime().authorize(actor, session["business_id"], session["context"])
        return session

    def event_stream(task_id, actor, cursor):
        async def stream():
            nonlocal cursor
            while True:
                task = allowed_task(actor, task_id)
                for entry in runtime().store.events(task_id, cursor):
                    cursor = entry["id"]
                    payload = json.dumps(entry["data"], ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {entry['event']}\ndata: {payload}\n\n"
                if task["status"] in ("completed", "partial", "failed", "cancelled"):
                    return
                await asyncio.sleep(0.08)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post(API + "/chat", responses={202: {"description": "Durable task accepted; Location is its query URL."}})
    async def chat(body: ChatInput, actor=Depends(identify),
                   key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)] = None):
        rt = runtime()
        data = body.model_dump(exclude_none=True)
        durable = body.execution_mode == "durable_plan"
        if not durable:
            data.pop("execution_mode")
            if body.budget is not None:
                raise Fault("INVALID_REQUEST", "budget 只适用于 durable_plan")
        elif body.mode != "offline":
            raise Fault("INVALID_REQUEST", "当前持久计划使用离线场景规划器")
        rt.authorize(actor, body.business_id, data["context"])
        if body.session_id:
            session = rt.store.session(actor, body.session_id, body.business_id)
            rt.authorize(actor, body.business_id, session["context"])
            if "context" not in body.model_fields_set:
                data["context"] = session["context"]
        if body.mode == "openai" and not model_ready:
            raise Fault("MODEL_NOT_CONFIGURED", "请先在本机配置模型地址和名称", 503)
        now = time.monotonic()
        window = bursts[(actor["tenant_id"], actor["org_id"], actor["user_id"])]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= 90 or len(rt.workers) >= 24:
            rt.store.audit("chat", "deny", actor, code="RATE_LIMITED")
            raise Fault("RATE_LIMITED", "请求过多，请稍后再试", 429, True)
        window.append(now)
        options = {}
        task = rt.store.find_replay(actor, data, key) if key and durable else None
        created = False
        if task is None:
            if durable:
                snapshots = app.state.snapshots
                snapshot_id = snapshots.current.get(body.business_id)
                if not snapshot_id:
                    raise Fault("PLAN_NOT_FOUND", "此场景没有已注册的计划模板", 404)
                try:
                    plan, budget = prepare_plan(snapshots.open(snapshot_id, body.business_id),
                                                body.business_id, body.message,
                                                rt.grant(actor, body.business_id)["permissions"],
                                                data["context"], budget=body.budget)
                except Fault as exc:
                    rt.store.audit("plan.validate", "deny", actor, resource=body.business_id, code=exc.code)
                    raise
                options = {"plan": plan, "budget": budget, "snapshot_id": snapshot_id}
            task, created = rt.store.start(actor, data, key, rt.catalog.scenarios[body.business_id]["version"],
                                          **options)
        if created and not durable:
            rt.launch(actor, data, task)
        if durable:
            task = allowed_task(actor, task["task_id"])
        if body.response_mode == "streaming":
            return event_stream(task["task_id"], actor, 0)
        if durable:
            location = API + "/tasks/" + task["task_id"]
            return JSONResponse(task, status_code=202, headers={"Location": location})
        worker = rt.workers.get(task["task_id"])
        if worker:
            await asyncio.gather(asyncio.shield(worker), return_exceptions=True)
        return allowed_task(actor, task["task_id"])

    @app.get(API + "/tasks")
    async def recent(business_id: Identifier | None = None, actor=Depends(identify)):
        if business_id:
            runtime().authorize(actor, business_id)
        visible = []
        for task in runtime().store.recent(actor, business_id):
            try:
                runtime().authorize_task(actor, task["business_id"], task["task_id"])
            except Fault:
                continue
            visible.append(task)
        return {"tasks": visible}

    @app.get(API + "/tasks/{task_id}")
    async def get_task(task_id: Identifier, actor=Depends(identify)):
        return allowed_task(actor, task_id)

    @app.get(API + "/tasks/{task_id}/trace")
    async def trace(task_id: Identifier, actor=Depends(identify)):
        allowed_task(actor, task_id)
        return {"events": runtime().store.events(task_id, 0)}

    @app.get(API + "/tasks/{task_id}/plan")
    async def get_plan(task_id: Identifier, actor=Depends(identify)):
        task = allowed_task(actor, task_id)
        rt = runtime()
        plan = rt.store.plan_view(task_id)
        visible = {t["name"] for t in rt.catalog.discover(task["business_id"])
                   if t["permission"] in rt.grant(actor, task["business_id"])["permissions"]
                   and t["action_level"] == "read"}
        if any(step["capability_id"] not in visible for step in plan["steps"]):
            rt.store.audit("plan.read", "deny", actor, task_id, code="PERMISSION_DENIED")
            raise Fault("PERMISSION_DENIED", "没有查看此计划所需的能力权限", 403)
        return plan

    @app.get(API + "/tasks/{task_id}/steps")
    async def get_steps(task_id: Identifier, actor=Depends(identify)):
        plan = await get_plan(task_id, actor)
        return {"task_id": task_id, "steps": plan["steps"]}

    @app.get(API + "/tasks/{task_id}/events")
    async def replay(task_id: Identifier, actor=Depends(identify),
                     cursor: Annotated[int, Header(alias="Last-Event-ID", ge=0)] = 0):
        allowed_task(actor, task_id)
        return event_stream(task_id, actor, cursor)

    @app.post(API + "/tasks/{task_id}/cancel", status_code=202)
    async def cancel(task_id: Identifier, actor=Depends(identify)):
        allowed_task(actor, task_id)
        result = runtime().store.cancel(actor, task_id)
        worker = runtime().workers.get(task_id)
        if worker and result["status"] == "cancelled":
            worker.cancel()
        return result

    @app.post("/internal/agent/discover", dependencies=[Depends(service)], operation_id="discover_capabilities")
    async def discover(body: DiscoverInput):
        gw = runtime().gateway(body.run_context_id)
        return {"business_id": gw.context()["business_id"], "capabilities": await gw.discover()}

    @app.post("/internal/agent/execute", dependencies=[Depends(service)], operation_id="execute_capability")
    async def execute(body: ExecuteInput):
        return await runtime().gateway(body.run_context_id).execute(body.capability_id, body.arguments, body.call_id)

    @app.post("/internal/agent/retrieve", dependencies=[Depends(service)], operation_id="retrieve_knowledge")
    async def retrieve(body: RetrieveInput):
        gw = runtime().gateway(body.run_context_id)
        if body.knowledge_scope and body.knowledge_scope != gw.context()["business_id"]:
            raise Fault("PERMISSION_DENIED", "知识源超出当前场景", 403)
        return {"chunks": await gw.retrieve(body.query, body.top_k)}

    return app
