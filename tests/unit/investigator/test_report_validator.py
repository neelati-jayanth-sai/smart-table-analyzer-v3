"""Report schema and deterministic reference-validation tests
(Architecture.md #30-#32)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sta.knowledge import KnowledgeBase
from sta.investigator.report import (
    KNOWLEDGE_READ_EVENT,
    DesignRecommendationStatus,
    Finding,
    FindingConfidence,
    InvestigationReport,
    OverallStatus,
    ReportReferenceValidator,
    ReportValidationError,
    Severity,
    collect_knowledge_refs,
    collect_result_refs,
)
from sta.results.models import QueryResult, RunRecord
from sta.results.store import ResultStore
from sta.tools.maintenance import MAINTENANCE_CONFIG_SPEC

TABLE = "demo.sales.orders"
SNAPSHOT = "9182781280348117982"


@pytest.fixture
def corpus(tmp_path: Path) -> KnowledgeBase:
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks" / "file-sizing.md").write_text("# File sizing\n", encoding="utf-8")
    (root / "runbooks" / "partitioning.md").write_text("# Partitioning\n", encoding="utf-8")
    return KnowledgeBase(root)


def make_report(**overrides):
    fields = {
        "table": TABLE,
        "snapshot_id": SNAPSHOT,
        "overall_status": "needs_attention",
        "current_issues": [
            {
                "finding": "Small files accumulate",
                "severity": "high",
                "confidence": "likely",
                "evidence": ["R001"],
                "knowledge": ["runbooks/file-sizing.md"],
                "explanation": "Median file size is far below target.",
                "likely_cause": "High-frequency commits",
            }
        ],
        "immediate_remediation": [
            {"action": "Compact existing files", "evidence": ["R001"], "reason": "Undersized files"}
        ],
        "future_table_design": {
            "partition_spec": {
                "current": "unpartitioned",
                "recommendation": "days(created_at)",
                "status": "recommended",
                "confidence": "likely",
                "evidence": ["R002", "R000"],
                "knowledge": ["runbooks/partitioning.md"],
                "reasoning": "Temporal filtering dominates.",
                "caveats": ["Existing files keep the old spec until rewritten."],
            },
            "sort_order": {
                "current": "none",
                "status": "no_change",
                "confidence": "possible",
                "evidence": ["R000"],
                "reasoning": "No workload evidence.",
            },
            "table_properties": [
                {
                    "property": "write.target-file-size-bytes",
                    "current": "134217728",
                    "recommendation": "536870912",
                    "evidence": ["R001", "R000"],
                    "reasoning": "Target the runbook range.",
                }
            ],
        },
        "no_change_decisions": ["Sort order stays none."],
        "limitations": ["Workload analysis disabled."],
    }
    fields.update(overrides)
    return fields


def make_validator(store: ResultStore, run_id: str, knowledge=None) -> ReportReferenceValidator:
    return ReportReferenceValidator(
        store=store,
        run_id=run_id,
        table=TABLE,
        snapshot_id=SNAPSHOT,
        knowledge=knowledge if knowledge is not None else corpus_for(run_id),
    )


_CORPUS_HOLDER: dict = {}


def corpus_for(_run_id: str):
    return _CORPUS_HOLDER["kb"]


@pytest.fixture
def run_with_results(tmp_path: Path, corpus):
    _CORPUS_HOLDER["kb"] = corpus
    store = ResultStore(tmp_path / "sta.sqlite3")
    run_id = "run_test"
    store.create_run(RunRecord(run_id=run_id, table=TABLE, snapshot_id=SNAPSHOT))
    store.store_result(
        QueryResult(run_id=run_id, tool_name="get_file_layout", query_version="v1", table=TABLE)
    )
    store.store_result(
        QueryResult(run_id=run_id, tool_name="get_partition_layout", query_version="v1", table=TABLE)
    )
    # Every knowledge path the default report cites was actually read in this
    # run, recorded as the persisted safe event the validator derives reads from.
    for path in ("runbooks/file-sizing.md", "runbooks/partitioning.md"):
        store.append_event(run_id, KNOWLEDGE_READ_EVENT, {"path": path})
    return store, run_id


class TestReportSchema:
    def test_valid_report_parses_with_exact_statuses(self) -> None:
        report = InvestigationReport.model_validate(make_report())

        assert report.overall_status.value == "needs_attention"
        assert report.current_issues[0].severity is Severity.HIGH
        assert report.current_issues[0].confidence is FindingConfidence.LIKELY
        assert (
            report.future_table_design.partition_spec.status
            is DesignRecommendationStatus.RECOMMENDED
        )
        assert report.future_table_design.sort_order.status is DesignRecommendationStatus.NO_CHANGE

    def test_json_round_trip(self) -> None:
        report = InvestigationReport.model_validate(make_report())
        assert InvestigationReport.model_validate(report.model_dump(mode="json")) == report

    def test_empty_sections_default(self) -> None:
        report = InvestigationReport.model_validate(
            {
                "table": TABLE,
                "snapshot_id": None,
                "overall_status": "healthy",
                "future_table_design": {
                    "partition_spec": {
                        "current": "unpartitioned",
                        "status": "no_change",
                        "confidence": "verified",
                        "reasoning": "No evidence of problems.",
                    },
                    "sort_order": {
                        "current": "none",
                        "status": "insufficient_evidence",
                        "confidence": "inconclusive",
                        "reasoning": "Nothing measured.",
                    },
                },
            }
        )
        assert report.current_issues == []
        assert report.limitations == []
        assert report.future_table_design.table_properties == []

    def test_invalid_overall_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(make_report(overall_status="broken"))

    def test_invalid_severity_and_confidence_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(
                make_report(current_issues=[{**make_report()["current_issues"][0], "severity": "huge"}])
            )
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(
                make_report(
                    current_issues=[
                        {**make_report()["current_issues"][0], "confidence": "certain"}
                    ]
                )
            )

    def test_invalid_design_status_rejected(self) -> None:
        fields = make_report()
        fields["future_table_design"]["partition_spec"]["status"] = "mandatory"
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(fields)

    def test_required_fields_enforced(self) -> None:
        missing_finding = make_report()
        missing_finding["current_issues"][0].pop("finding")
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(missing_finding)

        missing_explanation = make_report()
        missing_explanation["current_issues"][0].pop("explanation")
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(missing_explanation)

        missing_design = make_report()
        missing_design.pop("future_table_design")
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(missing_design)

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationReport.model_validate(make_report(health_score=0.87))


class TestReferenceCollection:
    def test_collects_evidence_and_prose_refs(self) -> None:
        report = InvestigationReport.model_validate(make_report())
        refs = collect_result_refs(report)
        locations = {ref for _, ref in refs}
        assert locations == {"R000", "R001", "R002"}

    def test_collects_knowledge_refs(self) -> None:
        report = InvestigationReport.model_validate(make_report())
        paths = {path for _, path in collect_knowledge_refs(report)}
        assert paths == {"runbooks/file-sizing.md", "runbooks/partitioning.md"}

    def test_prose_knowledge_mention_is_collected(self) -> None:
        fields = make_report()
        fields["future_table_design"]["partition_spec"]["reasoning"] = (
            "See runbooks/partitioning.md for standards."
        )
        report = InvestigationReport.model_validate(fields)
        paths = {path for _, path in collect_knowledge_refs(report)}
        assert "runbooks/partitioning.md" in paths


class TestReportReferenceValidator:
    def test_valid_report_passes(self, run_with_results) -> None:
        store, run_id = run_with_results
        assert make_validator(store, run_id).validate(make_report()) == []

    def test_unknown_result_reference_fails(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["current_issues"][0]["evidence"] = ["R099"]
        errors = make_validator(store, run_id).validate(fields)
        assert any("unknown result reference 'R099'" in error for error in errors)

    def test_prose_mention_of_missing_ref_fails(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["current_issues"][0]["explanation"] = "As R004 shows, files are small."
        errors = make_validator(store, run_id).validate(fields)
        assert any("unknown result reference 'R004'" in error for error in errors)

    def test_reserved_r000_is_always_valid(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["current_issues"][0]["evidence"] = ["R000"]
        assert make_validator(store, run_id).validate(fields) == []

    def test_malformed_references_fail(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["current_issues"][0]["evidence"] = ["R1", "r001", "X001"]
        errors = make_validator(store, run_id).validate(fields)
        assert len(errors) == 3
        assert all("malformed result reference" in error for error in errors)

    def test_result_from_other_table_fails(self, tmp_path, run_with_results) -> None:
        store, run_id = run_with_results
        store.store_result(
            QueryResult(
                run_id=run_id,
                tool_name="get_file_layout",
                query_version="v1",
                table="other.catalog.other.table",
            )
        )
        report = make_report()
        report["current_issues"][0]["evidence"] = ["R003"]
        errors = make_validator(store, run_id).validate(report)
        assert any("belongs to table" in error for error in errors)

    def test_result_from_other_run_not_visible(self, run_with_results) -> None:
        store, run_id = run_with_results
        # Rxxx ids are run-scoped; a validator bound to another run never sees R001.
        other = ReportReferenceValidator(
            store=store,
            run_id="run_other",
            table=TABLE,
            snapshot_id=SNAPSHOT,
            knowledge=_CORPUS_HOLDER["kb"],
        )
        errors = other.validate(make_report())
        assert any("unknown result reference 'R001'" in error for error in errors)

    def test_table_mismatch_fails(self, run_with_results) -> None:
        store, run_id = run_with_results
        errors = make_validator(store, run_id).validate(make_report(table="other.table"))
        assert any("does not match this run's table" in error for error in errors)

    def test_snapshot_mismatch_fails(self, run_with_results) -> None:
        store, run_id = run_with_results
        errors = make_validator(store, run_id).validate(make_report(snapshot_id="42"))
        assert any("does not match this run's snapshot" in error for error in errors)

    def test_unknown_knowledge_reference_fails(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["current_issues"][0]["knowledge"] = ["runbooks/nonexistent.md"]
        errors = make_validator(store, run_id).validate(fields)
        assert any("does not exist" in error for error in errors)

    def test_dict_reports_are_accepted(self, run_with_results) -> None:
        store, run_id = run_with_results
        errors = make_validator(store, run_id).validate(make_report())
        assert errors == []

    def test_schema_invalid_report_returns_error_not_exception(self, run_with_results) -> None:
        store, run_id = run_with_results
        errors = make_validator(store, run_id).validate({"table": TABLE})
        assert len(errors) == 1
        assert "does not match the schema" in errors[0]

    def test_require_valid_raises_and_returns_report(self, run_with_results) -> None:
        store, run_id = run_with_results
        validator = make_validator(store, run_id)
        report = validator.require_valid(make_report())
        assert isinstance(report, InvestigationReport)

        with pytest.raises(ReportValidationError):
            validator.require_valid(make_report(overall_status="healthy", snapshot_id="nope"))


# ---------------------------------------------------------------------------
# knowledge-read enforcement (Architecture.md #32: a search hit is not a read)
# ---------------------------------------------------------------------------


@pytest.fixture
def run_without_reads(tmp_path: Path, corpus):
    """A run with stored measurements but no knowledge reads: the documents
    the default report cites exist in the corpus yet were never read."""
    _CORPUS_HOLDER["kb"] = corpus
    store = ResultStore(tmp_path / "unread.sqlite3")
    run_id = "run_unread"
    store.create_run(RunRecord(run_id=run_id, table=TABLE, snapshot_id=SNAPSHOT))
    store.store_result(
        QueryResult(run_id=run_id, tool_name="get_file_layout", query_version="v1", table=TABLE)
    )
    store.store_result(
        QueryResult(
            run_id=run_id, tool_name="get_partition_layout", query_version="v1", table=TABLE
        )
    )
    return store, run_id


class TestKnowledgeReadEnforcement:
    def test_unread_structured_knowledge_reference_fails(self, run_without_reads) -> None:
        store, run_id = run_without_reads
        errors = make_validator(store, run_id).validate(make_report())
        unread = [e for e in errors if "cited but never read" in e]
        assert {"runbooks/file-sizing.md", "runbooks/partitioning.md"} <= {
            path for path in unread_paths(unread)
        }

    def test_unread_prose_knowledge_mention_fails(self, run_without_reads) -> None:
        store, run_id = run_without_reads
        fields = make_report()
        fields["future_table_design"]["sort_order"]["reasoning"] = (
            "Per runbooks/partitioning.md, sorting follows partitions."
        )
        fields["future_table_design"]["sort_order"]["knowledge"] = []
        errors = make_validator(store, run_id).validate(fields)
        assert any(
            "runbooks/partitioning.md" in error and "cited but never read" in error
            for error in errors
        )

    def test_read_knowledge_event_permits_citation(self, run_without_reads) -> None:
        store, run_id = run_without_reads
        for path in ("runbooks/file-sizing.md", "runbooks/partitioning.md"):
            store.append_event(run_id, KNOWLEDGE_READ_EVENT, {"path": path})
        fields = make_report()
        # Prose mention of the now-read document is allowed too.
        fields["future_table_design"]["sort_order"]["reasoning"] = (
            "Per runbooks/partitioning.md, sorting follows partitions."
        )
        assert make_validator(store, run_id).validate(fields) == []

    def test_reading_one_path_does_not_permit_citing_another(
        self, run_without_reads
    ) -> None:
        store, run_id = run_without_reads
        store.append_event(run_id, KNOWLEDGE_READ_EVENT, {"path": "runbooks/file-sizing.md"})
        fields = make_report()
        fields["current_issues"][0]["knowledge"] = ["runbooks/partitioning.md"]
        errors = make_validator(store, run_id).validate(fields)
        assert any(
            "runbooks/partitioning.md" in error and "cited but never read" in error
            for error in errors
        )


# ---------------------------------------------------------------------------
# metadata citation typing (Architecture.md #32)
# ---------------------------------------------------------------------------


class TestMetadataCitationTyping:
    def test_partition_and_sort_current_require_r000(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["future_table_design"]["partition_spec"]["evidence"] = ["R002"]
        fields["future_table_design"]["sort_order"]["evidence"] = ["R001"]
        errors = make_validator(store, run_id).validate(fields)
        assert any("partition_spec" in error for error in errors)
        assert any("sort_order" in error for error in errors)

    def test_partition_and_sort_current_with_r000_pass(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["future_table_design"]["partition_spec"]["evidence"] = ["R002", "R000"]
        fields["future_table_design"]["sort_order"]["evidence"] = ["R001", "R000"]
        assert make_validator(store, run_id).validate(fields) == []

    def test_property_current_requires_metadata_record(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["future_table_design"]["table_properties"][0]["evidence"] = ["R001"]
        errors = make_validator(store, run_id).validate(fields)
        assert any("table_properties[0]" in error for error in errors)

    def test_property_current_with_r000_passes(self, run_with_results) -> None:
        store, run_id = run_with_results
        fields = make_report()
        fields["future_table_design"]["table_properties"][0]["evidence"] = ["R001", "R000"]
        assert make_validator(store, run_id).validate(fields) == []

    def test_property_current_with_maintenance_measurement_passes(
        self, run_with_results
    ) -> None:
        store, run_id = run_with_results
        maintenance_ref = store.store_result(
            QueryResult(
                run_id=run_id,
                tool_name=MAINTENANCE_CONFIG_SPEC.name,
                query_version=MAINTENANCE_CONFIG_SPEC.query_version,
                table=TABLE,
            )
        )
        fields = make_report()
        fields["future_table_design"]["table_properties"][0]["evidence"] = [maintenance_ref]
        assert make_validator(store, run_id).validate(fields) == []

    def test_property_without_current_needs_no_metadata_citation(
        self, run_with_results
    ) -> None:
        """A property recommendation that states no current value makes no
        startup-metadata claim, so plain measurement evidence suffices."""
        store, run_id = run_with_results
        fields = make_report()
        fields["future_table_design"]["table_properties"][0] = {
            "property": "write.target-file-size-bytes",
            "recommendation": "536870912",
            "evidence": ["R001"],
            "reasoning": "Target the runbook range.",
        }
        assert make_validator(store, run_id).validate(fields) == []


# ---------------------------------------------------------------------------
# valid reports persist
# ---------------------------------------------------------------------------


class TestValidReportPersistence:
    def test_valid_report_round_trips_through_store(self, run_with_results) -> None:
        """A validated report persists and re-reads as the identical contract."""
        store, run_id = run_with_results
        validator = make_validator(store, run_id)
        report = validator.require_valid(make_report())

        store.store_report(run_id, report)
        stored = store.get_report(run_id)

        assert stored is not None
        assert InvestigationReport.model_validate(stored) == report


def unread_paths(errors: list[str]) -> set[str]:
    """Extract the cited-but-unread paths from validator error strings."""
    return {
        part.strip(" '")
        for error in errors
        if "cited but never read" in error
        for part in [error.split("knowledge reference ", 1)[1].split(" at ", 1)[0]]
    }