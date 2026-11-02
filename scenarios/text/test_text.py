import asyncio
from pathlib import Path

import pytest

from app.catalog import Catalog
from app.errors import Fault


def test_text_extension_scopes_tools_and_has_deterministic_counts():
    catalog = Catalog(Path(__file__).resolve().parents[1])
    assert catalog.retrieve("text", "会议纪要") == []
    with pytest.raises(Fault):
        asyncio.run(catalog.execute("text", "notes_search", {"text": "会议纪要"}))
    result = asyncio.run(catalog.execute("text", "text_stats", {"text": "red red blue"}))
    assert result["data"]["characters"] == 12
    assert result["data"]["keywords"][0] == {"term": "red", "count": 2}
    assert result["data"]["summary"]
    assert result["evidence"] == []


def test_maximum_length_input_still_returns_valid_output():
    catalog = Catalog(Path(__file__).resolve().parents[1])
    result = asyncio.run(catalog.execute("text", "text_stats", {"text": "ß" * 8192}))
    assert result["data"]["characters"] == 8192
    assert result["data"]["keywords"][0]["count"] == 1
