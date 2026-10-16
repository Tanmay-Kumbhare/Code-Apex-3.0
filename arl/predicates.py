"""
ARL Predicate Registry and Built-in Predicates.

Implements Part 4 Predicate Registry, built-in predicate catalog,
lowering contract (AST -> IR), YAML persistence, and one-time registration API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import yaml
from pydantic import BaseModel, Field

from arl.ir import (
    AllDifferent,
    And,
    Contiguous,
    Count,
    Eq,
    IRNode,
    Not,
    Sum,
    Var,
)


class PredicateDefinition(BaseModel):
    """Metadata and lowering definition for a registered predicate."""
    name: str
    args: List[Tuple[str, str]] = Field(default_factory=list)  # [(param_name, param_type)]
    returns: str
    description: str
    category: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "args": [{"name": a[0], "type": a[1]} for a in self.args],
            "returns": self.returns,
            "description": self.description,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PredicateDefinition:
        args_raw = data.get("args", [])
        args: List[Tuple[str, str]] = []
        for a in args_raw:
            if isinstance(a, dict):
                args.append((a.get("name", "arg"), a.get("type", "Any")))
            elif isinstance(a, (list, tuple)) and len(a) >= 2:
                args.append((str(a[0]), str(a[1])))
        return cls(
            name=data["name"],
            args=args,
            returns=data.get("returns", "Bool"),
            description=data.get("description", ""),
            category=data.get("category", "general"),
        )


class PredicateRegistry:
    """Registry managing predicate signatures and lowering functions."""

    def __init__(self) -> None:
        self.definitions: Dict[str, PredicateDefinition] = {}
        self.lower_functions: Dict[str, Callable[..., IRNode]] = {}

    def register(
        self,
        name: str,
        args: List[Tuple[str, str]],
        returns: str,
        description: str,
        lower: Optional[Callable[..., IRNode]] = None,
        category: str = "general",
        allow_override: bool = False,
    ) -> PredicateDefinition:
        """Register a new predicate. Registration is one-time per name."""
        if not allow_override and name in self.definitions:
            raise ValueError(f"Duplicate predicate registration: '{name}' is already registered")

        defn = PredicateDefinition(
            name=name,
            args=args,
            returns=returns,
            description=description,
            category=category,
        )
        self.definitions[name] = defn

        # Default lowering if none provided
        if lower is not None:
            self.lower_functions[name] = lower
        else:
            self.lower_functions[name] = lambda *a, **kw: Var(name=name, var_type=returns)

        return defn

    def has(self, name: str) -> bool:
        """Check if predicate name is registered."""
        return name in self.definitions

    def __contains__(self, name: str) -> bool:
        return name in self.definitions

    def get(self, name: str) -> Optional[PredicateDefinition]:
        return self.definitions.get(name)

    def get_lower_fn(self, name: str) -> Optional[Callable[..., IRNode]]:
        return self.lower_functions.get(name)

    def all(self) -> List[PredicateDefinition]:
        return list(self.definitions.values())

    def to_yaml(self) -> str:
        data = {name: d.to_dict() for name, d in self.definitions.items()}
        return yaml.dump(data, sort_keys=False, default_flow_style=None)

    def save_yaml(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_yaml())

    @classmethod
    def from_yaml(cls, content_or_path: Union[str, Path]) -> PredicateRegistry:
        p = Path(content_or_path)
        if isinstance(content_or_path, Path) or ("\n" not in str(content_or_path) and p.exists() and p.is_file()):
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        else:
            data = yaml.safe_load(str(content_or_path))

        registry = cls()
        if isinstance(data, dict):
            for pname, pdata in data.items():
                pdefn = PredicateDefinition.from_dict(pdata)
                registry.register(
                    name=pdefn.name,
                    args=pdefn.args,
                    returns=pdefn.returns,
                    description=pdefn.description,
                    category=pdefn.category,
                    allow_override=True,
                )
        return registry

    @classmethod
    def load_default(cls) -> PredicateRegistry:
        """Load default built-in predicate registry."""
        default_path = Path(__file__).parent.parent / "predicates.yaml"
        if default_path.exists():
            reg = cls.from_yaml(default_path)
            # Reattach lowering functions from builtins
            reg._attach_builtin_lowering()
            return reg
        return get_builtin_predicate_registry()

    def _attach_builtin_lowering(self) -> None:
        builtins = get_builtin_predicate_registry()
        for name, fn in builtins.lower_functions.items():
            self.lower_functions[name] = fn


def get_builtin_predicate_registry() -> PredicateRegistry:
    """Build the comprehensive built-in predicate registry defined in Part 4.1."""
    reg = PredicateRegistry()

    # ========================================================================
    # Relational Predicates
    # ========================================================================
    reg.register(
        name="same_cabinet",
        args=[("collection", "Signal[]")],
        returns="Bool",
        description="True if all signals in collection are assigned to the same Cabinet.",
        category="relational",
        lower=lambda coll, **kw: Eq(left=Var(name=f"same_cabinet({coll})", var_type="Bool"), right=True),
    )
    reg.register(
        name="same_cioc",
        args=[("collection", "Signal[]")],
        returns="Bool",
        description="True if all signals in collection are assigned to the same CIOC.",
        category="relational",
        lower=lambda coll, **kw: Eq(left=Var(name=f"same_cioc({coll})", var_type="Bool"), right=True),
    )
    reg.register(
        name="same_baseplate",
        args=[("collection", "Signal[]")],
        returns="Bool",
        description="True if all signals in collection are assigned to the same Baseplate.",
        category="relational",
        lower=lambda coll, **kw: Eq(left=Var(name=f"same_baseplate({coll})", var_type="Bool"), right=True),
    )
    reg.register(
        name="same_charm",
        args=[("a", "Signal"), ("b", "Signal")],
        returns="Bool",
        description="True if signals a and b occupy the same CHARM.",
        category="relational",
        lower=lambda a, b, **kw: Eq(left=Var(name=f"charm({a})", var_type="Int"), right=Var(name=f"charm({b})", var_type="Int")),
    )
    reg.register(
        name="adjacent",
        args=[("a", "Signal"), ("b", "Signal")],
        returns="Bool",
        description="True if signals a and b are placed in adjacent slots.",
        category="relational",
        lower=lambda a, b, **kw: Eq(left=Var(name=f"adjacent({a},{b})", var_type="Bool"), right=True),
    )
    reg.register(
        name="distance",
        args=[("a", "Signal"), ("b", "Signal")],
        returns="Int",
        description="Slot distance between signals a and b.",
        category="relational",
        lower=lambda a, b, **kw: Var(name=f"distance({a},{b})", var_type="Int"),
    )
    reg.register(
        name="shares_cable",
        args=[("a", "Signal"), ("b", "Signal")],
        returns="Bool",
        description="True if signals a and b belong to the same cable.",
        category="relational",
        lower=lambda a, b, **kw: Eq(left=Var(name=f"shares_cable({a},{b})", var_type="Bool"), right=True),
    )
    reg.register(
        name="shares_group",
        args=[("a", "Signal"), ("b", "Signal")],
        returns="Bool",
        description="True if signals a and b belong to the same group.",
        category="relational",
        lower=lambda a, b, **kw: Eq(left=Var(name=f"shares_group({a},{b})", var_type="Bool"), right=True),
    )

    # ========================================================================
    # Capacity Predicates
    # ========================================================================
    reg.register(
        name="free_slots",
        args=[("scope", "Entity")],
        returns="Int",
        description="Number of unassigned CHARM slots within scope.",
        category="capacity",
        lower=lambda scope, **kw: Var(name=f"free_slots({scope})", var_type="Int"),
    )
    reg.register(
        name="used_slots",
        args=[("scope", "Entity")],
        returns="Int",
        description="Number of assigned CHARM slots within scope.",
        category="capacity",
        lower=lambda scope, **kw: Var(name=f"used_slots({scope})", var_type="Int"),
    )
    reg.register(
        name="fill_percent",
        args=[("scope", "Entity")],
        returns="Float",
        description="Percentage of slots utilized within scope.",
        category="capacity",
        lower=lambda scope, **kw: Var(name=f"fill_percent({scope})", var_type="Float"),
    )
    reg.register(
        name="spare_percent",
        args=[("scope", "Entity")],
        returns="Float",
        description="Percentage of spare capacity remaining within scope.",
        category="capacity",
        lower=lambda scope, **kw: Var(name=f"spare_percent({scope})", var_type="Float"),
    )

    # ========================================================================
    # Contiguity Predicates
    # ========================================================================
    reg.register(
        name="contiguous",
        args=[("collection", "Signal[]"), ("order", "String")],
        returns="Bool",
        description="True if collection of signals are placed in contiguous slots.",
        category="contiguity",
        lower=lambda coll, order="asc", **kw: Contiguous(vars=[str(coll)], order=str(order)),
    )
    reg.register(
        name="consecutive",
        args=[("collection", "Signal[]")],
        returns="Bool",
        description="True if signals are placed in consecutive slots.",
        category="contiguity",
        lower=lambda coll, **kw: Contiguous(vars=[str(coll)], order="asc"),
    )
    reg.register(
        name="ordered_by",
        args=[("collection", "Signal[]"), ("field", "String"), ("order", "String")],
        returns="Bool",
        description="True if signals are ordered by field according to order.",
        category="contiguity",
        lower=lambda coll, field, order="asc", **kw: Contiguous(vars=[str(coll)], order=str(order)),
    )

    # ========================================================================
    # Cable / Pair Predicates
    # ========================================================================
    reg.register(
        name="pair_index",
        args=[("pair", "Pair")],
        returns="Int",
        description="1-based index of the pair in its cable.",
        category="cable_pair",
        lower=lambda pair, **kw: Var(name=f"pair_index({pair})", var_type="Int"),
    )
    reg.register(
        name="cable_size",
        args=[("cable", "Cable")],
        returns="CableSize",
        description="Total number of pairs in cable.",
        category="cable_pair",
        lower=lambda cable, **kw: Var(name=f"cable_size({cable})", var_type="CableSize"),
    )
    reg.register(
        name="wired_spare",
        args=[("pair", "Pair")],
        returns="Bool",
        description="True if pair is designated as a wired spare.",
        category="cable_pair",
        lower=lambda pair, **kw: Eq(left=Var(name=f"wired_spare({pair})", var_type="Bool"), right=True),
    )
    reg.register(
        name="orphan_pair",
        args=[("pair", "Pair")],
        returns="Bool",
        description="True if pair has no assigned signal and is not a wired spare.",
        category="cable_pair",
        lower=lambda pair, **kw: Eq(left=Var(name=f"orphan_pair({pair})", var_type="Bool"), right=True),
    )

    # ========================================================================
    # Hardware Predicates
    # ========================================================================
    reg.register(
        name="charm_for",
        args=[("io_type", "IoType")],
        returns="CharmType",
        description="Returns recommended CHARM type for given IO type.",
        category="hardware",
        lower=lambda io_type, **kw: Var(name=f"charm_for({io_type})", var_type="CharmType"),
    )
    reg.register(
        name="tb_for",
        args=[("io_type", "IoType")],
        returns="TbType",
        description="Returns recommended Terminal Block type for given IO type.",
        category="hardware",
        lower=lambda io_type, **kw: Var(name=f"tb_for({io_type})", var_type="TbType"),
    )
    reg.register(
        name="compatible",
        args=[("charm", "CharmType"), ("tb", "TbType")],
        returns="Bool",
        description="True if CHARM type and TB type are hardware-compatible.",
        category="hardware",
        lower=lambda charm, tb, **kw: Eq(left=Var(name=f"compatible({charm},{tb})", var_type="Bool"), right=True),
    )
    reg.register(
        name="mixable",
        args=[("charm_a", "CharmType"), ("charm_b", "CharmType")],
        returns="Bool",
        description="True if two CHARM types can reside on the same baseplate.",
        category="hardware",
        lower=lambda a, b, **kw: Eq(left=Var(name=f"mixable({a},{b})", var_type="Bool"), right=True),
    )

    # ========================================================================
    # Naming Predicates
    # ========================================================================
    reg.register(
        name="matches_pattern",
        args=[("name", "String"), ("pattern", "String")],
        returns="Bool",
        description="True if string matches regex pattern.",
        category="naming",
        lower=lambda name, pattern, **kw: Eq(left=Var(name=f"matches_pattern({name},{pattern})", var_type="Bool"), right=True),
    )
    reg.register(
        name="format",
        args=[("pattern", "String"), ("args", "Any")],
        returns="String",
        description="Formats pattern with arguments.",
        category="naming",
        lower=lambda pat, *args, **kw: Var(name=f"format({pat})", var_type="String"),
    )

    # ========================================================================
    # Revision Predicates
    # ========================================================================
    reg.register(
        name="changed_since",
        args=[("rev", "Rev")],
        returns="Bool",
        description="True if entity was modified after specified revision.",
        category="revision",
        lower=lambda rev, **kw: Eq(left=Var(name=f"changed_since({rev})", var_type="Bool"), right=True),
    )
    reg.register(
        name="added_in",
        args=[("rev", "Rev")],
        returns="Bool",
        description="True if entity was introduced in revision.",
        category="revision",
        lower=lambda rev, **kw: Eq(left=Var(name=f"added_in({rev})", var_type="Bool"), right=True),
    )
    reg.register(
        name="deleted_in",
        args=[("rev", "Rev")],
        returns="Bool",
        description="True if entity was removed in revision.",
        category="revision",
        lower=lambda rev, **kw: Eq(left=Var(name=f"deleted_in({rev})", var_type="Bool"), right=True),
    )
    reg.register(
        name="pinned",
        args=[("signal", "Signal")],
        returns="Bool",
        description="True if signal slot is pinned from previous revision.",
        category="revision",
        lower=lambda signal, **kw: Eq(left=Var(name=f"pinned({signal})", var_type="Bool"), right=True),
    )
    reg.register(
        name="moved_since",
        args=[("rev", "Rev")],
        returns="Bool",
        description="True if signal changed slot since revision.",
        category="revision",
        lower=lambda rev, **kw: Eq(left=Var(name=f"moved_since({rev})", var_type="Bool"), right=True),
    )

    return reg
