"""
ARL AST Normalization Module.

Produces a canonical, idempotent representation of the bound AST.
Preserves semantics and does not reorder non-commutative operations.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

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
    Objective,
    Override,
    PreferEffect,
    QuantifiedExpr,
    RequireEffect,
    Rule,
    RuleMeta,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
)

COMMUTATIVE_OPS = {"==", "!=", "+", "*", "and", "or"}


def expr_to_canonical_key(expr: Expr) -> str:
    """Deterministic string serialization used as canonical ordering key."""
    if isinstance(expr, LiteralExpr):
        return f"Lit({expr.value})"
    if isinstance(expr, IdentifierExpr):
        return f"Id({expr.name})"
    if isinstance(expr, SelfExpr):
        return "Self"
    if isinstance(expr, FieldAccessExpr):
        op = "?." if expr.is_optional else "."
        return f"Field({expr_to_canonical_key(expr.target)}{op}{expr.field_name})"
    if isinstance(expr, BinaryOp):
        return f"Bin({expr.op},{expr_to_canonical_key(expr.left)},{expr_to_canonical_key(expr.right)})"
    if isinstance(expr, UnaryOp):
        return f"Un({expr.op},{expr_to_canonical_key(expr.operand)})"
    if isinstance(expr, CallExpr):
        args_key = ",".join(expr_to_canonical_key(a) for a in expr.args)
        return f"Call({expr_to_canonical_key(expr.callee)},[{args_key}])"
    if isinstance(expr, AggregateExpr):
        return f"Agg({expr.func_name},{expr_to_canonical_key(expr.expr)})"
    if isinstance(expr, AsExpr):
        return f"As({expr_to_canonical_key(expr.expr)},{expr.target_type})"
    if isinstance(expr, IsSomeExpr):
        return f"IsSome({expr_to_canonical_key(expr.expr)},{expr.binding_var})"
    if isinstance(expr, IsNoneExpr):
        return f"IsNone({expr_to_canonical_key(expr.expr)})"
    return str(expr)


def normalize_expr(expr: Expr) -> Expr:
    """Normalize expression tree recursively."""
    if isinstance(expr, BinaryOp):
        norm_left = normalize_expr(expr.left)
        norm_right = normalize_expr(expr.right)

        # Commutative operator ordering
        if expr.op in COMMUTATIVE_OPS:
            key_left = expr_to_canonical_key(norm_left)
            key_right = expr_to_canonical_key(norm_right)
            if key_left > key_right:
                return BinaryOp(
                    left=norm_right,
                    op=expr.op,
                    right=norm_left,
                    location=expr.location,
                )

        return BinaryOp(
            left=norm_left,
            op=expr.op,
            right=norm_right,
            location=expr.location,
        )

    if isinstance(expr, UnaryOp):
        return UnaryOp(
            op=expr.op,
            operand=normalize_expr(expr.operand),
            location=expr.location,
        )

    if isinstance(expr, FieldAccessExpr):
        return FieldAccessExpr(
            target=normalize_expr(expr.target),
            field_name=expr.field_name,
            is_optional=expr.is_optional,
            location=expr.location,
        )

    if isinstance(expr, CallExpr):
        return CallExpr(
            callee=normalize_expr(expr.callee),
            args=[normalize_expr(a) for a in expr.args],
            location=expr.location,
        )

    if isinstance(expr, IndexExpr):
        return IndexExpr(
            target=normalize_expr(expr.target),
            index=normalize_expr(expr.index),
            location=expr.location,
        )

    if isinstance(expr, QuantifiedExpr):
        return QuantifiedExpr(
            kind=expr.kind,
            var_name=expr.var_name,
            collection=normalize_expr(expr.collection),
            where_clause=normalize_expr(expr.where_clause) if expr.where_clause else None,
            condition=normalize_expr(expr.condition),
            location=expr.location,
        )

    if isinstance(expr, AggregateExpr):
        return AggregateExpr(
            func_name=expr.func_name,
            expr=normalize_expr(expr.expr),
            location=expr.location,
        )

    if isinstance(expr, AsExpr):
        return AsExpr(
            expr=normalize_expr(expr.expr),
            target_type=expr.target_type,
            location=expr.location,
        )

    if isinstance(expr, IsSomeExpr):
        return IsSomeExpr(
            expr=normalize_expr(expr.expr),
            binding_var=expr.binding_var,
            location=expr.location,
        )

    if isinstance(expr, IsNoneExpr):
        return IsNoneExpr(
            expr=normalize_expr(expr.expr),
            location=expr.location,
        )

    if isinstance(expr, ListLiteralExpr):
        return ListLiteralExpr(
            elements=[normalize_expr(e) for e in expr.elements],
            location=expr.location,
        )

    return expr


def normalize_meta(meta: RuleMeta) -> RuleMeta:
    """Sort tags deterministically."""
    return RuleMeta(
        priority=meta.priority,
        weight=meta.weight,
        message=meta.message,
        tags=sorted(list(set(meta.tags))),
        rule_id=meta.rule_id,
        location=meta.location,
    )


def normalize_effect(effect: Effect) -> Effect:
    """Normalize expressions within an effect."""
    if isinstance(effect, RequireEffect):
        if isinstance(effect.target, Expr):
            return RequireEffect(target=normalize_expr(effect.target), location=effect.location)
        return effect

    if isinstance(effect, ForbidEffect):
        return ForbidEffect(expr=normalize_expr(effect.expr), location=effect.location)

    if isinstance(effect, PreferEffect):
        return PreferEffect(expr=normalize_expr(effect.expr), location=effect.location)

    if isinstance(effect, AssignEffect):
        return AssignEffect(
            field_name=effect.field_name,
            expr=normalize_expr(effect.expr),
            location=effect.location,
        )

    return effect


def normalize_rule(rule: Rule) -> Rule:
    """Normalize expressions and metadata in a rule."""
    norm_when = normalize_expr(rule.when) if rule.when else None
    norm_effects = [normalize_effect(e) for e in rule.effects]
    norm_meta = normalize_meta(rule.meta)

    return Rule(
        name=rule.name,
        scope=rule.scope,
        when=norm_when,
        effects=norm_effects,
        meta=norm_meta,
        location=rule.location,
    )


def normalize_ruleset(ruleset: Ruleset) -> Ruleset:
    """
    Produce canonical normalized AST for ruleset.
    Idempotent: normalize(normalize(ruleset)) == normalize(ruleset).
    """
    # Sort includes by path
    sorted_includes = sorted(ruleset.includes, key=lambda i: i.path)

    # Normalize rules
    norm_rules = [normalize_rule(r) for r in ruleset.rules]

    # Normalize objectives
    norm_objectives: List[Objective] = []
    for obj in ruleset.objectives:
        norm_prim = [normalize_expr(p) for p in obj.primary]
        norm_sec = [normalize_expr(s) for s in obj.secondary]
        sorted_weights = {k: obj.weights[k] for k in sorted(obj.weights.keys())}
        norm_objectives.append(
            Objective(
                name=obj.name,
                primary=norm_prim,
                secondary=norm_sec,
                weights=sorted_weights,
                location=obj.location,
            )
        )

    # Normalize overrides
    norm_overrides: List[Override] = []
    for ov in ruleset.overrides:
        norm_when = normalize_expr(ov.when) if ov.when else None
        norm_effects = [normalize_effect(e) for e in ov.effects]
        norm_meta = normalize_meta(ov.meta)
        norm_overrides.append(
            Override(
                target_rule=ov.target_rule,
                when=norm_when,
                effects=norm_effects,
                meta=norm_meta,
                location=ov.location,
            )
        )

    return Ruleset(
        name=ruleset.name,
        extends=ruleset.extends,
        includes=sorted_includes,
        rules=norm_rules,
        objectives=norm_objectives,
        overrides=norm_overrides,
        location=ruleset.location,
    )
