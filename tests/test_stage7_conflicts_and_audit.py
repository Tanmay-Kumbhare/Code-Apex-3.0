"""
ARL Stage 7 Test Suite: Error Framework, Static Conflicts, Run Report, and JSONL Audit.

Tests:
  1. Static Conflict Detection: E3001 (disjoint slot intervals & unsat cores).
  2. Static Conflict Detection: E3002 (hard rule contradicts assignment).
  3. Static Conflict Detection: E3005 (hard rule depends on soft rule).
  4. Exit Gate: Fatal E3xxx strictly prevents the solver from running (solver_invoked=False).
  5. Structured Run Report conforming to Part 6.3 schema and quarantine accounting.
  6. Append-Only JSONL audit event logging and provenance verification.
"""

from pathlib import Path
import tempfile
import pandas as pd
import pytest

from arl import (
    AuditEvent,
    ConflictChecker,
    ErrorCollector,
    EventLogger,
    GreedySolver,
    QuarantinedRuleInfo,
    RunReport,
    build_run_report,
    create_default_template,
    parse_ruleset,
    read_excel_mdb,
    run_allocation_pipeline,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _create_sample_excel(file_path: Path):
    df = pd.DataFrame([
        ["10-FT-101", "AI", "CBL-01", 1, "A", "AREA_01"],
        ["10-PT-102", "AI", "CBL-01", 2, "A", "AREA_01"],
    ], columns=["Tag Name", "I/O Type", "Cable No", "Pair #", "Channel Seg", "Plant Area"])
    df.to_excel(file_path, index=False)
    return file_path


class TestStaticConflictDetection:
    """Test static conflict checker between lowering and solving."""

    def test_e3001_disjoint_slot_intervals_unsat_core(self):
        """E3001 detected when two hard rules require disjoint slot ranges on the same scope."""
        src = """
        ruleset ConflictRules {
          rule R_LowSlots:
            scope: Signal
            require self.slot <= 4
            priority: hard

          rule R_HighSlots:
            scope: Signal
            require self.slot >= 8
            priority: hard
        }
        """
        ruleset = parse_ruleset(src)
        col = ErrorCollector()
        checker = ConflictChecker(collector=col)
        result = checker.check(ruleset)

        assert result.has_conflicts is True
        assert result.can_solve is False  # HARD GATE
        assert any(e.code == "E3001" for e in result.conflicts)
        assert len(result.unsat_cores) > 0
        assert set(result.unsat_cores[0]) == {"R_LowSlots", "R_HighSlots"}

    def test_e3002_hard_rule_contradicts_assignment(self):
        """E3002 detected when a hard rule contradicts an assignment on the same field."""
        src = """
        ruleset AssignmentContradiction {
          rule R_AssignSlot:
            scope: Signal
            assign slot = 2

          rule R_ForbidSlotTwo:
            scope: Signal
            require self.slot > 5
            priority: hard
        }
        """
        ruleset = parse_ruleset(src)
        col = ErrorCollector()
        checker = ConflictChecker(collector=col)
        result = checker.check(ruleset)

        assert result.has_conflicts is True
        assert result.can_solve is False  # HARD GATE
        assert any(e.code == "E3002" for e in result.conflicts)
        assert len(result.unsat_cores) > 0
        assert set(result.unsat_cores[0]) == {"R_AssignSlot", "R_ForbidSlotTwo"}

    def test_e3005_hard_rule_references_soft_rule(self):
        """E3005 detected when a hard rule depends on a soft-only rule."""
        src = """
        ruleset DependencyPriorityMismatch {
          rule R_SoftBase:
            scope: Signal
            require self.area == "AREA_01"
            priority: soft

          rule R_HardDependent:
            scope: Signal
            require rule R_SoftBase
            priority: hard
        }
        """
        ruleset = parse_ruleset(src)
        col = ErrorCollector()
        checker = ConflictChecker(collector=col)
        result = checker.check(ruleset)

        assert result.has_conflicts is True
        assert result.can_solve is False  # HARD GATE
        assert any(e.code == "E3005" for e in result.conflicts)


class TestHardGateBlocking:
    """Exit Gate requirement: Fatal E3xxx strictly prevents solver execution."""

    def test_fatal_e3xxx_blocks_solver(self, tmp_dir):
        """
        When a ruleset contains a fatal E3001 contradiction,
        run_allocation_pipeline blocks solver execution (solver_invoked=False).
        """
        mdb_file = tmp_dir / "input_mdb.xlsx"
        _create_sample_excel(mdb_file)

        rule_file = tmp_dir / "conflict_rules.arl"
        rule_file.write_text("""
        ruleset StrictConflict {
          rule R_A:
            scope: Signal
            require self.slot <= 3
            priority: hard

          rule R_B:
            scope: Signal
            require self.slot >= 9
            priority: hard
        }
        """)

        report_file = tmp_dir / "run_report.json"
        audit_file = tmp_dir / "events.jsonl"

        pipeline_res = run_allocation_pipeline(
            mdb_paths=mdb_file,
            ruleset_path=rule_file,
            audit_log_path=audit_file,
            report_path=report_file,
        )

        # 1. Pipeline failed due to conflict
        assert pipeline_res.success is False

        # 2. PROOF THAT SOLVER WAS NOT INVOKED
        assert pipeline_res.solver_invoked is False
        assert pipeline_res.placed_signals == 0
        assert pipeline_res.unplaced_signals == 2

        # 3. Report reflects 'rejected' status and conflict details
        assert pipeline_res.run_report is not None
        assert pipeline_res.run_report.status == "rejected"
        assert report_file.exists()

        # 4. Audit log records the blocked solver event
        assert audit_file.exists()
        events = EventLogger.read_jsonl(audit_file)
        assert any(e.get("event_type") == "solver_blocked" for e in events)


class TestRunReportSchema:
    """Test Part 6.3 structured run report schema and quarantine details."""

    def test_run_report_structure_and_quarantine(self):
        """Build run report and verify compliance with Part 6.3 schema."""
        src = """
        ruleset ReportSample {
          rule R_Valid :
            scope : Signal
            require : self.slot >= 1

          rule R_Broken :
            scope : Signal
            require : self.unknown_field == 123
        }
        """
        col = ErrorCollector()
        col.add_error(
            code="E2002",
            message="Unknown field 'unknown_field' on entity 'Signal'",
            rule_id="R_Broken",
            phase="typecheck",
        )

        quarantine_info = [
            QuarantinedRuleInfo(
                rule_name="R_Broken",
                rule_id="R_Broken",
                code="E2002",
                message="Unknown field 'unknown_field'",
                phase="typecheck",
            )
        ]

        ruleset = parse_ruleset(src)
        report = build_run_report(
            ruleset=ruleset,
            collector=col,
            quarantined_rules=quarantine_info,
        )

        rep_dict = report.to_dict()
        assert rep_dict["ruleset"] == "ReportSample"
        assert rep_dict["version"] == "2.1.0"
        assert rep_dict["status"] in ("failed", "compiled_with_warnings")
        assert rep_dict["summary"]["rules_total"] == 2
        assert rep_dict["summary"]["rules_quarantined"] == 1
        assert rep_dict["summary"]["rules_active"] == 1
        assert len(rep_dict["quarantine"]) == 1
        assert rep_dict["quarantine"][0]["rule_name"] == "R_Broken"


class TestAuditLogger:
    """Test append-only JSONL event logger and provenance tracking."""

    def test_audit_event_logging_and_reopening(self, tmp_dir):
        """Log lifecycle events and verify append-only serialization and deserialization."""
        log_file = tmp_dir / "audit_events.jsonl"
        logger = EventLogger(run_id="test_run_12345", log_file=log_file)

        logger.log(phase="parse", event_type="phase_start", outcome="success")
        logger.log(
            phase="typecheck",
            event_type="rule_decision",
            rule_id="R_Seg_A",
            outcome="success",
            details={"scope": "Signal", "priority": "hard"},
        )
        logger.log(
            phase="solve",
            event_type="solver_attempt",
            outcome="partial",
            details={"placed": 12, "unplaced": 2},
        )
        logger.log(
            phase="report",
            event_type="phase_end",
            outcome="success",
        )

        assert log_file.exists()
        events = EventLogger.read_jsonl(log_file)
        assert len(events) == 4

        # Verify event schema and deterministic structure
        assert events[0]["run_id"] == "test_run_12345"
        assert events[0]["phase"] == "parse"
        assert events[1]["rule_id"] == "R_Seg_A"
        assert events[2]["details"]["placed"] == 12
        assert "timestamp" in events[0]
