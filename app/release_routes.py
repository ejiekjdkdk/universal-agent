"""Operator-only management endpoints. Candidate names select locally reviewed bundles."""
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


SceneId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
CandidateId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Candidate(Strict):
    business_id: SceneId
    candidate: CandidateId


class Rollout(Strict):
    canary_percent: int = Field(default=100, ge=0, le=100)


class Scope(Strict):
    business_id: SceneId


def install_release_routes(app, runtime, admin_dependency):
    router = APIRouter(prefix="/api/v1/agent-admin", tags=["release management"])

    @router.post("/releases/validate")
    async def validate(body: Candidate, actor=Depends(admin_dependency)):
        return await app.state.releases.validate(body.business_id, body.candidate, actor=actor)

    @router.post("/releases", status_code=201)
    async def stage(body: Candidate, actor=Depends(admin_dependency)):
        return await app.state.releases.stage(body.business_id, body.candidate, actor=actor)

    @router.get("/releases/{release_id}")
    def details(release_id: Digest, actor=Depends(admin_dependency)):
        releases = app.state.releases
        with releases._admin("inspect", actor, release_id):
            result = releases.get(release_id)
            releases.store.audit("release.inspect", "allow", actor, resource=release_id)
        return result

    @router.post("/releases/{release_id}/activate")
    async def activate(release_id: Digest, body: Rollout, actor=Depends(admin_dependency)):
        return await app.state.releases.activate(release_id, body.canary_percent, actor=actor)

    @router.post("/releases/{release_id}/rollback")
    async def rollback(release_id: Digest, body: Strict, actor=Depends(admin_dependency)):
        return await app.state.releases.rollback(release_id, actor=actor)

    @router.post("/releases/{release_id}/revoke")
    def revoke(release_id: Digest, body: Strict, actor=Depends(admin_dependency)):
        return app.state.releases.revoke(release_id, actor=actor)

    @router.post("/capabilities/{id}/revoke")
    def revoke_tool(id: SceneId, body: Scope, actor=Depends(admin_dependency)):
        return app.state.releases.revoke_capability(body.business_id, id, actor=actor)

    app.include_router(router)
