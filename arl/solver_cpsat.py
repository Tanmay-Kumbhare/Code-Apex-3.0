"""
ARL OR-Tools CP-SAT Solver Backend and Unsat Core Minimizer.

Implements Part 8:
  - Decision variables: cabinet[group], cioc[cable], baseplate[cable, index], charm[signal].
  - Solver-agnostic IR translation with reification and auxiliary variables.
  - Rejection of invalid IR nodes with code E5005.
  - Assumption-based unsat-core extraction and bounded iterative minimization (labeled 'minimal' or 'approximate').
  - Configurable time limit (default 30.0s) and timeout handling with E5001.
  - Deterministic greedy fallback with degraded=True when enabled.
  - Independent validation of solver solutions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import BinaryOp, Expr, ForbidEffect, RequireEffect, Rule, Ruleset
from arl.entities import Baseplate, Cabinet, Cable, CIOC, Group, Pair, Project, Signal
from arl.errors import ArlError, ErrorCollector, Phase, Severity
from arl.ir import (
    AllDifferent,
    And,
    Contiguous,
    Count,
    Eq,
    Exists,
    ForAll,
    Gt,
    Gte,
    Implies,
    IRNode,
    Lt,
    Lte,
    Neq,
    Not,
    Objective as IRObjective,
    Or,
    Soft,
    Sum,
    Var,
)
from arl.schema import Schema
from arl.solver_greedy import (
    DEFAULT_HARDWARE_MAP,
    AllocationResult,
    GreedySolver,
    Placement,
)
from arl.tq import TQManager
from arl.validate import ValidationResult, Validator


class UnsatCore(BaseModel):
    """Result of assumption-based unsat core minimization."""
    rules: List[str] = Field(default_factory=list)
    status: str = "minimal"  # 'minimal' or 'approximate'
    iterations_used: int = 0
    budget_exhausted: bool = False
    evidence: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


@dataclass
class PhysicalSlot:
    """Flattened index for each discrete hardware terminal slot in project."""
    index: int
    cabinet_id: str
    cab_idx: int
    cioc_id: str
    cioc_idx: int
    cioc_seg_role: str
    baseplate_id: str
    bp_idx: int
    slot: int  # 1..12
    area: Optional[str] = None


class CPSATSolver:
    """Constraint programming solver backend using OR-Tools CP-SAT."""

    def __init__(
        self,
        schema: Optional[Schema] = None,
        tq_manager: Optional[TQManager] = None,
        collector: Optional[ErrorCollector] = None,
        time_limit_seconds: float = 30.0,
        minimization_budget: int = 50,
        enable_fallback: bool = True,
    ) -> None:
        self.schema = schema
        self.tq_manager = tq_manager or TQManager()
        self.collector = collector or ErrorCollector()
        self.time_limit_seconds = time_limit_seconds
        self.minimization_budget = minimization_budget
        self.enable_fallback = enable_fallback
        self.last_unsat_core: Optional[UnsatCore] = None

    def solve(
        self,
        project: Project,
        ruleset: Optional[Ruleset] = None,
        ir_nodes: Optional[List[IRNode]] = None,
        pins: Optional[Dict[str, Any]] = None,
    ) -> AllocationResult:
        """
        Formulate and solve signal allocation problem using OR-Tools CP-SAT.
        Falls back to greedy allocator if configured upon timeout or failure.
        Honors signal pins and move minimization objectives.
        """
        # Validate IR nodes if provided explicitly
        if ir_nodes:
            for node in ir_nodes:
                if not isinstance(node, (Var, Eq, Neq, Lt, Lte, Gt, Gte, And, Or, Not, Implies, AllDifferent, Count, Sum, Contiguous, ForAll, Exists, Soft, IRObjective)):
                    self.collector.add_error(
                        code="E5005",
                        message=f"Invalid or unsupported IR node encountered: {type(node).__name__}",
                        phase=Phase.SOLVE,
                        severity=Severity.ERROR,
                        context={"node_type": type(node).__name__},
                        suggestion="Ensure IR lowering produces standard Part 5 IR nodes",
                    )
                    if self.enable_fallback:
                        return self._run_greedy_fallback(project, ruleset, reason="E5005 invalid IR node", pins=pins)
                    return AllocationResult(success=False, stats={"error": "E5005"})

        signals: List[Signal] = project.get("signals", [])
        cabinets: List[Cabinet] = project.get("cabinets", [])

        if not signals:
            return AllocationResult(success=True, stats={"total_signals": 0, "placed_signals": 0})

        # 1. Enumerate and index physical slots
        physical_slots: List[PhysicalSlot] = []
        slot_idx = 0
        cab_id_to_idx: Dict[str, int] = {}
        cioc_id_to_idx: Dict[str, int] = {}
        bp_id_to_idx: Dict[str, int] = {}

        for c_idx, cab in enumerate(cabinets):
            cid = str(cab.get("id", f"CAB-{c_idx}"))
            cab_id_to_idx[cid] = c_idx
            cab_area = cab.get("area")

            for ci_idx, cioc in enumerate(cab.get("ciocs", [])):
                ciid = str(cioc.get("id", f"CIOC-{ci_idx}"))
                cioc_id_to_idx[ciid] = len(cioc_id_to_idx)
                seg_role = str(cioc.get("seg_role", "MIXED"))

                for bp_idx, bp in enumerate(cioc.get("baseplates", [])):
                    bpid = str(bp.get("id", f"BP-{bp_idx}"))
                    bp_id_to_idx[bpid] = len(bp_id_to_idx)
                    cap = int(bp.get("capacity", 12))

                    for s in range(1, cap + 1):
                        physical_slots.append(
                            PhysicalSlot(
                                index=slot_idx,
                                cabinet_id=cid,
                                cab_idx=c_idx,
                                cioc_id=ciid,
                                cioc_idx=cioc_id_to_idx[ciid],
                                cioc_seg_role=seg_role,
                                baseplate_id=bpid,
                                bp_idx=bp_id_to_idx[bpid],
                                slot=s,
                                area=cab_area,
                            )
                        )
                        slot_idx += 1

        total_slots = len(physical_slots)
        total_signals = len(signals)

        # Capacity sanity check
        if total_signals > total_slots:
            if self.enable_fallback:
                return self._run_greedy_fallback(project, ruleset, reason="Capacity overflow: total signals exceed slots")
            return AllocationResult(success=False, stats={"error": "E4007 Capacity overflow"})

        # 2. Build CP-SAT Model
        model = cp_model.CpModel()

        # Decision variables
        # slot_choice[s] in [0 .. total_slots - 1]
        slot_choice: Dict[str, cp_model.IntVar] = {}
        charm_var: Dict[str, cp_model.IntVar] = {}
        bp_var: Dict[str, cp_model.IntVar] = {}
        cioc_var: Dict[str, cp_model.IntVar] = {}
        cab_var: Dict[str, cp_model.IntVar] = {}

        for sig in signals:
            tag = str(sig.get("tag"))
            seg = sig.get("seg")
            sig_area = sig.get("area")

            # Filter valid slot domain for this signal based on segregation and area
            allowed_slots: List[int] = []
            for ps in physical_slots:
                # Segregation rule
                if seg == "A" and ps.cioc_seg_role == "B_ONLY":
                    continue
                if seg == "B" and ps.cioc_seg_role == "A_ONLY":
                    continue
                # Area matching rule
                if sig_area and ps.area and str(sig_area) != str(ps.area):
                    continue
                allowed_slots.append(ps.index)

            if not allowed_slots:
                # Infeasible signal placement
                if self.enable_fallback:
                    return self._run_greedy_fallback(project, ruleset, reason=f"Signal {tag} has no matching slot domain")
                return AllocationResult(success=False, unplaced_signals=[tag])

            sc = model.NewIntVarFromDomain(cp_model.Domain.FromValues(allowed_slots), f"slot_choice_{tag}")
            slot_choice[tag] = sc

            ch = model.NewIntVar(1, 12, f"charm_{tag}")
            charm_var[tag] = ch
            model.AddElement(sc, [ps.slot for ps in physical_slots], ch)

            b_v = model.NewIntVar(0, max(0, len(bp_id_to_idx) - 1), f"bp_{tag}")
            bp_var[tag] = b_v
            model.AddElement(sc, [ps.bp_idx for ps in physical_slots], b_v)

            ci_v = model.NewIntVar(0, max(0, len(cioc_id_to_idx) - 1), f"cioc_{tag}")
            cioc_var[tag] = ci_v
            model.AddElement(sc, [ps.cioc_idx for ps in physical_slots], ci_v)

            cb_v = model.NewIntVar(0, max(0, len(cab_id_to_idx) - 1), f"cab_{tag}")
            cab_var[tag] = cb_v
            model.AddElement(sc, [ps.cab_idx for ps in physical_slots], cb_v)

        # 3. Uniqueness constraint: AllDifferent physical slots
        model.AddAllDifferent(list(slot_choice.values()))

        # 4. Group and Cable Cohesion constraints
        # All signals in a group should share the same cabinet
        for grp in project.get("groups", []):
            grp_sigs = [s.get("tag") for s in grp.get("signals", []) if s.get("tag") in cab_var]
            if len(grp_sigs) > 1:
                first_cab = cab_var[grp_sigs[0]]
                for other_tag in grp_sigs[1:]:
                    model.Add(cab_var[other_tag] == first_cab)

        # All signals in a cable should share the same CIOC and preserve consecutive pair ordering
        for cbl in project.get("cables", []):
            cbl_sigs = sorted(
                [s for s in signals if s.get("cable") == cbl.get("id") and s.get("pair") is not None],
                key=lambda s: int(s.get("pair", 0)),
            )
            if len(cbl_sigs) > 1:
                first_cioc = cioc_var[str(cbl_sigs[0].get("tag"))]
                for other in cbl_sigs[1:]:
                    model.Add(cioc_var[str(other.get("tag"))] == first_cioc)

            # Pair ordering: pair i < pair i+1
            for i in range(len(cbl_sigs) - 1):
                s1_tag = str(cbl_sigs[i].get("tag"))
                s2_tag = str(cbl_sigs[i + 1].get("tag"))
                if s1_tag in slot_choice and s2_tag in slot_choice:
                    model.Add(slot_choice[s1_tag] < slot_choice[s2_tag])

        # 5. Translate Active Hard & Soft Rules with Assumption Literals
        assumptions: Dict[str, cp_model.BoolVar] = {}
        rule_literals: Dict[int, str] = {}
        objective_terms: List[Union[cp_model.IntVar, cp_model.LinearExpr]] = []

        if ruleset:
            for rule in ruleset.rules:
                r_name = rule.meta.rule_id or rule.name
                is_hard = (rule.meta.priority != "soft")

                # Associate assumption literal for hard rules
                if is_hard:
                    b_rule = model.NewBoolVar(f"assump_{r_name}")
                    assumptions[r_name] = b_rule
                    rule_literals[b_rule.Index()] = r_name

                # Translate Signal- and Charm-scoped rules
                if rule.scope in ("Signal", "Charm"):
                    for sig in signals:
                        if not self._eval_when_condition(rule.when, sig):
                            continue
                        tag = str(sig.get("tag"))
                        ch = charm_var[tag]

                        for effect in rule.effects:
                            if isinstance(effect, RequireEffect) and isinstance(effect.target, Expr):
                                self._translate_require_expr(
                                    effect.target, ch, model,
                                    assumption=assumptions.get(r_name) if is_hard else None,
                                    soft_weight=rule.meta.weight if not is_hard else None,
                                    objective_terms=objective_terms,
                                )
                            elif isinstance(effect, ForbidEffect):
                                self._translate_forbid_expr(
                                    effect.expr, ch, model,
                                    assumption=assumptions.get(r_name) if is_hard else None,
                                    soft_weight=rule.meta.weight if not is_hard else None,
                                    objective_terms=objective_terms,
                                )

        # Model signal pins and move minimization (Part 10.2 Pinning)
        if pins:
            coord_to_slot = {
                (ps.cabinet_id, ps.cioc_id, ps.baseplate_id, ps.slot): ps.index
                for ps in physical_slots
            }
            for pin in pins.values():
                if hasattr(pin, "is_relaxed") and pin.is_relaxed:
                    continue
                tag = str(pin.tag)
                target_slot = coord_to_slot.get((pin.cabinet_id, pin.cioc_id, pin.baseplate_id, pin.slot))
                if target_slot is not None and tag in slot_choice:
                    kind = str(getattr(pin, "kind", "soft")).lower()
                    if "hard" in kind:
                        model.Add(slot_choice[tag] == target_slot)
                    else:
                        is_at_pin = model.NewBoolVar(f"pin_match_{tag}")
                        model.Add(slot_choice[tag] == target_slot).OnlyEnforceIf(is_at_pin)
                        model.Add(slot_choice[tag] != target_slot).OnlyEnforceIf(is_at_pin.Not())
                        prio = int(getattr(pin, "priority", 1))
                        objective_terms.append(prio * 500 * is_at_pin)

        # Set objective (soft rules / packing)
        if objective_terms:
            model.Maximize(sum(objective_terms))

        # Add all assumptions to model
        if assumptions:
            model.AddAssumptions(list(assumptions.values()))

        # 6. Solve with Configured Parameters
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.time_limit_seconds
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        # 7. Evaluate Outcomes
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # Success! Extract placements
            allocations: Dict[str, Placement] = {}
            for sig in signals:
                tag = str(sig.get("tag"))
                chosen_idx = solver.Value(slot_choice[tag])
                ps = physical_slots[chosen_idx]
                io_type = str(sig.get("io_type", "AI"))
                charm_type, tb_type = DEFAULT_HARDWARE_MAP.get(io_type, ("AI", "STANDARD"))

                allocations[tag] = Placement(
                    tag=tag,
                    cabinet_id=ps.cabinet_id,
                    cioc_id=ps.cioc_id,
                    baseplate_id=ps.baseplate_id,
                    slot=ps.slot,
                    charm_type=charm_type,
                    tb_type=tb_type,
                    applied_rules=["R_CPSAT_Optimal" if status == cp_model.OPTIMAL else "R_CPSAT_Feasible"],
                )

            # Validate solution
            alloc_result = AllocationResult(
                success=True,
                degraded=False,
                allocations=allocations,
                unplaced_signals=[],
                tqs=self.tq_manager.get_all(),
                stats={
                    "solver": "CP-SAT",
                    "status": "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
                    "wall_time": solver.WallTime(),
                    "total_signals": total_signals,
                    "placed_signals": len(allocations),
                    "total_slots": total_slots,
                    "occupied_slots": len(allocations),
                    "spare_slots": total_slots - len(allocations),
                    "spare_percent": round((total_slots - len(allocations)) / total_slots * 100.0, 2),
                    "success_rate": 100.0,
                },
            )

            validator = Validator()
            val_res = validator.validate(project, alloc_result, ruleset=ruleset)
            if not val_res.valid:
                # Validation rejected CP-SAT output
                if self.enable_fallback:
                    return self._run_greedy_fallback(project, ruleset, reason="Independent validation rejected CP-SAT output", pins=pins)
                alloc_result.success = False
                return alloc_result

            return alloc_result

        elif status == cp_model.INFEASIBLE:
            # Model is infeasible. Perform assumption-based unsat-core minimization
            unsat_core = self._minimize_unsat_core(model, solver, assumptions)
            self.last_unsat_core = unsat_core

            self.collector.add_error(
                code="E5002",
                message=f"Model proved infeasible by CP-SAT. Unsat core: {unsat_core.rules}",
                phase=Phase.SOLVE,
                severity=Severity.FATAL,
                context={"core_rules": unsat_core.rules, "core_status": unsat_core.status},
            )
            self.tq_manager.create_tq(
                code="E3001",
                title=f"CP-SAT Unsat Core ({unsat_core.status.upper()}): {', '.join(unsat_core.rules)}",
                severity="critical",
                rules=unsat_core.rules,
                evidence=f"Solver proved constraints from rules {unsat_core.rules} are mutually unsatisfiable.",
                suggestions=["Relax one or more conflicting hard rules", "Check slot bounds or segregation constraints"],
            )

            if self.enable_fallback:
                return self._run_greedy_fallback(project, ruleset, reason="CP-SAT proved model infeasible", pins=pins)

            return AllocationResult(
                success=False,
                degraded=False,
                unplaced_signals=[s.get("tag") for s in signals if s.get("tag")],
                tqs=self.tq_manager.get_all(),
                stats={"solver": "CP-SAT", "status": "INFEASIBLE", "unsat_core": unsat_core.to_dict()},
            )

        else:
            # Timeout (cp_model.UNKNOWN) or limit exceeded
            self.collector.add_error(
                code="E5001",
                message=f"CP-SAT solver timed out after {self.time_limit_seconds}s limit",
                phase=Phase.SOLVE,
                severity=Severity.WARNING,
            )
            self.tq_manager.create_tq(
                code="E5001",
                title="Solver Timeout - Fallback Invoked",
                severity="medium",
                evidence=f"CP-SAT reached {self.time_limit_seconds}s time limit without finding feasible assignment.",
                suggestions=["Increase solver time limit", "Simplify complex soft rule constraints"],
            )

            if self.enable_fallback:
                return self._run_greedy_fallback(project, ruleset, reason="CP-SAT timeout (E5001)", pins=pins)

            return AllocationResult(
                success=False,
                degraded=True,
                unplaced_signals=[s.get("tag") for s in signals if s.get("tag")],
                tqs=self.tq_manager.get_all(),
                stats={"solver": "CP-SAT", "status": "TIMEOUT", "time_limit": self.time_limit_seconds},
            )

    def _eval_when_condition(self, when_expr: Optional[Expr], sig: Signal) -> bool:
        """Evaluate rule guard condition ('when') against a Signal entity."""
        if when_expr is None:
            return True
        if isinstance(when_expr, BinaryOp):
            left_field = getattr(when_expr.left, "field_name", getattr(when_expr.left, "name", None))
            right_val = getattr(when_expr.right, "value", None)
            sig_val = sig.get(left_field)
            if when_expr.op == "==":
                return str(sig_val) == str(right_val)
            elif when_expr.op == "!=":
                return str(sig_val) != str(right_val)
            elif when_expr.op in ("in", "not in"):
                el_vals = [getattr(e, "value", None) for e in getattr(when_expr.right, "elements", [])]
                if when_expr.op == "in":
                    return sig_val in el_vals
                else:
                    return sig_val not in el_vals
        return True

    def _translate_require_expr(
        self,
        expr: Expr,
        charm_var: cp_model.IntVar,
        model: cp_model.CpModel,
        assumption: Optional[cp_model.BoolVar] = None,
        soft_weight: Optional[float] = None,
        objective_terms: Optional[List[Any]] = None,
    ) -> None:
        """Translate a require AST expression into CP-SAT constraint."""
        if isinstance(expr, BinaryOp):
            # Handle self.slot op Val
            val = expr.right.value if hasattr(expr.right, "value") else None
            if isinstance(val, (int, float)):
                val_int = int(val)
                op = expr.op

                if soft_weight is not None and objective_terms is not None:
                    # Soft rule: reify condition
                    b_cond = model.NewBoolVar(f"soft_cond_{val_int}")
                    if op == "==":
                        model.Add(charm_var == val_int).OnlyEnforceIf(b_cond)
                        model.Add(charm_var != val_int).OnlyEnforceIf(~b_cond)
                    elif op == "<=":
                        model.Add(charm_var <= val_int).OnlyEnforceIf(b_cond)
                        model.Add(charm_var > val_int).OnlyEnforceIf(~b_cond)
                    elif op == ">=":
                        model.Add(charm_var >= val_int).OnlyEnforceIf(b_cond)
                        model.Add(charm_var < val_int).OnlyEnforceIf(~b_cond)
                    objective_terms.append(int(soft_weight * 10) * b_cond)
                    return

                # Hard rule constraint
                ct = None
                if op == "==":
                    ct = model.Add(charm_var == val_int)
                elif op == "!=":
                    ct = model.Add(charm_var != val_int)
                elif op == "<=":
                    ct = model.Add(charm_var <= val_int)
                elif op == "<":
                    ct = model.Add(charm_var < val_int)
                elif op == ">=":
                    ct = model.Add(charm_var >= val_int)
                elif op == ">":
                    ct = model.Add(charm_var > val_int)

                if ct is not None and assumption is not None:
                    ct.OnlyEnforceIf(assumption)

    def _translate_forbid_expr(
        self,
        expr: Expr,
        charm_var: cp_model.IntVar,
        model: cp_model.CpModel,
        assumption: Optional[cp_model.BoolVar] = None,
        soft_weight: Optional[float] = None,
        objective_terms: Optional[List[Any]] = None,
    ) -> None:
        """Translate a forbid AST expression into CP-SAT constraint."""
        if isinstance(expr, BinaryOp):
            val = expr.right.value if hasattr(expr.right, "value") else None
            if isinstance(val, (int, float)):
                val_int = int(val)
                op = expr.op
                ct = None
                # Forbid == means require !=
                if op == "==":
                    ct = model.Add(charm_var != val_int)
                elif op == "<=":
                    ct = model.Add(charm_var > val_int)
                elif op == ">=":
                    ct = model.Add(charm_var < val_int)

                if ct is not None and assumption is not None:
                    ct.OnlyEnforceIf(assumption)

    def _minimize_unsat_core(
        self,
        model: cp_model.CpModel,
        solver: cp_model.CpSolver,
        assumptions: Dict[str, cp_model.BoolVar],
    ) -> UnsatCore:
        """
        Perform bounded assumption-based unsat core minimization:
          1. Extract initial sufficient assumptions for infeasibility.
          2. Iteratively drop assumptions to find a minimal core within budget.
          3. Accurately label 'minimal' or 'approximate'.
        """
        # Initial core from CP-SAT
        sufficient_indices = set(solver.SufficientAssumptionsForInfeasibility())
        lit_to_name = {lit.Index(): name for name, lit in assumptions.items()}
        core_names = [lit_to_name[idx] for idx in sufficient_indices if idx in lit_to_name]

        if not core_names:
            core_names = list(assumptions.keys())

        # Prune assumptions iteratively within budget
        iterations = 0
        budget_exhausted = False
        current_core = list(core_names)

        # Iteratively try removing one rule at a time
        for r_name in list(core_names):
            if len(current_core) <= 1:
                break
            if iterations >= self.minimization_budget:
                budget_exhausted = True
                break

            iterations += 1
            test_rules = [r for r in current_core if r != r_name]
            test_assumptions = [assumptions[r] for r in test_rules]

            # Re-solve with test assumptions
            model.ClearAssumptions()
            model.AddAssumptions(test_assumptions)
            temp_solver = cp_model.CpSolver()
            temp_solver.parameters.max_time_in_seconds = 2.0
            test_status = temp_solver.Solve(model)

            if test_status == cp_model.INFEASIBLE:
                # Removal left it infeasible -> r_name is not part of minimal core
                current_core = test_rules

        status_label = "approximate" if budget_exhausted else "minimal"

        return UnsatCore(
            rules=current_core,
            status=status_label,
            iterations_used=iterations,
            budget_exhausted=budget_exhausted,
            evidence=f"Core pruned down to {len(current_core)} rules after {iterations} iterations.",
        )

    def _run_greedy_fallback(
        self,
        project: Project,
        ruleset: Optional[Ruleset],
        reason: str,
        pins: Optional[Dict[str, Any]] = None,
    ) -> AllocationResult:
        """Execute deterministic greedy solver fallback and mark degraded=True."""
        fallback_solver = GreedySolver(
            schema=self.schema,
            tq_manager=self.tq_manager,
            collector=self.collector,
        )
        result = fallback_solver.solve(project, ruleset=ruleset, pins=pins)
        result.degraded = True
        result.stats["solver"] = "Greedy (Fallback)"
        result.stats["fallback_reason"] = reason

        # Check with independent validator
        validator = Validator()
        val_res = validator.validate(project, result, ruleset=ruleset)
        if not val_res.valid:
            # Even fallback violated a hard rule -> reject output completely
            result.success = False

        return result
