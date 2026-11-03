"""导出既有公开接口、任务数据库定义及终态事件，供增量开发对照。"""
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import create_app
from app.store import TERMINAL_EVENTS


def snapshot():
    signatures = {}
    database = {}
    for name in ("main", "runtime", "providers", "store", "plan_store", "approvals", "skills", "triggers"):
        tree = ast.parse((ROOT / "app" / (name + ".py")).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                signatures[f"{name}.{node.name}"] = ast.dump(node.args)
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                        signatures[f"{name}.{node.name}.{member.name}"] = ast.dump(member.args)
        definitions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "CREATE TABLE" in node.value or "CREATE INDEX" in node.value:
                    definitions.append(" ".join(node.value.split()))
        database[name] = sorted(definitions)
    return {"openapi": create_app(model_settings={}).openapi(), "public_signatures": signatures,
            "database_definitions": database, "terminal_events": TERMINAL_EVENTS}


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, sort_keys=True, indent=2))
