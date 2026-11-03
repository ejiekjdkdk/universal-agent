from typing import Annotated, Literal

from fastapi import Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "reject"]
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=500)


def install_approval_routes(app, runtime, identify):
    base = "/api/v1/agent/approvals"

    @app.get(base)
    async def approval_queue(status: Literal["requested", "approved", "rejected", "expired", "cancelled"] | None = None,
                             limit: Annotated[int, Query(ge=1, le=100)] = 30,
                             offset: Annotated[int, Query(ge=0, le=10000)] = 0, actor=Depends(identify)):
        return {"approvals": runtime().store.approvals(actor, status, limit, offset), "limit": limit, "offset": offset}

    @app.get(base + "/{approval_id}")
    async def approval_details(approval_id: str, actor=Depends(identify)):
        return runtime().store.approval(actor, approval_id)

    @app.get(base + "/{approval_id}/receipt")
    async def verify_mock_receipt(approval_id: str, actor=Depends(identify)):
        return runtime().store.approval_receipt(actor, approval_id)

    @app.post(base + "/{approval_id}/decision")
    async def approval_decision(approval_id: str, body: Decision,
                                key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
                                actor=Depends(identify)):
        return runtime().store.decide_approval(actor, approval_id, body.decision, body.proposal_digest, key, body.comment)
