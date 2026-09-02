"""DuckDB targeted column-profile provider (Runtime_Environments_UI.md #11, #13).

The local implementation of the single expensive tool
(``analyze_partition_candidate``): a fixed aggregate over the pinned
snapshot's data files, executed by DuckDB through the one reviewed local
template (``queries/local/analyze_partition_candidate.sql``).

- The template is bound by the reviewed loader (``:column`` must be a plain
  identifier; ``:source`` is an internally rendered scan path, never model
  input). Values that reach SQL string literals (data-file paths, S3
  settings) are rendered as escaped SQL string literals so quote-containing
  values stay literal values.
- Column existence and identifier policy are already enforced upstream
  (parameter contract + backend); an unknown column simply yields no rows.
- Data files written by PyIceberg to local object storage use ``s3://``
  paths; DuckDB reads them through the statically bundled httpfs extension
  with the configured endpoint/credentials. Local ``file://``/plain paths
  need no engine configuration.
- An unavailable engine or unconfigured S3 access raises a typed, safe
  error instead of guessing (no silent fallback).
"""

import logging
from typing import Any
from urllib.parse import urlparse

from sta.execution.backends.local import LocalColumnProfile, LocalTableFixture
from sta.execution.errors import BackendExecutionError
from sta.execution.queries.loader import bind_template

logger = logging.getLogger(__name__)

_S3_SCHEME = "s3://"
_FILE_SCHEME = "file://"


class DuckDbCandidateProfileProvider:
    """``CandidateProfileProvider`` over the fixture's data files."""

    def __init__(
        self,
        fixture: LocalTableFixture,
        *,
        s3_properties: dict[str, str] | None = None,
        connection_factory: Any = None,
    ) -> None:
        self._fixture = fixture
        self._s3_properties = s3_properties or {}
        # Connection seam for tests; a real DuckDB connection by default.
        self._connection_factory = connection_factory

    def __call__(self, column: str) -> LocalColumnProfile | None:
        data_files = [entry.file_path for entry in self._fixture.data_files if entry.content == 0]
        if not data_files:
            return None  # nothing to profile; the caller reports no profile available
        sql = bind_template(
            "local",
            "analyze_partition_candidate",
            {"column": column, "source": _render_source(data_files)},
        )
        row = self._execute(sql, needs_s3=any(path.startswith(_S3_SCHEME) for path in data_files))
        if row is None:
            return None
        return LocalColumnProfile(
            total_value_count=_as_int(row.get("total_value_count")),
            null_count=_as_int(row.get("null_count")),
            nan_count=None,  # the reviewed template does not scan NaNs
            distinct_count=_as_int(row.get("distinct_count")),
            min_value=row.get("min_value"),
            max_value=row.get("max_value"),
            files_per_distinct_value_min=_as_int(row.get("files_per_distinct_value_min")),
            files_per_distinct_value_median=_as_float(row.get("files_per_distinct_value_median")),
            files_per_distinct_value_max=_as_int(row.get("files_per_distinct_value_max")),
            records_per_distinct_value_min=_as_int(row.get("records_per_distinct_value_min")),
            records_per_distinct_value_median=_as_float(row.get("records_per_distinct_value_median")),
            records_per_distinct_value_max=_as_int(row.get("records_per_distinct_value_max")),
            top_values=_as_top_values(row.get("top_values")),
        )

    def _execute(self, sql: str, *, needs_s3: bool) -> dict[str, Any] | None:
        if self._connection_factory is not None:
            return self._connection_factory(sql)
        try:
            import duckdb  # noqa: PLC0415 - boundary import, local environment only
        except ImportError as exc:  # pragma: no cover - duckdb is a hard dependency
            raise BackendExecutionError(
                "analyze_partition_candidate",
                "DuckDB is not installed; the local backend cannot run targeted analysis",
            ) from exc
        connection = duckdb.connect(database=":memory:")
        try:
            if needs_s3:
                _configure_s3(connection, self._s3_properties)
            relation = connection.execute(sql)
            row = relation.fetchone()
            if row is None:
                return None
            columns = [column[0] if isinstance(column, tuple) else column for column in relation.description]
            return dict(zip(columns, row, strict=True))
        except BackendExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - typed, safe engine failure
            logger.warning("duckdb candidate profile failed: %s", type(exc).__name__)
            raise BackendExecutionError(
                "analyze_partition_candidate",
                f"local targeted analysis failed ({type(exc).__name__})",
            ) from exc
        finally:
            connection.close()


def _sql_string_literal(value: str) -> str:
    """Render one DuckDB string literal, safe for any value: single quotes
    are doubled, so quote-containing paths/settings stay literal values
    (never string terminators, never statement structure)."""
    return "'" + value.replace("'", "''") + "'"


def _render_source(data_files: list[str]) -> str:
    """Scan source for the reviewed template: a read_parquet(...) file list
    with the source-file name exposed as the ``filename`` column.

    Built from Iceberg data-file paths (infrastructure values), never from
    model input; the binder additionally rejects SQL-comment markers. Paths
    are escaped SQL string literals (quote-safe by construction)."""
    paths = ", ".join(
        _sql_string_literal(path.replace(_FILE_SCHEME, "", 1)) for path in data_files
    )
    return f"read_parquet([{paths}], filename=true)"


def _configure_s3(connection: Any, properties: dict[str, str]) -> None:
    """Point DuckDB's httpfs S3 settings at the local object storage.

    DuckDB expects ``s3_endpoint`` as ``host:port`` without a URL scheme; the
    configured endpoint may include ``http://`` or ``https://``, so the scheme
    is stripped and ``s3_use_ssl`` is set to match.

    Every value is rendered as an escaped SQL string literal so
    quote-containing endpoints/credentials cannot break out of the ``SET``
    statement (DuckDB reads ``''`` as one literal quote)."""
    endpoint = properties.get("endpoint")
    if not endpoint:
        raise BackendExecutionError(
            "analyze_partition_candidate",
            "data files are on S3 but no object storage is configured "
            "(set S3_ENDPOINT, S3_ACCESS_KEY and S3_SECRET_KEY)",
        )
    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    if host and scheme in ("http", "https"):
        duckdb_endpoint = f"{host}:{port}" if port else host
        use_ssl = scheme == "https"
    else:
        # Endpoint was supplied without a scheme (e.g. host:port); pass it
        # through as-is and leave SSL at DuckDB's default.
        duckdb_endpoint = endpoint
        use_ssl = True

    try:
        connection.execute("LOAD httpfs")
    except Exception as exc:  # noqa: BLE001 - engine/extension failure is typed
        raise BackendExecutionError(
            "analyze_partition_candidate",
            f"the DuckDB httpfs extension is required to read S3 data files ({type(exc).__name__})",
        ) from exc
    connection.execute(f"SET s3_endpoint={_sql_string_literal(duckdb_endpoint)}")
    connection.execute(
        f"SET s3_use_ssl={_sql_string_literal('true' if use_ssl else 'false')}"
    )
    connection.execute(
        f"SET s3_region={_sql_string_literal(properties.get('region', 'us-east-1'))}"
    )
    if "access_key_id" in properties:
        connection.execute(
            f"SET s3_access_key_id={_sql_string_literal(properties['access_key_id'])}"
        )
    if "secret_access_key" in properties:
        connection.execute(
            f"SET s3_secret_access_key={_sql_string_literal(properties['secret_access_key'])}"
        )
    if "url_style" in properties:
        connection.execute(
            f"SET s3_url_style={_sql_string_literal(properties['url_style'])}"
        )


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_top_values(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value]
    return []


__all__ = ["DuckDbCandidateProfileProvider"]