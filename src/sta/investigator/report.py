"""Investigation report contract (Architecture.md #30-#32).

The report schema mirrors the report contract exactly: overall status, current
issues (findings with evidence), immediate remediation, future table design
(partition spec / sort order / table properties), no-change decisions and
limitations. Statuses are closed enumerations.

:class:`ReportReferenceValidator` performs only the deterministic checks code
can reliably perform — every referenced Rxxx exists for this run/table,
snapshot references are consistent, knowledge references exist and were
actually read in this run (derived from persisted safe events), sections
stating startup metadata facts cite the record that carries them, and required
fields validate against the schema. It never judges whether the model's
reasoning is correct (Architecture.md #32).
"""

import re
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from sta.tools.maintenance import MAINTENANCE_CONFIG_SPEC

# Rxxx result references: three or more digits (R000 is the reserved
# full-schema pseudo-result of the run, R001+ are stored measurements).
RESULT_REF_PATTERN = re.compile(r"^R\d{3,}$")
# Rxxx mentions inside free text (explanations, reasoning, caveats).
RESULT_REF_SCAN_PATTERN = re.compile(r"\bR\d{3,}\b")
# Knowledge references in free text: must carry a directory segment to avoid
# false positives on bare file names.
KNOWLEDGE_REF_SCAN_PATTERN = re.compile(r"\b[\w-]+/[\w-]+(?:/[\w.-]+)*\.md\b")

# Persisted safe event (Runtime_Environments_UI.md #27) emitted by the
# Investigator's successful read_knowledge tool call. It is the only accepted
# proof that a cited knowledge path was actually read in this run; a search
# hit never counts as a read.
KNOWLEDGE_READ_EVENT = "knowledge_read"

# The one measurement tool that reports effective maintenance/table-property
# configuration (Architecture.md #28: prefer effective configuration evidence).
# Besides R000 it is the only legitimate stored source of a current property
# value. Taken from the registry spec so the name cannot drift.
MAINTENANCE_CONFIG_TOOL = MAINTENANCE_CONFIG_SPEC.name


class FindingConfidence(str, Enum):
    """Finding confidence vocabulary (Architecture.md #31)."""

    VERIFIED = "verified"
    LIKELY = "likely"
    POSSIBLE = "possible"
    INCONCLUSIVE = "inconclusive"


class Severity(str, Enum):
    """Severity vocabulary (Architecture.md #31)."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DesignRecommendationStatus(str, Enum):
    """Design-recommendation status vocabulary (Architecture.md #31)."""

    RECOMMENDED = "recommended"
    CONSIDER = "consider"
    NO_CHANGE = "no_change"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class OverallStatus(str, Enum):
    """Report-level status for the OVERALL STATUS section. ``healthy`` reports
    a table with no material issues; ``inconclusive`` reports that the
    evidence was insufficient to decide (Architecture.md #26, #31)."""

    HEALTHY = "healthy"
    NEEDS_ATTENTION = "needs_attention"
    INCONCLUSIVE = "inconclusive"


class Finding(BaseModel):
    """One current issue. A finding references the stored results that
    support it (Architecture.md #24); knowledge references are context."""

    model_config = ConfigDict(extra="forbid")

    finding: str = Field(min_length=1)
    severity: Severity
    confidence: FindingConfidence
    evidence: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    explanation: str = Field(min_length=1)
    likely_cause: str | None = None


class RemediationAction(BaseModel):
    """One immediate-remediation action (repairs current layout/data)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class SpecRecommendation(BaseModel):
    """Partition-spec or sort-order recommendation. ``recommendation`` holds
    an Iceberg transform spec (Architecture.md #26) or ``unpartitioned``/
    ``none`` where applicable."""

    model_config = ConfigDict(extra="forbid")

    current: str = Field(min_length=1)
    recommendation: str | None = None
    status: DesignRecommendationStatus
    confidence: FindingConfidence
    evidence: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)
    caveats: list[str] = Field(default_factory=list)


class PropertyRecommendation(BaseModel):
    """One table-property recommendation. Configuration influences future
    writes only; it never retroactively rewrites existing files (§28)."""

    model_config = ConfigDict(extra="forbid")

    property: str = Field(min_length=1)
    current: str | None = None
    recommendation: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)


class FutureTableDesign(BaseModel):
    """FUTURE TABLE DESIGN section (Architecture.md #30)."""

    model_config = ConfigDict(extra="forbid")

    partition_spec: SpecRecommendation
    sort_order: SpecRecommendation
    table_properties: list[PropertyRecommendation] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    """The full report contract (Architecture.md #30)."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1)
    snapshot_id: str | None = None
    overall_status: OverallStatus
    current_issues: list[Finding] = Field(default_factory=list)
    immediate_remediation: list[RemediationAction] = Field(default_factory=list)
    future_table_design: FutureTableDesign
    no_change_decisions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResultLookup(Protocol):
    """Read/event seam over stored run results (satisfied by ResultStore).

    ``list_events`` provides the run's persisted safe events; the validator
    derives the set of knowledge paths actually read in this run from them.
    """

    def list_results(self, run_id: str) -> list[Any]: ...

    def list_events(self, run_id: str) -> list[Any]: ...


_REPORT_ADAPTER: TypeAdapter[InvestigationReport] = TypeAdapter(InvestigationReport)


class ReportValidationError(Exception):
    """Raised by ``require_valid`` when deterministic checks fail."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class ReportReferenceValidator:
    """Deterministic report-reference validation for one run.

    Checks (Architecture.md #32):

    - the report parses against the Pydantic schema (dicts are accepted),
    - report table/snapshot match this run,
    - every referenced Rxxx exists, is well-formed, and belongs to this
      run/table (R000, the reserved full-schema reference, is run-scoped by
      construction and always valid),
    - every knowledge reference exists in the curated corpus AND was actually
      read in this run, derived from the persisted safe ``knowledge_read``
      events of this run (a search hit is not a read),
    - every section stating a startup metadata fact (current partition spec,
      current sort order, current property value) cites the stored record
      that carries that fact: R000, or the IOMETE maintenance-configuration
      measurement for current property values,
    - the report cites at least one stored measurement (R001+).

    It deliberately does not judge reasoning quality: whether a stated value
    is correct is a benchmark concern, not a deterministic rule.
    """

    def __init__(
        self,
        *,
        store: ResultLookup,
        run_id: str,
        table: str,
        snapshot_id: str | None,
        knowledge,
    ) -> None:
        self._run_id = run_id
        self._table = table
        self._snapshot_id = snapshot_id
        self._store = store
        # knowledge is a KnowledgeBase or any object with known_paths().
        self._known_paths = set(knowledge.known_paths())

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def table(self) -> str:
        return self._table

    @property
    def snapshot_id(self) -> str | None:
        return self._snapshot_id

    def parse(self, report: "InvestigationReport | dict[str, Any]") -> InvestigationReport:
        """Parse a dict-shaped or model report into the schema."""
        if isinstance(report, InvestigationReport):
            return report
        return _REPORT_ADAPTER.validate_python(report)

    def validate(self, report: "InvestigationReport | dict[str, Any]") -> list[str]:
        """All deterministic errors; an empty list means the report is valid."""
        try:
            parsed = self.parse(report)
        except (ValidationError, ValueError) as exc:
            return [f"report does not match the schema: {_first_error(exc)}"]

        errors: list[str] = []
        errors.extend(self._validate_run_identity(parsed))
        stored = {result.result_id: result for result in self._store.list_results(self._run_id)}
        for location, reference in collect_result_refs(parsed):
            errors.extend(self._validate_result_ref(location, reference, stored))
        read_paths = self._read_knowledge_paths()
        for location, reference in collect_knowledge_refs(parsed):
            if reference not in self._known_paths:
                errors.append(
                    f"knowledge reference {reference!r} at {location} does not exist "
                    "in the curated knowledge corpus"
                )
            elif reference not in read_paths:
                errors.append(
                    f"knowledge reference {reference!r} at {location} was cited but never "
                    "read in this run; read it with read_knowledge before citing it"
                )
        errors.extend(self._validate_at_least_one_measurement(parsed, stored))
        errors.extend(self._validate_metadata_citations(parsed, stored))
        return errors

    def _validate_at_least_one_measurement(
        self, report: InvestigationReport, stored: dict[str, Any]
    ) -> list[str]:
        """A valid report must cite at least one stored measurement (R001+).

        R000 is the run-scoped full-schema reference, not a measurement, so a
        report that only cites R000 still has no table evidence.
        """
        measurement_refs = {
            ref
            for _, ref in collect_result_refs(report)
            if ref != "R000" and RESULT_REF_PATTERN.match(ref) and ref in stored
        }
        if not measurement_refs:
            return ["report must cite at least one stored measurement result (R001+)"]
        return []

    def require_valid(self, report: "InvestigationReport | dict[str, Any]") -> InvestigationReport:
        """Parse, validate and return the report; raises on any error."""
        errors = self.validate(report)
        if errors:
            raise ReportValidationError(errors)
        return self.parse(report)

    # -- checks -------------------------------------------------------------

    def _read_knowledge_paths(self) -> set[str]:
        """Knowledge paths actually read in this run, derived from the run's
        persisted safe events: every successful ``read_knowledge`` tool call
        appends one ``knowledge_read`` event carrying the document path
        (Runtime_Environments_UI.md #27). Events are re-read on every
        validation so model retries see reads that happened meanwhile."""
        read: set[str] = set()
        for event in self._store.list_events(self._run_id):
            if isinstance(event, dict):
                event_type, data = event.get("type"), event.get("data")
            else:
                event_type, data = getattr(event, "type", None), getattr(event, "data", None)
            if event_type != KNOWLEDGE_READ_EVENT:
                continue
            path = data.get("path") if isinstance(data, dict) else None
            if isinstance(path, str) and path:
                read.add(path)
        return read

    def _metadata_result_ids(self, stored: dict[str, Any]) -> set[str]:
        """Stored results that legitimately carry startup metadata facts: the
        reserved full-schema record R000, plus the IOMETE
        maintenance-configuration measurement where one exists (Architecture.md
        #28: prefer effective configuration evidence)."""
        ids = {"R000"}
        ids.update(
            result_id
            for result_id, result in stored.items()
            if getattr(result, "tool_name", None) == MAINTENANCE_CONFIG_TOOL
        )
        return ids

    def _validate_metadata_citations(
        self, report: InvestigationReport, stored: dict[str, Any]
    ) -> list[str]:
        """Startup metadata facts must cite the record that carries them.

        A section's ``current`` partition spec / sort order / property value is
        a startup metadata fact recorded in R000 (or measured by the IOMETE
        maintenance-configuration tool); a file/layout measurement never
        evidences it. This is citation typing only — code never judges whether
        the stated value itself is correct."""
        errors: list[str] = []
        metadata_ids = self._metadata_result_ids(stored)
        design = report.future_table_design
        for section in ("partition_spec", "sort_order"):
            recommendation: SpecRecommendation = getattr(design, section)
            if not metadata_ids.intersection(recommendation.evidence):
                errors.append(
                    f"future_table_design.{section} states the current "
                    f"{section.replace('_', ' ')} (startup metadata recorded in R000); "
                    "its evidence must include R000"
                )
        for index, prop in enumerate(design.table_properties):
            if prop.current is None:
                continue
            if not metadata_ids.intersection(prop.evidence):
                errors.append(
                    f"future_table_design.table_properties[{index}] states a current value "
                    f"for {prop.property!r} (startup metadata recorded in R000 or measured "
                    f"by {MAINTENANCE_CONFIG_TOOL}); its evidence must include R000 or "
                    "that measurement"
                )
        return errors

    def _validate_run_identity(self, report: InvestigationReport) -> list[str]:
        errors: list[str] = []
        if report.table != self._table:
            errors.append(
                f"report table {report.table!r} does not match this run's table {self._table!r}"
            )
        if report.snapshot_id != self._snapshot_id:
            errors.append(
                f"report snapshot_id {report.snapshot_id!r} does not match this "
                f"run's snapshot {self._snapshot_id!r}"
            )
        return errors

    def _validate_result_ref(self, location: str, reference: str, stored: dict) -> list[str]:
        if not RESULT_REF_PATTERN.match(reference):
            return [f"malformed result reference {reference!r} at {location}"]
        if reference == "R000":
            return []  # reserved run-scoped full-schema reference
        result = stored.get(reference)
        if result is None:
            return [f"unknown result reference {reference!r} at {location} (not stored in this run)"]
        if result.table != self._table:
            return [
                f"result reference {reference!r} at {location} belongs to table "
                f"{result.table!r}, not this run's table {self._table!r}"
            ]
        # A stored result measured on a concrete snapshot must belong to this
        # run's pinned snapshot (Architecture.md #15): evidence from another
        # table state is never valid support for this run's report. A result
        # without a snapshot is an explicitly not-pinned measurement and is
        # accepted as-is.
        if result.snapshot_id is not None and result.snapshot_id != self._snapshot_id:
            return [
                f"result reference {reference!r} at {location} was measured on "
                f"snapshot {result.snapshot_id!r}, not this run's snapshot "
                f"{self._snapshot_id!r}"
            ]
        return []


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------


def collect_result_refs(report: InvestigationReport) -> list[tuple[str, str]]:
    """Every Rxxx reference: structured evidence entries plus mentions inside
    free-text fields. Returns (location, reference) pairs, deduplicated."""
    refs: list[tuple[str, str]] = []

    def structured(location: str, entries: list[str]) -> None:
        for entry in entries:
            refs.append((location, entry))

    def scanned(location: str, text: str | None) -> None:
        if not text:
            return
        for match in RESULT_REF_SCAN_PATTERN.finditer(text):
            refs.append((location, match.group(0)))

    for index, finding in enumerate(report.current_issues):
        prefix = f"current_issues[{index}]"
        structured(f"{prefix}.evidence", finding.evidence)
        scanned(prefix, finding.finding)
        scanned(prefix, finding.explanation)
        scanned(prefix, finding.likely_cause)
    for index, action in enumerate(report.immediate_remediation):
        prefix = f"immediate_remediation[{index}]"
        structured(f"{prefix}.evidence", action.evidence)
        scanned(prefix, action.action)
        scanned(prefix, action.reason)
    design = report.future_table_design
    for section in ("partition_spec", "sort_order"):
        recommendation: SpecRecommendation = getattr(design, section)
        prefix = f"future_table_design.{section}"
        structured(f"{prefix}.evidence", recommendation.evidence)
        scanned(prefix, recommendation.current)
        scanned(prefix, recommendation.recommendation)
        scanned(prefix, recommendation.reasoning)
        for index, caveat in enumerate(recommendation.caveats):
            scanned(f"{prefix}.caveats[{index}]", caveat)
    for index, prop in enumerate(design.table_properties):
        prefix = f"future_table_design.table_properties[{index}]"
        structured(f"{prefix}.evidence", prop.evidence)
        scanned(prefix, prop.recommendation)
        scanned(prefix, prop.reasoning)
    for index, decision in enumerate(report.no_change_decisions):
        scanned(f"no_change_decisions[{index}]", decision)
    for index, limitation in enumerate(report.limitations):
        scanned(f"limitations[{index}]", limitation)
    return _dedupe(refs)


def collect_knowledge_refs(report: InvestigationReport) -> list[tuple[str, str]]:
    """Every knowledge reference: structured knowledge lists plus markdown
    path mentions inside free-text fields. Returns (location, path) pairs."""
    refs: list[tuple[str, str]] = []

    def structured(location: str, entries: list[str]) -> None:
        for entry in entries:
            refs.append((location, entry))

    def scanned(location: str, text: str | None) -> None:
        if not text:
            return
        for match in KNOWLEDGE_REF_SCAN_PATTERN.finditer(text):
            refs.append((location, match.group(0)))

    for index, finding in enumerate(report.current_issues):
        structured(f"current_issues[{index}].knowledge", finding.knowledge)
        scanned(f"current_issues[{index}]", finding.explanation)
        scanned(f"current_issues[{index}]", finding.likely_cause)
    for index, action in enumerate(report.immediate_remediation):
        structured(f"immediate_remediation[{index}].knowledge", action.knowledge)
    design = report.future_table_design
    structured("future_table_design.partition_spec.knowledge", design.partition_spec.knowledge)
    structured("future_table_design.sort_order.knowledge", design.sort_order.knowledge)
    scanned("future_table_design.partition_spec", design.partition_spec.reasoning)
    scanned("future_table_design.sort_order", design.sort_order.reasoning)
    for caveat in design.partition_spec.caveats:
        scanned("future_table_design.partition_spec.caveats", caveat)
    for caveat in design.sort_order.caveats:
        scanned("future_table_design.sort_order.caveats", caveat)
    for index, prop in enumerate(design.table_properties):
        structured(f"future_table_design.table_properties[{index}].knowledge", prop.knowledge)
    return _dedupe(refs)


def _dedupe(refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in refs:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            return f"{location}: {first.get('msg', 'invalid value')}"
    return str(exc)