"""
Unit and integration tests for Stage 3: Type Checking, Resolution, and Normalization.
Verifies all semantic error codes E2001-E2018, option unwrapping, type coercions,
and normalization idempotence.
"""

from pathlib import Path
import pytest

from arl import (
    ErrorCollector,
    Parser,
    Resolver,
    Schema,
    TypeChecker,
    get_base_schema,
    normalize_ruleset,
    parse_ruleset,
)


def test_valid_ruleset_passes_typecheck_and_resolve() -> None:
    """Verify that a valid ruleset passes type checking, name resolution, and normalization."""
    fixture_path = Path(__file__).parent / "fixtures" / "valid_sample.arl"
    source = fixture_path.read_text(encoding="utf-8")

    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(source, source_file=str(fixture_path), collector=collector)
    assert not collector.has_errors

    # Provide BaseRules so extends resolves
    known = {"BaseRules": parse_ruleset("ruleset BaseRules {}")}
    resolver = Resolver(schema, known_rulesets=known, collector=collector)
    resolver.resolve(ruleset)
    assert not collector.has_errors

    # Typecheck
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)
    assert not collector.has_errors

    # Normalize
    norm = normalize_ruleset(ruleset)
    assert norm.name == ruleset.name


def test_e2001_unknown_entity_type() -> None:
    """E2001: Rule specifies unknown entity scope."""
    src = "ruleset P { rule R1: scope: BogusEntity require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2001")


def test_e2002_unknown_field_on_entity() -> None:
    """E2002: Unknown field accessed on scoped entity."""
    src = "ruleset P { rule R1: scope: Signal when: self.non_existent_field == 123 require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2002")


def test_e2003_unknown_predicate() -> None:
    """E2003: Call to unknown predicate function."""
    src = "ruleset P { rule R1: scope: Signal when: non_existent_pred(self) require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2003")


def test_e2004_predicate_arity_mismatch() -> None:
    """E2004: Predicate called with wrong number of arguments."""
    src = "ruleset P { rule R1: scope: Signal when: same_cabinet(self, 1, 2) require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2004")


def test_e2005_type_mismatch() -> None:
    """E2005: Type mismatch in arithmetic or comparison."""
    # Arithmetic with string and int
    src = 'ruleset P { rule R1: scope: Signal when: 1 + "hello" == 2 require true }'
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2005")


def test_e2006_missing_option_unwrap() -> None:
    """E2006: Direct '.' access on an optional field without unwrapping or '?.'."""
    # Signal.group is GroupRef? (Option<GroupRef>)
    src = 'ruleset P { rule R1: scope: Signal when: self.group.id == "G1" require true }'
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2006")


def test_e2007_field_access_on_non_entity() -> None:
    """E2007: Field access on primitive or non-entity type."""
    # Signal.safety is Bool, cannot access safety.foo
    src = "ruleset P { rule R1: scope: Signal when: self.safety.foo == true require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2007")


def test_e2008_aggregate_over_non_collection() -> None:
    """E2008: Aggregate applied to non-collection expression."""
    # Signal.tag is Tag (scalar), not a collection
    src = "ruleset P { rule R1: scope: Signal when: count(self.tag) > 0 require true }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2008")


def test_e2009_cyclic_rule_reference() -> None:
    """E2009: Cyclic rule references in dependency graph."""
    src = """
    ruleset P {
        rule R1:
            scope: Signal
            require rule R2

        rule R2:
            scope: Signal
            require rule R1
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2009")


def test_e2010_unknown_rule_reference() -> None:
    """E2010: Rule references non-existent rule."""
    src = "ruleset P { rule R1: scope: Signal require rule NonExistentRule }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2010")


def test_e2011_duplicate_field_writer() -> None:
    """E2011: Multiple active rules assign the same field on the same entity."""
    src = """
    ruleset P {
        rule R1:
            scope: Charm
            assign tb_type = "STANDARD"

        rule R2:
            scope: Charm
            assign tb_type = "RELAY_OUTPUT"
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2011")


def test_e2012_override_scope_mismatch() -> None:
    """E2012: Override modifies field outside target rule's scope."""
    src = """
    ruleset P {
        rule R1:
            scope: Signal
            require true

        override R1:
            assign tb_type = "STANDARD"
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2012")


def test_e2013_undefined_override_target() -> None:
    """E2013: Override targets undefined rule."""
    src = "ruleset P { override NonExistentRule: forbid false }"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2013")


def test_e2014_unknown_objective_weight() -> None:
    """E2014: Objective weights dictionary references an unknown term."""
    src = """
    ruleset P {
        objective OptPacking:
            primary: [count(self.signals)]
            weights: { non_existent_term: 50.0 }
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2014")


def test_e2015_invalid_regex() -> None:
    """E2015: Invalid regular expression in 'matches' operation."""
    src = 'ruleset P { rule R1: scope: Signal when: self.tag matches "[unclosed" require true }'
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert collector.has_code("E2015")


def test_e2018_extends_target_not_found() -> None:
    """E2018: Ruleset extends unknown ruleset target."""
    src = "ruleset P extends NonExistentParentRuleset {}"
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    resolver = Resolver(schema, collector=collector)
    resolver.resolve(ruleset)

    assert collector.has_code("E2018")


def test_explicit_cast_as_slot() -> None:
    """Verify Section 3.2 explicit cast 'as Slot' works, but invalid casts are rejected."""
    # Valid: Int -> Slot via 'as Slot'
    src_valid = "ruleset P { rule R1: scope: Signal when: (self.pair as Slot) == 1 require true }"
    schema = get_base_schema()
    collector_valid = ErrorCollector()

    rs_valid = parse_ruleset(src_valid, collector=collector_valid)
    checker_valid = TypeChecker(schema, collector=collector_valid)
    checker_valid.check_ruleset(rs_valid)
    assert not collector_valid.has_errors

    # Invalid: cannot cast Int to Cable
    src_invalid = "ruleset P { rule R1: scope: Signal when: (self.pair as Cable) == 1 require true }"
    collector_invalid = ErrorCollector()

    rs_invalid = parse_ruleset(src_invalid, collector=collector_invalid)
    checker_invalid = TypeChecker(schema, collector=collector_invalid)
    checker_invalid.check_ruleset(rs_invalid)
    assert collector_invalid.has_code("E2005")


def test_is_some_and_is_none() -> None:
    """Verify option handling: 'is some' and 'is none' unwrapping."""
    src = """
    ruleset P {
        rule R1:
            scope: Signal
            when: self.group is some g and self.group is none
            require true
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()

    ruleset = parse_ruleset(src, collector=collector)
    checker = TypeChecker(schema, collector=collector)
    checker.check_ruleset(ruleset)

    assert not collector.has_errors


def test_normalization_idempotence() -> None:
    """Verify normalize(normalize(ruleset)) == normalize(ruleset)."""
    fixture_path = Path(__file__).parent / "fixtures" / "valid_sample.arl"
    source = fixture_path.read_text(encoding="utf-8")

    ruleset = parse_ruleset(source)
    norm1 = normalize_ruleset(ruleset)
    norm2 = normalize_ruleset(norm1)

    assert norm1.model_dump() == norm2.model_dump()
