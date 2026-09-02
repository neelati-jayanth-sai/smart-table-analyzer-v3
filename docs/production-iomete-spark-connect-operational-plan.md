# Production IOMETE Spark Connect Operational Plan

This document lists the concrete changes needed to make Smart Table Analyzer operational in production.

Production access model is strict:

```text
STA production → IOMETE Spark Connect → Spark SQL → Iceberg tables/metadata
```

Production must **not** use direct Iceberg/PyIceberg catalog access. PyIceberg and DuckDB remain local-development only.

Reference: IOMETE Spark Connect usage is based on IOMETE's Spark Connect flow: create/copy the Spark Connect endpoint from IOMETE, authenticate with a personal access token, then create a PySpark `SparkSession.builder.remote("<spark-connect-endpoint>").getOrCreate()`.

---

## Current production state

Implemented today:

- `src/sta/execution/backends/iomete.py`
  - Has the `IometeConnection` protocol seam:
    - `execute(sql: str) -> list[dict[str, Any]]`
  - Uses reviewed Spark/IOMETE SQL templates from `src/sta/execution/queries/iomete/`.
  - Does not import PyIceberg.
- `src/sta/config.py`
  - Production settings are `IOMETE_ENDPOINT`, `IOMETE_CATALOG`, and `IOMETE_TOKEN`.
  - PyIceberg properties are local-only.
- `src/sta/app/api.py`
  - Production currently fails closed with safe typed errors.

Missing today:

- A real Spark Connect client dependency.
- A real IOMETE Spark Connect connection adapter.
- A production metadata provider that builds `TableMetadata` through Spark SQL.
- Production app wiring that replaces the fail-closed placeholders.
- Live IOMETE integration tests.

---

## Required code changes

### 1. Add Spark Connect dependency

Update `pyproject.toml`.

Likely dependency:

```toml
"pyspark[connect]>=3.5"
```

Verify the exact version against the IOMETE Spark runtime version. IOMETE examples show Spark 3.5.x, but production should pin/align to the deployed IOMETE Spark Connect version.

Do not add Spark to the local execution path. Local Docker development must remain PyIceberg + DuckDB only.

---

### 2. Add a production connection adapter

Create a new module, for example:

```text
src/sta/execution/backends/iomete_spark_connect.py
```

Responsibilities:

- Own all Spark Connect session construction.
- Read only normalized `Settings` values passed from the app/config layer.
- Authenticate using `IOMETE_TOKEN` without logging it.
- Expose the existing `IometeConnection` protocol:

```python
class SparkConnectIometeConnection:
    def execute(self, sql: str) -> list[dict[str, Any]]:
        ...
```

Expected behavior:

- Build a Spark session with the configured Spark Connect endpoint:

```python
SparkSession.builder.remote(settings.iomete_endpoint).getOrCreate()
```

- Add IOMETE authentication according to the final IOMETE/Spark Connect requirement.
- Execute reviewed SQL only.
- Convert Spark rows to plain dictionaries.
- Never return Spark objects outside the adapter.
- Never log SQL with credentials or connection secrets.
- Wrap failures in safe typed STA errors, e.g. `BackendExecutionError`.

Open question to confirm against the target IOMETE deployment:

- Exact token injection mechanism for Spark Connect.
  - Possibilities include Spark config, headers, or IOMETE-specific client setup.
  - Keep this inside `iomete_spark_connect.py` only.

---

### 3. Add a production metadata provider

Production still needs startup `TableMetadata` before the Investigator runs.

Create a provider that implements `TableMetadataProvider`, for example:

```text
src/sta/execution/backends/iomete_metadata.py
```

Responsibilities:

- Resolve exactly `catalog.namespace.table` through IOMETE/Spark SQL.
- Build backend-independent `src/sta/context/table_metadata.py` models.
- Use Spark SQL metadata surfaces only, such as:
  - `DESCRIBE TABLE EXTENDED catalog.namespace.table`
  - `SHOW CREATE TABLE catalog.namespace.table`
  - Iceberg metadata tables where useful
- Extract at minimum:
  - canonical table name
  - schema fields and types
  - current snapshot id
  - format version if available
  - partition specs/current partition spec
  - sort order/current sort order where available
  - table properties, with secret filtering

Rules:

- Do not use PyIceberg in this provider.
- Do not dump raw/large DDL into model context.
- If parsing `SHOW CREATE TABLE` is needed, parse deterministically and persist only compact structured facts.
- If required metadata cannot be extracted, fail closed with an actionable safe error.

---

### 4. Wire production components in `src/sta/app/api.py`

Replace the current production fail-closed wiring in `default_components(settings)`.

Target behavior:

```text
if STA_ENV=production:
    connection = SparkConnectIometeConnection.from_settings(settings)
    metadata_provider = IometeSparkMetadataProvider(connection, settings)
    backend_factory = lambda table, metadata: IometeBackend(
        table,
        connection,
        maintenance_table=settings.iomete_maintenance_table,
        metadata=metadata,
    )
```

Likely supporting config addition:

```text
IOMETE_MAINTENANCE_TABLE=...
```

Only required if `get_iomete_maintenance_config` should be enabled in production.

Startup should fail fast when required production settings are absent or invalid.

---

### 5. Extend production settings safely

Update `src/sta/config.py` only if the Spark Connect adapter needs extra values.

Possible additions:

```text
IOMETE_MAINTENANCE_TABLE
IOMETE_SPARK_CONNECT_AUTH_MODE
IOMETE_SESSION_TIMEOUT_SECONDS
IOMETE_QUERY_TIMEOUT_SECONDS
```

Rules:

- Secrets must use `repr=False`.
- `safe_summary()` must mask secrets.
- Do not expose tokens in events, results, logs, reports, or model context.
- Keep local `ICEBERG_*` and `S3_*` settings local-only.

---

### 6. Validate reviewed IOMETE SQL templates against real Spark

Existing templates live under:

```text
src/sta/execution/queries/iomete/
```

For each tool, verify syntax and result shape in IOMETE:

- `get_snapshot_history`
- `get_file_layout`
- `get_file_layout_history`
- `get_partition_layout`
- `get_manifest_stats`
- `get_delete_file_stats`
- `get_column_metadata_metrics`
- `analyze_partition_candidate`
- `get_partition_spec_usage`
- `get_sort_order_usage`
- `get_iomete_maintenance_config`

Do not let the LLM generate SQL. If IOMETE syntax differs, update the reviewed template and add/adjust contract tests.

---

## Required tests

Add tests before enabling production by default.

### Unit tests

Add tests for the Spark Connect adapter using fakes/mocks:

```text
tests/unit/execution/test_iomete_spark_connect.py
```

Cover:

- session construction uses `IOMETE_ENDPOINT`
- token/config injection is secret-safe
- `execute()` returns `list[dict]`
- Spark exceptions become safe `BackendExecutionError`
- no token appears in exception messages or logs

Add metadata-provider tests:

```text
tests/unit/execution/test_iomete_metadata_provider.py
```

Cover:

- valid `catalog.namespace.table` resolves
- wrong catalog fails closed
- schema/partition/snapshot facts become `TableMetadata`
- missing required metadata fails safely
- raw DDL is not surfaced to TableContext/model context

### Contract tests

Extend or add:

```text
tests/contract/test_backend_parity.py
```

Production contract must match local result contracts, not SQL syntax.

Verify each IOMETE tool returns the same backend-independent fields as local where both are supported.

### Integration tests

Add live-IOMETE tests gated by environment variables, for example:

```text
STA_RUN_LIVE_IOMETE_TESTS=1
STA_ENV=production
IOMETE_ENDPOINT=...
IOMETE_CATALOG=...
IOMETE_TOKEN=...
STA_TEST_IOMETE_TABLE=...
```

Tests should skip unless explicitly enabled.

Cover:

1. Spark Connect session can start.
2. Target table resolves.
3. Startup `TableContext` is compact and metadata-derived.
4. Each deterministic query tool executes and persists an `Rxxx` result.
5. Full API run path works:

```text
table input → run created → progress streams → tool executes → Rxxx stored → report endpoint works
```

---

## Operational rollout checklist

Before calling production ready:

- [ ] Confirm IOMETE Spark Connect endpoint format.
- [ ] Confirm token/auth injection mechanism.
- [ ] Add Spark Connect dependency aligned with IOMETE Spark version.
- [ ] Implement `SparkConnectIometeConnection`.
- [ ] Implement IOMETE/Spark metadata provider.
- [ ] Wire production components in `default_components()`.
- [ ] Add safe config validation for all new production settings.
- [ ] Verify every IOMETE SQL template against real IOMETE.
- [ ] Run full local test suite.
- [ ] Run live IOMETE integration tests with explicit opt-in.
- [ ] Confirm no secrets appear in logs, progress events, `Rxxx` results, reports, or model context.
- [ ] Update `docs/current-state.md` and `docs/implementation-progress.md` after production validation.

---

## Non-goals

Do not add:

- PyIceberg production catalog access.
- DuckDB production execution.
- User-provided SQL.
- LLM-generated SQL.
- Broad column profiling.
- A second diagnosis/recommendation engine outside the Investigator.
- React/Vue/Angular or new UI framework.
- Redis/Celery/Kafka/microservices.

---

## Acceptance criteria

Production is operational only when:

1. A user enters only `catalog.namespace.table`.
2. STA resolves that table through IOMETE/Spark Connect.
3. STA builds compact metadata-derived `TableContext` without direct PyIceberg.
4. Query tools execute reviewed Spark SQL through Spark Connect.
5. Every measurement is persisted as `Rxxx`.
6. Investigator uses stored evidence and curated knowledge for interpretation.
7. Final report validates and renders through the API/UI.
8. Secrets are absent from logs, events, results, reports, and model context.
9. Local PyIceberg/DuckDB behavior remains unchanged.
