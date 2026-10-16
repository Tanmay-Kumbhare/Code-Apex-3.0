"""
ARL Constraint Intermediate Representation (IR).

A solver-agnostic, strongly-typed IR consumed by both CP-SAT and greedy backends.
Every node retains traceability metadata (rule_id, priority, weight, source location).
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field

from arl.errors import SourceLocation


class IRNode(BaseModel):
    """Base class for all solver-agnostic constraint IR nodes."""
    rule_id: Optional[str] = None
    priority: str = "hard"  # "hard" or "soft"
    weight: Optional[float] = None
    location: Optional[SourceLocation] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class Var(IRNode):
    """Decision or auxiliary variable: Var(name, type, domain)."""
    name: str
    var_type: str  # "Int", "Bool", "String", "Slot", "Entity", etc.
    domain: Optional[List[Any]] = None  # allowed values / bounds [min, max]


class Eq(IRNode):
    """Equality constraint: left == right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class Neq(IRNode):
    """Inequality constraint: left != right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class Lt(IRNode):
    """Less than: left < right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class Lte(IRNode):
    """Less than or equal: left <= right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class Gt(IRNode):
    """Greater than: left > right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class Gte(IRNode):
    """Greater than or equal: left >= right."""
    left: Union[IRNode, Any]
    right: Union[IRNode, Any]


class And(IRNode):
    """Logical conjunction: And(nodes)."""
    nodes: List[IRNode] = Field(default_factory=list)


class Or(IRNode):
    """Logical disjunction: Or(nodes)."""
    nodes: List[IRNode] = Field(default_factory=list)


class Not(IRNode):
    """Logical negation: Not(node)."""
    node: IRNode


class Implies(IRNode):
    """Logical implication: condition -> consequence."""
    condition: IRNode
    consequence: IRNode


class AllDifferent(IRNode):
    """All-different constraint on variables."""
    vars: List[Union[Var, str, IRNode]] = Field(default_factory=list)


class Count(IRNode):
    """Count constraint: count(vars) op value."""
    vars: List[Union[Var, str, IRNode]] = Field(default_factory=list)
    op: str  # "==", "!=", "<=", etc.
    value: Union[int, IRNode]


class Sum(IRNode):
    """Sum constraint: sum(vars) op value."""
    vars: List[Union[Var, str, IRNode]] = Field(default_factory=list)
    op: str
    value: Union[float, int, IRNode]


class Contiguous(IRNode):
    """Contiguity constraint on ordered variables/slots."""
    vars: List[Union[Var, str, IRNode]] = Field(default_factory=list)
    order: str = "asc"  # "asc" or "desc"


class ForAll(IRNode):
    """Universal quantification constraint."""
    var_name: str
    collection: Union[str, IRNode]
    predicate: IRNode


class Exists(IRNode):
    """Existential quantification constraint."""
    var_name: str
    collection: Union[str, IRNode]
    predicate: IRNode


class Soft(IRNode):
    """Soft constraint wrapper with associated weight."""
    node: IRNode
    weight: float = 1.0
    priority: str = "soft"


class Objective(IRNode):
    """Objective formulation for optimization solvers."""
    terms: List[IRNode] = Field(default_factory=list)
    weights: Dict[str, float] = Field(default_factory=dict)
    direction: str = "maximize"  # "maximize" or "minimize"
