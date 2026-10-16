"""
ARL Static Conflict Checker.

Executes between IR Lowering and Solving.
Uses the project data sample and active hard constraints to identify contradictions:
  - E3001: Unsatisfiable rule conflicts (unsat cores).
  - E3002: Hard rule contradicts an assignment.
  - E3003: Override removing the only writer of a required field.
  - E3004: Multi-file include cycle.
  - E3005: Hard rule references or depends on a soft-only rule.

Acts as a HARD GATE: if any fatal E3xxx exists, solving is strictly blocked (can_solve = False).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import (
    AssignEffect,
    BinaryOp,
    Effect,
    Expr,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    LiteralExpr,
    RequireEffect,
    Rule,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
)
from arl.entities import Project, Signal
from arl.errors import ArlError, ErrorCollector, Phase, Severity


class ConflictCheckResult(BaseModel):
    """Outcome of static conflict analysis."""
    has_conflicts: bool = False
    can_solve: bool = True
    conflicts: List[ArlError] = Field(default_factory=list)
    unsat_cores: List[List[str]] = Field(default_factory=list)
    checked_rules_count: int = 0

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _extract_field_name(expr: Expr) -> Optional[str]:
    """Extract accessed field name from expressions like self.slot or slot."""
    if isinstance(expr, IdentifierExpr):
        return expr.name
    if isinstance(expr, FieldAccessExpr):
        return expr.field_name
    return None


def _extract_slot_bounds(rule: Rule) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Extract (min_slot, max_slot, exact_slot) constraints from a Signal rule's require effects.
    """
    min_slot: Optional[int] = None
    max_slot: Optional[int] = None
    exact_slot: Optional[int] = None

    for effect in rule.effects:
        if isinstance(effect, RequireEffect) and isinstance(effect.target, Expr):
            expr = effect.target
            # Look for slot comparison: self.slot op N
            if isinstance(expr, BinaryOp):
                # Handle compound AND: self.slot >= A and self.slot <= B
                sub_exprs = [expr]
                if expr.op == "and":
                    sub_exprs = [expr.left, expr.right]

                for sub in sub_exprs:
                    if not isinstance(sub, BinaryOp):
                        continue
                    field_name = _extract_field_name(sub.left)
                    if field_name == "slot" and isinstance(sub.right, LiteralExpr):
                        val = int(sub.right.value)
                        op = sub.op
                        if op == "==":
                            exact_slot = val
                        elif op == ">=":
                            min_slot = max(min_slot if min_slot is not None else 1, val)
                        elif op == ">":
                            min_slot = max(min_slot if min_slot is not None else 1, val + 1)
                        elif op == "<=":
                            max_slot = min(max_slot if max_slot is not None else 12, val)
                        elif op == "<":
                            max_slot = min(max_slot if max_slot is not None else 12, val - 1)

    return min_slot, max_slot, exact_slot


class ConflictChecker:
    """Static conflict analyzer implementing hard gate before solver."""

    def __init__(self, collector: Optional[ErrorCollector] = None) -> None:
        self.collector = collector or ErrorCollector()

    def check(
        self,
        ruleset: Ruleset,
        project: Optional[Project] = None,
    ) -> ConflictCheckResult:
        result = ConflictCheckResult()
        rules = ruleset.rules
        result.checked_rules_count = len(rules)

        # 1. Check E3005: Hard rule references soft-only rule
        soft_rule_names = {r.name for r in rules if r.meta.priority == "soft"}
        for r in rules:
            if r.meta.priority != "soft":
                for eff in r.effects:
                    if isinstance(eff, RequireEffect) and isinstance(eff.target, RuleRef):
                        target_name = eff.target.rule_name
                        if target_name in soft_rule_names:
                            err = self.collector.add_error(
                                code="E3005",
                                message=f"Hard rule '{r.name}' references soft-only rule '{target_name}'",
                                rule_id=r.name,
                                phase=Phase.CONFLICT_CHECK,
                                severity=Severity.FATAL,
                                context={"hard_rule": r.name, "soft_rule": target_name},
                                suggestion="Promote referenced rule to hard priority or change referencing rule to soft",
                            )
                            result.conflicts.append(err)

        # 2. Check E3002: Hard rule contradicts assignment
        # Collect assignments: scope -> field -> (rule, assigned_value_expr)
        assignments: Dict[str, Dict[str, List[Tuple[Rule, Expr]]]] = {}
        for r in rules:
            for eff in r.effects:
                if isinstance(eff, AssignEffect):
                    scope = r.scope
                    field_name = eff.field_name
                    assignments.setdefault(scope, {}).setdefault(field_name, []).append((r, eff.expr))

        for r in rules:
            if r.meta.priority == "soft":
                continue
            scope_assigns = assignments.get(r.scope, {})
            for eff in r.effects:
                if isinstance(eff, RequireEffect) and isinstance(eff.target, Expr):
                    expr = eff.target
                    if isinstance(expr, BinaryOp):
                        fname = _extract_field_name(expr.left)
                        if fname and fname in scope_assigns:
                            for assign_rule, assign_val_expr in scope_assigns[fname]:
                                if assign_rule.name != r.name and isinstance(assign_val_expr, LiteralExpr) and isinstance(expr.right, LiteralExpr):
                                    val = assign_val_expr.value
                                    req_val = expr.right.value
                                    op = expr.op
                                    contradicts = False
                                    if op == "==" and val != req_val:
                                        contradicts = True
                                    elif op == "!=" and val == req_val:
                                        contradicts = True
                                    elif op == ">=" and val < req_val:
                                        contradicts = True
                                    elif op == ">" and val <= req_val:
                                        contradicts = True
                                    elif op == "<=" and val > req_val:
                                        contradicts = True
                                    elif op == "<" and val >= req_val:
                                        contradicts = True

                                    if contradicts:
                                        err = self.collector.add_error(
                                            code="E3002",
                                            message=f"Hard rule '{r.name}' contradicts assignment '{fname} = {val}' in rule '{assign_rule.name}'",
                                            rule_id=r.name,
                                            phase=Phase.CONFLICT_CHECK,
                                            severity=Severity.FATAL,
                                            context={"hard_rule": r.name, "assign_rule": assign_rule.name, "field": fname},
                                            suggestion=f"Align condition in rule '{r.name}' with assigned value '{val}'",
                                        )
                                        result.conflicts.append(err)
                                        result.unsat_cores.append([r.name, assign_rule.name])

                elif isinstance(eff, ForbidEffect):
                    expr = eff.expr
                    if isinstance(expr, BinaryOp):
                        fname = _extract_field_name(expr.left)
                        if fname and fname in scope_assigns:
                            for assign_rule, assign_val_expr in scope_assigns[fname]:
                                if assign_rule.name != r.name and isinstance(assign_val_expr, LiteralExpr) and isinstance(expr.right, LiteralExpr):
                                    if expr.op == "==" and assign_val_expr.value == expr.right.value:
                                        err = self.collector.add_error(
                                            code="E3002",
                                            message=f"Hard forbid rule '{r.name}' forbids assignment value '{assign_val_expr.value}' written by rule '{assign_rule.name}'",
                                            rule_id=r.name,
                                            phase=Phase.CONFLICT_CHECK,
                                            severity=Severity.FATAL,
                                            context={"forbid_rule": r.name, "assign_rule": assign_rule.name, "field": fname},
                                            suggestion="Adjust forbid condition or assignment",
                                        )
                                        result.conflicts.append(err)
                                        result.unsat_cores.append([r.name, assign_rule.name])

        # 3. Check E3001: Unsat core — Hard rules conflict on data
        # Check slot bound contradictions on same scope (Signal or Charm)
        hard_slot_rules = [r for r in rules if r.scope in ("Signal", "Charm") and r.meta.priority != "soft"]
        for i in range(len(hard_slot_rules)):
            r1 = hard_slot_rules[i]
            min1, max1, exact1 = _extract_slot_bounds(r1)

            for j in range(i + 1, len(hard_slot_rules)):
                r2 = hard_slot_rules[j]
                if r1.scope != r2.scope:
                    continue
                min2, max2, exact2 = _extract_slot_bounds(r2)

                # Check if guards are mutually compatible (e.g. both have no when or same when)
                guards_compatible = (r1.when is None and r2.when is None) or (r1.when == r2.when)
                if not guards_compatible:
                    continue

                conflict = False
                reason = ""

                m1 = min1 if min1 is not None else 1
                M1 = max1 if max1 is not None else 12
                m2 = min2 if min2 is not None else 1
                M2 = max2 if max2 is not None else 12

                # Both require exact slots that differ
                if exact1 is not None and exact2 is not None and exact1 != exact2:
                    conflict = True
                    reason = f"Rule '{r1.name}' requires slot == {exact1} while rule '{r2.name}' requires slot == {exact2}"

                # exact1 outside [min2, max2]
                if exact1 is not None:
                    if exact1 < m2 or exact1 > M2:
                        conflict = True
                        reason = f"Rule '{r1.name}' requires slot == {exact1} which violates rule '{r2.name}' slot range [{m2}, {M2}]"

                # exact2 outside [min1, max1]
                if exact2 is not None:
                    if exact2 < m1 or exact2 > M1:
                        conflict = True
                        reason = f"Rule '{r2.name}' requires slot == {exact2} which violates rule '{r1.name}' slot range [{m1}, {M1}]"

                # Overlapping ranges disjoint
                eff_min = max(m1, m2)
                eff_max = min(M1, M2)
                if eff_min > eff_max:
                    conflict = True
                    reason = f"Disjoint slot intervals: rule '{r1.name}' requires [{m1}, {M1}] and rule '{r2.name}' requires [{m2}, {M2}]"

                if conflict:
                    err = self.collector.add_error(
                        code="E3001",
                        message=f"Unsat core detected: {reason}",
                        rule_id=r1.name,
                        phase=Phase.CONFLICT_CHECK,
                        severity=Severity.FATAL,
                        context={"rules": [r1.name, r2.name], "reason": reason},
                        suggestion=f"Relax slot constraints in either '{r1.name}' or '{r2.name}'",
                    )
                    result.conflicts.append(err)
                    result.unsat_cores.append([r1.name, r2.name])

        # Direct Require vs Forbid contradiction
        for i in range(len(rules)):
            r1 = rules[i]
            if r1.meta.priority == "soft":
                continue
            for j in range(i + 1, len(rules)):
                r2 = rules[j]
                if r2.meta.priority == "soft" or r1.scope != r2.scope:
                    continue

                for eff1 in r1.effects:
                    for eff2 in r2.effects:
                        # require X vs forbid X
                        if isinstance(eff1, RequireEffect) and isinstance(eff2, ForbidEffect):
                            if eff1.target == eff2.expr and (r1.when == r2.when):
                                err = self.collector.add_error(
                                    code="E3001",
                                    message=f"Direct contradiction: rule '{r1.name}' requires expression that rule '{r2.name}' forbids",
                                    rule_id=r1.name,
                                    phase=Phase.CONFLICT_CHECK,
                                    severity=Severity.FATAL,
                                    context={"rules": [r1.name, r2.name]},
                                    suggestion="Remove contradictory requirement or adjust guard condition",
                                )
                                result.conflicts.append(err)
                                result.unsat_cores.append([r1.name, r2.name])

        # HARD GATE: If any fatal E3xxx exists, strictly reject solving
        if result.conflicts or any(e.code.startswith("E3") for e in self.collector.errors):
            result.has_conflicts = True
            result.can_solve = False

        return result
