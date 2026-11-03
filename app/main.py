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
from .worker import PlanWorker
from .identities import check_credentials, local_principals, role_source
from .releases import Releases
from .release_routes import install_release_routes
from .approval_routes import install_approval_routes
from .observability import install_observability_routes
from .evaluations import evaluate_release

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
        # 下游应用仍从 ASGI 的 receive 读取同一份、已校验大小的请求体。
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
    admin_token = os.getenv("AGENT_ADMIN_TOKEN")
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
        actors = local_principals(catalog, local_token, principals)
        check_credentials(actors, admin_token, service_token)
        if os.getenv("AGENT_REAL_WRITES", "0") != "0":
            raise ValueError("This release supports only the local mock ledger")
        app.state.snapshots = Snapshots(db_path, catalog)
        store = Store(db_path)
        rt = Runtime(catalog, store, actors, model)
        app.state.runtime = rt
        rt.role_source = role_source(catalog, local_token, principals, admin_token, service_token)
        dispatcher = None
        try:
            app.state.releases = Releases(store, app.state.snapshots, evaluate=evaluate_release)
            rt.bind_releases(app.state.releases, app.state.snapshots)
            if run_here:
                worker = PlanWorker(app.state.runtime, app.state.snapshots)
                dispatcher = asyncio.create_task(worker.run())
            yield
        finally:
            # 等待已取消的 Worker 退出，随后才能关闭它们共用的 SQLite 连接。
            workers = list(app.state.runtime.workers.values())
            if dispatcher:
                workers.append(dispatcher)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            store.close()

    app = FastAPI(title="Universal Agent", version="2.0.0", lifespan=lifespan)
    app.add_middleware(RequestLimits)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.exception_handler(Fault)
    async def known_error(request, error):
        return fail(uuid4().hex, error)

    @app.exception_handler(RequestValidationError)
    async def malformed(request, error):
        if request.url.path.startswith(API + "/approvals/") and request.url.path.endswith("/decision"):
            try:
                actor = identify(request)
            except Fault:
                actor = None
            runtime().store.audit("approval.input", "deny", actor, code="INVALID_REQUEST")
        return fail(uuid4().hex, Fault("INVALID_REQUEST", "请求字段、类型或长度无效"))

    def runtime():
        return app.state.runtime

    def bearer(request):
        kind, _, value = request.headers.get("authorization", "").partition(" ")
        return value if kind.lower() == "bearer" else ""

    def identify(request: Request):
        presented = bearer(request)
        if not presented.strip():
            raise Fault("UNAUTHORIZED", "请使用本地访问令牌", 401)
        for registered, actor in runtime().principals.items():
            if hmac.compare_digest(registered.encode(), presented.encode()):
                return actor
        raise Fault("UNAUTHORIZED", "请使用本地访问令牌", 401)

    def service(request: Request):
        if not service_token or not hmac.compare_digest(service_token.encode(), bearer(request).encode()):
            raise Fault("UNAUTHORIZED", "需要服务身份", 401)

    def admin(request: Request):
        if not admin_token or not hmac.compare_digest(admin_token.encode(), bearer(request).encode()):
            raise Fault("UNAUTHORIZED", "需要独立的管理令牌", 401)
        return {"tenant_id": "personal", "org_id": "local", "user_id": "release-admin"}

    install_approval_routes(app, runtime, identify)
    install_release_routes(app, runtime, admin)
    install_observability_routes(app, runtime, admin)

    def allowed_task(actor, task_id):
        task = runtime().store.task(actor, task_id)
        runtime().authorize_task(actor, task["business_id"], task_id)
        return task

    def page(filename):
        return FileResponse(ROOT / "static" / filename, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        })

    @app.get("/", include_in_schema=False)
    async def home():
        return page("index.html")

    @app.get("/review", include_in_schema=False)
    async def review():
        return page("review.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.post("/local/access", include_in_schema=False)
    async def access(request: Request):
        if (configured_token or principals is not None or os.getenv("AGENT_PRINCIPALS_FILE") or not request.client
                or request.client.host not in ("127.0.0.1", "::1")
                or request.headers.get("X-Local-UI") != "1"
                or request.headers.get("Origin", str(request.base_url).rstrip("/")) != str(request.base_url).rstrip("/")):
            raise Fault("PERMISSION_DENIED", "请手动输入本地访问令牌", 403)
        return JSONResponse({"token": local_token}, headers={"Cache-Control": "no-store"})

    @app.get(API + "/scenarios")
    async def scene_list(actor=Depends(identify)):
        agent_runtime = runtime()
        scenes = []
        unavailable = []
        for scenario_id in agent_runtime.catalog.scenarios:
            grant = agent_runtime.grant(actor, scenario_id)
            if not grant:
                continue
            try:
                selected = agent_runtime.releases.select(scenario_id, actor, "")
                catalog = agent_runtime.release_catalog(selected, scenario_id)
            except Fault as exc:
                if exc.code == "RELEASE_SNAPSHOT_INVALID":
                    unavailable.append(scenario_id)
                    continue
                if exc.code == "CAPABILITY_REVOKED":
                    continue
                raise
            scene = catalog.scenarios[scenario_id]
            objects = grant.get("objects", [])
            summary = {field: scene.get(field) for field in ("id", "name", "description", "version", "example")}
            plan_path = catalog.root / scenario_id / "plan.json"
            summary["durable_plan"] = plan_path.is_file()
            summary["default_object"] = objects[0] if len(objects) == 1 else None
            scenes.append(summary)
        return {"scenarios": scenes, "unavailable_scenarios": unavailable,
                "model_available": model_ready, "model_name": model.get("model", ""),
                "can_review": any("approval.decide" in g["permissions"] for g in actor["grants"].values())}

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
        agent_runtime = runtime()
        request_data = body.model_dump(exclude_none=True)
        is_durable = body.execution_mode == "durable_plan"
        if not is_durable:
            request_data.pop("execution_mode")
            if body.budget is not None:
                raise Fault("INVALID_REQUEST", "budget 只适用于 durable_plan")
        elif body.mode != "offline":
            raise Fault("INVALID_REQUEST", "当前持久计划使用离线场景规划器")
        agent_runtime.authorize(actor, body.business_id, request_data["context"])
        if body.session_id:
            session = agent_runtime.store.session(actor, body.session_id, body.business_id)
            agent_runtime.authorize(actor, body.business_id, session["context"])
            if "context" not in body.model_fields_set:
                request_data["context"] = session["context"]
        if body.mode == "openai" and not model_ready:
            raise Fault("MODEL_NOT_CONFIGURED", "请先在本机配置模型地址和名称", 503)
        now = time.monotonic()
        recent_requests = bursts[(actor["tenant_id"], actor["org_id"], actor["user_id"])]
        while recent_requests and recent_requests[0] < now - 60:
            recent_requests.popleft()
        if len(recent_requests) >= 90 or len(agent_runtime.workers) >= 24:
            agent_runtime.store.audit("chat", "deny", actor, code="RATE_LIMITED")
            raise Fault("RATE_LIMITED", "请求过多，请稍后再试", 429, True)
        recent_requests.append(now)

        # 重放必须返回首次提交的任务，不能因灰度比例变化而重新选版本。
        task = None
        if key:
            task = agent_runtime.store.find_replay(actor, request_data, key)
        created = False
        if task is None:
            scenario_id = body.business_id
            selected_release = agent_runtime.releases.select(scenario_id, actor, "")
            catalog = agent_runtime.release_catalog(selected_release, scenario_id)
            task_options = {}
            if is_durable:
                snapshots = app.state.snapshots
                snapshot_id = selected_release or snapshots.current.get(scenario_id)
                if not snapshot_id:
                    raise Fault("PLAN_NOT_FOUND", "此场景没有已注册的计划模板", 404)
                try:
                    pinned_catalog = snapshots.open(snapshot_id, scenario_id)
                    permissions = agent_runtime.grant(actor, scenario_id)["permissions"]
                    plan, budget = prepare_plan(
                        pinned_catalog, scenario_id, body.message, permissions, request_data["context"], budget=body.budget
                    )
                except Fault as exc:
                    agent_runtime.store.audit("plan.validate", "deny", actor, resource=body.business_id, code=exc.code)
                    raise
                task_options = {"plan": plan, "budget": budget, "snapshot_id": snapshot_id}
            elif selected_release:
                task_options["snapshot_id"] = selected_release

            # 入库时再核对一次发布绑定，防止准备计划期间恰好切换版本。
            def check_release_at_admission():
                agent_runtime.releases.guard_selection(selected_release, scenario_id, actor, "")

            task, created = agent_runtime.store.start(
                actor, request_data, key, catalog.scenarios[scenario_id]["version"],
                admit=check_release_at_admission, **task_options,
            )
        if created and not is_durable:
            agent_runtime.launch(actor, request_data, task)
        if is_durable:
            task = allowed_task(actor, task["task_id"])
        if body.response_mode == "streaming":
            return event_stream(task["task_id"], actor, 0)
        if is_durable:
            location = API + "/tasks/" + task["task_id"]
            return JSONResponse(task, status_code=202, headers={"Location": location})
        worker = agent_runtime.workers.get(task["task_id"])
        if worker:
            # 阻塞请求断开连接时，后台任务仍须继续执行并持久化结果。
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
        agent_runtime = runtime()
        plan = agent_runtime.store.plan_view(task_id)
        scenario_id = task["business_id"]
        permissions = agent_runtime.grant(actor, scenario_id)["permissions"]
        visible = set()
        for step in plan["steps"]:
            current_tool = agent_runtime.releases.current_tool(scenario_id, actor, "", step["capability_id"])
            if current_tool and current_tool["permission"] in permissions:
                visible.add(current_tool["name"])
        if any(step["capability_id"] not in visible for step in plan["steps"]):
            agent_runtime.store.audit("plan.read", "deny", actor, task_id, code="PERMISSION_DENIED")
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
