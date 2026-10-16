"""
ARL Type Checker and Semantic Analyzer.

Implements Part 3 Type System and semantic checks:
  - E2001: Unknown entity type
  - E2002: Unknown field on entity
  - E2003: Unknown predicate
  - E2004: Predicate arity mismatch
  - E2005: Type mismatch
  - E2006: Missing Option unwrap
  - E2007: Field access on non-entity
  - E2008: Aggregate over non-collection
  - E2014: Objective references unknown weight
  - E2015: Invalid regex
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

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
from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error
from arl.schema import Schema
from arl.types import (
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_NULL,
    TYPE_STRING,
    ArlType,
    DomainType,
    EntityType,
    EnumType,
    ListType,
    OptionType,
    PrimitiveType,
    ReferenceType,
    SetType,
    can_coerce,
)

# Built-in Predicates Catalog (Part 4.1)
BUILTIN_PREDICATES: Dict[str, Dict[str, Any]] = {
    # Relational
    "same_cabinet": {"arity": 1, "return": TYPE_BOOL},
    "same_cioc": {"arity": 1, "return": TYPE_BOOL},
    "same_baseplate": {"arity": 1, "return": TYPE_BOOL},
    "same_charm": {"arity": 2, "return": TYPE_BOOL},
    "adjacent": {"arity": 2, "return": TYPE_BOOL},
    "distance": {"arity": 2, "return": TYPE_INT},
    "shares_cable": {"arity": 2, "return": TYPE_BOOL},
    "shares_group": {"arity": 2, "return": TYPE_BOOL},
    # Capacity
    "free_slots": {"arity": 1, "return": TYPE_INT},
    "used_slots": {"arity": 1, "return": TYPE_INT},
    "fill_percent": {"arity": 1, "return": TYPE_FLOAT},
    "spare_percent": {"arity": 1, "return": TYPE_FLOAT},
    # Contiguity
    "contiguous": {"arity": 2, "return": TYPE_BOOL},
    "consecutive": {"arity": 1, "return": TYPE_BOOL},
    "ordered_by": {"arity": 3, "return": TYPE_BOOL},
    # Cable/Pair
    "pair_index": {"arity": 1, "return": TYPE_INT},
    "cable_size": {"arity": 1, "return": DomainType("CableSize")},
    "wired_spare": {"arity": 1, "return": TYPE_BOOL},
    "orphan_pair": {"arity": 1, "return": TYPE_BOOL},
    # Hardware
    "charm_for": {"arity": 1, "return": DomainType("CharmType")},
    "tb_for": {"arity": 1, "return": DomainType("TbType")},
    "compatible": {"arity": 2, "return": TYPE_BOOL},
    "mixable": {"arity": 2, "return": TYPE_BOOL},
    # Naming
    "matches_pattern": {"arity": 2, "return": TYPE_BOOL},
    "format": {"arity": 2, "return": TYPE_STRING},
    # Revision
    "changed_since": {"arity": 1, "return": TYPE_BOOL},
    "added_in": {"arity": 1, "return": TYPE_BOOL},
    "deleted_in": {"arity": 1, "return": TYPE_BOOL},
    "pinned": {"arity": 1, "return": TYPE_BOOL},
    "moved_since": {"arity": 1, "return": TYPE_BOOL},
}


class TypeEnvironment:
    """Type environment tracking variable bindings and scope."""

    def __init__(
        self,
        schema: Schema,
        scope_entity: Optional[str] = None,
        parent: Optional[TypeEnvironment] = None,
    ) -> None:
        self.schema = schema
        self.scope_entity = scope_entity
        self.parent = parent
        self.bindings: Dict[str, ArlType] = {}

    def lookup(self, name: str) -> Optional[ArlType]:
        if name in self.bindings:
            return self.bindings[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def bind(self, name: str, arl_type: ArlType) -> None:
        self.bindings[name] = arl_type


class TypeChecker:
    """Performs static type analysis on an ARL AST against a Schema."""

    def __init__(
        self,
        schema: Schema,
        collector: Optional[ErrorCollector] = None,
        registered_predicates: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.schema = schema
        self.collector = collector or ErrorCollector()
        self.predicates = {**BUILTIN_PREDICATES, **(registered_predicates or {})}
        # Store node type annotations: id(node) -> ArlType
        self.type_annotations: Dict[int, ArlType] = {}

    def get_inferred_type(self, node: Expr) -> Optional[ArlType]:
        return self.type_annotations.get(id(node))

    def check_ruleset(self, ruleset: Ruleset) -> None:
        """Typecheck all rules, objectives, and overrides in ruleset."""
        for rule in ruleset.rules:
            self.check_rule(rule)

        for override in ruleset.overrides:
            self.check_override(override, ruleset)

        for objective in ruleset.objectives:
            self.check_objective(objective)

    def check_rule(self, rule: Rule) -> None:
        """Typecheck a single rule."""
        rule_id = rule.name

        # E2001: Unknown entity scope
        entity_def = self.schema.get_entity(rule.scope)
        if not entity_def:
            self.collector.add_error(
                code="E2001",
                message=f"Unknown entity type '{rule.scope}' in rule '{rule.name}' scope",
                rule_id=rule_id,
                location=rule.location,
                phase=Phase.TYPECHECK,
                suggestion=f"Valid entities are: {', '.join(sorted(self.schema.entities.keys()))}",
            )
            return

        env = TypeEnvironment(self.schema, scope_entity=rule.scope)
        env.bind("self", EntityType(rule.scope))

        # Check when condition
        if rule.when:
            t_cond = self.check_expr(rule.when, env, rule_id)
            if t_cond and t_cond != TYPE_BOOL and not (isinstance(t_cond, OptionType) and t_cond.inner == TYPE_BOOL):
                self.collector.add_error(
                    code="E2005",
                    message=f"Rule 'when' condition must evaluate to Bool, found '{t_cond}'",
                    rule_id=rule_id,
                    location=rule.when.location,
                    phase=Phase.TYPECHECK,
                )

        # Check effects
        for effect in rule.effects:
            self.check_effect(effect, env, rule.scope, rule_id)

    def check_override(self, override: Override, ruleset: Ruleset) -> None:
        """Typecheck an override declaration."""
        target_rule = ruleset.get_rule(override.target_rule)
        scope = target_rule.scope if target_rule else "Signal"

        env = TypeEnvironment(self.schema, scope_entity=scope)
        env.bind("self", EntityType(scope))

        if override.when:
            t_cond = self.check_expr(override.when, env, override.target_rule)
            if t_cond and t_cond != TYPE_BOOL:
                self.collector.add_error(
                    code="E2005",
                    message=f"Override 'when' condition must evaluate to Bool, found '{t_cond}'",
                    location=override.when.location,
                    phase=Phase.TYPECHECK,
                )

        for effect in override.effects:
            self.check_effect(effect, env, scope, override.target_rule)

    def check_objective(self, objective: Objective) -> None:
        """Typecheck an objective declaration."""
        env = TypeEnvironment(self.schema, scope_entity="Project")
        env.bind("self", EntityType("Project"))

        all_terms = objective.primary + objective.secondary
        term_names: Set[str] = set()

        for term in all_terms:
            t = self.check_expr(term, env, objective.name)
            if isinstance(term, CallExpr) and isinstance(term.callee, IdentifierExpr):
                term_names.add(term.callee.name)
            elif isinstance(term, AggregateExpr):
                term_names.add(term.func_name)
            elif isinstance(term, IdentifierExpr):
                term_names.add(term.name)

        # Check weights: E2014 if weight refers to unknown term
        for w_key in objective.weights.keys():
            if term_names and w_key not in term_names:
                self.collector.add_error(
                    code="E2014",
                    message=f"Objective '{objective.name}' weights references unknown term '{w_key}'",
                    location=objective.location,
                    phase=Phase.TYPECHECK,
                    suggestion=f"Objective terms available: {', '.join(sorted(term_names))}",
                )

    def check_effect(
        self,
        effect: Effect,
        env: TypeEnvironment,
        scope: str,
        rule_id: str,
    ) -> None:
        """Typecheck an effect (require, forbid, assign, prefer)."""
        if isinstance(effect, RequireEffect):
            if isinstance(effect.target, Expr):
                t = self.check_expr(effect.target, env, rule_id)
                if t and t != TYPE_BOOL and not (isinstance(t, OptionType) and t.inner == TYPE_BOOL):
                    self.collector.add_error(
                        code="E2005",
                        message=f"Require effect expression must evaluate to Bool, found '{t}'",
                        rule_id=rule_id,
                        location=effect.location,
                        phase=Phase.TYPECHECK,
                    )

        elif isinstance(effect, ForbidEffect):
            t = self.check_expr(effect.expr, env, rule_id)
            if t and t != TYPE_BOOL and not (isinstance(t, OptionType) and t.inner == TYPE_BOOL):
                self.collector.add_error(
                    code="E2005",
                    message=f"Forbid effect expression must evaluate to Bool, found '{t}'",
                    rule_id=rule_id,
                    location=effect.location,
                    phase=Phase.TYPECHECK,
                )

        elif isinstance(effect, PreferEffect):
            t = self.check_expr(effect.expr, env, rule_id)
            if t and t != TYPE_BOOL and not (isinstance(t, OptionType) and t.inner == TYPE_BOOL):
                self.collector.add_error(
                    code="E2005",
                    message=f"Prefer effect expression must evaluate to Bool, found '{t}'",
                    rule_id=rule_id,
                    location=effect.location,
                    phase=Phase.TYPECHECK,
                )

        elif isinstance(effect, AssignEffect):
            # E2002: Check field exists on scope entity
            entity_def = self.schema.get_entity(scope)
            if not entity_def or effect.field_name not in entity_def.fields:
                self.collector.add_error(
                    code="E2002",
                    message=f"Unknown field '{effect.field_name}' on entity '{scope}' in assign effect",
                    rule_id=rule_id,
                    location=effect.location,
                    phase=Phase.TYPECHECK,
                )
                return

            fdef = entity_def.fields[effect.field_name]
            expected_type = self.schema.resolve_type(fdef.type_str)

            val_type = self.check_expr(effect.expr, env, rule_id)
            if val_type:
                # If target is Option<T>, value can be T or Option<T>
                inner_expected = expected_type.inner if isinstance(expected_type, OptionType) else expected_type
                is_valid_assign = (
                    can_coerce(val_type, inner_expected)
                    or can_coerce(val_type, expected_type)
                    or (isinstance(inner_expected, EnumType) and val_type == TYPE_STRING)
                    or (isinstance(inner_expected, DomainType) and val_type in (TYPE_STRING, TYPE_INT))
                )
                if not is_valid_assign:
                    self.collector.add_error(
                        code="E2005",
                        message=f"Type mismatch in assign to field '{effect.field_name}': expected '{expected_type}', found '{val_type}'",
                        rule_id=rule_id,
                        location=effect.location,
                        phase=Phase.TYPECHECK,
                    )

    def check_expr(
        self,
        expr: Expr,
        env: TypeEnvironment,
        rule_id: Optional[str] = None,
    ) -> Optional[ArlType]:
        """Typecheck expression recursively and annotate its inferred type."""
        t = self._check_expr_internal(expr, env, rule_id)
        if t:
            self.type_annotations[id(expr)] = t
        return t

    def _check_expr_internal(
        self,
        expr: Expr,
        env: TypeEnvironment,
        rule_id: Optional[str] = None,
    ) -> Optional[ArlType]:
        # Literals
        if isinstance(expr, LiteralExpr):
            if expr.literal_type == "int":
                return TYPE_INT
            if expr.literal_type == "float":
                return TYPE_FLOAT
            if expr.literal_type == "string":
                return TYPE_STRING
            if expr.literal_type == "bool":
                return TYPE_BOOL
            if expr.literal_type == "null":
                return TYPE_NULL
            return TYPE_STRING

        # List Literal
        if isinstance(expr, ListLiteralExpr):
            elem_types = [self.check_expr(e, env, rule_id) for e in expr.elements]
            elem_type = elem_types[0] if elem_types and elem_types[0] else TYPE_INT
            return ListType(elem_type)

        # Self
        if isinstance(expr, SelfExpr):
            if env.scope_entity:
                return EntityType(env.scope_entity)
            return None

        # Identifier
        if isinstance(expr, IdentifierExpr):
            bound = env.lookup(expr.name)
            if bound:
                return bound

            # Check if it's a field on self
            if env.scope_entity:
                entity_def = self.schema.get_entity(env.scope_entity)
                if entity_def and expr.name in entity_def.fields:
                    fdef = entity_def.fields[expr.name]
                    return self.schema.resolve_type(fdef.type_str)

            # Check if it's an enum variant or domain identifier
            for ename, edef in self.schema.enums.items():
                if expr.name in edef.values:
                    return EnumType(ename, edef.values)

            # If it's a known predicate name
            if expr.name in self.predicates:
                return PrimitiveType("Function")

            # Unknown field or variable: E2002
            if env.scope_entity:
                self.collector.add_error(
                    code="E2002",
                    message=f"Unknown identifier or field '{expr.name}' on '{env.scope_entity}'",
                    rule_id=rule_id,
                    location=expr.location,
                    phase=Phase.TYPECHECK,
                )
            return None

        # Field Access: target.field or target?.field
        if isinstance(expr, FieldAccessExpr):
            target_type = self.check_expr(expr.target, env, rule_id)
            if not target_type:
                return None

            # Handle Option unwrapping: E2006
            if isinstance(target_type, OptionType):
                if not expr.is_optional:
                    self.collector.add_error(
                        code="E2006",
                        message=f"Missing Option unwrap on '{expr.target}': access with '?.' instead of '.'",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                        suggestion=f"Use '?.' (e.g. target?.{expr.field_name}) or 'is some'",
                    )
                target_type = target_type.inner

            # Check target is Entity: E2007
            entity_name: Optional[str] = None
            if isinstance(target_type, EntityType):
                entity_name = target_type.name
            elif isinstance(target_type, ReferenceType):
                entity_name = target_type.target_entity
            else:
                self.collector.add_error(
                    code="E2007",
                    message=f"Field access on non-entity type '{target_type}' (field '{expr.field_name}')",
                    rule_id=rule_id,
                    location=expr.location,
                    phase=Phase.TYPECHECK,
                )
                return None

            # Check field exists on entity: E2002
            entity_def = self.schema.get_entity(entity_name)
            if not entity_def or expr.field_name not in entity_def.fields:
                self.collector.add_error(
                    code="E2002",
                    message=f"Unknown field '{expr.field_name}' on entity '{entity_name}'",
                    rule_id=rule_id,
                    location=expr.location,
                    phase=Phase.TYPECHECK,
                )
                return None

            fdef = entity_def.fields[expr.field_name]
            field_type = self.schema.resolve_type(fdef.type_str)

            if expr.is_optional and not isinstance(field_type, OptionType):
                return OptionType(field_type)
            return field_type

        # Call: callee(args)
        if isinstance(expr, CallExpr):
            if isinstance(expr.callee, IdentifierExpr):
                pname = expr.callee.name
                if pname not in self.predicates:
                    self.collector.add_error(
                        code="E2003",
                        message=f"Unknown predicate '{pname}'",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                        suggestion=f"Available predicates: {', '.join(sorted(self.predicates.keys()))}",
                    )
                    return None

                pinfo = self.predicates[pname]
                expected_arity = pinfo.get("arity", len(pinfo.get("params", [])))
                actual_arity = len(expr.args)

                # Check arity: E2004
                if actual_arity != expected_arity:
                    self.collector.add_error(
                        code="E2004",
                        message=f"Predicate '{pname}' arity mismatch: expected {expected_arity} arguments, found {actual_arity}",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                    )

                # Typecheck arguments
                for a in expr.args:
                    self.check_expr(a, env, rule_id)

                return pinfo.get("return", TYPE_BOOL)

            # Non-identifier callee
            for a in expr.args:
                self.check_expr(a, env, rule_id)
            return TYPE_BOOL

        # Aggregate: count(expr), sum(expr), min/max/avg(expr)
        if isinstance(expr, AggregateExpr):
            inner_type = self.check_expr(expr.expr, env, rule_id)
            if inner_type:
                # E2008: Aggregate over non-collection
                if not inner_type.is_collection:
                    self.collector.add_error(
                        code="E2008",
                        message=f"Aggregate '{expr.func_name}' cannot operate on non-collection type '{inner_type}'",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                    )
                    return None

            if expr.func_name == "count":
                return TYPE_INT
            if expr.func_name == "sum":
                return TYPE_FLOAT if inner_type and isinstance(inner_type, (ListType, SetType)) and inner_type.element == TYPE_FLOAT else TYPE_INT
            if expr.func_name in ("min", "max", "avg"):
                # min/max/avg on empty collection returns Option<Null> per §3.4
                elem_t = inner_type.element if inner_type and isinstance(inner_type, (ListType, SetType)) else TYPE_INT
                return OptionType(elem_t)
            if expr.func_name == "distinct":
                elem_t = inner_type.element if inner_type and isinstance(inner_type, (ListType, SetType)) else TYPE_STRING
                return SetType(elem_t)
            return TYPE_INT

        # Quantified: forall x in coll where cond : cond
        if isinstance(expr, QuantifiedExpr):
            coll_type = self.check_expr(expr.collection, env, rule_id)
            elem_type: ArlType = TYPE_STRING

            if coll_type:
                if not coll_type.is_collection:
                    self.collector.add_error(
                        code="E2008",
                        message=f"Quantifier '{expr.kind}' collection must be a collection, found '{coll_type}'",
                        rule_id=rule_id,
                        location=expr.collection.location,
                        phase=Phase.TYPECHECK,
                    )
                elif isinstance(coll_type, (ListType, SetType)):
                    elem_type = coll_type.element

            sub_env = TypeEnvironment(self.schema, parent=env)
            sub_env.bind(expr.var_name, elem_type)

            if expr.where_clause:
                where_t = self.check_expr(expr.where_clause, sub_env, rule_id)
                if where_t and where_t != TYPE_BOOL:
                    self.collector.add_error(
                        code="E2005",
                        message=f"Quantifier 'where' condition must evaluate to Bool, found '{where_t}'",
                        rule_id=rule_id,
                        location=expr.where_clause.location,
                        phase=Phase.TYPECHECK,
                    )

            cond_t = self.check_expr(expr.condition, sub_env, rule_id)
            if cond_t and cond_t != TYPE_BOOL:
                self.collector.add_error(
                    code="E2005",
                    message=f"Quantifier body condition must evaluate to Bool, found '{cond_t}'",
                    rule_id=rule_id,
                    location=expr.condition.location,
                    phase=Phase.TYPECHECK,
                )

            return TYPE_BOOL

        # Explicit Cast: as TargetType
        if isinstance(expr, AsExpr):
            inner_t = self.check_expr(expr.expr, env, rule_id)
            target_t = self.schema.resolve_type(expr.target_type)

            # Check valid explicit coercion: Int -> Slot, Tag -> String
            if inner_t and not can_coerce(inner_t, target_t, explicit=True):
                self.collector.add_error(
                    code="E2005",
                    message=f"Cannot cast from '{inner_t}' to '{target_t}'. Only Int -> Slot or Tag -> String allowed",
                    rule_id=rule_id,
                    location=expr.location,
                    phase=Phase.TYPECHECK,
                )
            return target_t

        # is some / is none
        if isinstance(expr, IsSomeExpr):
            inner_t = self.check_expr(expr.expr, env, rule_id)
            if inner_t and not inner_t.is_option and inner_t != TYPE_NULL:
                # Warning or type error if checking option on non-option
                pass
            if expr.binding_var and inner_t and isinstance(inner_t, OptionType):
                env.bind(expr.binding_var, inner_t.inner)
            return TYPE_BOOL

        if isinstance(expr, IsNoneExpr):
            self.check_expr(expr.expr, env, rule_id)
            return TYPE_BOOL

        # Unary Op
        if isinstance(expr, UnaryOp):
            sub_t = self.check_expr(expr.operand, env, rule_id)
            if expr.op == "not":
                if sub_t and sub_t != TYPE_BOOL and not (isinstance(sub_t, OptionType) and sub_t.inner == TYPE_BOOL):
                    self.collector.add_error(
                        code="E2005",
                        message=f"Operator 'not' expects Bool, found '{sub_t}'",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                    )
                return TYPE_BOOL
            if expr.op == "-":
                if sub_t and sub_t not in (TYPE_INT, TYPE_FLOAT):
                    self.collector.add_error(
                        code="E2005",
                        message=f"Unary '-' expects Int or Float, found '{sub_t}'",
                        rule_id=rule_id,
                        location=expr.location,
                        phase=Phase.TYPECHECK,
                    )
                return sub_t or TYPE_INT
            return sub_t

        # Binary Op
        if isinstance(expr, BinaryOp):
            t_left = self.check_expr(expr.left, env, rule_id)
            t_right = self.check_expr(expr.right, env, rule_id)

            # Regex validation: matches pattern
            if expr.op == "matches":
                if isinstance(expr.right, LiteralExpr) and expr.right.literal_type == "string":
                    pattern = str(expr.right.value)
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        # E2015: Invalid regex
                        self.collector.add_error(
                            code="E2015",
                            message=f"Invalid regular expression '{pattern}': {e}",
                            rule_id=rule_id,
                            location=expr.right.location,
                            phase=Phase.TYPECHECK,
                        )
                return TYPE_BOOL

            # Boolean ops: and, or
            if expr.op in ("and", "or"):
                for t, side in ((t_left, "left"), (t_right, "right")):
                    if t and t != TYPE_BOOL and not (isinstance(t, OptionType) and t.inner == TYPE_BOOL):
                        self.collector.add_error(
                            code="E2005",
                            message=f"Operator '{expr.op}' expects Bool on {side}, found '{t}'",
                            rule_id=rule_id,
                            location=expr.location,
                            phase=Phase.TYPECHECK,
                        )
                return TYPE_BOOL

            # Comparison ops
            if expr.op in ("==", "!=", "<", "<=", ">", ">="):
                if t_left and t_right:
                    u_left = t_left.inner if isinstance(t_left, OptionType) else t_left
                    u_right = t_right.inner if isinstance(t_right, OptionType) else t_right

                    # Coerce Int to Float if needed
                    if u_left == TYPE_INT and u_right == TYPE_FLOAT:
                        pass
                    elif u_left == TYPE_FLOAT and u_right == TYPE_INT:
                        pass
                    elif u_left == TYPE_NULL or u_right == TYPE_NULL:
                        pass
                    elif isinstance(u_left, DomainType) and u_left.name == "Slot" and u_right == TYPE_INT:
                        pass
                    elif isinstance(u_right, DomainType) and u_right.name == "Slot" and u_left == TYPE_INT:
                        pass
                    elif isinstance(u_left, EnumType) and u_right == TYPE_STRING:
                        pass
                    elif isinstance(u_right, EnumType) and u_left == TYPE_STRING:
                        pass
                    elif isinstance(u_left, DomainType) and u_right == TYPE_STRING:
                        pass
                    elif isinstance(u_right, DomainType) and u_left == TYPE_STRING:
                        pass
                    elif u_left != u_right and not can_coerce(u_left, u_right):
                        self.collector.add_error(
                            code="E2005",
                            message=f"Type mismatch in comparison '{expr.op}': cannot compare '{t_left}' with '{t_right}'",
                            rule_id=rule_id,
                            location=expr.location,
                            phase=Phase.TYPECHECK,
                        )
                return TYPE_BOOL

            # Membership: in, not in
            if expr.op in ("in", "not in"):
                if t_right and not t_right.is_collection:
                    self.collector.add_error(
                        code="E2008",
                        message=f"Right-hand side of '{expr.op}' must be a collection, found '{t_right}'",
                        rule_id=rule_id,
                        location=expr.right.location,
                        phase=Phase.TYPECHECK,
                    )
                return TYPE_BOOL

            # Arithmetic ops: +, -, *, /, %
            if expr.op in ("+", "-", "*", "/", "%"):
                if t_left and t_left not in (TYPE_INT, TYPE_FLOAT):
                    self.collector.add_error(
                        code="E2005",
                        message=f"Arithmetic operator '{expr.op}' expects numeric left operand, found '{t_left}'",
                        rule_id=rule_id,
                        location=expr.left.location,
                        phase=Phase.TYPECHECK,
                    )
                if t_right and t_right not in (TYPE_INT, TYPE_FLOAT):
                    self.collector.add_error(
                        code="E2005",
                        message=f"Arithmetic operator '{expr.op}' expects numeric right operand, found '{t_right}'",
                        rule_id=rule_id,
                        location=expr.right.location,
                        phase=Phase.TYPECHECK,
                    )
                if t_left == TYPE_FLOAT or t_right == TYPE_FLOAT or expr.op == "/":
                    return TYPE_FLOAT
                return TYPE_INT

        return None
