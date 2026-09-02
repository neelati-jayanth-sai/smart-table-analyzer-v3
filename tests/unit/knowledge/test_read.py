"""Knowledge reader tests: bounded line-range reads and traversal prevention
(Architecture.md #20)."""

from pathlib import Path

import pytest

from sta.knowledge.read import (
    MAX_FILE_BYTES,
    KnowledgeAccessError,
    KnowledgeNotFoundError,
    KnowledgePathError,
    KnowledgeReader,
    default_knowledge_root,
    document_title,
)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "iceberg").mkdir()
    (root / "runbooks" / "file-sizing.md").write_text(
        "# File sizing runbook\n\n"
        "Preferred data-file range: 256-512 MiB.\n\n"
        "## Before recommending compaction\n\n"
        "Confirm with measurements.\n",
        encoding="utf-8",
    )
    (root / "iceberg" / "files.md").write_text("# Data files\nline two\n", encoding="utf-8")
    (root / "INDEX.md").write_text("# Knowledge map\n", encoding="utf-8")
    (root / "notes.txt").write_text("not curated markdown\n", encoding="utf-8")
    (root / "runbooks" / "long.md").write_text(
        "\n".join(f"line {i}" for i in range(1, 51)) + "\n", encoding="utf-8"
    )
    return root


def test_reader_reads_whole_document(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    document = reader.read("runbooks/file-sizing.md")

    assert document.path == "runbooks/file-sizing.md"
    assert document.title == "File sizing runbook"
    assert document.total_lines == 7
    assert document.start_line == 1
    assert document.end_line == 7
    assert document.truncated is False
    assert document.lines[0] == "# File sizing runbook"


def test_reader_reads_bounded_line_range(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    document = reader.read("runbooks/file-sizing.md", start_line=5, end_line=7)

    assert document.lines == ["## Before recommending compaction", "", "Confirm with measurements."]
    assert document.total_lines == 7
    assert document.start_line == 5
    assert document.end_line == 7


def test_reader_clamps_end_line_to_document_length(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    document = reader.read("runbooks/file-sizing.md", start_line=7, end_line=50)

    assert document.end_line == 7
    assert document.truncated is True
    assert document.lines == ["Confirm with measurements."]


def test_reader_rejects_start_line_beyond_document_end(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgeAccessError, match="beyond the end"):
        reader.read("runbooks/file-sizing.md", start_line=8, end_line=50)


def test_reader_without_end_line_reads_a_window(corpus: Path) -> None:
    reader = KnowledgeReader(corpus, max_lines=3)

    document = reader.read("runbooks/long.md", start_line=10)

    assert document.start_line == 10
    assert document.end_line == 12
    assert document.lines == ["line 10", "line 11", "line 12"]


def test_reader_enforces_max_lines_per_read(corpus: Path) -> None:
    reader = KnowledgeReader(corpus, max_lines=10)

    with pytest.raises(KnowledgeAccessError, match="exceeds the 10-line read limit"):
        reader.read("runbooks/long.md", start_line=1, end_line=11)


def test_reader_allows_range_exactly_at_max_lines(corpus: Path) -> None:
    reader = KnowledgeReader(corpus, max_lines=10)

    document = reader.read("runbooks/long.md", start_line=1, end_line=10)

    assert document.end_line == 10
    assert len(document.lines) == 10


@pytest.mark.parametrize("bad_range", [(0, None), (0, 5), (5, 4), (-1, 3)])
def test_reader_rejects_invalid_ranges(corpus: Path, bad_range: tuple[int, int | None]) -> None:
    reader = KnowledgeReader(corpus)
    start, end = bad_range

    with pytest.raises(KnowledgeAccessError):
        reader.read("runbooks/file-sizing.md", start_line=start, end_line=end)


def test_reader_rejects_non_integer_line_bounds(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgeAccessError):
        reader.read("runbooks/file-sizing.md", start_line="1")  # type: ignore[arg-type]


def test_reader_clips_extremely_long_lines(corpus: Path) -> None:
    (corpus / "iceberg" / "wide.md").write_text("# wide\n" + "x" * 5000 + "\n", encoding="utf-8")
    reader = KnowledgeReader(corpus)

    document = reader.read("iceberg/wide.md", start_line=2, end_line=2)

    assert len(document.lines[0]) == 2000


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "runbooks/../../outside.md",
        "runbooks/../secret.md",
        "/etc/passwd.md",
        "~/notes.md",
        "runbooks\\file-sizing.md",
        "runbooks//file-sizing.md",
        "./runbooks/file-sizing.md",
        "runbooks/.",
        "runbooks/..",
        "",
        "runbook\x00s.md",
    ],
)
def test_reader_rejects_traversal_and_malformed_paths(corpus: Path, path: str) -> None:
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgePathError):
        reader.read(path)


def test_reader_rejects_non_markdown_files(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgePathError, match="curated markdown"):
        reader.read("notes.txt")


def test_reader_rejects_missing_document(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgeNotFoundError):
        reader.read("runbooks/missing.md")


def test_reader_rejects_symlink_escaping_corpus_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret\n", encoding="utf-8")
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "leak.md").symlink_to(outside / "secret.md")
    reader = KnowledgeReader(root)

    with pytest.raises(KnowledgePathError, match="escapes the knowledge root"):
        reader.read("leak.md")


def test_symlink_inside_root_is_readable(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "real.md").write_text("# Real\n", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "docs" / "real.md")
    reader = KnowledgeReader(root)

    assert reader.read("alias.md").title == "Real"


def test_reader_rejects_oversized_documents(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sta.knowledge.read.MAX_FILE_BYTES", 10)
    reader = KnowledgeReader(corpus)

    with pytest.raises(KnowledgeAccessError, match="byte corpus limit"):
        reader.read("runbooks/file-sizing.md")


def test_reader_requires_existing_root(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeAccessError, match="root does not exist"):
        KnowledgeReader(tmp_path / "nowhere")


def test_exists_returns_false_instead_of_raising(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    assert reader.exists("runbooks/file-sizing.md") is True
    assert reader.exists("../outside.md") is False
    assert reader.exists("runbooks/missing.md") is False


def test_list_paths_returns_sorted_relative_markdown_paths(corpus: Path) -> None:
    reader = KnowledgeReader(corpus)

    assert reader.list_paths() == [
        "INDEX.md",
        "iceberg/files.md",
        "runbooks/file-sizing.md",
        "runbooks/long.md",
    ]


def test_document_title_finds_first_h1() -> None:
    assert document_title(["intro", "# Title", "## Sub"]) == "Title"
    assert document_title(["no heading here"]) is None


def test_default_knowledge_root_respects_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STA_KNOWLEDGE_ROOT", str(tmp_path))
    assert default_knowledge_root() == tmp_path

    monkeypatch.delenv("STA_KNOWLEDGE_ROOT")
    root = default_knowledge_root()
    assert root.name == "knowledge"


def test_corpus_size_bound_is_deliberate() -> None:
    # Curated notes are small documents; the bound keeps reads bounded.
    assert MAX_FILE_BYTES == 1_000_000