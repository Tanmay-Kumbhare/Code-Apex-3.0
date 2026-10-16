"""
ARL Constraint IR Lowering Engine.

Transforms effective AST into solver-agnostic Constraint IR nodes.
Preserves rule provenance, priority, weights, and source locations.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from arl.ast import (
    AggregateExpr,
    AsExpr,
    AssignEffect,
    BinaryOp,
    CallExpr,
    Effect,
    Expr,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    IndexExpr,
    IsNoneExpr,
    IsSomeExpr,
    ListLiteralExpr,
    LiteralExpr,
    Objective as ASTObjective,
    PreferEffect,
    QuantifiedExpr,
    RequireEffect,
    Rule,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
)
from arl.errors import ErrorCollector, Phase, SourceLocation
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
from arl.predicates import PredicateRegistry, get_builtin_predicate_registry
from arl.schema import Schema


class Lowerer:
    """Translates effective ARL AST into solver-agnostic constraint IR."""

    def __init__(
        self,
        schema: Schema,
        predicate_registry: Optional[PredicateRegistry] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> None:
        self.schema = schema
        self.registry = predicate_registry or get_builtin_predicate_registry()
        self.collector = collector or ErrorCollector()
        self.rule_map: Dict[str, Rule] = {}

    def lower_ruleset(self, ruleset: Ruleset) -> List[IRNode]:
        """Lower complete ruleset into a list of IR constraint nodes."""
        self.rule_map = {r.name: r for r in ruleset.rules}
        ir_nodes: List[IRNode] = []

        # 1. Lower rules
        for rule in ruleset.rules:
            rule_nodes = self.lower_rule(rule)
            ir_nodes.extend(rule_nodes)

        # 2. Lower objectives
        for obj in ruleset.objectives:
            ir_obj = self.lower_objective(obj)
            ir_nodes.append(ir_obj)

        return ir_nodes

    def lower_rule(self, rule: Rule) -> List[IRNode]:
        """Lower a single rule into IR nodes."""
        nodes: List[IRNode] = []
        rule_priority = rule.meta.priority or "hard"
        rule_weight = rule.meta.weight or 1.0

        # Guard condition
        guard_ir: Optional[IRNode] = None
        if rule.when:
            guard_ir = self.lower_expr(rule.when, scope=rule.scope)

        for effect in rule.effects:
            effect_nodes = self.lower_effect(
                effect=effect,
                scope=rule.scope,
                rule_id=rule.name,
                priority=rule_priority,
                weight=rule_weight,
                location=rule.location,
            )
            for enode in effect_nodes:
                # Wrap with implication if guard condition exists
                if guard_ir:
                    wrapped = Implies(
                        condition=guard_ir,
                        consequence=enode,
                        rule_id=rule.name,
                        priority=rule_priority,
                        weight=rule_weight,
                        location=rule.location,
                    )
                    nodes.append(wrapped)
                else:
                    nodes.append(enode)

        return nodes

    def lower_effect(
        self,
        effect: Effect,
        scope: str,
        rule_id: str,
        priority: str,
        weight: float,
        location: Optional[SourceLocation],
    ) -> List[IRNode]:
        """Lower an effect into IR nodes."""
        if isinstance(effect, RequireEffect):
            # Inlining rule reference: require rule X
            if isinstance(effect.target, RuleRef):
                ref_name = effect.target.rule_name
                if ref_name in self.rule_map:
                    target_rule = self.rule_map[ref_name]
                    return self.lower_rule(target_rule)
                return []

            target_ir = self.lower_expr(effect.target, scope=scope)
            eq_node = Eq(
                left=target_ir,
                right=True,
                rule_id=rule_id,
                priority=priority,
                weight=weight,
                location=location,
            )
            return [eq_node]

        elif isinstance(effect, ForbidEffect):
            target_ir = self.lower_expr(effect.expr, scope=scope)
            eq_node = Eq(
                left=target_ir,
                right=False,
                rule_id=rule_id,
                priority=priority,
                weight=weight,
                location=location,
            )
            return [eq_node]

        elif isinstance(effect, AssignEffect):
            var_node = Var(name=f"{scope}.{effect.field_name}", var_type="Any")
            val_ir = self.lower_expr(effect.expr, scope=scope)
            eq_node = Eq(
                left=var_node,
                right=val_ir,
                rule_id=rule_id,
                priority=priority,
                weight=weight,
                location=location,
            )
            return [eq_node]

        elif isinstance(effect, PreferEffect):
            target_ir = self.lower_expr(effect.expr, scope=scope)
            soft_node = Soft(
                node=target_ir,
                weight=weight,
                rule_id=rule_id,
                priority="soft",
                location=location,
            )
            return [soft_node]

        return []

    def lower_expr(self, expr: Expr, scope: str) -> IRNode:
        """Lower AST expression into IR expression node."""
        if isinstance(expr, LiteralExpr):
            return Var(name=str(expr.value), var_type=expr.literal_type)

        if isinstance(expr, IdentifierExpr):
            return Var(name=f"{scope}.{expr.name}", var_type="Any")

        if isinstance(expr, SelfExpr):
            return Var(name=scope, var_type="Entity")

        if isinstance(expr, FieldAccessExpr):
            target_ir = self.lower_expr(expr.target, scope=scope)
            return Var(name=f"{target_ir.name}.{expr.field_name}", var_type="Any")

        if isinstance(expr, BinaryOp):
            left_ir = self.lower_expr(expr.left, scope=scope)
            right_ir = self.lower_expr(expr.right, scope=scope)

            if expr.op == "==":
                return Eq(left=left_ir, right=right_ir)
            if expr.op == "!=":
                return Neq(left=left_ir, right=right_ir)
            if expr.op == "<":
                return Lt(left=left_ir, right=right_ir)
            if expr.op == "<=":
                return Lte(left=left_ir, right=right_ir)
            if expr.op == ">":
                return Gt(left=left_ir, right=right_ir)
            if expr.op == ">=":
                return Gte(left=left_ir, right=right_ir)
            if expr.op == "and":
                return And(nodes=[left_ir, right_ir])
            if expr.op == "or":
                return Or(nodes=[left_ir, right_ir])

            return Var(name=f"({left_ir.name} {expr.op} {right_ir.name})", var_type="Expr")

        if isinstance(expr, UnaryOp):
            operand_ir = self.lower_expr(expr.operand, scope=scope)
            if expr.op == "not":
                return Not(node=operand_ir)
            return Var(name=f"-{operand_ir.name}", var_type="Number")

        if isinstance(expr, CallExpr):
            if isinstance(expr.callee, IdentifierExpr):
                pname = expr.callee.name
                lower_fn = self.registry.get_lower_fn(pname)
                args_ir = [self.lower_expr(a, scope=scope) for a in expr.args]
                if lower_fn:
                    try:
                        return lower_fn(*[a.name for a in args_ir])
                    except Exception:
                        pass
                return Var(name=f"{pname}({', '.join(a.name for a in args_ir)})", var_type="Pred")

        if isinstance(expr, QuantifiedExpr):
            pred_ir = self.lower_expr(expr.condition, scope=expr.var_name)
            coll_ir = self.lower_expr(expr.collection, scope=scope)
            if expr.kind == "forall":
                return ForAll(var_name=expr.var_name, collection=coll_ir, predicate=pred_ir)
            return Exists(var_name=expr.var_name, collection=coll_ir, predicate=pred_ir)

        if isinstance(expr, AggregateExpr):
            inner_ir = self.lower_expr(expr.expr, scope=scope)
            if expr.func_name == "count":
                return Count(vars=[inner_ir], op=">=", value=0)
            if expr.func_name == "sum":
                return Sum(vars=[inner_ir], op=">=", value=0)
            return Var(name=f"{expr.func_name}({inner_ir.name})", var_type="Agg")

        if isinstance(expr, AsExpr):
            return self.lower_expr(expr.expr, scope=scope)

        return Var(name=str(expr), var_type="Any")

    def lower_objective(self, obj: ASTObjective) -> IRObjective:
        """Lower an objective declaration into an IRObjective node."""
        terms_ir = [self.lower_expr(t, scope="Project") for t in (obj.primary + obj.secondary)]
        return IRObjective(
            terms=terms_ir,
            weights=obj.weights,
            rule_id=obj.name,
            priority="soft",
            location=obj.location,
        )
