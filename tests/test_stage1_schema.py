"""
Unit and integration tests for Stage 1: Schema and Entity Model.
"""

import tempfile
from pathlib import Path
import pytest

from arl import (
    Baseplate,
    Cabinet,
    Cable,
    Charm,
    CIOC,
    Entity,
    ErrorCollector,
    Group,
    Pair,
    Phase,
    Project,
    Schema,
    Severity,
    Signal,
    can_coerce,
    get_base_schema,
    parse_type_string,
)
from arl.types import (
    TYPE_BOOL,
    TYPE_FLOAT,
    TYPE_INT,
    TYPE_STRING,
    DomainType,
    EntityType,
    EnumType,
    ListType,
    OptionType,
    SetType,
)


def test_base_schema_entities_and_enums() -> None:
    """Verify base schema contains all entities and enums from Part 1."""
    schema = get_base_schema()

    # Check entities
    expected_entities = {
        "Signal",
        "Cable",
        "Pair",
        "Group",
        "Baseplate",
        "Charm",
        "CIOC",
        "Cabinet",
        "Project",
    }
    assert expected_entities.issubset(schema.all_entity_names())

    # Check enums
    expected_enums = {
        "IoType",
        "CableSize",
        "Seg",
        "CharmType",
        "TbType",
        "SegRole",
        "VoltageClass",
        "RedundancyKind",
    }
    assert expected_enums.issubset(schema.enums.keys())

    # Check specific enum values
    io_type = schema.get_enum("IoType")
    assert io_type is not None
    assert "AI" in io_type.values
    assert "DO_SOV" in io_type.values

    cable_size = schema.get_enum("CableSize")
    assert cable_size is not None
    assert set(cable_size.values) == {"2", "6", "12", "24"}


def test_load_default_from_file() -> None:
    """Verify loading schema directly from root schema.yaml file."""
    schema = Schema.load_default()
    assert schema.version == "2.1.0"
    assert "Signal" in schema.entities
    assert "Cabinet" in schema.entities
    assert schema.get_entity("Baseplate").fields["capacity"].default == 12


def test_base_schema_field_defaults() -> None:
    """Verify field types and default values match Section 1.2."""
    schema = get_base_schema()

    baseplate = schema.get_entity("Baseplate")
    assert baseplate is not None
    assert baseplate.fields["capacity"].default == 12
    assert baseplate.fields["capacity"].type_str == "Int"

    cioc = schema.get_entity("CIOC")
    assert cioc is not None
    assert cioc.fields["capacity"].default == 8

    cabinet = schema.get_entity("Cabinet")
    assert cabinet is not None
    assert cabinet.fields["capacity"].default == 4
    assert cabinet.fields["spare_target_percent"].default == 10.0


def test_schema_yaml_persistence_roundtrip() -> None:
    """Verify schema can be saved to YAML and reloaded without loss."""
    schema = get_base_schema()
    yaml_text = schema.to_yaml()

    reloaded = Schema.from_yaml(yaml_text)
    assert reloaded.version == schema.version
    assert set(reloaded.entities.keys()) == set(schema.entities.keys())
    assert set(reloaded.enums.keys()) == set(schema.enums.keys())

    # Verify field on Signal
    sig_reloaded = reloaded.get_entity("Signal")
    sig_orig = schema.get_entity("Signal")
    assert sig_reloaded is not None and sig_orig is not None
    assert sig_reloaded.fields["tag"].type_str == sig_orig.fields["tag"].type_str
    assert sig_reloaded.fields["group"].is_optional == sig_orig.fields["group"].is_optional


def test_schema_extension_register_api() -> None:
    """Verify Section 1.4 register_field, register_entity, register_enum APIs."""
    schema = get_base_schema()

    # 1. Register new enum
    schema.register_enum("FieldbusProtocol", ["FOUNDATION_H1", "PROFIBUS_PA", "MODBUS_TCP"])
    assert "FieldbusProtocol" in schema.enums
    assert schema.get_enum("FieldbusProtocol").values == ["FOUNDATION_H1", "PROFIBUS_PA", "MODBUS_TCP"]

    # 2. Register new entity
    schema.register_entity(
        "JunctionBox",
        fields={
            "id": "JBId",
            "area": "Area?",
            "max_cables": "Int = 16",
        },
    )
    assert "JunctionBox" in schema.entities
    jb = schema.get_entity("JunctionBox")
    assert jb.fields["max_cables"].default == 16

    # 3. Register new field on existing entity
    schema.register_field("Signal", "power_rating", "Float?", default=0.5)
    sig = schema.get_entity("Signal")
    assert "power_rating" in sig.fields
    assert sig.fields["power_rating"].default == 0.5
    assert sig.fields["power_rating"].is_optional is True

    # 4. Resolve types for the extended schema
    t_enum = schema.resolve_type("FieldbusProtocol")
    assert isinstance(t_enum, EnumType)
    assert t_enum.name == "FieldbusProtocol"

    t_entity = schema.resolve_type("JunctionBox")
    assert isinstance(t_entity, EntityType)
    assert t_entity.name == "JunctionBox"


def test_type_system_coercions() -> None:
    """Verify Section 3.2 explicit-only coercions."""
    # Int -> Float in arithmetic is valid
    assert can_coerce(TYPE_INT, TYPE_FLOAT) is True

    # Int -> Slot only via explicit coercion
    slot_type = DomainType("Slot")
    assert can_coerce(TYPE_INT, slot_type, explicit=False) is False
    assert can_coerce(TYPE_INT, slot_type, explicit=True) is True

    # Tag -> String only via explicit coercion
    tag_type = DomainType("Tag")
    assert can_coerce(tag_type, TYPE_STRING, explicit=False) is False
    assert can_coerce(tag_type, TYPE_STRING, explicit=True) is True

    # Int -> String or String -> Int rejected
    assert can_coerce(TYPE_INT, TYPE_STRING, explicit=True) is False
    assert can_coerce(TYPE_STRING, TYPE_INT, explicit=True) is False


def test_type_parsing() -> None:
    """Verify parsing of complex types (Option, List, Set)."""
    schema = get_base_schema()

    t_opt = schema.resolve_type("GroupRef?")
    assert isinstance(t_opt, OptionType)
    assert t_opt.is_option is True

    t_list = schema.resolve_type("Pair[]")
    assert isinstance(t_list, ListType)
    assert t_list.is_collection is True

    t_set = schema.resolve_type("Set<Tag>")
    assert isinstance(t_set, SetType)
    assert t_set.is_collection is True


def test_entity_instance_defaults_and_validation() -> None:
    """Verify Entity instances inherit schema defaults and validate required fields."""
    schema = get_base_schema()

    # Baseplate default capacity = 12
    bp = Baseplate(schema=schema, id="BP-01", index=1)
    assert bp.capacity == 12

    # Cabinet default capacity = 4, spare_target_percent = 10.0
    cab = Cabinet(schema=schema, id="CAB-01", area="Area1", unit="Unit1", seq=1)
    assert cab.capacity == 4
    assert cab.spare_target_percent == 10.0

    # Validation: missing required field 'tag' on Signal should log E4001
    collector = ErrorCollector()
    bad_sig = Signal(schema=schema, io_type="AI")  # Missing 'tag', 'cable', 'pair', 'safety'
    is_valid = bad_sig.validate(collector=collector)
    assert is_valid is False
    assert collector.has_code("E4001")


def test_schema_field_access_errors() -> None:
    """Verify E2001 and E2002 error generation on invalid field accesses."""
    schema = get_base_schema()
    collector = ErrorCollector()

    # E2001: Unknown entity
    fdef = schema.validate_field_access("NonExistentEntity", "foo", collector=collector)
    assert fdef is None
    assert collector.has_code("E2001")

    # E2002: Unknown field
    collector.clear()
    fdef2 = schema.validate_field_access("Signal", "non_existent_field", collector=collector)
    assert fdef2 is None
    assert collector.has_code("E2002")


def test_dynamic_entity_fields_without_engine_changes() -> None:
    """
    Verify requirement: 'Adding a field must not require changing the engine.'
    Adding a field dynamically to schema allows the entity to store and access it.
    """
    schema = get_base_schema()
    schema.register_field("Signal", "hazard_class", "String?", default="Class_I_Div_1")

    sig = Signal(schema=schema, tag="FIT-101", io_type="AI", cable="C-01", pair=1, safety=True)
    # Default is automatically available
    assert sig.hazard_class == "Class_I_Div_1"

    # Can set custom value
    sig.hazard_class = "Zone_0"
    assert sig.hazard_class == "Zone_0"
    assert sig["hazard_class"] == "Zone_0"


def test_reference_resolution() -> None:
    """Verify reference resolution for SignalRef, CableRef, CiocRef, etc."""
    schema = get_base_schema()

    sig_def = schema.resolve_reference("SignalRef")
    assert sig_def is not None
    assert sig_def.name == "Signal"

    cioc_def = schema.resolve_reference("CiocRef")
    assert cioc_def is not None
    assert cioc_def.name == "CIOC"

    cab_def = schema.resolve_reference("CabinetRef")
    assert cab_def is not None
    assert cab_def.name == "Cabinet"

    # Reference to non-existent entity returns None
    assert schema.resolve_reference("NoSuchEntityRef") is None


def test_duplicate_registration_rejection() -> None:
    """Verify duplicate entity, enum, and field registrations are rejected."""
    schema = get_base_schema()

    # Duplicate entity
    with pytest.raises(ValueError, match="Duplicate entity registration"):
        schema.register_entity("Signal", fields={"foo": "String"})

    # Duplicate enum
    with pytest.raises(ValueError, match="Duplicate enum registration"):
        schema.register_enum("IoType", ["AI", "AO"])

    # Duplicate field on existing entity
    with pytest.raises(ValueError, match="Duplicate field registration"):
        schema.register_field("Signal", "tag", "String")


def test_invalid_field_types_and_defaults() -> None:
    """Verify rejection of invalid types, references, and default values."""
    schema = get_base_schema()

    # Invalid entity reference target
    with pytest.raises(ValueError, match="Invalid entity reference"):
        schema.register_field("Signal", "fake_ref", "FakeEntityRef")

    # Nested option type rejected
    with pytest.raises(ValueError, match="Nested Option types"):
        schema.register_field("Signal", "nested_opt", "Option<Option<Int>>")

    with pytest.raises(ValueError, match="Invalid nested option type syntax"):
        schema.register_field("Signal", "double_q", "Int??")

    # Invalid default value for Int
    with pytest.raises(ValueError, match="Invalid default value"):
        schema.register_field("Signal", "bad_int", "Int", default="not_an_int")

    # Invalid default value for Enum
    with pytest.raises(ValueError, match="Invalid default value"):
        schema.register_field("Signal", "bad_enum", "IoType", default="UNKNOWN_IO_KIND")


def test_schema_version_compatibility() -> None:
    """Verify semantic version compatibility checking."""
    schema = Schema(version="2.1.0")

    # Compatible with same major, lower or equal minor
    assert schema.check_version_compatibility("2.0.0") is True
    assert schema.check_version_compatibility("2.1.0") is True

    # Incompatible with higher minor
    assert schema.check_version_compatibility("2.2.0") is False

    # Incompatible with different major
    assert schema.check_version_compatibility("1.9.0") is False
    assert schema.check_version_compatibility("3.0.0") is False
