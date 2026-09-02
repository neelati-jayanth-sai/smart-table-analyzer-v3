"""DuckDB targeted-profile provider tests (Runtime_Environments_UI.md #11, #13).

Focus: SQL-safety of the values that reach DuckDB string literals — data-file
paths (``:source``) and the httpfs S3 settings — and the typed fail-closed
behaviour when S3 data files cannot be read. Quote-containing values must
stay literal values (never statement structure), verified against real DuckDB
where practical.
"""

import pytest

from sta.execution.backends.duckdb_candidate import (
    DuckDbCandidateProfileProvider,
    _configure_s3,
    _render_source,
    _sql_string_literal,
)
from sta.execution.backends.local import LocalColumnProfile, LocalFileEntry, LocalTableFixture
from sta.execution.errors import BackendExecutionError


class _RecordingConnection:
    """Fake DuckDB connection that records executed statements."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


def _fixture_with_data_files(*paths: str) -> LocalTableFixture:
    return LocalTableFixture(
        table="demo.sales.orders",
        snapshot_id=123,
        data_files=[
            LocalFileEntry(file_path=path, file_size_bytes=100, content=0, record_count=1)
            for path in paths
        ],
        column_profiles={"amount": LocalColumnProfile(total_value_count=1)},
    )


# ---------------------------------------------------------------------------
# quote-safe SQL string rendering
# ---------------------------------------------------------------------------


def test_sql_string_literal_doubles_single_quotes() -> None:
    assert _sql_string_literal("it's") == "'it''s'"
    assert _sql_string_literal("plain") == "'plain'"
    assert _sql_string_literal("'") == "''''"


def test_render_source_escapes_quote_in_path() -> None:
    source = _render_source(["s3://bucket/it's/file.parquet"])
    assert source == "read_parquet(['s3://bucket/it''s/file.parquet'], filename=true)"


def test_render_source_strips_file_scheme() -> None:
    source = _render_source(["file:///data/dir/a'b.parquet"])
    assert source == "read_parquet(['/data/dir/a''b.parquet'], filename=true)"


def test_render_source_keeps_multiple_paths() -> None:
    source = _render_source(["/data/a.parquet", "/data/b.parquet"])
    assert source == "read_parquet(['/data/a.parquet', '/data/b.parquet'], filename=true)"


def test_configure_s3_escapes_quote_containing_values() -> None:
    connection = _RecordingConnection()

    _configure_s3(
        connection,
        {
            "endpoint": "http://ho'st:9000",
            "region": "us'east",
            "access_key_id": "minio'admin",
            "secret_access_key": "sec'ret",
            "url_style": "pa'th",
        },
    )

    statements = connection.statements
    assert statements[0] == "LOAD httpfs"
    # DuckDB expects host:port without a URL scheme; http means use_ssl=false.
    assert "SET s3_endpoint='ho''st:9000'" in statements
    assert "SET s3_use_ssl='false'" in statements
    assert "SET s3_region='us''east'" in statements
    assert "SET s3_access_key_id='minio''admin'" in statements
    assert "SET s3_secret_access_key='sec''ret'" in statements
    assert "SET s3_url_style='pa''th'" in statements
    # No statement terminates its string early: each SET carries balanced,
    # escaped quoting only.
    for statement in statements:
        if statement.startswith("SET"):
            assert statement.count("'") % 2 == 0


def test_configure_s3_requires_endpoint() -> None:
    with pytest.raises(BackendExecutionError, match="no object storage is configured"):
        _configure_s3(_RecordingConnection(), {})


def test_configure_s3_accepts_real_duckdb_with_quote_values() -> None:
    """The escaped SET statements execute on a real DuckDB connection and the
    engine stores the literal values (quote round-trips exactly)."""
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        _configure_s3(
            connection,
            {
                "endpoint": "http://ho'st:9000",
                "region": "us'east-1",
                "access_key_id": "key'with'quotes",
                "secret_access_key": "sec'ret",
                "url_style": "path",
            },
        )
        # DuckDB expects host:port without a URL scheme; http endpoints set
        # use_ssl=false.
        assert connection.execute("SELECT current_setting('s3_endpoint')").fetchone()[0] == (
            "ho'st:9000"
        )
        assert connection.execute("SELECT current_setting('s3_use_ssl')").fetchone()[0] is False
        assert connection.execute("SELECT current_setting('s3_region')").fetchone()[0] == (
            "us'east-1"
        )
        assert connection.execute("SELECT current_setting('s3_access_key_id')").fetchone()[0] == (
            "key'with'quotes"
        )
        assert connection.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0] == (
            "sec'ret"
        )
    finally:
        connection.close()


def test_provider_profiles_quote_named_local_file() -> None:
    """End-to-end against real DuckDB: a data file whose path contains a
    single quote is read as a literal path, and the profile is computed."""
    duckdb = pytest.importorskip("duckdb")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = tempfile.mkdtemp(dir=tmp)
        path = f"{base}/data'file.parquet"
        duckdb.sql(
            "SELECT * FROM (VALUES (1, 10.0), (2, 20.0), (3, NULL)) AS t(id, amount)"
        ).write_parquet(path)

        fixture = _fixture_with_data_files(path)
        provider = DuckDbCandidateProfileProvider(fixture)

        profile = provider("amount")

    assert profile is not None
    assert profile.total_value_count == 3
    assert profile.null_count == 1
    assert profile.distinct_count == 2
    assert profile.min_value == "10.0"
    assert profile.max_value == "20.0"


def test_provider_s3_files_without_configuration_fail_typed() -> None:
    """S3 data files without object-store configuration fail closed with the
    actionable typed error — never a silent fallback or a partial scan."""
    fixture = _fixture_with_data_files("s3://bucket/data/0000.parquet")
    provider = DuckDbCandidateProfileProvider(fixture, s3_properties={})

    with pytest.raises(BackendExecutionError, match="no object storage is configured"):
        provider("amount")


def test_provider_renders_bound_sql_through_connection_seam() -> None:
    """The connection seam receives the reviewed template with the escaped
    scan source bound in; no S3 configuration is attempted for local paths."""
    captured: list[str] = []

    def connection_factory(sql: str) -> dict | None:
        captured.append(sql)
        return {
            "total_value_count": 3,
            "null_count": 0,
            "distinct_count": 3,
            "min_value": "1",
            "max_value": "3",
        }

    fixture = _fixture_with_data_files("file:///data/it's.parquet")
    provider = DuckDbCandidateProfileProvider(
        fixture, connection_factory=connection_factory
    )

    profile = provider("amount")

    assert profile is not None
    assert len(captured) == 1
    assert "read_parquet(['/data/it''s.parquet'], filename=true)" in captured[0]
    assert "s3_endpoint" not in captured[0]