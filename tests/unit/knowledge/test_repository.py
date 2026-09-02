"""Knowledge repository facade tests, including the real curated corpus
(Architecture.md #19-#20)."""

from pathlib import Path

import pytest

from sta.knowledge import (
    DEFAULT_SEARCH_LIMIT,
    KnowledgeAccessError,
    KnowledgeBase,
)

REPO_KNOWLEDGE = Path(__file__).resolve().parents[3] / "knowledge"


def test_knowledge_base_searches_and_reads(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks" / "file-sizing.md").write_text(
        "# File sizing\n\nPreferred range: 256-512 MiB.\n", encoding="utf-8"
    )
    kb = KnowledgeBase(root)

    assert kb.root == root
    assert kb.known_paths() == ["runbooks/file-sizing.md"]
    assert kb.exists("runbooks/file-sizing.md") is True

    hits = kb.search("preferred file size")
    assert [hit.path for hit in hits] == ["runbooks/file-sizing.md"]

    document = kb.read("runbooks/file-sizing.md", start_line=3, end_line=3)
    assert document.lines == ["Preferred range: 256-512 MiB."]

    assert kb.read("runbooks/file-sizing.md").content.startswith("# File sizing")


def test_knowledge_base_default_search_limit_is_five(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    for index in range(8):
        (root / f"note-{index}.md").write_text(f"# Doc {index}\nneedle here\n", encoding="utf-8")
    kb = KnowledgeBase(root)

    assert len(kb.search("needle")) == DEFAULT_SEARCH_LIMIT
    assert len(kb.search("needle", limit=8)) == 8


def test_knowledge_base_requires_existing_root(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeAccessError):
        KnowledgeBase(tmp_path / "missing")


def test_real_curated_corpus_contains_required_sections() -> None:
    if not REPO_KNOWLEDGE.is_dir():  # pragma: no cover - repository layout guard
        pytest.skip("repository knowledge corpus not found")
    kb = KnowledgeBase(REPO_KNOWLEDGE)

    paths = kb.known_paths()
    for required in (
        "INDEX.md",
        "runbooks/file-sizing.md",
        "runbooks/partitioning.md",
        "runbooks/sort-orders.md",
        "runbooks/maintenance.md",
        "iomete/platform.md",
        "iomete/maintenance.md",
        "iomete/spark-writes.md",
        "iceberg/files.md",
        "iceberg/partitions.md",
    ):
        assert required in paths, f"missing curated document: {required}"


def test_real_corpus_file_sizing_runbook_search_and_read() -> None:
    if not REPO_KNOWLEDGE.is_dir():  # pragma: no cover
        pytest.skip("repository knowledge corpus not found")
    kb = KnowledgeBase(REPO_KNOWLEDGE)

    hits = kb.search("preferred data-file size compaction")
    assert hits, "file-sizing runbook should match file-size guidance"
    assert hits[0].path == "runbooks/file-sizing.md"

    document = kb.read("runbooks/file-sizing.md")
    assert "256" in document.content  # the team's preferred lower bound
    assert "# File sizing runbook" in document.content
    assert kb.read("runbooks/file-sizing.md", start_line=11, end_line=11).lines[0].startswith("## ")


def test_real_corpus_search_rejects_nothing_benign() -> None:
    if not REPO_KNOWLEDGE.is_dir():  # pragma: no cover
        pytest.skip("repository knowledge corpus not found")
    kb = KnowledgeBase(REPO_KNOWLEDGE)

    # Unmatched queries return no hits instead of failing.
    assert kb.search("qqqq wwww") == []
    # Every known path is readable end to end.
    for path in kb.known_paths():
        document = kb.read(path)
        assert document.total_lines >= 1