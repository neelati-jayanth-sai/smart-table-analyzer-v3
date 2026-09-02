"""Bounded lexical search over the curated knowledge corpus (Architecture.md #19-#20).

No vector database, no embeddings, no RAG framework: token-overlap scoring
over curated markdown, with weighted title/heading matches and a bounded
best-line snippet per hit. Results are deterministic: same corpus, same query,
same ranking (score desc, then more matched terms, then path order).
"""

import re

from pydantic import BaseModel

from sta.knowledge.read import KnowledgeAccessError, KnowledgeReader

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10

# Weighted token occurrences: titles and section headings describe what a
# section is about; body occurrences confirm coverage.
_TITLE_WEIGHT = 5.0
_HEADING_WEIGHT = 3.0
_BODY_WEIGHT = 1.0
_MAX_BODY_OCCURRENCES = 10

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_MIN_TOKEN_LENGTH = 2

SNIPPET_MAX_CHARS = 240


class KnowledgeHit(BaseModel):
    """One search result: a document path plus a bounded, located snippet."""

    path: str
    title: str | None = None
    score: float
    matched_terms: list[str]
    snippet: str
    snippet_line: int


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens of at least two characters."""
    return [
        token
        for token in _TOKEN_PATTERN.findall(text.lower())
        if len(token) >= _MIN_TOKEN_LENGTH
    ]


def search_corpus(
    reader: KnowledgeReader,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[KnowledgeHit]:
    """Rank corpus documents against the query and return the top bounded hits."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")
    limit = min(limit, MAX_SEARCH_LIMIT)

    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []

    hits: list[KnowledgeHit] = []
    for path in reader.list_paths():
        try:
            document = reader.read(path)
        except (KnowledgeAccessError, OSError):
            continue  # unreadable entries are skipped; search stays bounded
        score, matched = _score_document(terms, document.title or "", document.lines)
        if score <= 0.0 or not matched:
            continue
        snippet, snippet_line = _best_snippet(terms, document.title, document.lines)
        hits.append(
            KnowledgeHit(
                path=document.path,
                title=document.title,
                score=round(score, 4),
                matched_terms=matched,
                snippet=snippet,
                snippet_line=snippet_line,
            )
        )

    hits.sort(key=lambda hit: (-hit.score, -len(hit.matched_terms), hit.path))
    return hits[:limit]


def _score_document(terms: list[str], title: str, lines: list[str]) -> tuple[float, list[str]]:
    title_tokens = set(tokenize(title))
    heading_lines = [line.lstrip() for line in lines if line.lstrip().startswith("#")]
    heading_tokens = [set(tokenize(line.lstrip("#").strip())) for line in heading_lines]

    total = 0.0
    matched: list[str] = []
    for term in terms:
        weight = 0.0
        if term in title_tokens:
            weight += _TITLE_WEIGHT
        if any(term in tokens for tokens in heading_tokens):
            weight += _HEADING_WEIGHT
        occurrences = sum(1 for line in lines if term in tokenize(line))
        if occurrences:
            weight += _BODY_WEIGHT * min(occurrences, _MAX_BODY_OCCURRENCES)
        if weight > 0.0:
            matched.append(term)
            total += weight
    return total, matched


def _best_snippet(terms: list[str], title: str | None, lines: list[str]) -> tuple[str, int]:
    """Bounded snippet: the line matching the most query terms.

    Ties go to the earliest line; a title-only match falls back to the title
    text at line 1. Heading lines are stripped to their text.
    """
    best_line = 0
    best_matches = 0
    for number, line in enumerate(lines, start=1):
        matches = _line_matches(terms, line)
        if matches > best_matches:
            best_matches = matches
            best_line = number
    if best_line:
        line = lines[best_line - 1]
        if line.lstrip().startswith("#"):
            line = line.lstrip("#").strip()
        return _clip(line), best_line
    if title:
        return _clip(title), 1
    return "", 1


def _line_matches(terms: list[str], line: str) -> int:
    present = set(tokenize(line))
    return sum(1 for term in terms if term in present)


def _clip(text: str) -> str:
    stripped = text.strip()
    if len(stripped) > SNIPPET_MAX_CHARS:
        return stripped[:SNIPPET_MAX_CHARS].rstrip() + "…"
    return stripped