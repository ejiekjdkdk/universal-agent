"""从本地角色文件读取授权，令牌由服务端环境变量提供。"""
import json
import os
from pathlib import Path
import re

from .errors import Fault

def local_principals(catalog, default_token, supplied=None):
    path = os.getenv("AGENT_PRINCIPALS_FILE")
    if supplied is not None:
        return supplied
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
    actors, subjects = {}, set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"token_env", "tenant_id", "org_id", "user_id", "grants"}:
            raise ValueError("Invalid principal fields")
        variable = entry["token_env"]
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", variable):
            raise ValueError("Invalid principal credential variable")
        token = os.environ.get(variable)
        subject = tuple(entry[key] for key in ("tenant_id", "org_id", "user_id"))
        if not all(isinstance(s, str) and 0 < len(s) <= 128 for s in subject):
            raise ValueError("Invalid principal identity")
        if not token or token in actors or subject in subjects:
            raise ValueError("Missing or duplicate principal credential/identity")
        grants = entry["grants"]
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
        actors[token] = {k: v for k, v in entry.items() if k != "token_env"}
        subjects.add(subject)
    return actors


def check_credentials(actors, admin, service):
    extra = [value for value in (admin, service) if value]
    if len(set(extra)) != len(extra) or any(value in actors for value in extra):
        raise ValueError("Administrative, service and user credentials must be distinct")


def role_source(catalog, default_token, supplied, admin, service):
    """API 与 Worker 每次鉴权都重读同一份角色文件，使撤权及时生效。"""
    if supplied is not None or not os.getenv("AGENT_PRINCIPALS_FILE"):
        return None
    def current():
        try:
            actors = local_principals(catalog, default_token)
            check_credentials(actors, admin, service)
            return actors
        except (ValueError, OSError, TypeError, KeyError):
            raise Fault("IDENTITY_SOURCE_UNAVAILABLE", "本地授权配置不可用", 503) from None
    return current
