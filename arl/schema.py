"""
ARL Schema and Entity Model System.

Provides schema definitions, validation, dynamic extension (register_field,
register_entity, register_enum), persistence to/from YAML, and schema versioning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import yaml
from pydantic import BaseModel, Field, field_validator

from arl.errors import ArlError, ErrorCollector, Phase, Severity, make_error
from arl.types import (
    ArlType,
    DomainType,
    EntityType,
    EnumType,
    ListType,
    OptionType,
    PrimitiveType,
    ReferenceType,
    SetType,
    STANDARD_DOMAIN_TYPES,
    parse_type_string,
    validate_value_for_type,
)


def parse_semver(v: str) -> Tuple[int, int, int]:
    """Parse a semantic version string 'MAJOR.MINOR.PATCH'."""
    parts = str(v).strip().split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        raise ValueError(f"Invalid semantic version string: '{v}'")


def parse_field_spec(name: str, spec: Any) -> FieldDefinition:
    """
    Parse a field specification which may be:
      - A type string: "Tag", "GroupRef?", "Pair[]", "Int = 12", "Float = 10.0"
      - A dict: {"type": "Int", "default": 12, "description": "..."}
      - A FieldDefinition instance
    """
    if isinstance(spec, FieldDefinition):
        return spec

    if isinstance(spec, dict):
        type_str = spec.get("type", "String")
        default = spec.get("default", None)
        desc = spec.get("description", None)
        is_opt = spec.get("is_optional", None)
        is_optional = is_opt if is_opt is not None else ("?" in type_str or default is not None)
        return FieldDefinition(
            name=name,
            type_str=type_str,
            default=default,
            description=desc,
            is_optional=is_optional,
        )

    if isinstance(spec, str):
        type_part = spec.strip()
        default_val: Optional[Any] = None
        if "=" in type_part:
            t_side, d_side = type_part.split("=", 1)
            type_part = t_side.strip()
            d_str = d_side.strip()
            # Parse default literal
            if d_str.lower() == "true":
                default_val = True
            elif d_str.lower() == "false":
                default_val = False
            elif d_str.lower() in ("null", "none"):
                default_val = None
            else:
                try:
                    default_val = int(d_str)
                except ValueError:
                    try:
                        default_val = float(d_str)
                    except ValueError:
                        default_val = d_str.strip('"\'')

        is_optional = type_part.endswith("?") or type_part.startswith("Option<") or default_val is not None
        return FieldDefinition(
            name=name,
            type_str=type_part,
            default=default_val,
            is_optional=is_optional,
        )

    return FieldDefinition(name=name, type_str=str(spec))


class FieldDefinition(BaseModel):
    name: str
    type_str: str
    default: Optional[Any] = None
    description: Optional[str] = None
    is_optional: bool = False

    def to_spec_string(self) -> str:
        s = self.type_str
        if self.default is not None:
            s += f" = {self.default}"
        return s


class EntityDefinition(BaseModel):
    name: str
    fields: Dict[str, FieldDefinition] = Field(default_factory=dict)
    parent: Optional[str] = None
    description: Optional[str] = None

    def get_field(self, field_name: str) -> Optional[FieldDefinition]:
        return self.fields.get(field_name)

    def add_field(self, field_def: FieldDefinition) -> None:
        self.fields[field_def.name] = field_def


class EnumDefinition(BaseModel):
    name: str
    values: List[str]

    @classmethod
    def from_raw(cls, name: str, values: List[Any]) -> EnumDefinition:
        return cls(name=name, values=[str(v) for v in values])


class Schema(BaseModel):
    version: str = "2.1.0"
    enums: Dict[str, EnumDefinition] = Field(default_factory=dict)
    entities: Dict[str, EntityDefinition] = Field(default_factory=dict)
    domain_types: List[str] = Field(default_factory=lambda: sorted(list(STANDARD_DOMAIN_TYPES)))

    def get_entity(self, name: str) -> Optional[EntityDefinition]:
        return self.entities.get(name)

    def get_enum(self, name: str) -> Optional[EnumDefinition]:
        return self.enums.get(name)

    def get_enum_type(self, name: str) -> Optional[EnumType]:
        enum_def = self.get_enum(name)
        if enum_def:
            return EnumType(enum_def.name, enum_def.values)
        return None

    def all_enum_types(self) -> Dict[str, EnumType]:
        return {name: EnumType(ed.name, ed.values) for name, ed in self.enums.items()}

    def all_entity_names(self) -> Set[str]:
        return set(self.entities.keys())

    def all_domain_names(self) -> Set[str]:
        return set(self.domain_types)

    def check_version_compatibility(self, required_version: str) -> bool:
        """
        Check if schema version is compatible with required_version under SemVer rules.
        Same major version, schema minor >= required minor.
        """
        curr = parse_semver(self.version)
        req = parse_semver(required_version)
        if curr[0] != req[0]:
            return False
        if curr[1] < req[1]:
            return False
        if curr[1] == req[1] and curr[2] < req[2]:
            return False
        return True

    def resolve_reference(self, ref_target: Union[str, ReferenceType]) -> Optional[EntityDefinition]:
        """
        Resolve an entity reference (e.g. 'SignalRef' or 'CableRef') to its target EntityDefinition.
        """
        target_name = ref_target.target_entity if isinstance(ref_target, ReferenceType) else str(ref_target)
        if target_name.endswith("Ref"):
            target_name = target_name[:-3]

        # Exact match
        if target_name in self.entities:
            return self.entities[target_name]

        # Case-insensitive match (e.g. Cioc -> CIOC)
        for ename, edef in self.entities.items():
            if ename.lower() == target_name.lower():
                return edef
        return None

    def register_entity(
        self,
        name: str,
        fields: Optional[Union[Dict[str, Any], List[FieldDefinition]]] = None,
        parent: Optional[str] = None,
        description: Optional[str] = None,
        allow_override: bool = False,
    ) -> EntityDefinition:
        """
        Register a new entity definition in the schema.
        Validates duplicate names and fields.
        """
        if not allow_override and name in self.entities:
            raise ValueError(f"Duplicate entity registration: '{name}' already exists in schema (E2017)")

        if parent and parent not in self.entities:
            raise ValueError(f"Unknown parent entity '{parent}' for entity '{name}' (E2001)")

        entity_def = EntityDefinition(name=name, parent=parent, description=description)

        if fields:
            field_items = fields if isinstance(fields, list) else [
                parse_field_spec(k, v) for k, v in fields.items()
            ]
            seen_fnames: Set[str] = set()
            for f in field_items:
                if f.name in seen_fnames:
                    raise ValueError(f"Duplicate field '{f.name}' declared in entity '{name}'")
                seen_fnames.add(f.name)
                entity_def.add_field(f)

        self.entities[name] = entity_def
        return entity_def

    def register_field(
        self,
        entity_name: str,
        field_name: str,
        type_str: str,
        default: Any = None,
        description: Optional[str] = None,
        allow_override: bool = False,
    ) -> FieldDefinition:
        """
        Register a field on an existing entity.
        Validates:
          - Entity exists (E2001)
          - Duplicate field name
          - Valid type reference
          - Valid default value for declared type
          - Valid entity reference
        """
        entity = self.get_entity(entity_name)
        if not entity:
            raise KeyError(f"Entity '{entity_name}' does not exist in schema (E2001)")

        if not allow_override and field_name in entity.fields:
            raise ValueError(f"Duplicate field registration: field '{field_name}' already exists on entity '{entity_name}'")

        # Validate type reference and syntax
        try:
            resolved_type = self.resolve_type(type_str)
        except Exception as e:
            raise ValueError(f"Invalid type declaration '{type_str}' for field '{field_name}': {e}") from e

        # Validate entity reference target
        if isinstance(resolved_type, ReferenceType) or (
            isinstance(resolved_type, OptionType) and isinstance(resolved_type.inner, ReferenceType)
        ):
            ref = resolved_type if isinstance(resolved_type, ReferenceType) else resolved_type.inner
            target_ent = self.resolve_reference(ref)
            if not target_ent:
                raise ValueError(f"Invalid entity reference '{ref}': target entity '{ref.target_entity}' not found in schema")

        # Validate default value against declared type
        if default is not None:
            if not validate_value_for_type(default, resolved_type):
                raise ValueError(
                    f"Invalid default value {default!r} for field '{field_name}' of type '{resolved_type}'"
                )

        is_optional = type_str.endswith("?") or type_str.startswith("Option<") or default is not None
        field_def = FieldDefinition(
            name=field_name,
            type_str=type_str,
            default=default,
            description=description,
            is_optional=is_optional,
        )
        entity.add_field(field_def)
        return field_def

    def register_enum(
        self,
        name: str,
        values: List[Any],
        allow_override: bool = False,
    ) -> EnumDefinition:
        """Register a new enum type. Validates duplicate names and non-empty values."""
        if not allow_override and name in self.enums:
            raise ValueError(f"Duplicate enum registration: '{name}' already exists in schema")

        if not values:
            raise ValueError(f"Enum '{name}' must have at least one value")

        enum_def = EnumDefinition.from_raw(name, values)
        self.enums[name] = enum_def
        return enum_def

    def resolve_type(self, type_str: str) -> ArlType:
        """Parse and resolve a type string against this schema."""
        return parse_type_string(
            type_str,
            known_enums=self.all_enum_types(),
            known_entities=self.all_entity_names(),
            known_domains=self.all_domain_names(),
        )

    def validate_field_access(
        self,
        entity_name: str,
        field_name: str,
        collector: Optional[ErrorCollector] = None,
        rule_id: Optional[str] = None,
    ) -> Optional[FieldDefinition]:
        """Validate that entity exists (E2001) and field exists on entity (E2002)."""
        entity = self.get_entity(entity_name)
        if not entity:
            if collector:
                collector.add_error(
                    code="E2001",
                    message=f"Unknown entity type '{entity_name}'",
                    rule_id=rule_id,
                    context={"entity": entity_name},
                    suggestion=f"Valid entities are: {', '.join(sorted(self.entities.keys()))}",
                )
            return None

        field_def = entity.get_field(field_name)
        if not field_def:
            if collector:
                collector.add_error(
                    code="E2002",
                    message=f"Unknown field '{field_name}' on entity '{entity_name}'",
                    rule_id=rule_id,
                    context={"entity": entity_name, "field": field_name},
                    suggestion=f"Valid fields on '{entity_name}' are: {', '.join(sorted(entity.fields.keys()))}",
                )
            return None

        return field_def

    def to_dict(self) -> Dict[str, Any]:
        """Convert schema to clean dict representation for YAML."""
        data: Dict[str, Any] = {
            "version": self.version,
            "domain_types": self.domain_types,
            "enums": {name: ed.values for name, ed in self.enums.items()},
            "entities": {},
        }
        for ename, edef in self.entities.items():
            ent_dict: Dict[str, Any] = {}
            if edef.parent:
                ent_dict["parent"] = edef.parent
            if edef.description:
                ent_dict["description"] = edef.description
            fields_dict: Dict[str, str] = {}
            for fname, fdef in edef.fields.items():
                fields_dict[fname] = fdef.to_spec_string()
            ent_dict["fields"] = fields_dict
            data["entities"][ename] = ent_dict
        return data

    def to_yaml(self) -> str:
        return yaml.dump(self.to_dict(), sort_keys=False, default_flow_style=None)

    def save_yaml(self, path: Union[str, Path]) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_yaml())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Schema:
        version = str(data.get("version", "2.1.0"))
        domain_types = data.get("domain_types", sorted(list(STANDARD_DOMAIN_TYPES)))

        enums: Dict[str, EnumDefinition] = {}
        for ename, evalues in data.get("enums", {}).items():
            if isinstance(evalues, list):
                enums[ename] = EnumDefinition.from_raw(ename, evalues)
            elif isinstance(evalues, dict) and "values" in evalues:
                enums[ename] = EnumDefinition.from_raw(ename, evalues["values"])

        entities: Dict[str, EntityDefinition] = {}
        for ename, edata in data.get("entities", {}).items():
            parent = edata.get("parent") if isinstance(edata, dict) else None
            desc = edata.get("description") if isinstance(edata, dict) else None
            fields_raw = edata.get("fields", {}) if isinstance(edata, dict) else {}
            if isinstance(edata, dict) and not fields_raw:
                fields_raw = {k: v for k, v in edata.items() if k not in ("parent", "description", "fields")}

            entity = EntityDefinition(name=ename, parent=parent, description=desc)
            for fname, fspec in fields_raw.items():
                entity.add_field(parse_field_spec(fname, fspec))
            entities[ename] = entity

        return cls(
            version=version,
            enums=enums,
            entities=entities,
            domain_types=domain_types,
        )

    @classmethod
    def from_yaml(cls, content_or_path: Union[str, Path]) -> Schema:
        if isinstance(content_or_path, Path):
            with open(content_or_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        elif isinstance(content_or_path, str) and "\n" not in content_or_path and Path(content_or_path).is_file():
            with open(content_or_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        else:
            data = yaml.safe_load(str(content_or_path))

        if not isinstance(data, dict):
            raise ValueError("Schema YAML root must be a mapping/dict")

        return cls.from_dict(data)

    @classmethod
    def load_default(cls) -> Schema:
        """Load default base schema."""
        default_path = Path(__file__).parent.parent / "schema.yaml"
        if default_path.exists():
            return cls.from_yaml(default_path)
        return get_base_schema()


def get_base_schema() -> Schema:
    """Build the base default schema specified in Part 1."""
    schema = Schema(version="2.1.0")

    # 1.3 Enum types
    enums_data = {
        "IoType": ["AI", "AO", "DI", "DO", "DO_SOV", "DO_HORN", "DO_BEACON", "DO_SOUNDER", "DO_RELAY"],
        "CableSize": [2, 6, 12, 24],
        "Seg": ["A", "B"],
        "CharmType": ["AI", "AO", "DI", "HIGH_SIDE_DO", "ISOLATED_DO", "SPARE"],
        "TbType": ["STANDARD", "FUSED_INJECTED_POWER", "RELAY_OUTPUT"],
        "SegRole": ["A_ONLY", "B_ONLY", "MIXED"],
        "VoltageClass": ["LV", "MV", "HV"],
        "RedundancyKind": ["NONE", "DUAL", "TMR"],
    }
    for ename, evalues in enums_data.items():
        schema.register_enum(ename, evalues)

    # 1.2 Base schema entities
    schema.register_entity(
        "Signal",
        fields={
            "tag": "Tag",
            "io_type": "IoType",
            "cable": "CableRef",
            "pair": "Int",
            "group": "GroupRef?",
            "seg": "Seg?",
            "voltage": "VoltageClass?",
            "safety": "Bool",
            "redundant": "RedundancyKind?",
            "area": "Area?",
            "unit": "Unit?",
            "revision_added": "Rev?",
            "revision_deleted": "Rev?",
        },
    )

    schema.register_entity(
        "Cable",
        fields={
            "id": "CableId",
            "size": "CableSize",
            "pairs": "Pair[]",
            "from": "JBId?",
        },
    )

    schema.register_entity(
        "Pair",
        fields={
            "cable": "CableRef",
            "index": "Int",
            "signal": "SignalRef?",
            "wired_spare": "Bool",
        },
    )

    schema.register_entity(
        "Group",
        fields={
            "id": "GroupId",
            "signals": "Signal[]",
            "cables": "Cable[]",
        },
    )

    schema.register_entity(
        "Baseplate",
        fields={
            "id": "BaseplateId",
            "cioc": "CiocRef",
            "index": "Int",
            "charms": "Charm[]",
            "capacity": "Int = 12",
        },
    )

    schema.register_entity(
        "Charm",
        fields={
            "slot": "Int",
            "signal": "SignalRef?",
            "charm_type": "CharmType?",
            "tb_type": "TbType?",
        },
    )

    schema.register_entity(
        "CIOC",
        fields={
            "id": "CiocId",
            "cabinet": "CabinetRef",
            "index": "Int",
            "baseplates": "Baseplate[]",
            "capacity": "Int = 8",
            "seg_role": "SegRole",
        },
    )

    schema.register_entity(
        "Cabinet",
        fields={
            "id": "CabinetId",
            "area": "Area",
            "unit": "Unit",
            "seq": "Int",
            "ciocs": "CIOC[]",
            "capacity": "Int = 4",
            "spare_target_percent": "Float = 10.0",
        },
    )

    schema.register_entity(
        "Project",
        fields={
            "cabinets": "Cabinet[]",
            "cables": "Cable[]",
            "groups": "Group[]",
            "signals": "Signal[]",
        },
    )

    return schema
