"""Skill 管理面：鉴权主体决定审核者，不信任请求中的状态或签名。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .errors import Fault
from .skills import SkillError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Candidate(Strict):
    business_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    version: str = Field(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")


class Validation(Candidate):
    tools: list[str] = Field(default_factory=list, max_length=128)
    capability_bindings: dict[str, str] = Field(default_factory=dict)


class Rollout(Candidate):
    canary_percent: int = Field(default=100, ge=1, le=100)


def install_skill_routes(app, runtime, admin_dependency):
    router = APIRouter(prefix="/api/v1/agent-admin/skills", tags=["SkillOps"])

    def invoke(operation, body, actor, **kwargs):
        rt = runtime()
        reviewer = actor["user_id"]
        try:
            if operation == "validate":
                if body.business_id not in rt.catalog.scenarios:
                    raise SkillError("受信任目录中无此业务")
                available = [t["name"] for t in rt.catalog.discover(body.business_id) if t["action_level"] == "read"]
                if rt.catalog.scenarios[body.business_id].get("documents"):
                    available.append("retrieve_knowledge")
                kwargs.update(tools=body.tools, available_tools=available, capability_bindings=body.capability_bindings)
            result = getattr(rt.skills, operation)(body.business_id, body.name, body.version, reviewer=reviewer, **kwargs)
            rt.store.audit("skill." + operation, "allow", actor, resource=body.name)
            return result or {"status": "revoked"}
        except SkillError as exc:
            rt.store.audit("skill." + operation, "deny", actor, resource=body.name, code="SKILL_GOVERNANCE_DENIED")
            raise Fault("SKILL_GOVERNANCE_DENIED", str(exc), 409) from None

    @router.post("/validate")
    def validate(body: Validation, actor=Depends(admin_dependency)):
        return invoke("validate", body, actor)

    @router.post("/stage")
    def stage(body: Candidate, actor=Depends(admin_dependency)):
        return invoke("stage", body, actor)

    @router.post("/activate")
    def activate(body: Rollout, actor=Depends(admin_dependency)):
        return invoke("activate", body, actor, canary_percent=body.canary_percent)

    @router.post("/rollback")
    def rollback(body: Candidate, actor=Depends(admin_dependency)):
        return invoke("rollback", body, actor)

    @router.post("/revoke")
    def revoke(body: Candidate, actor=Depends(admin_dependency)):
        return invoke("revoke", body, actor)

    @router.get("/audit")
    def audit(business_id: str | None = None, actor=Depends(admin_dependency)):
        return {"events": runtime().skills.audit(business_id)}

    @router.get("/{business_id}/{name}/{version}")
    def inspect(business_id: str, name: str, version: str, actor=Depends(admin_dependency)):
        try:
            return runtime().skills.governance(business_id, name, version)
        except SkillError as exc:
            raise Fault("SKILL_GOVERNANCE_DENIED", str(exc), 409) from None

    app.include_router(router)
