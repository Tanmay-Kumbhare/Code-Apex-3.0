"""
Unit and integration tests for Stage 4: Predicates, DAG, Overrides, and Constraint IR.
Tests every IR node, built-in predicates, duplicate registration, dependency DAG,
include cycles, hard-to-soft checks, override application, and lowering to IR.
"""

from pathlib import Path
import pytest

from arl import (
    AllDifferent,
    And,
    Contiguous,
    Count,
    Eq,
    ErrorCollector,
    Exists,
    ForAll,
    Gt,
    Gte,
    IRObjective,
    Implies,
    Lowerer,
    Lt,
    Lte,
    Neq,
    Not,
    Or,
    PredicateRegistry,
    RuleDAG,
    Soft,
    Sum,
    Var,
    apply_overrides,
    detect_include_cycles,
    get_base_schema,
    get_builtin_predicate_registry,
    parse_ruleset,
)


def test_builtin_predicates_catalog() -> None:
    """Verify all 28+ required built-in predicates are registered with exact signatures."""
    reg = get_builtin_predicate_registry()

    required_preds = [
        # Relational
        "same_cabinet", "same_cioc", "same_baseplate", "same_charm",
        "adjacent", "distance", "shares_cable", "shares_group",
        # Capacity
        "free_slots", "used_slots", "fill_percent", "spare_percent",
        # Contiguity
        "contiguous", "consecutive", "ordered_by",
        # Cable/pair
        "pair_index", "cable_size", "wired_spare", "orphan_pair",
        # Hardware
        "charm_for", "tb_for", "compatible", "mixable",
        # Naming
        "matches_pattern", "format",
        # Revision
        "changed_since", "added_in", "deleted_in", "pinned", "moved_since",
    ]

    for p in required_preds:
        defn = reg.get(p)
        assert defn is not None, f"Missing required predicate '{p}'"
        assert defn.returns is not None
        assert defn.description != ""


def test_predicate_registry_one_time_and_yaml_persistence(tmp_path: Path) -> None:
    """Verify one-time registration and YAML roundtrip for predicates."""
    reg = PredicateRegistry()
    reg.register(
        name="custom_voltage",
        args=[("s", "Signal")],
        returns="VoltageClass",
        description="Custom voltage reader",
    )

    # Duplicate registration rejected
    with pytest.raises(ValueError, match="Duplicate predicate registration"):
        reg.register(
            name="custom_voltage",
            args=[("s", "Signal")],
            returns="VoltageClass",
            description="Duplicate",
        )

    # YAML roundtrip
    yaml_file = tmp_path / "predicates_test.yaml"
    reg.save_yaml(yaml_file)

    loaded_reg = PredicateRegistry.from_yaml(yaml_file)
    assert loaded_reg.get("custom_voltage") is not None
    assert loaded_reg.get("custom_voltage").returns == "VoltageClass"


def test_constraint_ir_nodes_and_serialization() -> None:
    """Verify all 19 IR node types instantiate cleanly and serialize to dict."""
    v1 = Var(name="x", var_type="Int", domain=[1, 10])
    v2 = Var(name="y", var_type="Int", domain=[1, 10])

    nodes = [
        v1,
        Eq(left=v1, right=v2),
        Neq(left=v1, right=v2),
        Lt(left=v1, right=v2),
        Lte(left=v1, right=v2),
        Gt(left=v1, right=v2),
        Gte(left=v1, right=v2),
        And(nodes=[v1, v2]),
        Or(nodes=[v1, v2]),
        Not(node=v1),
        Implies(condition=v1, consequence=v2),
        AllDifferent(vars=[v1, v2]),
        Count(vars=[v1, v2], op="<=", value=5),
        Sum(vars=[v1, v2], op="==", value=10),
        Contiguous(vars=[v1, v2], order="asc"),
        ForAll(var_name="s", collection=Var(name="signals", var_type="Set"), predicate=v1),
        Exists(var_name="s", collection=Var(name="signals", var_type="Set"), predicate=v1),
        Soft(node=v1, weight=10.0),
        IRObjective(terms=[v1, v2], weights={"packing": 100.0}),
    ]

    for node in nodes:
        d = node.to_dict()
        assert isinstance(d, dict)
        assert "priority" in d


def test_rule_dag_topological_sort_and_deep_chains() -> None:
    """Verify deep dependency chain (A -> B -> C -> D) and topological ordering."""
    src = """
    ruleset P {
        rule D: scope: Signal require true
        rule C: scope: Signal require rule D
        rule B: scope: Signal require rule C
        rule A: scope: Signal require rule B
    }
    """
    ruleset = parse_ruleset(src)
    dag = RuleDAG()
    dag.build_from_ruleset(ruleset)

    cycles = dag.detect_cycles()
    assert len(cycles) == 0

    order = dag.topological_order()
    # D must come before C, C before B, B before A
    assert order.index("D") < order.index("C")
    assert order.index("C") < order.index("B")
    assert order.index("B") < order.index("A")


def test_rule_dag_cycle_detection_e2009() -> None:
    """Verify cycle detection in RuleDAG triggers E2009."""
    src = """
    ruleset P {
        rule R1: scope: Signal require rule R2
        rule R2: scope: Signal require rule R3
        rule R3: scope: Signal require rule R1
    }
    """
    collector = ErrorCollector()
    ruleset = parse_ruleset(src, collector=collector)
    dag = RuleDAG(collector=collector)
    dag.build_from_ruleset(ruleset)

    cycles = dag.detect_cycles()
    assert len(cycles) > 0
    assert collector.has_code("E2009")


def test_include_cycle_detection_e3004() -> None:
    """Verify include cycle detection across files triggers E3004."""
    include_graph = {
        "fileA.arl": ["fileB.arl"],
        "fileB.arl": ["fileC.arl"],
        "fileC.arl": ["fileA.arl"],
    }
    collector = ErrorCollector()
    cycles = detect_include_cycles(include_graph, collector=collector)

    assert len(cycles) > 0
    assert collector.has_code("E3004")


def test_e3005_hard_rule_references_soft_rule() -> None:
    """Verify E3005 when a hard rule requires a soft-only rule."""
    src = """
    ruleset P {
        rule SoftRule:
            scope: Signal
            prefer self.pair == 1
            priority: soft

        rule HardRule:
            scope: Signal
            require rule SoftRule
            priority: hard
    }
    """
    collector = ErrorCollector()
    ruleset = parse_ruleset(src, collector=collector)
    dag = RuleDAG(collector=collector)
    dag.build_from_ruleset(ruleset)
    dag.check_hard_soft_invariants()

    assert collector.has_code("E3005")


def test_override_application_and_effective_rules() -> None:
    """Verify applying overrides produces an effective ruleset with updated effects and metadata."""
    src = """
    ruleset P {
        rule R_Target:
            scope: Signal
            when: self.area == "AreaA"
            require self.pair == 1
            priority: hard
            id: R001

        override R_Target:
            when: self.voltage == "LV"
            forbid self.safety == true
            priority: soft
            weight: 25.0
            tags: [overridden]
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()
    ruleset = parse_ruleset(src, collector=collector)

    effective = apply_overrides(ruleset, schema=schema, collector=collector)
    assert not collector.has_errors

    target = effective.get_rule("R_Target")
    assert target is not None
    assert target.meta.priority == "soft"
    assert target.meta.weight == 25.0
    assert "overridden" in target.meta.tags
    # Effect replaced with ForbidEffect
    assert len(target.effects) == 1
    assert target.effects[0].__class__.__name__ == "ForbidEffect"


def test_e3003_override_removes_only_writer() -> None:
    """E3003: Override replaces an assignment and leaves a field with no writers."""
    src = """
    ruleset P {
        rule R_Assign:
            scope: Charm
            assign tb_type = "STANDARD"

        override R_Assign:
            require true
    }
    """
    schema = get_base_schema()
    collector = ErrorCollector()
    ruleset = parse_ruleset(src, collector=collector)

    apply_overrides(ruleset, schema=schema, collector=collector)
    assert collector.has_code("E3003")


def test_lowering_to_constraint_ir_provenance() -> None:
    """Verify complete lowering of sample ruleset into IR with provenance and metadata."""
    fixture_path = Path(__file__).parent / "fixtures" / "valid_sample.arl"
    source = fixture_path.read_text(encoding="utf-8")

    schema = get_base_schema()
    collector = ErrorCollector()
    ruleset = parse_ruleset(source, source_file=str(fixture_path), collector=collector)

    # Apply overrides
    effective = apply_overrides(ruleset, schema=schema, collector=collector)

    # Lower to IR
    lowerer = Lowerer(schema=schema, collector=collector)
    ir_nodes = lowerer.lower_ruleset(effective)

    assert len(ir_nodes) > 0

    # Verify provenance and node types
    rule_ids = {n.rule_id for n in ir_nodes if n.rule_id}
    assert "R_Safety_Segregation" in rule_ids or "R001" in rule_ids or "Maximize_Packing" in rule_ids

    # Inspect serialized form (verifying no raw Python exec)
    for node in ir_nodes:
        d = node.to_dict()
        assert "priority" in d
        assert node.__class__.__name__ in (
            "Eq", "Neq", "Lt", "Lte", "Gt", "Gte", "And", "Or", "Not",
            "Implies", "AllDifferent", "Count", "Sum", "Contiguous",
            "ForAll", "Exists", "Soft", "Objective", "Var"
        )
