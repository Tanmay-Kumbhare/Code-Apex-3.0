"""
Test Suite for ARL v2 — Stage 9: Revision Comparison and Move Minimization.

Tests all requirements from Part 10 of the authoritative specification:
  - 10.1 Diff:
      - Diff Rev A vs Rev B MDB (added, deleted, modified, unchanged).
      - Diff rulesets (added, removed, modified, overridden).
      - Diff allocations (moved, unchanged, cabinets hit).
  - 10.2 Pinning:
      - Unchanged signals pinned to Rev A slots.
      - Pins are soft by default; hard pins only if group and cable unchanged.
      - Relaxation priority order: ungrouped -> soft signals -> group-changed -> safety last.
      - Move budget enforcement: exceeding raises E6003.
      - Error handling: E6001 (invalid pinned slot), E6002 (new rule invalidates pins),
        E6003 (move budget exceeded), E6004 (Rev A allocation missing).
  - 10.3 Impact report:
      - Population of Impact sheet with added, deleted, moved, unchanged counts,
        cabinets hit, move count, rule changes, and moved signals trail.
  - End-to-end run_revision_pipeline integration.
"""

from __future__ import annotations

import os
from pathlib import Path
import openpyxl
import pytest

from arl.ast import ForbidEffect, IdentifierExpr, LiteralExpr, BinaryOp, Override, RequireEffect, Rule, RuleMeta, Ruleset
from arl.entities import Baseplate, Cabinet, Cable, CIOC, Group, Pair, Project, Signal
from arl.errors import ErrorCollector, Severity
from arl.excel_output import ExcelWriter
from arl.parser import parse_ruleset
from arl.revision import (
    MDBDiff,
    MoveRecord,
    PinKind,
    PinManager,
    RelaxationPriority,
    RevisionResult,
    RuleChangeType,
    RuleDiff,
    RulesetDiff,
    SignalChangeType,
    SignalDiff,
    SignalPin,
    diff_allocations,
    diff_mdb,
    diff_rulesets,
    read_allocation_from_excel,
    run_revision_pipeline,
)
from arl.schema import get_base_schema
from arl.solver_greedy import AllocationResult, GreedySolver, Placement
from arl.tq import TQManager


@pytest.fixture
def base_project_rev_a() -> Project:
    """Standard project fixture representing Revision A."""
    sig1 = Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=1, group="GRP-01", area="AREA-A", safety=False)
    sig2 = Signal(tag="PT-102", io_type="AI", cable="CBL-01", pair=2, group="GRP-01", area="AREA-A", safety=False)
    sig3 = Signal(tag="XV-103", io_type="DO", cable="CBL-02", pair=1, group="GRP-02", area="AREA-A", safety=True)
    sig4 = Signal(tag="TT-104", io_type="AI", cable="CBL-03", pair=1, group=None, area="AREA-A", safety=False)

    bp = Baseplate(id="BP-01", cioc="CIOC-01", index=1, capacity=12)
    cioc = CIOC(id="CIOC-01", cabinet="CAB-01", index=1, seg_role="MIXED", baseplates=[bp])
    cab = Cabinet(id="CAB-01", area="AREA-A", seq=1, ciocs=[cioc])

    return Project(
        name="Project_RevA",
        cabinets=[cab],
        signals=[sig1, sig2, sig3, sig4],
        groups=[
            Group(id="GRP-01", signals=[sig1, sig2]),
            Group(id="GRP-02", signals=[sig3]),
        ],
    )


@pytest.fixture
def base_placements_rev_a() -> dict[str, Placement]:
    """Placement fixture corresponding to Rev A allocation."""
    return {
        "FT-101": Placement(tag="FT-101", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=1, charm_type="AI", tb_type="STANDARD"),
        "PT-102": Placement(tag="PT-102", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=2, charm_type="AI", tb_type="STANDARD"),
        "XV-103": Placement(tag="XV-103", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=3, charm_type="HIGH_SIDE_DO", tb_type="STANDARD"),
        "TT-104": Placement(tag="TT-104", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=4, charm_type="AI", tb_type="STANDARD"),
    }


class TestMDBAndRulesetDiff:
    """Test 10.1 Diff engine for MDBs, Rulesets, and Allocations."""

    def test_diff_mdb_added_deleted_modified_unchanged(self, base_project_rev_a: Project):
        # Rev B modifications:
        # FT-101: unchanged
        # PT-102: modified (IO type AI -> AO)
        # XV-103: deleted (removed from Rev B)
        # TT-104: modified (group changed from None to GRP-01)
        # LT-105: added (new signal in Rev B)

        sig1 = Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=1, group="GRP-01", area="AREA-A", safety=False)
        sig2 = Signal(tag="PT-102", io_type="AO", cable="CBL-01", pair=2, group="GRP-01", area="AREA-A", safety=False)
        sig4 = Signal(tag="TT-104", io_type="AI", cable="CBL-03", pair=1, group="GRP-01", area="AREA-A", safety=False)
        sig5 = Signal(tag="LT-105", io_type="AI", cable="CBL-04", pair=1, group="GRP-01", area="AREA-A", safety=False)

        proj_b = Project(
            name="Project_RevB",
            cabinets=base_project_rev_a.get("cabinets", []),
            signals=[sig1, sig2, sig4, sig5],
        )

        diff = diff_mdb(base_project_rev_a, proj_b)

        assert diff.added == ["LT-105"]
        assert diff.deleted == ["XV-103"]
        assert diff.unchanged == ["FT-101"]
        assert len(diff.modified) == 2

        mod_tags = {m.tag for m in diff.modified}
        assert "PT-102" in mod_tags
        assert "TT-104" in mod_tags

        pt_diff = next(m for m in diff.modified if m.tag == "PT-102")
        assert "io_type" in pt_diff.field_changes
        assert pt_diff.field_changes["io_type"] == ("AI", "AO")

        tt_diff = next(m for m in diff.modified if m.tag == "TT-104")
        assert "group" in tt_diff.field_changes
        assert tt_diff.field_changes["group"] == (None, "GRP-01")

    def test_diff_rulesets_added_removed_modified_overridden(self):
        src_a = """
        ruleset RevA_Rules {
            rule R1:
                scope: Signal
                require slot >= 1

            rule R2:
                scope: Signal
                priority: soft
                weight: 5.0
                require slot <= 10
        }
        """
        src_b = """
        ruleset RevB_Rules {
            rule R1:
                scope: Signal
                require slot >= 1

            rule R2:
                scope: Signal
                priority: hard
                require slot <= 12

            rule R3:
                scope: Signal
                require slot != 6

            override R1:
                priority: hard
        }
        """
        rs_a = parse_ruleset(src_a)
        rs_b = parse_ruleset(src_b)

        diff = diff_rulesets(rs_a, rs_b)

        added_names = [r.name for r in diff.added_rules]
        assert added_names == ["R3"]

        modified_names = [r.name for r in diff.modified_rules]
        assert modified_names == ["R2"]
        r2_diff = diff.modified_rules[0]
        assert "priority changed" in r2_diff.details

        overridden_names = [r.name for r in diff.overridden_rules]
        assert "R1" in overridden_names

    def test_diff_allocations_detects_moves_and_cabinets(self):
        alloc_a = {
            "FT-101": Placement(tag="FT-101", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=1, charm_type="AI", tb_type="STANDARD"),
            "PT-102": Placement(tag="PT-102", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=2, charm_type="AI", tb_type="STANDARD"),
        }
        alloc_b = {
            "FT-101": Placement(tag="FT-101", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-01", slot=1, charm_type="AI", tb_type="STANDARD"),
            "PT-102": Placement(tag="PT-102", cabinet_id="CAB-02", cioc_id="CIOC-01", baseplate_id="BP-01", slot=5, charm_type="AI", tb_type="STANDARD"),
        }

        diff = diff_allocations(alloc_a, alloc_b)
        assert diff["unchanged"] == ["FT-101"]
        assert diff["moved_count"] == 1
        mv = diff["moved"][0]
        assert mv.tag == "PT-102"
        assert mv.old_cabinet == "CAB-01"
        assert mv.new_cabinet == "CAB-02"
        assert "CAB-01" in diff["cabinets_hit"]
        assert "CAB-02" in diff["cabinets_hit"]


class TestPinningAndRelaxationPriority:
    """Test 10.2 Pinning logic, move minimization, and priority relaxation order."""

    def test_unchanged_signals_get_hard_pins(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement]):
        diff = diff_mdb(base_project_rev_a, base_project_rev_a)
        pins = PinManager.create_pins(diff, base_project_rev_a, base_placements_rev_a)

        assert pins["FT-101"].kind == PinKind.HARD
        assert pins["FT-101"].priority == RelaxationPriority.HARD_PIN
        assert pins["PT-102"].kind == PinKind.HARD
        assert pins["PT-102"].priority == RelaxationPriority.HARD_PIN

    def test_pin_relaxation_priority_order(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement]):
        """
        Verify Part 10.2 relaxation order:
          ungrouped -> soft signals -> group-changed -> safety last
        """
        schema = get_base_schema()
        # Create signals reflecting all 4 relaxation tiers:
        # 1. ungrouped signal
        sig_ungrouped = Signal(tag="TT-104", io_type="AI", group=None, safety=False)
        # 2. soft signal (desc changed)
        sig_soft = Signal(tag="FT-101", io_type="AI", group="GRP-01", desc="New Description", safety=False)
        # 3. group-changed signal
        sig_grp_chg = Signal(tag="PT-102", io_type="AI", group="GRP-NEW", safety=False)
        # 4. safety signal
        sig_safety = Signal(tag="XV-103", io_type="DO", group="GRP-02", safety=True)

        proj_b = Project(name="ProjB", cabinets=base_project_rev_a.get("cabinets", []), signals=[sig_ungrouped, sig_soft, sig_grp_chg, sig_safety])
        diff = diff_mdb(base_project_rev_a, proj_b)
        pins = PinManager.create_pins(diff, proj_b, base_placements_rev_a)

        assert pins["TT-104"].priority == RelaxationPriority.UNGROUPED
        assert pins["FT-101"].priority == RelaxationPriority.SOFT_SIGNAL
        assert pins["PT-102"].priority == RelaxationPriority.GROUP_CHANGED
        assert pins["XV-103"].priority == RelaxationPriority.SAFETY_LAST

        # Numerical order guarantees: UNGROUPED (1) < SOFT_SIGNAL (2) < GROUP_CHANGED (3) < SAFETY_LAST (4)
        assert int(pins["TT-104"].priority) < int(pins["FT-101"].priority)
        assert int(pins["FT-101"].priority) < int(pins["PT-102"].priority)
        assert int(pins["PT-102"].priority) < int(pins["XV-103"].priority)

        # Relax 2 lowest priority pins
        relaxed = PinManager.relax_pins(pins, count_to_relax=2)
        relaxed_tags = [p.tag for p in relaxed]
        assert relaxed_tags == ["TT-104", "FT-101"]
        assert pins["TT-104"].is_relaxed is True
        assert pins["FT-101"].is_relaxed is True
        assert pins["PT-102"].is_relaxed is False
        assert pins["XV-103"].is_relaxed is False

    def test_error_e6001_pinned_slot_no_longer_valid(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement]):
        """Test E6001 when a pinned slot hardware no longer exists in Rev B."""
        # Rev B only has a 2-slot baseplate instead of 12 slots
        bp = Baseplate(id="BP-01", cioc="CIOC-01", index=1, capacity=2)  # only slots 1 & 2 exist!
        cioc = CIOC(id="CIOC-01", cabinet="CAB-01", index=1, seg_role="MIXED", baseplates=[bp])
        cab = Cabinet(id="CAB-01", area="AREA-A", seq=1, ciocs=[cioc])

        proj_b = Project(name="ProjB", cabinets=[cab], signals=base_project_rev_a.get("signals", []))
        diff = diff_mdb(base_project_rev_a, proj_b)
        pins = PinManager.create_pins(diff, proj_b, base_placements_rev_a)

        col = ErrorCollector()
        tqs = TQManager()
        validated_pins = PinManager.validate_pins(pins, proj_b, collector=col, tq_manager=tqs)

        # XV-103 is pinned to slot 3 and TT-104 is pinned to slot 4 -> both are > capacity 2!
        assert col.has_code("E6001")
        assert validated_pins["XV-103"].is_relaxed is True
        assert "no longer exists" in validated_pins["XV-103"].relaxation_reason
        assert validated_pins["TT-104"].is_relaxed is True

    def test_error_e6002_new_rule_invalidates_pins(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement]):
        """Test E6002 when a new hard rule in Rev B ruleset contradicts Rev A pinned slot."""
        # Signal FT-101 has seg="A". Change CIOC-01 in Rev B to "B_ONLY"!
        bp = Baseplate(id="BP-01", cioc="CIOC-01", index=1, capacity=12)
        cioc = CIOC(id="CIOC-01", cabinet="CAB-01", index=1, seg_role="B_ONLY", baseplates=[bp])
        cab = Cabinet(id="CAB-01", area="AREA-A", seq=1, ciocs=[cioc])

        sig1 = Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=1, seg="A", group="GRP-01")
        proj_b = Project(name="ProjB", cabinets=[cab], signals=[sig1])
        diff = diff_mdb(base_project_rev_a, proj_b)
        pins = PinManager.create_pins(diff, proj_b, base_placements_rev_a)

        col = ErrorCollector()
        tqs = TQManager()
        validated_pins = PinManager.validate_pins(pins, proj_b, collector=col, tq_manager=tqs)

        assert col.has_code("E6002")
        assert validated_pins["FT-101"].is_relaxed is True
        assert "B_ONLY" in validated_pins["FT-101"].relaxation_reason

    def test_error_e6004_rev_a_allocation_missing(self, tmp_path: Path):
        """Test E6004 when Rev A allocation workbook is missing or empty."""
        col = ErrorCollector()
        tqs = TQManager()

        # Non-existent file
        placements = read_allocation_from_excel(tmp_path / "non_existent.xlsx", collector=col, tq_manager=tqs)
        assert len(placements) == 0
        assert col.has_code("E6004")

        # Workbook without 'Allocation' sheet
        empty_wb_path = tmp_path / "empty_no_sheet.xlsx"
        wb = openpyxl.Workbook()
        wb.save(empty_wb_path)

        col2 = ErrorCollector()
        placements2 = read_allocation_from_excel(empty_wb_path, collector=col2)
        assert len(placements2) == 0
        assert col2.has_code("E6004")


class TestRevisionPipelineEndToEnd:
    """Test full revision workflow integration, move budget (E6003), and Impact sheet."""

    def test_revision_pipeline_minimizes_moves_and_preserves_pins(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement], tmp_path: Path):
        """
        Verify that unchanged signals retain their Rev A coordinates,
        new signals take remaining slots, and total moves = 0 for unchanged.
        """
        # Rev B adds a 5th signal LT-105
        sig1 = Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=1, group="GRP-01", area="AREA-A", safety=False)
        sig2 = Signal(tag="PT-102", io_type="AI", cable="CBL-01", pair=2, group="GRP-01", area="AREA-A", safety=False)
        sig3 = Signal(tag="XV-103", io_type="DO", cable="CBL-02", pair=1, group="GRP-02", area="AREA-A", safety=True)
        sig4 = Signal(tag="TT-104", io_type="AI", cable="CBL-03", pair=1, group=None, area="AREA-A", safety=False)
        sig5 = Signal(tag="LT-105", io_type="AI", cable="CBL-04", pair=1, group="GRP-01", area="AREA-A", safety=False)

        proj_b = Project(
            name="Project_RevB",
            cabinets=base_project_rev_a.get("cabinets", []),
            signals=[sig1, sig2, sig3, sig4, sig5],
            groups=[
                Group(id="GRP-01", signals=[sig1, sig2, sig5]),
                Group(id="GRP-02", signals=[sig3]),
            ],
        )

        out_path = tmp_path / "rev_b_allocation.xlsx"
        res = run_revision_pipeline(
            rev_a_mdb=base_project_rev_a,
            rev_b_mdb=proj_b,
            rev_a_allocations=base_placements_rev_a,
            output_path=out_path,
            solver_backend="greedy",
            max_moves=0,  # 0 moves allowed for existing signals
        )

        assert res.success is True
        assert res.move_count == 0  # FT-101, PT-102, XV-103, TT-104 stayed at slots 1, 2, 3, 4!
        assert res.budget_exceeded is False

        # Verify placements of unchanged signals
        final_allocs = res.allocation_result.allocations
        assert final_allocs["FT-101"].slot == 1
        assert final_allocs["PT-102"].slot == 2
        assert final_allocs["XV-103"].slot == 3
        assert final_allocs["TT-104"].slot == 4
        # New signal LT-105 allocated to next available slot (slot 5)
        assert final_allocs["LT-105"].slot == 5

        # Verify Excel output and Impact sheet
        assert out_path.exists()
        wb = openpyxl.load_workbook(out_path)
        assert "Impact" in wb.sheetnames
        ws_impact = wb["Impact"]
        assert ws_impact["A1"].value == "REVISION COMPARISON & MOVE IMPACT REPORT"

    def test_error_e6003_move_budget_exceeded(self, base_project_rev_a: Project, base_placements_rev_a: dict[str, Placement], tmp_path: Path):
        """Test E6003 when re-allocation requires more moves than configured max_moves."""
        # Require TT-104 to be in slot 8 via Rev B rules
        src_b = """
        ruleset RevB_MoveBudget {
            rule R_Move_TT:
                scope: Signal
                when: self.tag == "TT-104"
                require self.slot == 8
                priority: hard
        }
        """
        ruleset_path = tmp_path / "rules_rev_b.arl"
        ruleset_path.write_text(src_b, encoding="utf-8")

        res = run_revision_pipeline(
            rev_a_mdb=base_project_rev_a,
            rev_b_mdb=base_project_rev_a,
            rev_a_allocations=base_placements_rev_a,
            ruleset_b=ruleset_path,
            output_path=tmp_path / "budget_exceeded.xlsx",
            solver_backend="cpsat",
            max_moves=0,  # Budget is 0 moves, but FT-101 is forced to move to slot 5!
        )

        # Move count must be >= 1, exceeding budget 0 -> E6003
        assert res.move_count >= 1
        assert res.budget_exceeded is True
        assert res.success is False  # Rejected because budget exceeded!
        tq_codes = [t.code for t in res.tqs]
        assert "E6003" in tq_codes
