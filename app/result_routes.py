"""结果句柄使用任务所有权与实时能力授权，不能复用已撤销的运行上下文。"""
import hashlib
import json

from fastapi import Depends
from fastapi.responses import JSONResponse

from .errors import Fault
from .mcp_gateway import MCPContext, _json as mcp_json


API = "/api/v1/agent"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def authorize_result(runtime, actor, metadata, provenance):
    task_id = metadata["task_id"]
    task = runtime.store.task(actor, task_id)
    business = task["business_id"]
    runtime.authorize_task(actor, business, task_id)
    snapshot = provenance["snapshot_id"]
    if runtime.store.task_snapshot(task_id) != snapshot:
        raise Fault("RESULT_INTEGRITY_ERROR", "结果发布版本不属于当前任务。", 502)
    name = provenance["capability_id"]
    runtime.releases.guard(snapshot, business, name)
    catalog = runtime.release_catalog(snapshot, business)
    tool = next((tool for tool in catalog.discover(business) if tool["name"] == name), None)
    current = runtime.releases.current_tool(business, actor, "", name)
    grant = runtime.grant(actor, business)
    if not tool or not current or current["action_level"] != provenance["action_level"]:
        raise Fault("CAPABILITY_REVOKED", "结果来源能力已不可用。", 403)
    version = tool.get("version", catalog.scenarios[business]["version"])
    if version != provenance["capability_version"] or tool["permission"] != provenance["permission"]:
        raise Fault("CAPABILITY_REVOKED", "结果来源的固定能力声明已改变。", 403)
    if tool["permission"] not in grant["permissions"] or current["permission"] not in grant["permissions"]:
        raise Fault("PERMISSION_DENIED", "读取结果的工具权限已撤销。", 403)
    scope = runtime.store.scope(task_id)
    for definition in (tool, current):
        field = definition.get("object_ids_field")
        if field:
            objects = provenance["arguments"].get(field)
            if not isinstance(objects, list) or not objects or not set(objects) <= set(scope.get("object_ids", [])):
                raise Fault("PERMISSION_DENIED", "结果来源对象不再符合当前能力范围。", 403)
    pin = provenance.get("skill")
    if runtime.store.skill_pin(task_id) != pin:
        raise Fault("RESULT_INTEGRITY_ERROR", "结果技能绑定不属于当前任务。", 502)
    if pin:
        context = {**actor, "business_id": business, "task_id": task_id}
        skill = runtime.load_skill(context, pin)
        tools = runtime.visible_tools(actor, business, catalog, snapshot)
        available = runtime.skill_capabilities(actor, business, tools, snapshot)
        if name not in skill["tools"] or not set(skill["tools"]) <= available:
            raise Fault("SKILL_UNAVAILABLE", "结果关联的技能依赖已失效。", 403)
    binding = provenance.get("mcp")
    if binding:
        gateway = catalog._mcp_gateway
        if gateway is None:
            raise Fault("CAPABILITY_REVOKED", "结果来源MCP配置已停用。", 403)
        remote_context = MCPContext(tenant_id=actor["tenant_id"], principal_id=json.dumps([actor["org_id"], actor["user_id"]]),
                             run_context_id="result-read", trace_id=task_id, authorize=lambda: None)
        current_binding = gateway._resolve(binding["server_id"], binding["tool_id"], remote_context)
        digest = hashlib.sha256(mcp_json(current_binding).encode()).hexdigest()
        if digest != binding["config_digest"]:
            raise Fault("CAPABILITY_REVOKED", "结果来源MCP映射已改变。", 403)


def install_result_routes(app, runtime, identify):
    @app.get(API + "/tasks/{task_id}/results")
    async def task_results(task_id: str, actor=Depends(identify)):
        rt = runtime()
        task = rt.store.task(actor, task_id)
        rt.authorize_task(actor, task["business_id"], task_id)
        with rt.store._lock:
            rows = rt.store._db.execute("SELECT result_ref FROM tool_results WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        items = []
        for row in rows:
            try:
                items.append(rt.result_store.metadata(row[0], actor, lambda meta, source: authorize_result(rt, actor, meta, source)))
            except Fault as error:
                if error.code not in {"RESULT_NOT_FOUND", "RESULT_EXPIRED"}:
                    raise
        return JSONResponse({"results": items}, headers=HEADERS)

    @app.get(API + "/results/{result_ref}")
    async def metadata(result_ref: str, actor=Depends(identify)):
        rt = runtime()
        result = rt.result_store.metadata(result_ref, actor, lambda meta, source: authorize_result(rt, actor, meta, source))
        return JSONResponse(result, headers=HEADERS)

    @app.get(API + "/results/{result_ref}/pages")
    async def page(result_ref: str, cursor: str | None = None, actor=Depends(identify)):
        rt = runtime()
        result = rt.result_store.page(result_ref, cursor, actor, lambda meta, source: authorize_result(rt, actor, meta, source))
        return JSONResponse(result, headers=HEADERS)

    @app.delete(API + "/results/{result_ref}")
    async def revoke(result_ref: str, actor=Depends(identify)):
        rt = runtime()
        result = rt.result_store.revoke(result_ref, actor, lambda meta, source: authorize_result(rt, actor, meta, source))
        return JSONResponse(result, headers=HEADERS)
