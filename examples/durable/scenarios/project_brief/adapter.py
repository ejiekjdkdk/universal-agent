"""Read-only, self-contained project brief example."""


def _record(title, retrieve):
    evidence = retrieve(title, 1)
    summary = evidence[0]["snippet"] if evidence else "未找到对应记录。"
    return {"data": {"summary": summary, "count": len(evidence)}, "evidence": evidence}


def progress(arguments, retrieve):
    return _record("项目进度", retrieve)


def risks(arguments, retrieve):
    return _record("项目风险", retrieve)


def compose(arguments, retrieve):
    summary = "项目进度\n" + arguments["progress"] + "\n\n待跟进的风险\n" + arguments["risks"]
    return {"data": {"summary": summary}, "evidence": []}
