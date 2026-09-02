"""Knowledge access: bounded search and read over the curated corpus
(Architecture.md #19-#20). The Investigator gets only ``search`` and bounded
``read``; there is no vector store, embedding or RAG framework."""

from sta.knowledge.read import (
    DEFAULT_MAX_LINES_PER_READ,
    KNOWLEDGE_ROOT_ENV_VAR,
    KnowledgeAccessError,
    KnowledgeDocument,
    KnowledgeNotFoundError,
    KnowledgePathError,
    KnowledgeReader,
    default_knowledge_root,
)
from sta.knowledge.repository import KnowledgeBase
from sta.knowledge.search import (
    DEFAULT_SEARCH_LIMIT,
    KnowledgeHit,
    search_corpus,
    tokenize,
)

__all__ = [
    "DEFAULT_MAX_LINES_PER_READ",
    "DEFAULT_SEARCH_LIMIT",
    "KNOWLEDGE_ROOT_ENV_VAR",
    "KnowledgeAccessError",
    "KnowledgeBase",
    "KnowledgeDocument",
    "KnowledgeHit",
    "KnowledgeNotFoundError",
    "KnowledgePathError",
    "KnowledgeReader",
    "default_knowledge_root",
    "search_corpus",
    "tokenize",
]