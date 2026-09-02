"""Lexical knowledge search tests: deterministic bounded ranking
(Architecture.md #20)."""

from pathlib import Path

import pytest

from sta.knowledge.read import KnowledgeReader
from sta.knowledge.search import (
    MAX_SEARCH_LIMIT,
    SNIPPET_MAX_CHARS,
    search_corpus,
    tokenize,
)


@pytest.fixture
def reader(tmp_path: Path) -> KnowledgeReader:
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "iceberg").mkdir()
    (root / "runbooks" / "file-sizing.md").write_text(
        "# File sizing runbook\n\n"
        "Preferred data-file range: 256-512 MiB.\n\n"
        "## Before recommending compaction\n\n"
        "Confirm with file-layout measurements before compaction.\n",
        encoding="utf-8",
    )
    (root / "iceberg" / "sort-orders.md").write_text(
        "# Sort orders\n\nSort orders are defined per table. Sorting adds write cost.\n",
        encoding="utf-8",
    )
    (root / "INDEX.md").write_text("# Knowledge map\nUse search, then read a bounded range.\n", encoding="utf-8")
    return KnowledgeReader(root)


def test_tokenize_lowercases_and_splits_on_non_alphanumeric() -> None:
    assert tokenize("File-Size 256MiB!") == ["file", "size", "256mib"]
    assert tokenize("a I") == []  # single-character tokens are dropped
    assert tokenize("") == []


def test_search_ranks_matching_documents_first(reader: KnowledgeReader) -> None:
    hits = search_corpus(reader, "file compaction")

    assert hits, "expected at least one hit"
    assert hits[0].path == "runbooks/file-sizing.md"
    assert set(hits[0].matched_terms) == {"file", "compaction"}
    assert hits[0].snippet_line >= 1
    assert all(hit.path != "iceberg/sort-orders.md" or hit.score < hits[0].score for hit in hits)


def test_search_title_match_outranks_body_match(reader: KnowledgeReader) -> None:
    hits = search_corpus(reader, "sort orders")

    assert hits[0].path == "iceberg/sort-orders.md"
    assert hits[0].title == "Sort orders"


def test_search_returns_empty_for_empty_or_unknown_queries(reader: KnowledgeReader) -> None:
    assert search_corpus(reader, "") == []
    assert search_corpus(reader, "   !!! ??? ") == []
    assert search_corpus(reader, "zzzunmatchablezzz") == []


def test_search_is_deterministic(reader: KnowledgeReader) -> None:
    first = search_corpus(reader, "file compaction")
    second = search_corpus(reader, "file compaction")

    assert [(h.path, h.score, h.snippet_line) for h in first] == [
        (h.path, h.score, h.snippet_line) for h in second
    ]


def test_search_orders_ties_by_path(reader: KnowledgeReader) -> None:
    hits = search_corpus(reader, "bounded")

    paths = [hit.path for hit in hits]
    assert paths == sorted(paths)


def test_search_limit_is_bounded_and_validated(reader: KnowledgeReader) -> None:
    assert len(search_corpus(reader, "file", limit=100)) <= MAX_SEARCH_LIMIT
    assert len(search_corpus(reader, "file", limit=1)) == 1

    with pytest.raises(ValueError):
        search_corpus(reader, "file", limit=0)


def test_search_hits_carry_bounded_snippets(reader: KnowledgeReader) -> None:
    long_line = "compaction " * 100
    root = reader.root
    (root / "iceberg" / "long.md").write_text(
        "# Long\n\n" + long_line + "\n", encoding="utf-8"
    )

    hits = search_corpus(reader, "compaction")

    long_hits = [hit for hit in hits if hit.path == "iceberg/long.md"]
    assert long_hits
    assert len(long_hits[0].snippet) <= SNIPPET_MAX_CHARS + 1  # + ellipsis
    assert long_hits[0].snippet.endswith("…")


def test_search_snippet_points_at_heading_when_headings_match(reader: KnowledgeReader) -> None:
    hits = search_corpus(reader, "recommending compaction")

    sizing = next(hit for hit in hits if hit.path == "runbooks/file-sizing.md")
    assert sizing.snippet == "Before recommending compaction"
    assert sizing.snippet_line == 5


def test_search_skips_unreadable_documents(reader: KnowledgeReader, monkeypatch: pytest.MonkeyPatch) -> None:
    (reader.root / "broken.md").write_text("# x\n", encoding="utf-8")
    original_read = reader.read

    def exploding_read(path, start_line=1, end_line=None):
        if path == "broken.md":
            raise OSError("unreadable")
        return original_read(path, start_line=start_line, end_line=end_line)

    monkeypatch.setattr(reader, "read", exploding_read)

    hits = search_corpus(reader, "file size compaction")

    assert all(hit.path != "broken.md" for hit in hits)
    assert hits  # the rest of the corpus is still searchable