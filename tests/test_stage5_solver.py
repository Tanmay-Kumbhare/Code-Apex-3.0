"""
ARL Stage 5 Test Suite: Greedy Solver, TQ Framework, Data Quality, and Independent Validation.

Tests cover:
  1. Clean allocation fixture with >= 90% (100%) success rate.
  2. Full capacity overflow with unplaced signals covered by structured TQs.
  3. Data quality checks E4001-E4010 detection and TQ creation.
  4. CIOC Segregation (A_ONLY, B_ONLY) enforcement and validation.
  5. Injected hard-rule violation detection and allocation rejection by Validator.
  6. Determinism property test across repeated executions.
"""

import pytest

from arl import (
    AllocationResult,
    Baseplate,
    Cabinet,
    Cable,
    CIOC,
    GreedySolver,
    Group,
    Pair,
    Placement,
    Project,
    Signal,
    TQManager,
    Validator,
    check_data_quality,
    parse_ruleset,
)


def _make_clean_project(num_signals: int = 18) -> Project:
    """Helper to construct a deterministic valid project."""
    cables = [
        Cable(
            id=f"CBL-{i:02d}",
            size=6,
            pairs=[Pair(index=j, cable=f"CBL-{i:02d}", wired_spare=False) for j in range(1, 7)],
        )
        for i in range(1, 4)
    ]

    io_types = ["AI", "AO", "DI", "DO", "DO_SOV"]
    signals = []
    for i in range(num_signals):
        cbl_idx = (i // 6) + 1
        p_idx = (i % 6) + 1
        signals.append(
            Signal(
                tag=f"FT-{100 + i:03d}",
                io_type=io_types[i % len(io_types)],
                cable=f"CBL-{cbl_idx:02d}",
                pair=p_idx,
                seg="A" if i % 2 == 0 else "B",
                area="AREA_01",
            )
        )

    baseplates = [
        Baseplate(id=f"BP-{i}", index=i, capacity=12)
        for i in range(1, 3)  # 2 baseplates * 12 = 24 slots total
    ]

    ciocs = [
        CIOC(id="CIOC-01", index=1, seg_role="MIXED", baseplates=baseplates)
    ]

    cabinets = [
        Cabinet(id="CAB-01", area="AREA_01", seq=1, ciocs=ciocs)
    ]

    groups = [
        Group(id="GRP-01", signals=signals)
    ]

    return Project(
        name="Clean_Benchmark_Project",
        cabinets=cabinets,
        signals=signals,
        cables=cables,
        groups=groups,
    )


class TestGreedySolver:
    """Test deterministic greedy allocator."""

    def test_clean_allocation_success(self):
        """Clean project achieves 100% allocation success (satisfies >= 90% benchmark)."""
        project = _make_clean_project(num_signals=18)
        solver = GreedySolver()
        result = solver.solve(project)

        assert result.success is True
        assert len(result.unplaced_signals) == 0
        assert len(result.allocations) == 18
        assert result.stats["success_rate"] == 100.0
        assert result.stats["spare_slots"] == 6  # 24 total - 18 placed

        # Validate with independent validator
        validator = Validator()
        val_result = validator.validate(project, result)
        assert val_result.valid is True
        assert len(val_result.violations) == 0
        assert val_result.checks_run > 0

    def test_capacity_overflow_with_tq(self):
        """When signals exceed hardware capacity, allocator records unplaced signals with TQ."""
        # Project with 1 baseplate (12 slots) and 16 signals
        project = _make_clean_project(num_signals=16)
        # Restrict project to only 1 baseplate of capacity 12
        bp = Baseplate(id="BP-01", index=1, capacity=12)
        cioc = CIOC(id="CIOC-01", index=1, seg_role="MIXED", baseplates=[bp])
        cab = Cabinet(id="CAB-01", area="AREA_01", seq=1, ciocs=[cioc])
        project["cabinets"] = [cab]

        solver = GreedySolver()
        result = solver.solve(project)

        assert result.success is False
        assert len(result.allocations) == 12
        assert len(result.unplaced_signals) == 4
        assert len(result.tqs) == 4

        # Every unplaced signal must have a corresponding TQ with E4007
        unplaced_set = set(result.unplaced_signals)
        for tq in result.tqs:
            assert tq.code == "E4007"
            assert any(s in unplaced_set for s in tq.affected["signals"])

        # Validator verifies that every unplaced signal is covered by a TQ
        validator = Validator()
        val_result = validator.validate(project, result)
        assert val_result.valid is True  # Slot bounds and uniqueness are valid; unplaced have TQs

    def test_cioc_segregation(self):
        """Seg A and Seg B signals are segregated into A_ONLY and B_ONLY CIOCs."""
        sig_a1 = Signal(tag="AI-001", io_type="AI", cable="CBL-01", pair=1, seg="A")
        sig_a2 = Signal(tag="AI-002", io_type="AI", cable="CBL-01", pair=2, seg="A")
        sig_b1 = Signal(tag="BI-001", io_type="DI", cable="CBL-02", pair=1, seg="B")
        sig_b2 = Signal(tag="BI-002", io_type="DI", cable="CBL-02", pair=2, seg="B")

        bp_a = Baseplate(id="BP-A", index=1, capacity=12)
        cioc_a = CIOC(id="CIOC-A", index=1, seg_role="A_ONLY", baseplates=[bp_a])

        bp_b = Baseplate(id="BP-B", index=1, capacity=12)
        cioc_b = CIOC(id="CIOC-B", index=2, seg_role="B_ONLY", baseplates=[bp_b])

        cab = Cabinet(id="CAB-01", seq=1, ciocs=[cioc_a, cioc_b])
        project = Project(
            name="Seg_Project",
            cabinets=[cab],
            signals=[sig_a1, sig_a2, sig_b1, sig_b2],
        )

        solver = GreedySolver()
        result = solver.solve(project)
        assert result.success is True

        assert result.allocations["AI-001"].cioc_id == "CIOC-A"
        assert result.allocations["AI-002"].cioc_id == "CIOC-A"
        assert result.allocations["BI-001"].cioc_id == "CIOC-B"
        assert result.allocations["BI-002"].cioc_id == "CIOC-B"

        validator = Validator()
        val_result = validator.validate(project, result)
        assert val_result.valid is True

    def test_solver_determinism(self):
        """Greedy solver output must be strictly deterministic across repeated runs."""
        project = _make_clean_project(num_signals=15)

        run1 = GreedySolver().solve(project)
        run2 = GreedySolver().solve(project)
        run3 = GreedySolver().solve(project)

        assert run1.allocations.keys() == run2.allocations.keys() == run3.allocations.keys()
        for tag in run1.allocations:
            p1 = run1.allocations[tag]
            p2 = run2.allocations[tag]
            p3 = run3.allocations[tag]
            assert (p1.cabinet_id, p1.cioc_id, p1.baseplate_id, p1.slot) == (p2.cabinet_id, p2.cioc_id, p2.baseplate_id, p2.slot)
            assert (p1.cabinet_id, p1.cioc_id, p1.baseplate_id, p1.slot) == (p3.cabinet_id, p3.cioc_id, p3.baseplate_id, p3.slot)


class TestDataQuality:
    """Test data quality checks E4001-E4010."""

    def test_duplicate_signal_tags(self):
        """E4009 detected for duplicate signal tags with TQ generated."""
        project = Project(
            name="Dup_Test",
            signals=[
                Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=1),
                Signal(tag="FT-101", io_type="AI", cable="CBL-01", pair=2),
            ],
            cables=[Cable(id="CBL-01", size=6, pairs=[Pair(index=1, cable="CBL-01"), Pair(index=2, cable="CBL-01")])],
        )
        tq_mgr = TQManager()
        passed = check_data_quality(project, tq_manager=tq_mgr)
        assert passed is False
        assert any(t.code == "E4009" for t in tq_mgr.get_all())

    def test_invalid_cable_size(self):
        """E4002 detected for non-standard cable size."""
        project = Project(
            name="Cable_Test",
            signals=[],
            cables=[Cable(id="CBL-01", size=7)],  # 7 is not 2, 6, 12, or 24
        )
        passed = check_data_quality(project)
        assert passed is False

    def test_group_seg_mismatch(self):
        """E4005 detected when group contains both Seg A and Seg B signals."""
        sig_a = Signal(tag="S-A", io_type="AI", cable="CBL-01", pair=1, seg="A")
        sig_b = Signal(tag="S-B", io_type="AI", cable="CBL-01", pair=2, seg="B")
        group = Group(id="GRP-MISMATCH", signals=[sig_a, sig_b])
        project = Project(name="Grp_Test", signals=[sig_a, sig_b], groups=[group])

        tq_mgr = TQManager()
        passed = check_data_quality(project, tq_manager=tq_mgr)
        assert passed is False
        assert any(t.code == "E4005" for t in tq_mgr.get_all())


class TestIndependentValidator:
    """Test independent validation and rejection of invalid allocations."""

    def test_hard_rule_violation_rejected(self):
        """When an allocation violates an active hard rule, Validator rejects it."""
        project = _make_clean_project(num_signals=10)
        solver = GreedySolver()
        alloc_result = solver.solve(project)
        assert alloc_result.success is True

        # Define an active hard rule forbidding slot > 4
        rule_src = """
        ruleset SafetyRules {
          rule R_MaxSlot :
            scope : Signal
            require : self.slot <= 4
            meta : priority : hard
                   id : "R_MaxSlot"
                   message : "Slots above 4 are restricted"
        }
        """
        ruleset = parse_ruleset(rule_src)

        validator = Validator()
        val_result = validator.validate(project, alloc_result, ruleset=ruleset)

        # Signals placed at slots 5, 6, etc. must trigger violations and reject allocation
        assert val_result.valid is False
        assert len(val_result.violations) > 0
        assert any(v.rule_id == "R_MaxSlot" for v in val_result.violations)

    def test_slot_conflict_rejected(self):
        """Validator rejects an allocation with two signals placed in the same slot."""
        project = _make_clean_project(num_signals=2)
        # Create synthetic conflicting allocation
        allocations = {
            "FT-100": Placement(
                tag="FT-100", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-1", slot=1,
                charm_type="AI", tb_type="STANDARD"
            ),
            "FT-101": Placement(
                tag="FT-101", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-1", slot=1,  # Duplicate slot 1
                charm_type="AI", tb_type="STANDARD"
            ),
        }
        bad_result = AllocationResult(
            success=True,
            allocations=allocations,
            unplaced_signals=[],
            tqs=[],
        )

        validator = Validator()
        val_result = validator.validate(project, bad_result)
        assert val_result.valid is False
        assert any(v.rule_id == "V_SlotConflict" for v in val_result.violations)

    def test_slot_out_of_bounds_rejected(self):
        """Validator rejects an allocation with slot number out of 1..12."""
        project = _make_clean_project(num_signals=1)
        bad_result = AllocationResult(
            success=True,
            allocations={
                "FT-100": Placement(
                    tag="FT-100", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-1", slot=15,
                    charm_type="AI", tb_type="STANDARD"
                )
            },
        )
        validator = Validator()
        val_result = validator.validate(project, bad_result)
        assert val_result.valid is False
        assert any(v.rule_id == "V_SlotBounds" for v in val_result.violations)

    def test_unallocated_signal_without_tq_rejected(self):
        """Validator rejects if a signal is unallocated without any TQ."""
        project = _make_clean_project(num_signals=2)
        # Allocate only FT-100, leaving FT-101 unallocated with NO TQ
        partial_result = AllocationResult(
            success=False,
            allocations={
                "FT-100": Placement(
                    tag="FT-100", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-1", slot=1,
                    charm_type="AI", tb_type="STANDARD"
                )
            },
            unplaced_signals=[],
            tqs=[],
        )
        validator = Validator()
        val_result = validator.validate(project, partial_result)
        assert val_result.valid is False
        assert any(v.rule_id == "V_Completeness" for v in val_result.violations)

    def test_hardware_incompatibility_warning(self):
        """Validator records warning for hardware incompatibility."""
        project = Project(
            name="HW_Test",
            cabinets=[Cabinet(id="CAB-01", ciocs=[CIOC(id="CIOC-01", baseplates=[Baseplate(id="BP-1", capacity=12)])])],
            signals=[Signal(tag="FT-RELAY", io_type="DO_RELAY")],
        )
        # Expected CHARM for DO_RELAY is ISOLATED_DO, but place on AI
        result = AllocationResult(
            success=True,
            allocations={
                "FT-RELAY": Placement(
                    tag="FT-RELAY", cabinet_id="CAB-01", cioc_id="CIOC-01", baseplate_id="BP-1", slot=1,
                    charm_type="AI", tb_type="STANDARD"
                )
            },
        )
        validator = Validator()
        val_result = validator.validate(project, result)
        assert any(v.rule_id == "V_HardwareCompatibility" for v in val_result.violations)

