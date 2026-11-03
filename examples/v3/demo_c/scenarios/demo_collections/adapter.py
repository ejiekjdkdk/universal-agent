"""只汇总当前授权集合；业务规则留在独立项目中。"""
import json
from pathlib import Path
import re

from app.errors import Fault


def count_items(arguments, retrieve):
    allowed = set(arguments["collection_ids"])
    requested = set(re.findall(r"(?<![A-Za-z0-9_-])C-[0-9]{2}(?![A-Za-z0-9_-])", arguments["text"]))
    if not requested <= allowed:
        raise Fault("PERMISSION_DENIED", "请求包含本次范围以外的集合。", 403)
    selected = requested or allowed
    records = json.loads(Path(__file__).with_name("collections.json").read_text(encoding="utf-8"))
    total = 0
    evidence = []
    for identifier in sorted(selected):
        record = records.get(identifier)
        if record is None:
            raise Fault("INVALID_REQUEST", "集合不存在，不能把缺失数据计为零。", 400)
        total += record["count"]
        evidence.append({"source_id": identifier, "object_id": identifier, "title": record["title"],
                         "snippet": f"{record['title']}有{record['count']}件虚构物品。", "version": "1.0.0",
                         "source_ref": "demo://collections/" + identifier, "data_as_of": record["data_as_of"]})
    return {"data": {"summary": f"授权集合共 {total} 件虚构物品。", "total": total}, "evidence": evidence}
