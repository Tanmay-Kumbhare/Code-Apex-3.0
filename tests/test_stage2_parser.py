"""
Unit and integration tests for Stage 2: Lexer, Parser, and AST.
Tests complete grammar productions, operator precedence, source locations,
and syntax error codes E1001-E1007, E2016, E2017.
"""

from pathlib import Path
import pytest

from arl import (
    AssignEffect,
    BinaryOp,
    CallExpr,
    ErrorCollector,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    LiteralExpr,
    PreferEffect,
    QuantifiedExpr,
    RequireEffect,
    RuleMeta,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
    parse_ruleset,
)
from arl.lexer import Lexer, TokenType
from arl.parser import Parser


def test_parse_valid_sample_fixture() -> None:
    """Parse representative valid ARL file and inspect complete AST."""
    fixture_path = Path(__file__).parent / "fixtures" / "valid_sample.arl"
    source = fixture_path.read_text(encoding="utf-8")

    collector = ErrorCollector()
    ruleset = parse_ruleset(source, source_file=str(fixture_path), collector=collector)

    # Must parse with zero errors
    assert not collector.has_errors
    assert ruleset.name == "ProjectAR1"
    assert ruleset.extends == "BaseRules"
    assert ruleset.location is not None
    assert ruleset.location.file == str(fixture_path)

    # Check includes
    assert len(ruleset.includes) == 1
    assert ruleset.includes[0].path == "common_rules.arl"

    # Check rules
    assert len(ruleset.rules) == 6

    # 1. R_Safety_Segregation
    r1 = ruleset.get_rule("R_Safety_Segregation")
    assert r1 is not None
    assert r1.scope == "Signal"
    assert isinstance(r1.when, BinaryOp)
    assert r1.when.op == "=="
    assert len(r1.effects) == 1
    assert isinstance(r1.effects[0], RequireEffect)
    assert isinstance(r1.effects[0].target, CallExpr)
    assert r1.meta.priority == "hard"
    assert r1.meta.rule_id == "R001"
    assert "safety" in r1.meta.tags

    # 2. R_Assign_TB
    r2 = ruleset.get_rule("R_Assign_TB")
    assert r2 is not None
    assert isinstance(r2.effects[0], AssignEffect)
    assert r2.effects[0].field_name == "tb_type"

    # 3. R_Prefer_Contiguous
    r3 = ruleset.get_rule("R_Prefer_Contiguous")
    assert r3 is not None
    assert isinstance(r3.effects[0], PreferEffect)
    assert r3.meta.priority == "soft"
    assert r3.meta.weight == 10.0

    # 4. R_Rule_Reference
    r4 = ruleset.get_rule("R_Rule_Reference")
    assert r4 is not None
    assert isinstance(r4.effects[0], RequireEffect)
    assert isinstance(r4.effects[0].target, RuleRef)
    assert r4.effects[0].target.rule_name == "R_Safety_Segregation"

    # 5. R_Quantifier_And_Aggregate
    r5 = ruleset.get_rule("R_Quantifier_And_Aggregate")
    assert r5 is not None
    assert isinstance(r5.effects[0].target, QuantifiedExpr)
    q = r5.effects[0].target
    assert q.kind == "forall"
    assert q.var_name == "bp"
    assert q.where_clause is not None

    # 6. R_Optional_Chaining_And_Ops
    r6 = ruleset.get_rule("R_Optional_Chaining_And_Ops")
    assert r6 is not None
    assert isinstance(r6.effects[0], ForbidEffect)

    # Objectives
    assert len(ruleset.objectives) == 1
    obj = ruleset.objectives[0]
    assert obj.name == "Maximize_Packing"
    assert len(obj.primary) == 2
    assert len(obj.secondary) == 1
    assert obj.weights["used_slots"] == 100.0

    # Overrides
    assert len(ruleset.overrides) == 1
    ov = ruleset.overrides[0]
    assert ov.target_rule == "R_Safety_Segregation"
    assert isinstance(ov.effects[0], ForbidEffect)


def test_operator_precedence() -> None:
    """Verify operator precedence: not > and > or, * / > + -, comparisons."""
    # Boolean precedence: not a and b or c -> ((not a) and b) or c
    src = """
    ruleset P {
        rule R:
            scope: Signal
            when: not a and b or c
            require true
    }
    """
    rs = parse_ruleset(src)
    r = rs.rules[0]
    cond = r.when
    assert isinstance(cond, BinaryOp)
    assert cond.op == "or"
    assert isinstance(cond.left, BinaryOp)
    assert cond.left.op == "and"
    assert isinstance(cond.left.left, UnaryOp)
    assert cond.left.left.op == "not"

    # Arithmetic precedence: a + b * c -> a + (b * c)
    src_arith = """
    ruleset P {
        rule R:
            scope: Signal
            when: a + b * c == 42
            require true
    }
    """
    rs_arith = parse_ruleset(src_arith)
    cond_eq = rs_arith.rules[0].when
    assert isinstance(cond_eq, BinaryOp)
    assert cond_eq.op == "=="
    add_op = cond_eq.left
    assert isinstance(add_op, BinaryOp)
    assert add_op.op == "+"
    assert isinstance(add_op.right, BinaryOp)
    assert add_op.right.op == "*"


def test_error_e1001_unexpected_token() -> None:
    """Verify E1001 on unexpected token."""
    src = "ruleset P { rule R : scope : Signal @invalid require true }"
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1001")


def test_error_e1002_unterminated_string() -> None:
    """Verify E1002 on unterminated string literal."""
    src = 'ruleset P { rule R : scope : Signal when: self.tag == "unterminated require true }'
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1002")


def test_error_e1003_missing_colon() -> None:
    """Verify E1003 on missing colon after rule name."""
    src = "ruleset P { rule R scope: Signal require true }"
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1003")


def test_error_e1004_duplicate_rule_id() -> None:
    """Verify E1004 on duplicate explicit rule ID."""
    src = """
    ruleset P {
        rule R1:
            scope: Signal
            require true
            id: DUP01

        rule R2:
            scope: Signal
            require true
            id: DUP01
    }
    """
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1004")


def test_error_e1005_invalid_block_structure() -> None:
    """Verify E1005 on unclosed or missing braces."""
    src = "ruleset P rule R: scope: Signal require true"
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1005")

    # Unclosed brace
    src_unclosed = "ruleset P { rule R: scope: Signal require true"
    collector2 = ErrorCollector()
    parse_ruleset(src_unclosed, collector=collector2)
    assert collector2.has_code("E1005")


def test_error_e1006_invalid_numeric_literal() -> None:
    """Verify E1006 on malformed numbers."""
    src = "ruleset P { rule R: scope: Signal when: self.pair == 12.34.56 require true }"
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1006")


def test_error_e1007_invalid_escape_sequence() -> None:
    """Verify E1007 on invalid escape sequence in string."""
    src = 'ruleset P { rule R: scope: Signal when: self.tag == "\\k" require true }'
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E1007")


def test_error_e2016_reserved_keyword_as_ident() -> None:
    """Verify E2016 when reserved keyword is used as identifier."""
    src = "ruleset P { rule forall : scope : Signal require true }"
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E2016")


def test_error_e2017_duplicate_rule_name() -> None:
    """Verify E2017 on duplicate rule name in same ruleset."""
    src = """
    ruleset P {
        rule R_Same:
            scope: Signal
            require true

        rule R_Same:
            scope: Signal
            require false
    }
    """
    collector = ErrorCollector()
    parse_ruleset(src, collector=collector)
    assert collector.has_code("E2017")


def test_rule_quarantine_error_recovery() -> None:
    """Verify rule quarantine: broken rule is flagged, valid rule is still parsed."""
    src = """
    ruleset P {
        rule BadRule scope Signal require true

        rule GoodRule:
            scope: Signal
            require true
            id: GOOD01
    }
    """
    collector = ErrorCollector()
    ruleset = parse_ruleset(src, collector=collector)

    # BadRule generated error E1003
    assert collector.has_code("E1003")
    # GoodRule was still successfully recovered and parsed
    assert ruleset.get_rule("GoodRule") is not None
    assert ruleset.get_rule("GoodRule").meta.rule_id == "GOOD01"
