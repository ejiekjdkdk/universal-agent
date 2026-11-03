"""按 Unicode 码点计数字符，并统计非空行和高频词项。"""
import re
from collections import Counter


def statistics(arguments, retrieve):
    text = arguments["text"]
    tokens = re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text.casefold())
    counts = Counter(tokens).most_common(5)
    keywords = [{"term": term, "count": count} for term, count in counts]
    nonempty = sum(bool(line.strip()) for line in text.splitlines())
    summary = f"共 {len(text)} 个字符（含空白），{nonempty} 个非空行。"
    if keywords:
        labels = [(term if len(term) <= 40 else term[:40] + "…", count) for term, count in counts]
        summary += " 高频词项：" + "、".join(f"{term}（{count} 次）" for term, count in labels) + "。"
    data = {"summary": summary, "characters": len(text), "lines": nonempty, "keywords": keywords}
    return {"data": data, "evidence": []}
