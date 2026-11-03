"""Load a reviewed local role map; credentials remain in server-side environment variables."""
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
        grants = {sid: {"permissions": sorted({"knowledge.read"} | {t["permission"] for t in catalog.discover(sid)})}
                  for sid in catalog.scenarios}
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
            if not all(isinstance(values, list) and all(isinstance(v, str) and v for v in values)
                       for values in grant.values()):
                raise ValueError("Invalid permission or resource list")
        actors[token] = {k: v for k, v in entry.items() if k != "token_env"}
        subjects.add(subject)
    return actors


def check_credentials(actors, admin, service):
    extra = [value for value in (admin, service) if value]
    if len(set(extra)) != len(extra) or any(value in actors for value in extra):
        raise ValueError("Administrative, service and user credentials must be distinct")


def role_source(catalog, default_token, supplied, admin, service):
    """Workers and the API consult the same operator-owned file at authorization time."""
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
