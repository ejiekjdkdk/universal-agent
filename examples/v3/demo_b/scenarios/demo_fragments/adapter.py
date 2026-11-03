"""从获准检索到的片段生成确定性的摘录式摘要。"""


def summarize(arguments, retrieve):
    allowed = set(arguments["fragment_ids"])
    hits = [hit for hit in retrieve(arguments["text"], 10) if hit["object_id"] in allowed]
    if not hits:
        return {"data": {"summary": "授权片段中未检索到相关内容。"}, "evidence": []}
    lines = [f"{hit['title']}（{hit['source_id']}）：{hit['snippet']}" for hit in hits]
    return {"data": {"summary": "\n".join(lines)}, "evidence": hits}
