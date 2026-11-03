"""Standalone project backend. Its only agent dependency is the public HTTP API."""
import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Project records")
PROJECT_TOKEN = os.environ["PROJECT_API_TOKEN"]
AGENT_TOKEN = os.environ["AGENT_ACCESS_TOKEN"]
if not PROJECT_TOKEN or not AGENT_TOKEN or PROJECT_TOKEN == AGENT_TOKEN:
    raise RuntimeError("Configure two distinct, nonempty project and agent tokens.")
AGENT_API = f"http://127.0.0.1:{int(os.environ.get('AGENT_PORT', '8765'))}/api/v1/agent"


def authenticate(authorization: Annotated[str, Header()] = ""):
    if not hmac.compare_digest(authorization.encode(), f"Bearer {PROJECT_TOKEN}".encode()):
        raise HTTPException(401, "Project token required")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)
    mode: Literal["offline", "openai"] = "offline"


@app.get("/v1/records/{record_id}", dependencies=[Depends(authenticate)])
def read_record(record_id: str):
    records = json.loads((ROOT / "records.json").read_text(encoding="utf-8"))
    if record_id not in records:
        raise HTTPException(404, "Unknown record")
    return {"id": record_id, **records[record_id]}


@app.post("/ask", dependencies=[Depends(authenticate)])
async def ask(question: Question,
              key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)] = None):
    """Submit a question and fetch its persisted trace using the same agent identity."""
    headers = {"Authorization": "Bearer " + AGENT_TOKEN}
    payload = {"business_id": "project_records", **question.model_dump(), "response_mode": "blocking"}
    try:
        async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=130) as client:
            response = await client.post(AGENT_API + "/chat", json=payload,
                                         headers={"Idempotency-Key": key or uuid4().hex})
            response.raise_for_status()
            task = response.json()
            trace = await client.get(AGENT_API + "/tasks/" + task["task_id"] + "/trace")
            trace.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, "Agent API rejected the request") from None
    except httpx.RequestError:
        raise HTTPException(502, "Agent API unavailable") from None
    return {"task_id": task["task_id"], "status": task["status"], "answer": task["answer"],
            "error": task["error"], "evidence": task["evidence"], "tool_calls": task["tool_calls"],
            "trace": trace.json()["events"]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PROJECT_API_PORT", "8877")),
                proxy_headers=False)
