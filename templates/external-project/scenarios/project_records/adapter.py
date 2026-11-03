"""通过项目自己的服务读取记录，由该服务负责业务数据。"""
import os
import re

import httpx


async def lookup(arguments, retrieve):
    codes = set(re.findall(r"(?<![A-Za-z0-9_-])DOC-[0-9]{3,6}(?![A-Za-z0-9_-])",
                           arguments["text"].upper()))
    if len(codes) != 1:
        return {"data": {"summary": "请提供一个记录编号，例如 DOC-101。"}, "evidence": []}
    code = codes.pop()
    port = int(os.environ.get("PROJECT_API_PORT", "8877"))
    url = f"http://127.0.0.1:{port}/v1/records/{code}"
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        response = await client.get(url, headers={"Authorization": "Bearer " + os.environ["PROJECT_API_TOKEN"]})
    if response.status_code == 404:
        return {"data": {"summary": f"项目中没有找到 {code}。"}, "evidence": []}
    response.raise_for_status()
    record = response.json()
    if record["id"] != code:
        raise ValueError("Project returned a different record")
    summary = f"{code}《{record['title']}》当前状态为{record['status']}。"
    return {"data": {"summary": summary}, "evidence": [{
        "source_id": code, "title": record["title"],
        "snippet": record["status"], "version": record["version"],
    }]}
