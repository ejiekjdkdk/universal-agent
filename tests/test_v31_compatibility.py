"""以安全修复后的 V3 为准，禁止增量开发悄悄改变已发布契约。"""
import json
from pathlib import Path

from scripts.compatibility_snapshot import snapshot


def test_v31_preserves_v3_public_and_database_contracts():
    expected = json.loads((Path(__file__).parent / "fixtures" / "v31-baseline.json").read_text(encoding="utf-8"))
    assert snapshot() == expected
