"""Bounded, path-safe reading of the curated knowledge corpus (Architecture.md #19-#20).

Knowledge is curated filesystem markdown outside the Python packages. The
Investigator only ever receives bounded excerpts through this module:

- paths are relative POSIX paths that must resolve inside the corpus root
  (traversal prevention: ``..``, absolute paths, backslashes, symlinks
  escaping the root and non-markdown files are all rejected),
- reads are line-ranged and size-bounded, so the model can pull exactly the
  relevant section instead of whole documents.

This module informs; it never interprets. Knowledge is not table evidence.
"""

import os
from pathlib import Path, PurePosixPath
from pydantic import BaseModel

# Curated corpus: markdown only.
ALLOWED_SUFFIXES = frozenset({".md"})

# Bounds that keep a single read from flooding model context. A curated
# knowledge note larger than MAX_FILE_BYTES indicates a problem with the
# corpus, not something to stream into a prompt.
DEFAULT_MAX_LINES_PER_READ = 200
MAX_LINE_CHARS = 2000
MAX_FILE_BYTES = 1_000_000

# Environment override so deployments can point at their own curated corpus.
KNOWLEDGE_ROOT_ENV_VAR = "STA_KNOWLEDGE_ROOT"


class KnowledgeAccessError(Exception):
    """Base class for deterministic knowledge-access failures."""


class KnowledgePathError(KnowledgeAccessError):
    """The requested path is malformed or escapes the corpus root."""


class KnowledgeNotFoundError(KnowledgeAccessError):
    """The path is well-formed but does not exist in the corpus."""


class KnowledgeDocument(BaseModel):
    """One bounded excerpt of a knowledge document (1-based, inclusive range)."""

    path: str
    title: str | None = None
    total_lines: int
    start_line: int
    end_line: int
    lines: list[str]
    truncated: bool = False

    @property
    def content(self) -> str:
        return "\n".join(self.lines)


class KnowledgeReader:
    """Reads curated documents inside one corpus root, always bounded."""

    def __init__(self, root: str | os.PathLike[str], *, max_lines: int = DEFAULT_MAX_LINES_PER_READ):
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        self._root = Path(root)
        if not self._root.is_dir():
            raise KnowledgeAccessError(f"knowledge root does not exist: {self._root}")
        self._max_lines = max_lines

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_lines(self) -> int:
        return self._max_lines

    def list_paths(self) -> list[str]:
        """All curated documents as sorted relative POSIX paths."""
        paths: list[str] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            relative = path.relative_to(self._root).as_posix()
            paths.append(relative)
        return paths

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path)
        except KnowledgeAccessError:
            return False
        return True

    def resolve(self, path: str) -> Path:
        """Validate a corpus-relative path and return its resolved location.

        Raises :class:`KnowledgePathError` for traversal attempts or bad
        formats and :class:`KnowledgeNotFoundError` for missing documents.
        """
        relative = self._validate_relative_path(path)
        root = self._root.resolve()
        resolved = (root / relative).resolve()
        # Symlink defense: a curated file must not resolve outside the root.
        if not resolved.is_relative_to(root):
            raise KnowledgePathError(f"path escapes the knowledge root: {path}")
        if not resolved.is_file():
            raise KnowledgeNotFoundError(f"knowledge document not found: {relative}")
        return resolved

    def read(self, path: str, start_line: int = 1, end_line: int | None = None) -> KnowledgeDocument:
        """Read one bounded line range (1-based, inclusive, bounded size)."""
        relative = self._validate_relative_path(path)
        resolved = self.resolve(path)
        lines = self._load_lines(resolved, relative)
        title = document_title(lines)
        total = len(lines)

        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise KnowledgeAccessError(f"start_line must be a positive integer, got {start_line!r}")
        if end_line is not None and (
            not isinstance(end_line, int) or isinstance(end_line, bool) or end_line < start_line
        ):
            raise KnowledgeAccessError(
                f"end_line must be an integer >= start_line ({start_line}), got {end_line!r}"
            )
        requested_end = end_line if end_line is not None else start_line + self._max_lines - 1
        if requested_end - start_line + 1 > self._max_lines:
            raise KnowledgeAccessError(
                f"requested range {start_line}-{requested_end} exceeds the "
                f"{self._max_lines}-line read limit; request a smaller range"
            )
        if start_line > total:
            raise KnowledgeAccessError(
                f"start_line {start_line} is beyond the end of {relative} ({total} lines)"
            )

        clamped_end = min(requested_end, total)
        selected = [_clip(line) for line in lines[start_line - 1 : clamped_end]]
        # Only an explicitly requested range beyond the document end counts as
        # truncated; the default window read is the natural bounded read.
        truncated = end_line is not None and clamped_end < end_line
        return KnowledgeDocument(
            path=relative,
            title=title,
            total_lines=total,
            start_line=start_line,
            end_line=clamped_end,
            lines=selected,
            truncated=truncated,
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _validate_relative_path(path: str) -> str:
        if not isinstance(path, str) or not path:
            raise KnowledgePathError("knowledge path must be a non-empty string")
        if "\x00" in path:
            raise KnowledgePathError("knowledge path contains a null byte")
        if "\\" in path:
            raise KnowledgePathError(f"knowledge path must use '/' separators: {path}")
        pure = PurePosixPath(path)
        if pure.is_absolute() or path.startswith("/") or path.startswith("~"):
            raise KnowledgePathError(f"knowledge path must be relative to the corpus root: {path}")
        segments = path.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise KnowledgePathError(f"knowledge path must not contain traversal segments: {path}")
        if PurePosixPath(path).suffix.lower() not in ALLOWED_SUFFIXES:
            raise KnowledgePathError(f"only curated markdown documents are readable: {path}")
        return pure.as_posix()

    @staticmethod
    def _load_lines(resolved: Path, relative: str) -> list[str]:
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES:
            raise KnowledgeAccessError(
                f"knowledge document {relative} exceeds the {MAX_FILE_BYTES}-byte corpus limit"
            )
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise KnowledgeAccessError(f"knowledge document {relative} is not readable") from exc
        return text.splitlines()


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS]


def document_title(lines: list[str]) -> str | None:
    """First level-1 markdown heading, without the leading '# '."""
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return None


def default_knowledge_root() -> Path:
    """Locate the curated corpus: env override, then the repository root."""
    override = os.environ.get(KNOWLEDGE_ROOT_ENV_VAR)
    if override:
        return Path(override)
    # src/sta/knowledge/read.py -> repository root is three levels up.
    return Path(__file__).resolve().parents[3] / "knowledge"