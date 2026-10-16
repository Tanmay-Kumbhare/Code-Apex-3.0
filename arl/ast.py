"""
ARL Abstract Syntax Tree (AST) Model.

Defines typed AST nodes for the complete ARL grammar specified in Part 2.
Every node retains source location information (file, line, column).
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field

from arl.errors import SourceLocation


class AstNode(BaseModel):
    """Base class for all AST nodes with source location tracking."""
    location: Optional[SourceLocation] = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


# ============================================================================
# Expressions
# ============================================================================

class Expr(AstNode, ABC):
    """Abstract base class for all expressions and conditions."""
    pass


class LiteralExpr(Expr):
    """Literal values: integers, floats, strings, booleans, null."""
    value: Any
    literal_type: str  # 'int', 'float', 'string', 'bool', 'null'


class ListLiteralExpr(Expr):
    """List / Collection literal: [item1, item2, ...]"""
    elements: List[Expr] = Field(default_factory=list)


class IdentifierExpr(Expr):
    """Variable or entity field identifier."""
    name: str


class SelfExpr(Expr):
    """The 'self' keyword expression referring to the scoped entity."""
    pass


class BinaryOp(Expr):
    """
    Binary operation expression:
      - Boolean: 'and', 'or'
      - Comparison: '==', '!=', '<', '<=', '>', '>=', 'in', 'not in', 'matches', 'is'
      - Arithmetic: '+', '-', '*', '/', '%'
    """
    left: Expr
    op: str
    right: Expr


class UnaryOp(Expr):
    """Unary operation expression: '-' or 'not'."""
    op: str
    operand: Expr


class FieldAccessExpr(Expr):
    """Field access: target.field or optional chaining target?.field."""
    target: Expr
    field_name: str
    is_optional: bool = False  # True for ?.


class CallExpr(Expr):
    """Function or predicate call: callee(args)."""
    callee: Expr
    args: List[Expr] = Field(default_factory=list)


class IndexExpr(Expr):
    """Index or subscript access: target[index]."""
    target: Expr
    index: Expr


class QuantifiedExpr(Expr):
    """
    Quantified expression:
      (forall | exists) IDENT in collection (where condition)? : condition
    """
    kind: str  # 'forall' or 'exists'
    var_name: str
    collection: Expr
    where_clause: Optional[Expr] = None
    condition: Expr


class AggregateExpr(Expr):
    """
    Aggregate expression:
      (count | sum | min | max | avg | distinct)(expr)
    """
    func_name: str  # 'count', 'sum', 'min', 'max', 'avg', 'distinct'
    expr: Expr


class AsExpr(Expr):
    """Explicit cast expression: expr as TargetType (e.g. self.slot as Slot)."""
    expr: Expr
    target_type: str


class IsSomeExpr(Expr):
    """Option unwrap test: expr is some (with optional binding variable 'is some x')."""
    expr: Expr
    binding_var: Optional[str] = None


class IsNoneExpr(Expr):
    """Option empty test: expr is none."""
    expr: Expr


# ============================================================================
# Effects & Meta
# ============================================================================

class RuleRef(AstNode):
    """Reference to another rule: rule IDENT."""
    rule_name: str


class Effect(AstNode, ABC):
    """Base class for rule effects."""
    pass


class RequireEffect(Effect):
    """require (ruleRef | expr)"""
    target: Union[RuleRef, Expr]


class ForbidEffect(Effect):
    """forbid expr"""
    expr: Expr


class AssignEffect(Effect):
    """assign field = expr"""
    field_name: str
    expr: Expr


class PreferEffect(Effect):
    """prefer expr"""
    expr: Expr


class RuleMeta(AstNode):
    """
    Metadata attached to rules and overrides:
      priority: 'hard' | 'soft'
      weight: NUMBER
      message: STRING
      tags: [IDENT, ...]
      id: IDENT
    """
    priority: Optional[str] = None  # 'hard' | 'soft'
    weight: Optional[float] = None
    message: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    rule_id: Optional[str] = None


# ============================================================================
# Top-Level Statements & Declarations
# ============================================================================

class Include(AstNode):
    """include "path/to/file.arl" """
    path: str


class Rule(AstNode):
    """
    rule IDENT :
      scope : entitytype
      when : condition (optional)
      effect+
      meta*
    """
    name: str
    scope: str
    when: Optional[Expr] = None
    effects: List[Effect] = Field(default_factory=list)
    meta: RuleMeta = Field(default_factory=RuleMeta)

    @property
    def priority(self) -> Optional[str]:
        return self.meta.priority if self.meta else None

    @property
    def weight(self) -> Optional[float]:
        return self.meta.weight if self.meta else None

    @property
    def id(self) -> Optional[str]:
        return self.meta.rule_id if self.meta else None


class Objective(AstNode):
    """
    objective IDENT :
      primary : [objterm, ...]
      secondary : [objterm, ...] (optional)
      weights : {IDENT : NUMBER, ...} (optional)
    """
    name: str
    primary: List[Expr] = Field(default_factory=list)
    secondary: List[Expr] = Field(default_factory=list)
    weights: Dict[str, float] = Field(default_factory=dict)


class Override(AstNode):
    """
    override IDENT :
      when : condition (optional)
      effect+
      meta*
    """
    target_rule: str
    when: Optional[Expr] = None
    effects: List[Effect] = Field(default_factory=list)
    meta: RuleMeta = Field(default_factory=RuleMeta)


class Ruleset(AstNode):
    """
    ruleset IDENT (extends IDENT)? {
      (rule | objective | override | include)*
    }
    """
    name: str
    extends: Optional[str] = None
    includes: List[Include] = Field(default_factory=list)
    rules: List[Rule] = Field(default_factory=list)
    objectives: List[Objective] = Field(default_factory=list)
    overrides: List[Override] = Field(default_factory=list)

    def get_rule(self, name: str) -> Optional[Rule]:
        for r in self.rules:
            if r.name == name:
                return r
        return None
