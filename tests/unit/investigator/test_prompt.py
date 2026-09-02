"""Prompt regression tests for report defects observed in live runs.

The defects were model-reasoning failures, so the fix is model instruction,
not deterministic code: these tests assert the system prompt and the compact
report contract carry the corrective rules. They are pure string/registry
checks — no model call, no diagnosis engine.

Defects covered:

- orders_bad_spec: the model read the file-sizing runbook (256-512 MiB) but
  called a configured 128 MiB target "compliant" and recommended no change.
  Rules: quote curated standards exactly as written; a configured value is
  compliant only inside the read range — outside is a deviation to report.
- events_partition_candidate: the model concluded insufficient_evidence for a
  partition recommendation on a temporal column without ever measuring the
  column. Rule: metadata metrics first, targeted candidate analysis only when
  metadata is insufficient.
- events_status_partitioned: the model must evaluate one selected non-identifier
  candidate against the current bad spec without broad profiling. Rule: the
  prompt must carry the candidate evidence workflow (metadata -> targeted ->
  current-spec comparison -> supported/consider/insufficient_evidence).
- orders_day_partitioned: the model overreacted to locally small physical
  files on a table with only 30 files. Rule: a small absolute file count is
  not material fragmentation; no remediation without the runbook's
  materiality criteria.
"""

import pytest

from sta.investigator.prompt import build_report_contract, build_system_prompt
from sta.tools.registry import DEFAULT_REGISTRY

SYSTEM_PROMPT = build_system_prompt()


def temporal_candidate_rule_line() -> str:
    """The single prompt line that carries the temporal-candidate rule.

    One tool per line in the tool catalog, so the only line naming both tools
    is the rule itself — this also proves the rule names the real tools.
    """
    lines = [
        line
        for line in SYSTEM_PROMPT.splitlines()
        if "get_column_metadata_metrics" in line and "analyze_partition_candidate" in line
    ]
    assert len(lines) == 1, f"expected exactly one rule line naming both tools, got: {lines}"
    return lines[0]


class TestFaithfulStandardsRule:
    """orders_bad_spec regression: runbook values must be quoted exactly and
    compliance judged strictly against the read range."""

    def test_system_prompt_requires_exact_quotation_of_curated_values(self):
        assert "Quote curated standards faithfully" in SYSTEM_PROMPT
        assert "exactly as written" in SYSTEM_PROMPT
        assert "never paraphrase" in SYSTEM_PROMPT

    def test_system_prompt_forbids_compliance_outside_the_read_range(self):
        assert "compliant only when it falls inside the range" in SYSTEM_PROMPT
        assert "outside that range is a deviation to report" in SYSTEM_PROMPT
        assert '"compliant"' in SYSTEM_PROMPT  # the forbidden label itself

    def test_compact_contract_repeats_the_faithful_standards_rule(self):
        contract = build_report_contract()
        assert "Quote standards faithfully" in contract
        assert "outside the range is a deviation to report, not compliance" in contract


class TestTemporalCandidateRule:
    """events_partition_candidate regression: metadata metrics before the
    targeted candidate analysis for non-identifier temporal columns."""

    def test_rule_requires_metadata_metrics_first(self):
        line = temporal_candidate_rule_line()
        assert "get_column_metadata_metrics" in line
        assert "first" in line

    def test_rule_allows_targeted_analysis_only_when_metadata_is_insufficient(self):
        line = temporal_candidate_rule_line()
        assert "only when that metadata measurement is insufficient" in line

    def test_rule_forbids_insufficient_evidence_without_metadata_measurement(self):
        line = temporal_candidate_rule_line()
        assert "insufficient_evidence" in line
        assert "metadata metrics have not" in line

    def test_rule_tools_exist_in_the_registry(self):
        line = temporal_candidate_rule_line()
        assert "get_column_metadata_metrics" in DEFAULT_REGISTRY
        assert "analyze_partition_candidate" in DEFAULT_REGISTRY

    def test_rule_is_scoped_to_non_identifier_temporal_columns(self):
        line = temporal_candidate_rule_line()
        assert "non-identifier temporal column" in line


class TestFileCountMaterialityRule:
    """orders_day_partitioned regression: a small absolute file count is not
    material fragmentation; no remediation without runbook criteria."""

    def test_system_prompt_distinguishes_small_count_from_material_fragmentation(self):
        assert "small absolute file count" in SYSTEM_PROMPT
        assert "material fragmentation" in SYSTEM_PROMPT

    def test_system_prompt_requires_runbook_materiality_criteria_before_remediation(self):
        assert "materiality criteria" in SYSTEM_PROMPT

    def test_system_prompt_prefers_no_change_when_criteria_are_not_met(self):
        assert "prefer `no_change`" in SYSTEM_PROMPT


class TestCaseSensitivePropertyKeysRule:
    """orders_bad_spec_caps_properties regression: Iceberg property keys are
    case-sensitive, and inert uppercase case variants must be recognizable as
    such, never treated as effective writer configuration."""

    def test_system_prompt_states_property_keys_are_case_sensitive(self):
        assert "Iceberg property keys are case-sensitive" in SYSTEM_PROMPT
        assert "only the exact lowercase writer keys" in SYSTEM_PROMPT

    def test_system_prompt_keeps_case_variants_visible_but_inert(self):
        assert "WRITE.TARGET-FILE-SIZE-BYTES" in SYSTEM_PROMPT
        assert "inert custom metadata" in SYSTEM_PROMPT
        assert "never treat its value as an effective setting" in SYSTEM_PROMPT

    def test_system_prompt_requires_checking_the_lowercase_key_before_compliance(self):
        assert "whether the effective lowercase key is actually present" in SYSTEM_PROMPT


class TestPartitionCandidateWorkflowRule:
    """events_status_partitioned regression: the prompt must carry the full
    evidence workflow for evaluating one selected non-identifier candidate
    against the current partition spec."""

    def test_system_prompt_declares_candidate_evidence_workflow(self):
        assert "Partition-candidate evidence workflow" in SYSTEM_PROMPT
        assert "never broad profiling" in SYSTEM_PROMPT

    def test_workflow_requires_metadata_first(self):
        assert "Measure it with `get_column_metadata_metrics` first" in SYSTEM_PROMPT

    def test_workflow_allows_targeted_only_when_metadata_insufficient(self):
        assert "Run the targeted `analyze_partition_candidate` only if those metrics are" in SYSTEM_PROMPT
        assert "insufficient to judge the candidate" in SYSTEM_PROMPT

    def test_workflow_requires_current_spec_facts_for_comparison(self):
        assert "get_partition_layout" in SYSTEM_PROMPT
        assert "get_partition_spec_usage" in SYSTEM_PROMPT
        assert "factual current-spec facts" in SYSTEM_PROMPT

    def test_workflow_forbids_scoring_or_ranking(self):
        assert "do not score or rank candidates" in SYSTEM_PROMPT

    def test_workflow_uses_supported_consider_insufficient_vocabulary(self):
        assert "`supported`" in SYSTEM_PROMPT
        assert "`consider`" in SYSTEM_PROMPT
        assert "`insufficient_evidence`" in SYSTEM_PROMPT


@pytest.mark.parametrize(
    "rule_fragment",
    [
        "exactly as written",
        "compliant only when it falls inside the range",
        "small absolute file count",
        "material fragmentation",
        "materiality criteria",
        "Iceberg property keys are case-sensitive",
        "never treat its value as an effective setting",
        "Partition-candidate evidence workflow",
        "never broad profiling",
        "do not score or rank candidates",
    ],
)
def test_rule_survives_prompt_rendering(rule_fragment):
    """The rules are rendered into the final system prompt (catalog and
    contract inserted), not just present in the template constant."""
    assert rule_fragment in build_system_prompt()