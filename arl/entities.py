"""
ARL Entity Model and Instances.

Schema-driven entity instances for the ARL engine.
Allows dynamic field access, validation against Schema,
and ensures adding a field to the schema does not require engine changes.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Set, Union
from arl.errors import ArlError, ErrorCollector
from arl.schema import EntityDefinition, FieldDefinition, Schema


class Entity:
    """
    Schema-backed entity instance.
    Fields defined in the schema (or dynamically registered) are accessible
    as attributes or dict items.
    """

    def __init__(
        self,
        entity_type: str,
        schema: Optional[Schema] = None,
        **initial_fields: Any,
    ) -> None:
        object.__setattr__(self, "_entity_type", entity_type)
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_data", {})

        # Populate defaults from schema if available
        if schema:
            entity_def = schema.get_entity(entity_type)
            if entity_def:
                for fname, fdef in entity_def.fields.items():
                    if fdef.default is not None:
                        self._data[fname] = fdef.default

        # Assign provided initial fields
        for k, v in initial_fields.items():
            self[k] = v

    @property
    def entity_type(self) -> str:
        return self._entity_type

    def get_schema(self) -> Optional[Schema]:
        return self._schema

    def set_schema(self, schema: Schema) -> None:
        object.__setattr__(self, "_schema", schema)

    def __getattr__(self, name: str) -> Any:
        if not isinstance(name, str):
            raise AttributeError(f"Attribute name must be str, got {type(name).__name__}")
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        if name in self._data:
            return self._data[name]

        # Check if field exists in schema with a default or is optional
        if self._schema:
            entity_def = self._schema.get_entity(self._entity_type)
            if entity_def and name in entity_def.fields:
                fdef = entity_def.fields[name]
                if fdef.default is not None:
                    return fdef.default
                if fdef.is_optional:
                    return None

        raise AttributeError(f"'{self._entity_type}' instance has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if not isinstance(name, str) or name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = value

    def __getitem__(self, key: str) -> Any:
        return self.__getattr__(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = getattr(self, key)
            return val if val is not None else default
        except AttributeError:
            return default


    def to_dict(self) -> Dict[str, Any]:
        """Convert entity data to dictionary."""
        result: Dict[str, Any] = {}
        for k, v in self._data.items():
            if isinstance(v, Entity):
                result[k] = v.to_dict()
            elif isinstance(v, list):
                result[k] = [item.to_dict() if isinstance(item, Entity) else item for item in v]
            else:
                result[k] = v
        return result

    def validate(
        self,
        collector: Optional[ErrorCollector] = None,
        schema: Optional[Schema] = None,
    ) -> bool:
        """Validate this entity instance against schema."""
        s = schema or self._schema
        if not s:
            return True

        entity_def = s.get_entity(self._entity_type)
        if not entity_def:
            if collector:
                collector.add_error(
                    code="E2001",
                    message=f"Unknown entity type '{self._entity_type}'",
                )
            return False

        valid = True
        # Check required fields
        for fname, fdef in entity_def.fields.items():
            if fname not in self._data or self._data[fname] is None:
                if not fdef.is_optional and fdef.default is None:
                    valid = False
                    if collector:
                        collector.add_error(
                            code="E4001",
                            message=f"Missing required field '{fname}' on entity '{self._entity_type}'",
                            context={"entity": self._entity_type, "field": fname},
                        )
        return valid

    def __repr__(self) -> str:
        # Display identity or main identifier
        ident = self._data.get("id") or self._data.get("tag") or self._data.get("index") or self._data.get("slot")
        id_str = f" id={ident}" if ident is not None else ""
        return f"<{self._entity_type}{id_str} fields={list(self._data.keys())}>"


# Typed convenience subclasses for the standard hierarchy
class Signal(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Signal", schema, **fields)


class Cable(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Cable", schema, **fields)


class Pair(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Pair", schema, **fields)


class Group(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Group", schema, **fields)


class Baseplate(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Baseplate", schema, **fields)


class Charm(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Charm", schema, **fields)


class CIOC(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("CIOC", schema, **fields)


class Cabinet(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Cabinet", schema, **fields)


class Project(Entity):
    def __init__(self, schema: Optional[Schema] = None, **fields: Any) -> None:
        super().__init__("Project", schema, **fields)
        if "cabinets" not in self._data:
            self._data["cabinets"] = []
        if "cables" not in self._data:
            self._data["cables"] = []
        if "groups" not in self._data:
            self._data["groups"] = []
        if "signals" not in self._data:
            self._data["signals"] = []
