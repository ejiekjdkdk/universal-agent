"""从本地角色文件读取授权，令牌由服务端环境变量提供。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from .errors import Fault


def _validate_principals(catalog, actors):
    """每个令牌和主体必须唯一，否则按身份刷新授权时可能选中另一个角色。"""
    if not isinstance(actors, dict):
        raise ValueError("Invalid principal mapping")
    subjects = set()
    for token, actor in actors.items():
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Missing principal credential")
        if not isinstance(actor, dict) or set(actor) != {"tenant_id", "org_id", "user_id", "grants"}:
            raise ValueError("Invalid principal fields")
        subject = tuple(actor[key] for key in ("tenant_id", "org_id", "user_id"))
        if not all(isinstance(s, str) and s.strip() and len(s) <= 128 for s in subject):
            raise ValueError("Invalid principal identity")
        if subject in subjects:
            raise ValueError("Duplicate principal identity")
        subjects.add(subject)
        grants = actor["grants"]
        if not isinstance(grants, dict):
            raise ValueError("Invalid grants")
        for scene_id, grant in grants.items():
            if scene_id not in catalog.scenarios or not isinstance(grant, dict) or "permissions" not in grant:
                raise ValueError("Invalid scenario grant")
            if set(grant) - {"permissions", "objects", "resource_types"}:
                raise ValueError("Unknown grant fields")
            for values in grant.values():
                if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                    raise ValueError("Invalid permission or resource list")
    return actors


def local_principals(catalog, default_token, supplied=None):
    path = os.getenv("AGENT_PRINCIPALS_FILE")
    if supplied is not None:
        return _validate_principals(catalog, deepcopy(supplied))
    if not path:
        grants = {}
        for scene_id in catalog.scenarios:
            permissions = {"knowledge.read"}
            for tool in catalog.discover(scene_id):
                permissions.add(tool["permission"])
            grants[scene_id] = {"permissions": sorted(permissions)}
        return {default_token: {"tenant_id": "personal", "org_id": "local", "user_id": "owner", "grants": grants}}
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Invalid principal file")
    actors = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"token_env", "tenant_id", "org_id", "user_id", "grants"}:
            raise ValueError("Invalid principal fields")
        variable = entry["token_env"]
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable):
            raise ValueError("Invalid principal credential variable")
        token = os.environ.get(variable)
        if token in actors:
            raise ValueError("Duplicate principal credential")
        actors[token] = {k: v for k, v in entry.items() if k != "token_env"}
    return _validate_principals(catalog, actors)


def check_credentials(actors, admin, service):
    extra = [value for value in (admin, service) if value]
    if len(set(extra)) != len(extra) or any(value in actors for value in extra):
        raise ValueError("Administrative, service and user credentials must be distinct")


def role_source(catalog, default_token, supplied, admin, service):
    """鉴权使用当前角色配置，热更新出现歧义时拒绝整个配置。"""
    if supplied is None and not os.getenv("AGENT_PRINCIPALS_FILE"):
        return None
    def current():
        try:
            actors = local_principals(catalog, default_token, supplied)
            check_credentials(actors, admin, service)
            return actors
        except (ValueError, OSError, TypeError, KeyError, RuntimeError):
            raise Fault("IDENTITY_SOURCE_UNAVAILABLE", "本地授权配置不可用", 503) from None
    return current
