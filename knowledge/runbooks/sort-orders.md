---
source_type: team_runbook
source_ref: data-platform-runbook
last_verified: 2026-08-28
trust: curated
---
# Sort-order runbook

Team standards for Iceberg sort orders.

## Standards

- Every large table should have an intentional decision about sort order, recorded either as a defined sort order or as an explicit no-change decision.
- When a sort order is justified, prefer the column(s) with stable, high-clustering value that match dominant filter patterns. Timestamp-adjacent columns and low-cardinality dimension columns are typical candidates.
- Never recommend sort order based on speculation about queries. Sort-order recommendations are conservative without workload evidence.

## Conservative default

Because workload analysis is disabled in STA, sort-order recommendations require strong justification from table evidence (for example, column metadata bounds showing naturally clustered columns) plus this runbook's standards. `insufficient_evidence` is the expected status when only generic guidance exists.

## Trade-offs to state

- Sorting increases write cost on every affected write.
- A defined sort order applies to future writes only; existing files stay as written until rewritten.
- Actual file clustering can drift from the declared sort order; sort-order usage measurements are separate from the definition.