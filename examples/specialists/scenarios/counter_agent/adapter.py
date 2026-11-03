"""专业能力只处理输入，实际委派由受控任务网关完成。"""

def search(arguments, retrieve):
    hits = retrieve(arguments["text"])
    summary = "\n".join(item["snippet"] for item in hits) or "未找到相关资料。"
    return {"data": {"summary": summary}, "evidence": hits}

def count(arguments, retrieve):
    return {"data": {"summary": f"输入共有 {len(arguments['text'])} 个字符。"}, "evidence": []}
