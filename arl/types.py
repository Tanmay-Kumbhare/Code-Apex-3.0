"""
ARL Type System.

Defines the Hindley-Milner-style types, domain types, enums,
option types, collections, explicit coercions, reference types, and type parsing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class ArlType(ABC):
    """Base class for all ARL types."""

    @abstractmethod
    def __str__(self) -> str:
        pass

    @abstractmethod
    def __eq__(self, other: object) -> bool:
        pass

    @abstractmethod
    def __hash__(self) -> int:
        pass

    @property
    def is_option(self) -> bool:
        return False

    @property
    def is_collection(self) -> bool:
        return False

    @property
    def is_reference(self) -> bool:
        return False


class PrimitiveType(ArlType):
    """Base types: Int, Float, Bool, String, Null, Void."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PrimitiveType) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("PrimitiveType", self.name))


# Canonical primitive instances
TYPE_INT = PrimitiveType("Int")
TYPE_FLOAT = PrimitiveType("Float")
TYPE_BOOL = PrimitiveType("Bool")
TYPE_STRING = PrimitiveType("String")
TYPE_NULL = PrimitiveType("Null")
TYPE_VOID = PrimitiveType("Void")

BASE_TYPES = {
    "Int": TYPE_INT,
    "Float": TYPE_FLOAT,
    "Bool": TYPE_BOOL,
    "String": TYPE_STRING,
    "Null": TYPE_NULL,
    "Void": TYPE_VOID,
}


class DomainType(ArlType):
    """Domain-specific typed identifiers: Tag, CableId, CiocId, Slot, etc."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DomainType) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("DomainType", self.name))


STANDARD_DOMAIN_TYPES = {
    "Tag",
    "CableId",
    "CiocId",
    "BaseplateId",
    "CabinetId",
    "Slot",
    "Area",
    "Unit",
    "Rev",
    "JBId",
    "GroupId",
}


class EnumType(ArlType):
    """Enumeration type with fixed set of allowed string/int values."""

    def __init__(self, name: str, values: Sequence[Any]) -> None:
        self.name = name
        self.values = tuple(str(v) for v in values)

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnumType) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("EnumType", self.name))


class EntityType(ArlType):
    """Entity reference type: Signal, Cable, Pair, Group, Baseplate, CIOC, Cabinet, Project."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EntityType) and self.name == other.name

    def __hash__(self) -> int:
        return hash(("EntityType", self.name))


class ReferenceType(ArlType):
    """Entity reference type: e.g. CableRef -> Cable, SignalRef -> Signal."""

    def __init__(self, target_entity: str) -> None:
        self.target_entity = target_entity

    @property
    def name(self) -> str:
        # Standardize name format
        return f"{self.target_entity}Ref"

    def __str__(self) -> str:
        return self.name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ReferenceType) and self.target_entity.lower() == other.target_entity.lower()

    def __hash__(self) -> int:
        return hash(("ReferenceType", self.target_entity.lower()))

    @property
    def is_reference(self) -> bool:
        return True


class OptionType(ArlType):
    """Option<T> representing an optional value."""

    def __init__(self, inner: ArlType) -> None:
        if isinstance(inner, OptionType):
            raise ValueError("Nested Option types (e.g. Option<Option<T>>) are invalid")
        self.inner = inner

    def __str__(self) -> str:
        return f"Option<{self.inner}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OptionType) and self.inner == other.inner

    def __hash__(self) -> int:
        return hash(("OptionType", self.inner))

    @property
    def is_option(self) -> bool:
        return True


class SetType(ArlType):
    """Set<T> representing an unordered set of elements."""

    def __init__(self, element: ArlType) -> None:
        self.element = element

    def __str__(self) -> str:
        return f"Set<{self.element}>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SetType) and self.element == other.element

    def __hash__(self) -> int:
        return hash(("SetType", self.element))

    @property
    def is_collection(self) -> bool:
        return True


class ListType(ArlType):
    """List / Array type: T[]."""

    def __init__(self, element: ArlType) -> None:
        self.element = element

    def __str__(self) -> str:
        return f"{self.element}[]"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ListType) and self.element == other.element

    def __hash__(self) -> int:
        return hash(("ListType", self.element))

    @property
    def is_collection(self) -> bool:
        return True


class TupleType(ArlType):
    """Tuple (T1, T2, ...)."""

    def __init__(self, elements: Sequence[ArlType]) -> None:
        self.elements = tuple(elements)

    def __str__(self) -> str:
        inner = ", ".join(str(e) for e in self.elements)
        return f"({inner})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TupleType) and self.elements == other.elements

    def __hash__(self) -> int:
        return hash(("TupleType", self.elements))


def can_coerce(source: ArlType, target: ArlType, explicit: bool = False) -> bool:
    """
    Check if source can be coerced to target.
    Section 3.2 Coercions (explicit only):
      - Int -> Float in arithmetic (implicit or explicit)
      - Int -> Slot only via 'as Slot' (explicit only)
      - Tag -> String (explicit only)
      - Everything else rejected. No implicit conversions.
    """
    if source == target:
        return True

    # Int -> Float allowed in arithmetic
    if source == TYPE_INT and target == TYPE_FLOAT:
        return True

    if explicit:
        # Int -> Slot
        if source == TYPE_INT and isinstance(target, DomainType) and target.name == "Slot":
            return True
        # Tag -> String
        if isinstance(source, DomainType) and source.name == "Tag" and target == TYPE_STRING:
            return True

    return False


def resolve_entity_name(ref_name: str, known_entities: Optional[Set[str]]) -> Optional[str]:
    """Resolve target entity from a Ref type name (e.g., 'CiocRef' -> 'CIOC')."""
    if not ref_name.endswith("Ref"):
        return None
    stem = ref_name[:-3]
    if known_entities:
        # Exact match
        if stem in known_entities:
            return stem
        # Case-insensitive match (e.g. Cioc -> CIOC)
        for ent in known_entities:
            if ent.lower() == stem.lower():
                return ent
    return stem


def parse_type_string(
    type_str: str,
    known_enums: Optional[Dict[str, EnumType]] = None,
    known_entities: Optional[Set[str]] = None,
    known_domains: Optional[Set[str]] = None,
) -> ArlType:
    """
    Parse a type signature string into an ArlType object.
    Supports:
      - Option<T> or T?
      - Set<T>
      - T[]
      - Base types (Int, Float, Bool, String)
      - Domain types (Tag, Slot, etc.)
      - Enums
      - Entities
      - Entity references (SignalRef, CableRef, etc.)
    """
    s = type_str.strip()
    if not s:
        raise ValueError("Empty type string")

    # Handle double question marks or invalid option
    if s.endswith("??"):
        raise ValueError(f"Invalid nested option type syntax: '{s}'")

    # Handle suffix '?' -> Option<T>
    if s.endswith("?") and not s.startswith("Option<"):
        inner = parse_type_string(s[:-1], known_enums, known_entities, known_domains)
        return OptionType(inner)

    # Handle Option<T>
    if s.startswith("Option<") and s.endswith(">"):
        inner_str = s[7:-1].strip()
        if not inner_str:
            raise ValueError(f"Empty inner type in '{s}'")
        inner = parse_type_string(inner_str, known_enums, known_entities, known_domains)
        return OptionType(inner)

    # Handle Set<T>
    if s.startswith("Set<") and s.endswith(">"):
        inner_str = s[4:-1].strip()
        if not inner_str:
            raise ValueError(f"Empty element type in '{s}'")
        inner = parse_type_string(inner_str, known_enums, known_entities, known_domains)
        return SetType(inner)

    # Handle T[]
    if s.endswith("[]"):
        inner_str = s[:-2].strip()
        if not inner_str:
            raise ValueError(f"Empty element type in '{s}'")
        inner = parse_type_string(inner_str, known_enums, known_entities, known_domains)
        return ListType(inner)

    # Base types
    if s in BASE_TYPES:
        return BASE_TYPES[s]

    # Enums
    if known_enums and s in known_enums:
        return known_enums[s]

    # Entities
    if known_entities and s in known_entities:
        return EntityType(s)

    # Entity references (e.g. CableRef, SignalRef, CiocRef, CabinetRef, GroupRef)
    if s.endswith("Ref"):
        target_entity = resolve_entity_name(s, known_entities)
        if target_entity:
            return ReferenceType(target_entity)

    # Known domain types
    if known_domains and s in known_domains:
        return DomainType(s)
    if s in STANDARD_DOMAIN_TYPES:
        return DomainType(s)

    # By default, treat as DomainType
    return DomainType(s)


def validate_value_for_type(val: Any, arl_type: ArlType) -> bool:
    """Validate if a python value conforms to an ArlType."""
    if val is None:
        return arl_type.is_option

    # If option, unwrap and check inner
    if isinstance(arl_type, OptionType):
        return validate_value_for_type(val, arl_type.inner)

    if isinstance(arl_type, PrimitiveType):
        if arl_type == TYPE_INT:
            return isinstance(val, int) and not isinstance(val, bool)
        if arl_type == TYPE_FLOAT:
            return isinstance(val, (int, float)) and not isinstance(val, bool)
        if arl_type == TYPE_BOOL:
            return isinstance(val, bool)
        if arl_type == TYPE_STRING:
            return isinstance(val, str)
        if arl_type == TYPE_NULL:
            return val is None
        return True

    if isinstance(arl_type, EnumType):
        return str(val) in arl_type.values

    if isinstance(arl_type, DomainType):
        # Domain identifiers can be string or int (e.g. Slot index, Tag string)
        return isinstance(val, (str, int))

    if isinstance(arl_type, ReferenceType):
        return isinstance(val, (str, int))

    if isinstance(arl_type, ListType):
        if not isinstance(val, list):
            return False
        return all(validate_value_for_type(item, arl_type.element) for item in val)

    if isinstance(arl_type, SetType):
        if not isinstance(val, (set, list)):
            return False
        return all(validate_value_for_type(item, arl_type.element) for item in val)

    return True
