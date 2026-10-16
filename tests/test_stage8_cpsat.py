"""
ARL Stage 8 Test Suite: OR-Tools CP-SAT Backend, Unsat Cores, and Fallback.

Tests:
  1. CP-SAT solves feasible models to optimality/feasibility.
  2. Soft rule objective optimization.
  3. CP-SAT proves infeasibility and extracts assumption-based unsat cores.
  4. Solver timeout (E5001) triggers greedy fallback with degraded=True.
  5. Invalid or unsupported IR node triggers E5005.
  6. Exit Gate:
     - Case A: CP-SAT solves the model.
     - Case B: CP-SAT proves infeasibility with an unsat core.
     - Case C: Fallback is invoked when CP-SAT fails or times out.
"""

from pathlib import Path
import tempfile
import pytest

from arl import (
    AllocationResult,
    Baseplate,
    Cabinet,
    Cable,
    CIOC,
    CPSATSolver,
    ErrorCollector,
    Group,
    Pair,
    Project,
    Signal,
    TQManager,
    UnsatCore,
    Validator,
    parse_ruleset,
    run_allocation_pipeline,
)
from arl.ir import IRNode


def _create_small_project(num_signals: int = 6) -> Project:
    """Helper to build a small valid project for CP-SAT solving."""
    signals = [
        Signal(
            tag=f"10-FT-{100 + i}",
            io_type="AI" if i % 2 == 0 else "DI",
            cable="CBL-01",
            pair=i + 1,
            seg="A",
            area="AREA_01",
        )
        for i in range(num_signals)
    ]
    bp = Baseplate(id="BP-01", index=1, capacity=12)
    cioc = CIOC(id="CIOC-01", index=1, seg_role="MIXED", baseplates=[bp])
    cab = Cabinet(id="CAB-01", area="AREA_01", seq=1, ciocs=[cioc])
    cbl = Cable(
        id="CBL-01",
        size=6,
        pairs=[Pair(index=j, cable="CBL-01", wired_spare=False) for j in range(1, 7)],
    )
    grp = Group(id="GRP-01", signals=signals, cables=[cbl])

    return Project(
        name="CPSAT_Test_Project",
        cabinets=[cab],
        signals=signals,
        cables=[cbl],
        groups=[grp],
    )


class TestCPSATSolver:
    """Test OR-Tools CP-SAT backend capabilities."""

    def test_cpsat_solves_feasible_model(self):
        """Case A (Exit Gate): CP-SAT solves a feasible allocation model."""
        project = _create_small_project(num_signals=6)
        solver = CPSATSolver(time_limit_seconds=5.0)
        result = solver.solve(project)

        assert result.success is True
        assert result.degraded is False
        assert result.stats["solver"] == "CP-SAT"
        assert len(result.allocations) == 6
        assert len(result.unplaced_signals) == 0

        # Validate with independent validator
        val_res = Validator().validate(project, result)
        assert val_res.valid is True
        assert len(val_res.violations) == 0

    def test_soft_rule_objective(self):
        """CP-SAT incorporates soft rule weights into objective maximization."""
        project = _create_small_project(num_signals=2)
        # Soft rule preferring slot == 5
        src = """
        ruleset SoftObjectiveRules {
          rule R_PreferSlotFive:
            scope: Signal
            require self.slot == 5
            priority: soft
            weight: 10.0
        }
        """
        ruleset = parse_ruleset(src)
        solver = CPSATSolver(time_limit_seconds=5.0)
        result = solver.solve(project, ruleset=ruleset)

        assert result.success is True
        # At least one signal should be placed in preferred slot 5
        placed_slots = [p.slot for p in result.allocations.values()]
        assert 5 in placed_slots

    def test_cpsat_proves_infeasibility_and_unsat_core(self):
        """Case B (Exit Gate): CP-SAT proves infeasibility and extracts unsat core."""
        project = _create_small_project(num_signals=2)

        # Contradictory hard rules on slot requirements
        src = """
        ruleset InfeasibleHardRules {
          rule R_RequireSlotOne:
            scope: Signal
            require self.slot == 1
            priority: hard

          rule R_RequireSlotTwo:
            scope: Signal
            require self.slot == 2
            priority: hard
        }
        """
        ruleset = parse_ruleset(src)
        col = ErrorCollector()
        tqs = TQManager()
        solver = CPSATSolver(
            collector=col,
            tq_manager=tqs,
            enable_fallback=False,  # Disable fallback to observe raw INFEASIBLE status
            minimization_budget=20,
        )

        result = solver.solve(project, ruleset=ruleset)

        assert result.success is False
        assert result.stats["status"] == "INFEASIBLE"
        assert solver.last_unsat_core is not None

        core = solver.last_unsat_core
        assert core.status in ("minimal", "approximate")
        assert len(core.rules) > 0
        assert any(t.code == "E3001" for t in tqs.get_all())

    def test_solver_timeout_triggers_fallback(self):
        """Case C (Exit Gate): Solver timeout triggers deterministic greedy fallback with degraded=True."""
        project = _create_small_project(num_signals=6)
        col = ErrorCollector()
        tqs = TQManager()

        # Set absurdly tiny time limit to guarantee timeout
        solver = CPSATSolver(
            collector=col,
            tq_manager=tqs,
            time_limit_seconds=0.000001,  # Forces timeout
            enable_fallback=True,
        )
        result = solver.solve(project)

        # Fallback placed the signals
        assert result.degraded is True
        assert result.stats["solver"] == "Greedy (Fallback)"
        assert any(e.code == "E5001" for e in col.errors)
        assert any(t.code == "E5001" for t in tqs.get_all())

    def test_unsupported_ir_node_e5005(self):
        """E5005 emitted when an invalid or unsupported IR node is encountered."""
        class DummyUnsupportedIRNode(IRNode):
            pass

        project = _create_small_project(num_signals=2)
        col = ErrorCollector()
        solver = CPSATSolver(
            collector=col,
            enable_fallback=False,
        )

        result = solver.solve(project, ir_nodes=[DummyUnsupportedIRNode()])
        assert result.success is False
        assert any(e.code == "E5005" for e in col.errors)


class TestPipelineCPSATIntegration:
    """Test full pipeline running with CP-SAT solver backend."""

    def test_pipeline_with_cpsat(self, tmp_path):
        """Pipeline executes end-to-end using CP-SAT backend."""
        mdb_file = tmp_path / "cpsat_mdb.xlsx"
        import pandas as pd
        df = pd.DataFrame([
            ["10-FT-101", "AI", "CBL-01", 1, "A", "AREA_01"],
            ["10-PT-102", "AI", "CBL-01", 2, "A", "AREA_01"],
            ["10-TT-103", "AI", "CBL-01", 3, "A", "AREA_01"],
        ], columns=["Tag Name", "I/O Type", "Cable No", "Pair #", "Channel Seg", "Plant Area"])
        df.to_excel(mdb_file, index=False)

        out_file = tmp_path / "cpsat_allocated.xlsx"

        res = run_allocation_pipeline(
            mdb_paths=mdb_file,
            output_path=out_file,
            solver_backend="cpsat",
            time_limit_seconds=5.0,
        )

        assert res.success is True
        assert res.placed_signals == 3
        assert res.validation_passed is True
        assert out_file.exists()
