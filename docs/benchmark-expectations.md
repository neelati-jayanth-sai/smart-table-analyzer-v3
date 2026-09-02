# Benchmark expectations

This document distinguishes what the deterministic benchmark tests prove from
what the live Investigator must still prove through model judgment.

## Deterministic coverage (the bench)

The local Docker Iceberg environment and the tests in
``tests/integration/test_local_benchmarks.py`` and ``tests/benchmark`` exist to
verify the *measurement layer*:

- real Iceberg tables are created with reproducible, multi-symptom layouts,
- the PyIceberg/DuckDB local backend reads only Iceberg metadata and the single
  targeted DuckDB scan for ``analyze_partition_candidate``,
- every tool returns the contract-shaped values the Investigator consumes,
- architecture invariants are enforced before any expensive work runs.

What these tests prove:

```text
file counts / sizes / record counts are correct in metadata
partition layouts and spec usage reflect the actual written files
snapshot and manifest counts match the deterministic seed
identifier-like columns are rejected by analyze_partition_candidate
only analyze_partition_candidate is a targeted data-scan tool
metadata aggregation tools never need a column-profile provider
targeted temporal candidate analysis returns real null/distinct/min/max values
```

What these tests do **not** prove:

```text
that a live Investigator will actually call the right tools in the right order
  (scripted regressions in tests/e2e prove the prompt carries the tool-order
   rules and that the real agent loop supports the required order; live
   adherence is still model judgment)
that the Investigator correctly interprets "small" as "recommend compaction"
that the Investigator recommends NO CHANGE when evidence is weak
that the Investigator distinguishes immediate remediation from future design
that the Investigator writes a report whose citations pass the report validator
that the Investigator handles model transport failures gracefully
```

Those are Investigator-judgment behaviors. They are exercised by focused unit
and integration tests around the report validator, the investigator seam, and
live-model scenarios in a properly configured environment.

## Live-run report defects and corrective prompt rules

Fresh production-style live runs exposed three concrete report defects. In
every case the tools measured correctly; the model misreasoned over correct
evidence. The fix is in the Investigator prompt/report contract and in
scripted regression tests — never in a deterministic diagnosis or
recommendation engine, and never in hardcoded conclusions. The deterministic
layer still only measures and validates references.

| Persisted run (table) | Observed defect | Corrective model rule (prompt) | Scripted regression |
|---|---|---|---|
| ``demo.orders_bad_spec`` | The runbook ``runbooks/file-sizing.md`` prescribes exactly 256–512 MiB; the report said the configured 128 MiB target "matches standards" and recommended no change | Quote curated standards exactly as written; a configured value is compliant only inside the read range — outside it is a deviation to report | prompt-content assertions (``tests/unit/investigator/test_prompt.py``) |
| ``demo.events_partition_candidate`` | Temporal column + unpartitioned layout, but the report concluded ``insufficient_evidence`` without ever measuring the column | Metadata metrics first (``get_column_metadata_metrics``); targeted ``analyze_partition_candidate`` only when metadata is insufficient; never ``insufficient_evidence`` while the metadata measurement is missing | scripted tool-order regression (``tests/e2e/test_investigator_loop_e2e.py``) |
| ``demo.orders_day_partitioned`` | Only 30 files (intentionally reasonable day spec); the model risked overreacting to locally small physical files | A small absolute file count is not material fragmentation; no remediation without the runbook's materiality criteria; prefer ``no_change`` otherwise | prompt-content assertions (``tests/unit/investigator/test_prompt.py``) |

These rules are model instructions. The scripted tests assert (a) the delivered
system/user prompts contain them and (b) the prescribed tool-selection order
flows through the real Pydantic AI agent loop, event stream and report
validator with a deterministic scripted model — no live model call is needed
for that.

## Benchmark tables

| Table | Purpose | Key deterministic assertions |
|---|---|---|
| ``demo.orders_bad_spec`` | Poor partition spec + tiny files + many snapshots/manifests | 336 hourly partitions/files, median file size < 200 KiB, one snapshot per append |
| ``demo.orders_bad_spec_caps_properties`` | Casing test: same bad spec plus deliberately inert UPPERCASE custom properties | 72 hourly partitions/files from real writes; uppercase keys preserved verbatim and surfaced in the TableContext with raw casing (inert, distinguishable from effective keys); only the lowercase metrics key is effective; measured files carry full metrics despite ``WRITE.METADATA.METRICS.DEFAULT=none`` |
| ``demo.orders_day_partitioned`` | Reasonable temporal partition + healthier layout | 30 daily partitions, 300 k rows, files > 300 KiB, sort order in use |
| ``demo.events_partition_candidate`` | Temporal column ready for targeted candidate analysis | unpartitioned, 50 k rows, DuckDB returns distinct/min/max for ``event_timestamp`` |
| ``demo.customer_orders_identifier_heavy`` | Identifier-heavy schema + identity partition on an ID | 2,000 customer partitions, candidate analysis rejected for every ID column |

### ``demo.orders_bad_spec_caps_properties`` — the casing test (truthful framing)

This table exists to test property-key **casing**, a recurring user mistake.
It stores uppercase custom property keys such as ``WRITE.TARGET-FILE-SIZE-BYTES``
alongside the deliberately bad ``hours(event_timestamp)`` partition spec.

Facts the deterministic tests pin down (``tests/benchmark``):

- Iceberg property keys are case-sensitive; engines honor only the lowercase
  writer keys. The uppercase keys are preserved byte-for-byte as inert custom
  table metadata (through PyIceberg metadata and STA's ``TableMetadata``),
  and they control nothing — no engine, no STA tool, no derived fact.
- STA's curated relevant-properties allowlist recognizes known keys
  case-insensitively but preserves their raw casing, so the compact
  TableContext (and R000 full schema) surface the inert uppercase keys
  verbatim next to the effective lowercase key
  (``write.metadata.metrics.default=full``). Neither key can masquerade as
  the other: only the exact lowercase key drives derived facts such as
  metrics availability, and the prompt tells the Investigator that
  property-key casing is significant.
- The measured layout (72 hourly files/snapshots, tiny files, full per-file
  metrics) comes from the real writes under the engine defaults plus the
  effective lowercase metrics key — it is reported factually regardless of
  what the inert keys claim.

The table and its docs must never imply uppercase property keys control
Iceberg behavior; they demonstrate the opposite.

## Running the benchmark

```bash
python scripts/reset_local.py      # docker compose down/up + re-seed
pytest tests/integration/test_local_benchmarks.py tests/benchmark -v
```

## Live Investigator judgment

After the deterministic bench passes, the live scenarios (not run in CI without
a configured model) are:

1. Analyze ``demo.orders_bad_spec`` and expect a report that cites file-layout
   and partition-layout measurements and separates "compact now" from
   "partition by day for future writes". The writer target (128 MiB) must be
   reported as **outside** the runbook's exact 256–512 MiB range — as a
   deviation and the justification for a target-size recommendation — never as
   compliant; the runbook's numbers must be quoted exactly.
2. Analyze ``demo.orders_day_partitioned`` and expect either ``NO CHANGE`` or
   very conservative recommendations because the layout is already reasonable:
   30 files is a small absolute count, not material fragmentation, so no
   remediation without the runbook's materiality criteria being met.
3. Analyze ``demo.events_partition_candidate`` and expect the Investigator to
   measure the temporal column with ``get_column_metadata_metrics`` first and
   use ``analyze_partition_candidate`` only if that metadata is insufficient —
   not an identifier column, and not an ``insufficient_evidence`` conclusion
   before the metadata measurement exists.
4. Analyze ``demo.customer_orders_identifier_heavy`` and expect the
   Investigator to reject identifier partition candidates and to recommend
   dropping the identity-on-customer_id spec rather than scanning IDs.

A failing live scenario feeds back into either the deterministic contract (if a
tool returned wrong facts), the report validator (if a citation rule was
violated), or the prompt/knowledge (if the model misread correct evidence).
