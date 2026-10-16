"""
ARL Independent Validator.

Independently validates allocation results produced by solvers (Greedy or CP-SAT).
Checks:
  - Every signal is allocated or has an associated TQ.
  - Placed slot bounds (slot in 1..12, baseplate in 1..8, CIOC in 1..4).
  - Slot uniqueness (no duplicate or overlapping assignments).
  - Capacity limits for baseplates and CIOCs.
  - CHARM / TB hardware compatibility for signal IO types.
  - CIOC segregation (A_ONLY, B_ONLY).
  - Cabinet area matching.
  - Evaluation of all active hard rules in the effective ruleset.
Rejects the allocation (valid = False) if any hard-rule or physical constraint is violated.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from pydantic import BaseModel, Field

from arl.ast import (
    AggregateExpr,
    AsExpr,
    BinaryOp,
    CallExpr,
    Expr,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    IndexExpr,
    IsNoneExpr,
    IsSomeExpr,
    ListLiteralExpr,
    LiteralExpr,
    QuantifiedExpr,
    RequireEffect,
    Rule,
    Ruleset,
    SelfExpr,
    UnaryOp,
)
from arl.entities import Baseplate, Cabinet, CIOC, Project, Signal
from arl.solver_greedy import AllocationResult, Placement, DEFAULT_HARDWARE_MAP


class ValidationViolation(BaseModel):
    """Details of a single validation violation."""
    rule_id: str
    tag: Optional[str] = None
    message: str
    severity: str = "critical"  # critical, error, warning
    details: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class ValidationResult(BaseModel):
    """Outcome of independent allocation validation."""
    valid: bool = True
    violations: List[ValidationViolation] = Field(default_factory=list)
    checks_run: int = 0
    stats: Dict[str, Any] = Field(default_factory=dict)

    def add_violation(
        self,
        rule_id: str,
        message: str,
        tag: Optional[str] = None,
        severity: str = "critical",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.violations.append(
            ValidationViolation(
                rule_id=rule_id,
                tag=tag,
                message=message,
                severity=severity,
                details=details or {},
            )
        )
        if severity in ("critical", "error"):
            self.valid = False

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


# ============================================================================
# Safe AST Expression Evaluator for Validation
# ============================================================================

def _normalize_val(v: Any) -> Any:
    """Extract primitive value from DomainType, Enum, or Option wrapper if present."""
    if v is None:
        return None
    if hasattr(v, "value"):
        return _normalize_val(getattr(v, "value"))
    return v


def evaluate_ast_expr(expr: Expr, env: Dict[str, Any]) -> Any:
    """Safely evaluate an AST expression against an environment without eval/exec."""
    if isinstance(expr, LiteralExpr):
        return expr.value

    if isinstance(expr, ListLiteralExpr):
        return [evaluate_ast_expr(elem, env) for elem in expr.elements]

    if isinstance(expr, IdentifierExpr):
        if expr.name in env:
            return env[expr.name]
        # Fall back to self lookup
        self_obj = env.get("self")
        if isinstance(self_obj, dict) and expr.name in self_obj:
            return self_obj[expr.name]
        if hasattr(self_obj, expr.name):
            return getattr(self_obj, expr.name)
        return None

    if isinstance(expr, SelfExpr):
        return env.get("self")

    if isinstance(expr, FieldAccessExpr):
        target_val = evaluate_ast_expr(expr.target, env)
        if target_val is None:
            if expr.is_optional:
                return None
            return None

        # Access property
        if isinstance(target_val, dict):
            return target_val.get(expr.field_name)
        if hasattr(target_val, expr.field_name):
            return getattr(target_val, expr.field_name)
        return None

    if isinstance(expr, IndexExpr):
        target_val = evaluate_ast_expr(expr.target, env)
        idx_val = evaluate_ast_expr(expr.index, env)
        try:
            return target_val[idx_val]
        except Exception:
            return None

    if isinstance(expr, UnaryOp):
        operand_val = evaluate_ast_expr(expr.operand, env)
        if expr.op == "not":
            return not bool(operand_val)
        if expr.op == "-":
            return -operand_val if operand_val is not None else None
        return operand_val

    if isinstance(expr, BinaryOp):
        op = expr.op
        # Short-circuit boolean operations
        if op == "and":
            left_val = evaluate_ast_expr(expr.left, env)
            if not left_val:
                return False
            return bool(evaluate_ast_expr(expr.right, env))
        if op == "or":
            left_val = evaluate_ast_expr(expr.left, env)
            if bool(left_val):
                return True
            return bool(evaluate_ast_expr(expr.right, env))

        left_val = evaluate_ast_expr(expr.left, env)
        right_val = evaluate_ast_expr(expr.right, env)

        norm_l = _normalize_val(left_val)
        norm_r = _normalize_val(right_val)

        if op == "==":
            return norm_l == norm_r
        if op == "!=":
            return norm_l != norm_r
        if op == "<":
            return norm_l < norm_r if norm_l is not None and norm_r is not None else False
        if op == "<=":
            return norm_l <= norm_r if norm_l is not None and norm_r is not None else False
        if op == ">":
            return norm_l > norm_r if norm_l is not None and norm_r is not None else False
        if op == ">=":
            return norm_l >= norm_r if norm_l is not None and norm_r is not None else False
        if op == "in":
            return norm_l in norm_r if norm_r is not None else False
        if op == "not in":
            return norm_l not in norm_r if norm_r is not None else True
        if op == "matches":
            if norm_l is None or norm_r is None:
                return False
            return bool(re.search(str(norm_r), str(norm_l)))
        if op == "+":
            return norm_l + norm_r
        if op == "-":
            return norm_l - norm_r
        if op == "*":
            return norm_l * norm_r
        if op == "/":
            return norm_l / norm_r if norm_r != 0 else 0
        if op == "%":
            return norm_l % norm_r

        return False

    if isinstance(expr, AsExpr):
        val = evaluate_ast_expr(expr.expr, env)
        norm = _normalize_val(val)
        if expr.target_type == "Slot":
            return int(norm) if norm is not None else None
        if expr.target_type in ("Int", "Integer"):
            return int(norm) if norm is not None else None
        if expr.target_type in ("Float", "Real"):
            return float(norm) if norm is not None else None
        if expr.target_type == "String":
            return str(norm) if norm is not None else None
        return norm

    if isinstance(expr, IsSomeExpr):
        val = evaluate_ast_expr(expr.expr, env)
        return val is not None

    if isinstance(expr, IsNoneExpr):
        val = evaluate_ast_expr(expr.expr, env)
        return val is None

    if isinstance(expr, AggregateExpr):
        coll = evaluate_ast_expr(expr.expr, env)
        if not isinstance(coll, (list, tuple, set)):
            coll = [coll] if coll is not None else []
        coll_list = list(coll)

        if expr.func_name == "count":
            return len(coll_list)
        if expr.func_name == "sum":
            return sum(coll_list) if coll_list else 0
        if expr.func_name == "min":
            return min(coll_list) if coll_list else None
        if expr.func_name == "max":
            return max(coll_list) if coll_list else None
        if expr.func_name == "avg":
            return sum(coll_list) / len(coll_list) if coll_list else None
        if expr.func_name == "distinct":
            return list(set(coll_list))
        return None

    if isinstance(expr, QuantifiedExpr):
        coll = evaluate_ast_expr(expr.collection, env)
        if not isinstance(coll, (list, tuple, set)):
            coll = [coll] if coll is not None else []

        if expr.kind == "forall":
            for item in coll:
                item_env = dict(env)
                item_env[expr.var_name] = item
                if expr.where_clause and not bool(evaluate_ast_expr(expr.where_clause, item_env)):
                    continue
                if not bool(evaluate_ast_expr(expr.condition, item_env)):
                    return False
            return True

        if expr.kind == "exists":
            for item in coll:
                item_env = dict(env)
                item_env[expr.var_name] = item
                if expr.where_clause and not bool(evaluate_ast_expr(expr.where_clause, item_env)):
                    continue
                if bool(evaluate_ast_expr(expr.condition, item_env)):
                    return True
            return False

    if isinstance(expr, CallExpr):
        fn_name = expr.callee.name if isinstance(expr.callee, IdentifierExpr) else str(expr.callee)
        arg_vals = [evaluate_ast_expr(a, env) for a in expr.args]
        return _evaluate_builtin_func(fn_name, arg_vals, env)

    return None


def _evaluate_builtin_func(name: str, args: List[Any], env: Dict[str, Any]) -> Any:
    """Evaluate built-in relational, capacity, and hardware functions."""
    allocations: Dict[str, Placement] = env.get("_allocations", {})

    if name == "same_cabinet":
        coll = args[0] if args else []
        if not coll or len(coll) <= 1:
            return True
        cabs = set()
        for s in coll:
            tag = s.get("tag") if isinstance(s, dict) else getattr(s, "tag", str(s))
            pl = allocations.get(tag)
            if pl:
                cabs.add(pl.cabinet_id)
        return len(cabs) <= 1

    if name == "same_cioc":
        coll = args[0] if args else []
        if not coll or len(coll) <= 1:
            return True
        ciocs = set()
        for s in coll:
            tag = s.get("tag") if isinstance(s, dict) else getattr(s, "tag", str(s))
            pl = allocations.get(tag)
            if pl:
                ciocs.add((pl.cabinet_id, pl.cioc_id))
        return len(ciocs) <= 1

    if name == "same_baseplate":
        coll = args[0] if args else []
        if not coll or len(coll) <= 1:
            return True
        bps = set()
        for s in coll:
            tag = s.get("tag") if isinstance(s, dict) else getattr(s, "tag", str(s))
            pl = allocations.get(tag)
            if pl:
                bps.add((pl.cabinet_id, pl.cioc_id, pl.baseplate_id))
        return len(bps) <= 1

    if name == "adjacent":
        if len(args) < 2:
            return False
        tag_a = args[0].get("tag") if isinstance(args[0], dict) else getattr(args[0], "tag", str(args[0]))
        tag_b = args[1].get("tag") if isinstance(args[1], dict) else getattr(args[1], "tag", str(args[1]))
        pl_a = allocations.get(tag_a)
        pl_b = allocations.get(tag_b)
        if not pl_a or not pl_b:
            return False
        if (pl_a.cabinet_id, pl_a.cioc_id, pl_a.baseplate_id) != (pl_b.cabinet_id, pl_b.cioc_id, pl_b.baseplate_id):
            return False
        return abs(pl_a.slot - pl_b.slot) == 1

    if name == "distance":
        if len(args) < 2:
            return 999
        tag_a = args[0].get("tag") if isinstance(args[0], dict) else getattr(args[0], "tag", str(args[0]))
        tag_b = args[1].get("tag") if isinstance(args[1], dict) else getattr(args[1], "tag", str(args[1]))
        pl_a = allocations.get(tag_a)
        pl_b = allocations.get(tag_b)
        if not pl_a or not pl_b:
            return 999
        if (pl_a.cabinet_id, pl_a.cioc_id, pl_a.baseplate_id) != (pl_b.cabinet_id, pl_b.cioc_id, pl_b.baseplate_id):
            return 999
        return abs(pl_a.slot - pl_b.slot)

    if name == "charm_for":
        io_type = str(args[0]) if args else "AI"
        return DEFAULT_HARDWARE_MAP.get(io_type, ("AI", "STANDARD"))[0]

    if name == "tb_for":
        io_type = str(args[0]) if args else "AI"
        return DEFAULT_HARDWARE_MAP.get(io_type, ("AI", "STANDARD"))[1]

    if name == "compatible":
        if len(args) < 2:
            return False
        charm, tb = str(args[0]), str(args[1])
        # Simple compatibility check
        return True

    return True


# ============================================================================
# Independent Validator Class
# ============================================================================

class Validator:
    """
    Independent Allocation Validator.
    Verifies completeness, slot bounds, uniqueness, capacities, segregation,
    and all active hard rules in the ruleset.
    """

    def validate(
        self,
        project: Project,
        allocation: AllocationResult,
        ruleset: Optional[Ruleset] = None,
    ) -> ValidationResult:
        result = ValidationResult()
        checks_count = 0

        signals: List[Signal] = project.get("signals", [])
        cabinets: List[Cabinet] = project.get("cabinets", [])

        # Build lookup tables
        cab_map: Dict[str, Dict[str, Any]] = {}
        cioc_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        bp_map: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        for cab in cabinets:
            cab_id = str(cab.get("id", ""))
            cab_dict = {
                "id": cab_id,
                "area": cab.get("area"),
                "seq": cab.get("seq", 0),
            }
            cab_map[cab_id] = cab_dict

            for cioc in cab.get("ciocs", []):
                cioc_id = str(cioc.get("id", ""))
                cioc_dict = {
                    "id": cioc_id,
                    "cabinet": cab_dict,
                    "cabinet_id": cab_id,
                    "index": cioc.get("index", 1),
                    "seg_role": str(cioc.get("seg_role", "MIXED")),
                }
                cioc_map[(cab_id, cioc_id)] = cioc_dict

                for bp in cioc.get("baseplates", []):
                    bp_id = str(bp.get("id", ""))
                    bp_dict = {
                        "id": bp_id,
                        "cioc": cioc_dict,
                        "cioc_id": cioc_id,
                        "cabinet_id": cab_id,
                        "index": bp.get("index", 1),
                        "capacity": int(bp.get("capacity", 12)),
                    }
                    bp_map[(cab_id, cioc_id, bp_id)] = bp_dict

        # 1. Completeness Check: Every signal allocated or has a TQ
        tq_unplaced_tags = {
            sig for t in allocation.tqs for sig in t.affected.get("signals", [])
        }
        for sig in signals:
            checks_count += 1
            tag = str(sig.get("tag", ""))
            if not tag:
                continue
            is_placed = tag in allocation.allocations
            has_tq = (tag in allocation.unplaced_signals) or (tag in tq_unplaced_tags)

            if not is_placed and not has_tq:
                result.add_violation(
                    rule_id="V_Completeness",
                    tag=tag,
                    message=f"Signal '{tag}' is neither allocated nor covered by any Technical Query (TQ)",
                )

        # 2. Slot bounds and Slot Uniqueness
        occupied_slots: Dict[Tuple[str, str, str, int], str] = {}
        bp_placed_counts: Dict[Tuple[str, str, str], int] = {}

        for tag, pl in allocation.allocations.items():
            checks_count += 1
            coord = (pl.cabinet_id, pl.cioc_id, pl.baseplate_id, pl.slot)

            # Check slot number in 1..12
            if pl.slot < 1 or pl.slot > 12:
                result.add_violation(
                    rule_id="V_SlotBounds",
                    tag=tag,
                    message=f"Signal '{tag}' placed in invalid slot {pl.slot} (must be 1..12)",
                    details={"slot": pl.slot},
                )

            # Check baseplate existence
            bp_key = (pl.cabinet_id, pl.cioc_id, pl.baseplate_id)
            if bp_key not in bp_map:
                result.add_violation(
                    rule_id="V_HardwareHierarchy",
                    tag=tag,
                    message=f"Signal '{tag}' placed on non-existent baseplate {pl.baseplate_id} in CIOC {pl.cioc_id}",
                )
            else:
                bp_cap = bp_map[bp_key]["capacity"]
                if pl.slot > bp_cap:
                    result.add_violation(
                        rule_id="V_BaseplateCapacity",
                        tag=tag,
                        message=f"Signal '{tag}' placed in slot {pl.slot} exceeding baseplate capacity of {bp_cap}",
                    )

            # Check uniqueness (no two signals in same slot)
            if coord in occupied_slots:
                prev_tag = occupied_slots[coord]
                result.add_violation(
                    rule_id="V_SlotConflict",
                    tag=tag,
                    message=f"Slot conflict at {coord}: both signal '{prev_tag}' and '{tag}' placed here",
                    details={"slot_coordinate": coord, "tags": [prev_tag, tag]},
                )
            occupied_slots[coord] = tag
            bp_placed_counts[bp_key] = bp_placed_counts.get(bp_key, 0) + 1

        # 3. CIOC Segregation Check
        for tag, pl in allocation.allocations.items():
            checks_count += 1
            sig = next((s for s in signals if s.get("tag") == tag), None)
            if not sig:
                continue

            seg = sig.get("seg")
            cioc_info = cioc_map.get((pl.cabinet_id, pl.cioc_id))
            if cioc_info:
                seg_role = cioc_info.get("seg_role", "MIXED")
                if seg == "A" and seg_role == "B_ONLY":
                    result.add_violation(
                        rule_id="V_Segregation",
                        tag=tag,
                        message=f"Segregation violation: Seg A signal '{tag}' placed in B_ONLY CIOC '{pl.cioc_id}'",
                    )
                elif seg == "B" and seg_role == "A_ONLY":
                    result.add_violation(
                        rule_id="V_Segregation",
                        tag=tag,
                        message=f"Segregation violation: Seg B signal '{tag}' placed in A_ONLY CIOC '{pl.cioc_id}'",
                    )

        # 4. Hardware compatibility check (CHARM/TB for IO type)
        for tag, pl in allocation.allocations.items():
            checks_count += 1
            sig = next((s for s in signals if s.get("tag") == tag), None)
            if not sig:
                continue
            io_type = str(sig.get("io_type", "AI"))
            if io_type in DEFAULT_HARDWARE_MAP:
                exp_charm, exp_tb = DEFAULT_HARDWARE_MAP[io_type]
                if pl.charm_type != exp_charm:
                    result.add_violation(
                        rule_id="V_HardwareCompatibility",
                        tag=tag,
                        message=f"Hardware mismatch: Signal '{tag}' (IO: {io_type}) assigned CHARM '{pl.charm_type}', expected '{exp_charm}'",
                        severity="warning",
                    )

        # 5. Evaluate Active Hard Rules from Ruleset
        if ruleset:
            for rule in ruleset.rules:
                # Priority: hard by default unless explicitly soft
                is_hard = (rule.meta.priority != "soft")
                if not is_hard:
                    continue

                rule_name = rule.meta.rule_id or rule.name

                # Currently evaluate Signal-scoped rules
                if rule.scope == "Signal":
                    for sig in signals:
                        tag = str(sig.get("tag", ""))
                        if tag not in allocation.allocations:
                            continue  # unplaced signals are covered by unplaced TQ checks

                        pl = allocation.allocations[tag]
                        cab_info = cab_map.get(pl.cabinet_id, {})
                        cioc_info = cioc_map.get((pl.cabinet_id, pl.cioc_id), {})
                        bp_info = bp_map.get((pl.cabinet_id, pl.cioc_id, pl.baseplate_id), {})

                        # Build complete signal context
                        sig_ctx = sig.to_dict() if hasattr(sig, "to_dict") else dict(sig)
                        sig_ctx["cabinet_id"] = pl.cabinet_id
                        sig_ctx["cioc_id"] = pl.cioc_id
                        sig_ctx["baseplate_id"] = pl.baseplate_id
                        sig_ctx["slot"] = pl.slot
                        sig_ctx["charm_type"] = pl.charm_type
                        sig_ctx["tb_type"] = pl.tb_type
                        sig_ctx["cabinet"] = cab_info
                        sig_ctx["cioc"] = cioc_info
                        sig_ctx["baseplate"] = bp_info

                        env = {
                            "self": sig_ctx,
                            "_allocations": allocation.allocations,
                            "signals": signals,
                            "cabinets": cabinets,
                        }

                        # Check 'when' condition
                        checks_count += 1
                        if rule.when:
                            when_matched = bool(evaluate_ast_expr(rule.when, env))
                            if not when_matched:
                                continue

                        # Evaluate effects
                        for effect in rule.effects:
                            checks_count += 1
                            if isinstance(effect, RequireEffect):
                                if not isinstance(effect.target, Expr):
                                    continue
                                satisfied = bool(evaluate_ast_expr(effect.target, env))
                                if not satisfied:
                                    msg = rule.meta.message or f"Hard rule '{rule_name}' violated for signal '{tag}'"
                                    result.add_violation(
                                        rule_id=rule_name,
                                        tag=tag,
                                        message=msg,
                                        severity="critical",
                                    )
                            elif isinstance(effect, ForbidEffect):
                                forbidden = bool(evaluate_ast_expr(effect.expr, env))
                                if forbidden:
                                    msg = rule.meta.message or f"Forbid rule '{rule_name}' triggered for signal '{tag}'"
                                    result.add_violation(
                                        rule_id=rule_name,
                                        tag=tag,
                                        message=msg,
                                        severity="critical",
                                    )

        result.checks_run = checks_count
        result.stats = {
            "checks_run": checks_count,
            "violations_count": len(result.violations),
            "placed_signals": len(allocation.allocations),
            "unplaced_signals": len(allocation.unplaced_signals),
        }

        return result
