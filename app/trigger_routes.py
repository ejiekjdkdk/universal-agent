"""签名内部入口；来源签名凭据与业务用户令牌独立。"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


def build_trigger_router(gateway):
    router = APIRouter()

    @router.post("/internal/agent/triggers/events")
    async def trigger_event(request: Request):
        result = gateway().ingest(await request.body(), request.headers)
        return JSONResponse(result, status_code=202 if result["created"] else 200,
                            headers={"Location": "/api/v1/agent/tasks/" + result["task_id"]})

    return router
