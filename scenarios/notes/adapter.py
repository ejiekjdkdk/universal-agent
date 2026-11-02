"""Local demo adapter. Only the scenario-bound retrieval function can search notes."""


def search(arguments, retrieve):
    chunks = retrieve(arguments["text"], 5)
    if chunks:
        summary = "\n".join(f"《{c['title']}》：{c['snippet']}" for c in chunks)
    else:
        summary = "在本场景的笔记中未找到相关片段。"
    return {"data": {"summary": summary, "matches": len(chunks)}, "evidence": chunks}
