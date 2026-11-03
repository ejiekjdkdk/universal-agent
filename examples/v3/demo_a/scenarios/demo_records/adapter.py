"""虚构登记簿的本地只读适配器。"""
import json
from pathlib import Path
import re

from app.errors import Fault


def lookup(arguments, retrieve):
    codes = set(re.findall(r"(?<![A-Za-z0-9_-])R-[0-9]{4}(?![A-Za-z0-9_-])", arguments["text"]))
    if len(codes) != 1:
        raise Fault("INVALID_REQUEST", "请提供一个登记编号，例如 R-1001。", 400)
    code = codes.pop()
    if code not in arguments["record_ids"]:
        raise Fault("PERMISSION_DENIED", "编号超出本次查询范围。", 403)
    entries = json.loads(Path(__file__).with_name("records.json").read_text(encoding="utf-8"))
    row = entries.get(code)
    if row is None:
        return {"data": {"summary": "登记簿中没有这个编号。"}, "evidence": []}
    facts = f"{row['title']}位于{row['location']}，状态为{row['status']}。"
    return {
        "data": {"summary": f"{code} {facts} 登记时间 {row['data_as_of']}。"},
        "evidence": [{"source_id": code, "object_id": code, "title": row["title"], "snippet": facts,
                      "version": "1.0.0", "source_ref": f"demo://records/{code}", "data_as_of": row["data_as_of"]}],
    }
