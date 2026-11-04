"""在原Store事务中加密保存完整工具结果，并提供受控、有时限的分页读取。"""
import base64
import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import Fault


SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_results (
    result_ref TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
    execution_id TEXT NOT NULL, expires_at REAL NOT NULL, byte_count INTEGER NOT NULL,
    metadata TEXT NOT NULL, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
    UNIQUE(task_id,execution_id)
);
CREATE INDEX IF NOT EXISTS tool_results_expiry ON tool_results(expires_at);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_p2_policy(settings):
    """只接受部署者提供的配置，用户请求和技能不能打开结果保留或共享缓存。"""
    policy = settings.get("p2")
    if policy is None:
        path = os.getenv("AGENT_P2_POLICY")
        if not path:
            return {}
        with Path(path).open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("P2配置超过大小上限")
        from .catalog import _reject_duplicate_keys
        policy = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(policy, dict) or set(policy) - {"results", "read_cache"}:
        raise ValueError("P2配置格式无效或存在未知字段")
    return policy


def _bounded(value):
    """先限制结构，再序列化；过深结构和异常数值不能绕过字节上限。"""
    remaining = [(value, 0)]
    nodes = 0
    while remaining:
        current, depth = remaining.pop()
        nodes += 1
        if depth > 32 or nodes > 65536:
            raise Fault("TOOL_OUTPUT_INVALID", "结果结构超过深度或节点上限。", 502)
        if isinstance(current, dict):
            if len(current) > 10000:
                raise Fault("TOOL_OUTPUT_INVALID", "结果对象字段超过上限。", 502)
            for key, child in current.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 4096:
                    raise Fault("TOOL_OUTPUT_INVALID", "结果对象字段无效。", 502)
                remaining.append((child, depth + 1))
        elif isinstance(current, list):
            if len(current) > 10000:
                raise Fault("TOOL_OUTPUT_INVALID", "结果数组超过上限。", 502)
            for child in current:
                remaining.append((child, depth + 1))
        elif isinstance(current, str) and len(current.encode("utf-8")) > 262144:
            raise Fault("TOOL_OUTPUT_INVALID", "结果文本超过上限。", 502)
    try:
        return _json(value).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise Fault("TOOL_OUTPUT_INVALID", "结果不是有效的有界JSON。", 502) from None


class ResultStore:
    """仅管理结果，不管理任务状态；外部读取必须传入可信实时授权回调。"""

    def __init__(self, store, settings):
        allowed = {"keys", "active_key", "ttl_seconds", "max_bytes", "page_bytes", "quota_bytes"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("结果存储配置含未知字段")
        self.store = store
        self.keys = {}
        bindings = settings.get("keys", {})
        if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 8:
            raise ValueError("结果密钥清单无效")
        for key_id, variable in bindings.items():
            if (not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id)
                    or not isinstance(variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", variable)):
                raise ValueError("结果密钥必须通过环境变量引用")
            encoded = os.getenv(variable, "")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", encoded):
                raise ValueError("结果密钥缺失或不是256位十六进制值")
            self.keys[key_id] = bytes.fromhex(encoded)
        active_key = settings.get("active_key")
        if not isinstance(active_key, str) or active_key not in self.keys:
            raise ValueError("当前结果密钥未配置")
        self.active_key: str = active_key
        limits = {}
        for name, default, low, high in (
            ("ttl_seconds", 86400, 60, 604800), ("max_bytes", 1048576, 1024, 4194304),
            ("page_bytes", 32768, 256, 65536), ("quota_bytes", 67108864, 1024, 1073741824),
        ):
            value = settings.get(name, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("结果存储限额无效")
            limits[name] = value
        self.ttl_seconds = limits["ttl_seconds"]
        self.max_bytes = limits["max_bytes"]
        self.page_bytes = limits["page_bytes"]
        self.quota_bytes = limits["quota_bytes"]
        # 不使用executescript，避免它提前提交调用者已有的事务。
        with store._transaction():
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    store._db.execute(statement)
            finished_tasks = store._db.execute(
                "SELECT DISTINCT t.task_id,t.finished_at FROM tasks t JOIN tool_results r USING(task_id) "
                "WHERE t.finished_at IS NOT NULL"
            ).fetchall()
            for task in finished_tasks:
                completed_at = datetime.fromisoformat(task["finished_at"]).timestamp()
                self.finish_task(task["task_id"], completed_at)

    async def maintain(self):
        """空闲期间也清除到期密文；读取到期时间的拒绝不依赖此定时器。"""
        while True:
            self.purge()
            await asyncio.sleep(60)

    def put(self, task_id, result, provenance):
        raw = _bounded(result)
        payload = _bounded({"result": result, "provenance": provenance})
        if len(payload) > self.max_bytes:
            raise Fault("RESULT_TOO_LARGE", "完整结果超过批准存储上限。", 502)
        execution_id = result.get("execution_id")
        if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 128:
            raise Fault("TOOL_OUTPUT_INVALID", "结果缺少执行标识。", 502)
        digest = hashlib.sha256(raw).hexdigest()
        with self.store._transaction():
            task = self.store._task_row(task_id)
            existing = self.store._db.execute(
                "SELECT result_ref FROM tool_results WHERE task_id=? AND execution_id=?", (task_id, execution_id)
            ).fetchone()
            if existing:
                saved = self.load(existing[0])
                if saved["metadata"]["digest"] != digest or saved["provenance"] != provenance:
                    raise Fault("RESULT_CONFLICT", "已保存的执行结果不能被替换。", 409)
                return existing[0]
            self.purge()
            used_bytes = self.store._db.execute(
                "SELECT COALESCE(SUM(length(ciphertext)+length(nonce)+length(CAST(metadata AS BLOB))),0) "
                "FROM tool_results"
            ).fetchone()[0]
            now = time.time()
            ref = "res_" + secrets.token_hex(24)
            metadata = {
                "result_ref": ref,
                "task_id": task_id,
                "execution_id": execution_id,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "byte_count": len(payload),
                "result_bytes": len(raw),
                "digest": digest,
                "key_id": self.active_key,
                "owner": {
                    "tenant_id": task["tenant_id"],
                    "org_id": task["org_id"],
                    "user_id": task["user_id"],
                    "business_id": task["business_id"],
                },
            }
            metadata_json = _json(metadata)
            metadata_bytes = metadata_json.encode("utf-8")
            # 配额包含12字节随机数与16字节认证标签，不只计算明文载荷。
            stored_bytes = len(payload) + 12 + 16 + len(metadata_bytes)
            if used_bytes + stored_bytes > self.quota_bytes:
                raise Fault("RESULT_QUOTA_EXCEEDED", "结果存储已达容量上限。", 503)
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(self.keys[self.active_key]).encrypt(nonce, payload, metadata_bytes)
            self.store._db.execute(
                "INSERT INTO tool_results VALUES(?,?,?,?,?,?,?,?)",
                (ref, task_id, execution_id, metadata["expires_at"], len(payload), metadata_json, nonce, ciphertext),
            )
            self.store._audit("result.store", "allow", dict(task), task_id, ref, None)
            return ref

    def validate_output(self, output):
        """在能力Schema处理前检查原输出结构，避免先遍历无限规模数据。"""
        if len(_bounded(output)) > self.max_bytes:
            raise Fault("RESULT_TOO_LARGE", "完整结果超过批准存储上限。", 502)

    def _row(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"res_[a-f0-9]{48}", ref):
            raise Fault("RESULT_NOT_FOUND", "结果不可用。", 404)
        row = self.store._db.execute("SELECT * FROM tool_results WHERE result_ref=?", (ref,)).fetchone()
        if row is None:
            raise Fault("RESULT_NOT_FOUND", "结果不可用。", 404)
        return row

    def _decode(self, row):
        try:
            metadata = json.loads(row["metadata"])
            for key in ("result_ref", "task_id", "execution_id", "expires_at", "byte_count"):
                if metadata[key] != row[key]:
                    raise ValueError("结果行元数据不一致")
            key_id = metadata["key_id"]
            if key_id not in self.keys:
                raise Fault("RESULT_KEY_UNAVAILABLE", "读取此结果所需的密钥不可用。", 503)
            raw = AESGCM(self.keys[key_id]).decrypt(row["nonce"], row["ciphertext"], row["metadata"].encode())
            payload = json.loads(raw)
            if len(raw) != row["byte_count"]:
                raise ValueError("结果摘要不一致")
            result_digest = hashlib.sha256(_json(payload["result"]).encode()).hexdigest()
            if result_digest != metadata["digest"]:
                raise ValueError("结果摘要不一致")
        except (InvalidTag, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise Fault("RESULT_INTEGRITY_ERROR", "结果完整性校验失败。", 502) from None
        if metadata["expires_at"] <= time.time():
            raise Fault("RESULT_EXPIRED", "结果已过期。", 410)
        return {"metadata": metadata, **payload}

    def load(self, ref):
        """仅供已通过原Gateway/租约检查的内部恢复路径调用，不是公共API。"""
        with self.store._lock:
            return self._decode(self._row(ref))

    def _authorized(self, ref, actor, authorize):
        try:
            with self.store._lock:
                row = self._row(ref)
                try:
                    owner = json.loads(row["metadata"])["owner"]
                    same_owner = all(actor.get(key) == owner[key] for key in ("tenant_id", "org_id", "user_id"))
                except (ValueError, KeyError, TypeError):
                    same_owner = False
                if not same_owner:
                    raise Fault("RESULT_NOT_FOUND", "结果不可用。", 404)
                record = self._decode(row)
                authorize(record["metadata"], record["provenance"])
                return record
        except Fault as error:
            self.store.audit("result.read", "deny", actor, resource=ref, code=error.code)
            raise

    def metadata(self, ref, actor, authorize):
        record = self._authorized(ref, actor, authorize)
        metadata = record["metadata"]
        result = record["result"]
        summary = result.get("data", {}).get("summary", "")
        if not isinstance(summary, str):
            summary = ""
        self.store.audit("result.metadata", "allow", actor, metadata["task_id"], ref)
        return {
            "result_ref": metadata["result_ref"],
            "task_id": metadata["task_id"],
            "execution_id": metadata["execution_id"],
            "created_at": metadata["created_at"],
            "expires_at": metadata["expires_at"],
            "digest": metadata["digest"],
            "result_bytes": metadata["result_bytes"],
            "summary": summary[:2048],
            "summary_truncated": len(summary) > 2048,
            "evidence_count": len(result.get("evidence", [])),
            "encoding": "utf-8-json",
        }

    def page(self, ref, cursor, actor, authorize):
        record = self._authorized(ref, actor, authorize)
        meta = record["metadata"]
        raw = _json(record["result"]).encode("utf-8")
        cursor_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-result-cursor-v1"
        ).derive(self.keys[meta["key_id"]])
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError("游标无效")
                encoded, signature = cursor.split(".")
                if not re.fullmatch(r"[a-f0-9]{64}", signature):
                    raise ValueError("游标签名无效")
                expected_signature = hmac.new(cursor_key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected_signature):
                    raise ValueError("游标签名不符")
                decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != encoded:
                    raise ValueError("游标编码无效")
                value = json.loads(decoded)
                offset = value["offset"]
                if set(value) != {"ref", "digest", "offset"}:
                    raise ValueError("游标范围无效")
                if value["ref"] != ref or value["digest"] != meta["digest"]:
                    raise ValueError("游标范围无效")
                if type(offset) is not int or not 0 < offset < len(raw):
                    raise ValueError("游标范围无效")
            except (ValueError, TypeError, KeyError, UnicodeError):
                self.store.audit("result.page", "deny", actor, meta["task_id"], ref, "RESULT_CURSOR_INVALID")
                raise Fault("RESULT_CURSOR_INVALID", "分页游标无效。", 400) from None
        # 只在页尾退回不完整UTF-8字符，下一页从精确字节位置继续。
        content = raw[offset:offset + self.page_bytes].decode("utf-8", errors="ignore")
        end = offset + len(content.encode("utf-8"))
        next_cursor = None
        if end < len(raw):
            value = _json({"ref": ref, "digest": meta["digest"], "offset": end}).encode()
            encoded = base64.urlsafe_b64encode(value).decode().rstrip("=")
            signature = hmac.new(cursor_key, encoded.encode(), hashlib.sha256).hexdigest()
            next_cursor = encoded + "." + signature
        self.store.audit("result.page", "allow", actor, meta["task_id"], ref)
        return {
            "result_ref": ref,
            "digest": meta["digest"],
            "offset": offset,
            "content": content,
            "encoding": "utf-8-json",
            "next_cursor": next_cursor,
            "expires_at": meta["expires_at"],
        }

    def revoke(self, ref, actor, authorize):
        with self.store._transaction():
            record = self._authorized(ref, actor, authorize)
            self.store._db.execute("DELETE FROM tool_results WHERE result_ref=?", (ref,))
            self.store._audit("result.revoke", "allow", actor, record["metadata"]["task_id"], ref, None)
        return {"result_ref": ref, "deleted": True}

    def purge(self):
        with self.store._transaction():
            return self.store._db.execute("DELETE FROM tool_results WHERE expires_at<=?", (time.time(),)).rowcount

    def finish_task(self, task_id, completed_at=None):
        """终态保留期从完成时起算；重新认证元数据，不复活已过期或已删除的结果。"""
        with self.store._transaction():
            now = time.time()
            rows = self.store._db.execute(
                "SELECT * FROM tool_results WHERE task_id=? AND expires_at>?", (task_id, now)
            ).fetchall()
            for row in rows:
                try:
                    if "finished_at" in json.loads(row["metadata"]):
                        continue
                except (ValueError, TypeError):
                    raise Fault("RESULT_INTEGRITY_ERROR", "结果元数据无效。", 502) from None
                record = self._decode(row)
                metadata = record["metadata"]
                finished_at = completed_at
                if finished_at is None:
                    finished_at = now
                metadata["finished_at"] = finished_at
                metadata["expires_at"] = finished_at + self.ttl_seconds
                encoded = _json(metadata)
                payload = _json({"result": record["result"], "provenance": record["provenance"]}).encode()
                nonce = secrets.token_bytes(12)
                ciphertext = AESGCM(self.keys[metadata["key_id"]]).encrypt(nonce, payload, encoded.encode())
                self.store._db.execute(
                    "UPDATE tool_results SET expires_at=?,metadata=?,nonce=?,ciphertext=? WHERE result_ref=?",
                    (metadata["expires_at"], encoded, nonce, ciphertext, row["result_ref"]),
                )
