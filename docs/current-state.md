# Smart Table Analyzer — Current State

Operator/developer snapshot of what is implemented and verified in this
checkout. Source-of-truth design docs: `Architecture.md` and
`Runtime_Environments_UI.md`. Benchmark detail: `docs/benchmark-expectations.md`.
Progress history: `docs/implementation-progress.md`.

Everything below was verified against the current source and a full test run
(`pytest tests -q` → **386 passed**).

---

## What works now

- **Investigation lifecycle** — a run takes exactly one input (the Iceberg
  table name): resolve table → pin snapshot → build the compact
  metadata-derived `TableContext` (full schema persisted as the reserved
  pseudo-result `R000`) → Pydantic AI Investigator loop over validated query
  tools + curated knowledge → structured report validated and persisted in
  SQLite. In-process lifecycle with cancellation; cancellation preserves all
  stored evidence and activity history.
- **Deterministic query tools** — 11 reviewed tools in a static registry:
  `get_snapshot_history`, `get_file_layout`, `get_file_layout_history`,
  `get_partition_layout`, `get_manifest_stats`, `get_delete_file_stats`,
  `get_column_metadata_metrics`, `analyze_partition_candidate`,
  `get_partition_spec_usage`, `get_sort_order_usage`,
  `get_iomete_maintenance_config`. The model never writes SQL; tools measure
  only — they never diagnose or recommend.
- **Result store** — immutable per-run `Rxxx` results with query version,
  parameters, and snapshot scope (`snapshot_id: null` is the explicit
  "not pinned" mark). Reports and safe progress events persist alongside.
- **Knowledge repository** — curated filesystem content (`iceberg/`,
  `iomete/`, `runbooks/` — file-sizing, maintenance, partitioning,
  sort-orders — plus `diagnostics/` and `spark/`), bounded lexical search and
  path-safe read, `knowledge_read` events, unavailable to the Investigator
  until the first measurement (`R001+`) exists.
- **Report validation** — deterministic reference checks (see "Report
  guarantees" below); violations produce safe `report_rejected` events without
  persisting raw model output.
- **API + UI** — FastAPI app with SSE progress streaming, and a vanilla
  HTML/CSS/JS UI (no build pipeline) with run progress, activity feed,
  clickable result audit views, environment badge, and cancel.
- **Local environment** — Docker compose stack (MinIO + Iceberg REST catalog,
  no Spark), PyIceberg catalog provider, DuckDB execution, deterministic
  seed/reset scripts, configuration validated at startup with fail-fast and
  masked secrets.
- **Production (IOMETE)** — reviewed Spark SQL templates exist for all tools;
  the production backend currently fails closed with typed, safe errors until
  the production adapter is deployed. Live IOMETE behavior is unvalidated
  (see "Known limitations").

---

## Local startup and UI

```bash
docker compose up -d            # MinIO + Iceberg REST catalog (no Spark)
python scripts/seed_local.py    # deterministic benchmark tables in namespace demo
uvicorn sta.app.api:app --reload
```

Open `http://localhost:8000`, enter a fully qualified
`catalog.namespace.table`, and start the analysis.

- Reset to a known state: `python scripts/reset_local.py`
  (compose down `-v` → up → re-seed).
- Configuration is read from the environment plus `.env` (template:
  `.env.example`), validated at startup with fail-fast. Local requires
  `ICEBERG_CATALOG_URI`; the `s3://` warehouse requires the full S3 block
  (`S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`); omitting S3 entirely is
  the supported offline `file://` data mode (reported as `DATA_ACCESS_MODE`).
- Secrets are never echoed: settings `repr`/`safe_summary` mask them; the UI
  shows only the environment badge (e.g. `LOCAL · Docker Iceberg`).

### Sample tables (as entered in the UI)

The local catalog name is `local` (`ICEBERG_CATALOG_NAME`), and the API
requires the three-part form:

```text
local.demo.healthy_table
local.demo.orders_bad_spec
local.demo.orders_bad_spec_caps_properties
local.demo.orders_day_partitioned
local.demo.events_partition_candidate
local.demo.customer_orders_identifier_heavy
```

Further seeded scenario tables: `local.demo.small_files_table`,
`local.demo.partition_fragmentation_table`,
`local.demo.delete_files_unsupported_table`,
`local.demo.snapshot_growth_table`, `local.demo.no_sort_order_table`,
`local.demo.identifier_heavy_table`, `local.demo.missing_metrics_table`,
`local.demo.multiple_issues_table`,
`local.demo.small_files_not_material_table`,
`local.demo.wide_schema_table`.

---

## Investigator / model configuration

- **Enablement**: the Ollama investigator is enabled by `OLLAMA_API_KEY`
  (the legacy `LOCAL_OLAMMA_API_KEY` alias is normalized as a secret).
  Investigator selection precedence: injected model/callback (test seams) →
  `STA_INVESTIGATOR_MODEL` override → configured Ollama investigator. There is
  **no fallback model/provider**; without a configured investigator each run
  fails closed with a typed configuration error.
- **Ollama Cloud (default)** — with no `OLLAMA_BASE_URL`, a custom Pydantic AI
  model adapter talks to Ollama Cloud's native `/api/chat` endpoint with the
  model pinned to exactly `gpt-oss:120b-cloud`. The `-cloud` tag is
  hosted-only and does not exist on local daemons.
- **Local Ollama daemon** — with an explicitly set `OLLAMA_BASE_URL`, Pydantic
  AI's bundled `OllamaModel` is used against the daemon with the local tag
  `gpt-oss:120b`. Naming differs by design (`-cloud` is hosted-only); it is
  the same Ollama gpt-oss 120b model, never another vendor or model.
- **Secrets**: the API key travels only in the `Authorization: Bearer` header,
  is excluded from `repr`/`str`, masked in safe summaries, and never appears
  in logs, events, results, or model context.
- **Cloud adapter constraints** (by design, fail loudly rather than pretend):
  no streaming, no pre-request token counting, text-only prompts, and no
  native JSON-schema enforcement — structured reports use Pydantic AI's
  tool-output mechanism, which is verified to work.
- **Transport-failure hardening**: HTTP errors and unreachable endpoints
  normalize to a typed, safe `ModelTransportError`; the run emits a
  `model_failed` event describing the failure class (e.g. authentication),
  never the response body, key, endpoint, or raw model output. The
  `report_rejected` outcome (validation failure) remains distinct.

---

## Deterministic tools and partition-candidate behavior

- Tool parameters are validated before any backend is touched (plain
  identifier parts, non-negative integers) — no quoting/escaping tricks can
  reach a query template.
- `analyze_partition_candidate(column)` is the single targeted data-scan tool.
  **Identifier-like columns (`id`, `*_id`, `uuid`, `*_uuid`, `guid`, `*_guid`)
  are hard-rejected** in its parameter contract, so expensive candidate
  analysis can never run on them (Architecture invariant 14). All other tools
  aggregate Iceberg metadata / Parquet footers without data scans.
- Metadata-first ordering (measure `get_column_metadata_metrics` for a
  materially considered temporal column before any targeted candidate
  analysis; never conclude `insufficient_evidence` while that measurement is
  missing) is carried by the Investigator prompt/report contract and asserted
  by scripted end-to-end regressions; live adherence remains model judgment.
- Every stored result records the snapshot actually measured; historical
  metadata surfaces are content-filtered and null-safe so files are not
  double-counted across snapshots.

---

## Result, SSE, and report guarantees

- **Results**: `R000` is the run-scoped full-schema record (never table
  evidence); `R001+` are immutable measurement results. Reports may only cite
  results that exist and belong to the run/table.
- **Report validation rules** (deterministic, in
  `sta.investigator.report`):
  - every cited knowledge path must have been actually read in this run via a
    persisted `knowledge_read` event — a search hit is not a read;
  - current partition spec / sort order citations must include `R000`;
  - current property values must cite `R000` or the IOMETE
    `get_iomete_maintenance_config` measurement;
  - every report must cite at least one stored measurement (`R001+`).
  Rejected reports surface as a safe `report_rejected` event; raw model
  output is not persisted.
- **SSE progress**: `GET /api/runs/{run_id}/events` — events are ordered per
  run and persisted in SQLite; reconnecting clients replay missed events via
  `Last-Event-ID` (or `after_event_id`); heartbeats keep idle streams alive;
  the stream terminates after the run reaches a terminal state. Event payloads
  are safe (no chain-of-thought, SQL, or secrets).
- **Report contract**: Pydantic `InvestigationReport` — overall status,
  current issues (severity/confidence/evidence), immediate remediation, future
  table design (partition spec, sort order, table properties), no-change
  decisions, and limitations. `NO CHANGE` and `INCONCLUSIVE` are valid
  outcomes; partition recommendations are Iceberg transforms; immediate
  remediation is separate from future design.
- **Failure UX**: typed, safe errors at each seam (unresolved table,
  unconfigured backend, unconfigured investigator, model transport failure,
  report rejection); stored results and events remain available after failures.

---

## Tests — current count and known warnings

**386 tests pass** (pytest, ~1 min):

| Suite | Tests |
|---|---|
| `tests/unit` (context 77, execution 50, investigator 101, knowledge 52, results 33) | 313 |
| `tests/contract` (tool + backend parity) | 18 |
| `tests/integration` (local lifecycle + benchmarks) | 38 |
| `tests/benchmark` (multi-symptom tables) | 9 |
| `tests/e2e` (scripted investigator loop) | 8 |

Known warnings (2, both accepted):

1. `Field name "schema" in "QueryResult" shadows an attribute in parent
   "BaseModel"` — the architecture-required field name (Architecture.md §17).
2. PyIceberg `write.parquet.row-group-size-bytes` "not implemented" notice
   emitted while seeding benchmark tables — harmless.

---

## Validated benchmark tables

Deterministic bench (Docker-local):

```bash
python scripts/reset_local.py
pytest tests/integration/test_local_benchmarks.py tests/benchmark -v
```

| Table | Validated behavior |
|---|---|
| `demo.orders_bad_spec` | 336 hourly partitions/files, tiny files, one snapshot per append |
| `demo.orders_bad_spec_caps_properties` | 72 hourly files from real writes; deliberately inert UPPERCASE custom properties preserved verbatim and surfaced in the TableContext with raw casing; only the lowercase keys are effective |
| `demo.orders_day_partitioned` | 30 daily partitions, 300 k rows, files > 300 KiB, sort order in use |
| `demo.events_partition_candidate` | unpartitioned; targeted temporal candidate analysis returns real null/distinct/min/max values |
| `demo.customer_orders_identifier_heavy` | 2,000 customer partitions; candidate analysis rejected for every ID column |

Live Investigator evidence (real model, Docker-local, persisted in
`sta.sqlite3`): `local.demo.orders_bad_spec` and
`local.demo.orders_bad_spec_caps_properties` both **completed** with persisted,
validated reports (`R000`–`R002`, three `knowledge_read` events each,
`needs_attention` findings citing file-layout/partition-layout measurements).
Earlier live runs exposed three model-reasoning defects; the corrective prompt
rules and their scripted regressions are recorded in
`docs/benchmark-expectations.md`.

---

## Known limitations and blocked production validation

- **Blocked — live IOMETE validation**: running the production integration
  suite and Docker smoke tests requires deployment credentials/Docker access
  not available in this checkout. In production mode the app boots, but runs
  fail closed at table resolution with a typed, safe error until the IOMETE
  adapter is deployed and validated.
- **Live Investigator judgment is not CI-provable**: scripted regressions
  prove the prompts carry the tool-order/materiality rules and that the real
  agent loop supports them; actual model adherence and interpretation quality
  need live scenarios in a configured environment.
- **Cloud model adapter is minimal by design**: no streaming, no pre-request
  token counting, no multimodal input; structured output relies on the
  tool-output fallback because Ollama Cloud does not enforce JSON schemas.
- **MVP runtime shape**: single FastAPI process, in-process async tasks,
  SQLite store, bounded concurrency (`STA_MAX_CONCURRENT_RUNS`, default 2) —
  no distributed runtime.
- **Workload analysis is disabled** by design (MVP); reports state this
  limitation once.
- The `QueryResult.schema` Pydantic warning is architecture-accepted, not a
  defect.