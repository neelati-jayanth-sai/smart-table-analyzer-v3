"""Knowledge repository facade (Architecture.md #19-#20).

The Investigator gets exactly two operations over the curated corpus —
``search`` and bounded ``read`` — plus the list of known paths used by report
reference validation. This is the single place that composes the reader and
the lexical search; nothing here interprets content or touches table evidence.
"""

from pathlib import Path

from sta.knowledge.read import (
    DEFAULT_MAX_LINES_PER_READ,
    KnowledgeDocument,
    KnowledgeReader,
    default_knowledge_root,
)
from sta.knowledge.search import (
    DEFAULT_SEARCH_LIMIT,
    KnowledgeHit,
    search_corpus,
)

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "KnowledgeBase",
]


class KnowledgeBase:
    """The curated knowledge corpus as the Investigator sees it."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_lines_per_read: int = DEFAULT_MAX_LINES_PER_READ,
        default_search_limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> None:
        if root is None:
            root = default_knowledge_root()
        self._reader = KnowledgeReader(root, max_lines=max_lines_per_read)
        self._default_search_limit = default_search_limit

    @property
    def root(self) -> Path:
        return self._reader.root

    def search(self, query: str, limit: int | None = None) -> list[KnowledgeHit]:
        """Lexical search over the corpus; bounded, deterministic ranking."""
        return search_corpus(
            self._reader, query, limit=self._default_search_limit if limit is None else limit
        )

    def read(self, path: str, start_line: int = 1, end_line: int | None = None) -> "KnowledgeDocument":
        """Read one bounded line range of a curated document."""
        return self._reader.read(path, start_line=start_line, end_line=end_line)

    def known_paths(self) -> list[str]:
        """Every curated document path; used to validate report references."""
        return self._reader.list_paths()

    def exists(self, path: str) -> bool:
        return self._reader.exists(path)