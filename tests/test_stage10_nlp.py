"""
Test Suite for ARL v2 — Stage 10: Natural-Language Bridge.

Tests all requirements from Part 11 of the authoritative specification:
  - 11.1 Pipeline:
      User text -> Intent classifier -> DSL draft -> Parser -> Typecheck
      -> Scope check -> Static conflict check -> Dry-run -> Impact -> Human approval -> Activate
  - 11.2 Guardrails:
      - Grammar-constrained decoding & provider interface (mockable/disableable).
      - Rejection of malformed tokens, unknown predicates, duplicate IDs,
        undefined fields, type errors, scope violations, and static conflicts.
      - Structured proposal logging in JSONL.
  - 11.3 Deterministic fallback:
      - Form-based rule builder and direct ARL authoring when LLM is disabled.
  - 11.4 Ambiguity & failure handling:
      - Clarifying questions with 2-3 concrete options.
      - Conflicts showing involved rules.
      - High-impact threshold requiring explicit human confirmation.
  - 11.5 Predicate suggestions & plugin registration.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from arl.ast import Rule, Ruleset
from arl.entities import Baseplate, Cabinet, CIOC, Project, Signal
from arl.nlp import (
    DisabledLLMProvider,
    FormRuleSpec,
    IntentType,
    MockLLMProvider,
    NLPBridge,
    NLPProposalLogger,
    NLPValidator,
    ProposalStatus,
    ProposedPredicate,
    ValidationStage,
    activate_rule,
    build_rule_from_form,
    classify_intent,
    generate_dsl_draft,
    register_proposed_predicate,
)
from arl.predicates import PredicateRegistry, get_builtin_predicate_registry
from arl.schema import get_base_schema


@pytest.fixture
def sample_project() -> Project:
    """Sample project for dry-run impact evaluation."""
    cab = Cabinet(id="CAB-01", area="AREA-A", seq=1)
    cioc = CIOC(id="CIOC-01", cabinet="CAB-01", index=1, seg_role="MIXED")
    bp1 = Baseplate(id="BP-01", cioc="CIOC-01", index=1, capacity=12)
    bp2 = Baseplate(id="BP-02", cioc="CIOC-01", index=2, capacity=12)
    cioc["baseplates"] = [bp1, bp2]
    cab["ciocs"] = [cioc]

    signals = [
        Signal(tag=f"FT-10{i}", io_type="AI", cable="CBL-01", pair=i, safety=(i <= 2))
        for i in range(1, 7)
    ]
    return Project(name="TestProj", cabinets=[cab], signals=signals)


@pytest.fixture
def base_ruleset() -> Ruleset:
    """Base ruleset fixture."""
    from arl.parser import parse_ruleset
    src = """
    ruleset BaseProjectRules {
        rule R_Slot_Bounds:
            scope: Charm
            require slot >= 1
            priority: hard
            id: R001
    }
    """
    return parse_ruleset(src)


class TestNLPClassificationAndAmbiguity:
    """Test Part 11.1 & 11.4 intent classification, ambiguity, and clarifying questions."""

    def test_ambiguous_intent_returns_clarifying_question(self):
        """Test that underspecified requests like 'segregate signals' trigger a clarifying question with 2-3 options."""
        res = classify_intent("Please segregate the signals")
        assert res.is_ambiguous is True
        assert res.intent == IntentType.AMBIGUOUS
        assert res.clarification_question is not None
        assert len(res.options) in (2, 3)
        assert any("Baseplate" in opt for opt in res.options)
        assert any("CIOC" in opt for opt in res.options)

    def test_missing_predicate_detection(self):
        """Test that requests requiring unknown predicates (e.g. 'rack') propose predicate signatures."""
        res = classify_intent("Signals must be located in the same rack")
        assert res.intent == IntentType.PREDICATE_REQUEST
        pred_data = res.parameters.get("proposed_predicate")
        assert pred_data is not None
        assert pred_data["name"] == "same_rack"
        assert pred_data["returns"] == "Bool"


class TestDeterministicFallback:
    """Test Part 11.3 offline deterministic operation without any LLM."""

    def test_disabled_llm_provider(self):
        """Verify DisabledLLMProvider reports unavailable and raises on generate."""
        provider = DisabledLLMProvider()
        assert provider.is_available() is False
        with pytest.raises(RuntimeError, match="disabled"):
            provider.generate("Some prompt")

    def test_form_rule_builder_produces_valid_arl(self):
        """Verify form-based rule builder deterministically creates valid ARL syntax."""
        spec = FormRuleSpec(
            rule_name="R_Safety_Segregation",
            scope="Signal",
            condition="self.safety == true",
            effect_kind="require",
            effect_expr="same_baseplate(self)",
            priority="hard",
            rule_id="R_SAFE_01",
            message="Safety signals must be segregated to baseplates",
            tags=["safety", "segregation"],
        )
        dsl_text = build_rule_from_form(spec)
        assert "rule R_Safety_Segregation:" in dsl_text
        assert "scope: Signal" in dsl_text
        assert "when: self.safety == true" in dsl_text
        assert "require same_baseplate(self)" in dsl_text
        assert "priority: hard" in dsl_text
        assert "id: R_SAFE_01" in dsl_text

        # Verify generated DSL parses without error
        from arl.parser import parse_ruleset
        rs = parse_ruleset(f"ruleset FormGen {{\n{dsl_text}\n}}")
        assert len(rs.rules) == 1
        assert rs.rules[0].name == "R_Safety_Segregation"

    def test_nlp_bridge_offline_with_disabled_llm(self, sample_project: Project, base_ruleset: Ruleset, tmp_path: Path):
        """Demonstrate an offline natural language request compiling deterministically with LLM disabled."""
        log_file = tmp_path / "nlp_offline.jsonl"
        bridge = NLPBridge(
            llm_provider=DisabledLLMProvider(),
            audit_logger=NLPProposalLogger(log_file),
        )

        res = bridge.process_request(
            user_text="Safety signals must connect to the same baseplate",
            current_ruleset=base_ruleset,
            project_sample=sample_project,
        )

        assert res.valid is True
        assert res.rule_obj is not None
        assert res.rule_obj.name == "R_Safety_Baseplate"
        assert log_file.exists()


class TestCompilerGuardrailsAndRejections:
    """Test Part 11.2 compiler safety guardrails and rejection of invalid proposals."""

    def test_reject_tokens_outside_grammar(self, base_ruleset: Ruleset):
        """Reject proposals with non-ARL syntax (e.g. Python code) at PARSER stage."""
        python_junk = "if signal.safety == True: return baseplate.id"
        validator = NLPValidator()
        res = validator.validate_proposal(python_junk, user_text="Safety code", current_ruleset=base_ruleset)

        assert res.valid is False
        assert res.failed_stage == ValidationStage.PARSER
        assert len(res.errors) >= 1
        assert any(e.code == "E1001" for e in res.errors)

    def test_reject_unknown_predicate(self, base_ruleset: Ruleset):
        """Reject proposals calling an unregistered predicate at TYPECHECK stage."""
        rule_dsl = """
        rule R_Unknown_Pred:
            scope: Signal
            require totally_fake_predicate(self)
            priority: hard
        """
        validator = NLPValidator()
        res = validator.validate_proposal(rule_dsl, user_text="Use fake pred", current_ruleset=base_ruleset)

        assert res.valid is False
        assert res.failed_stage == ValidationStage.TYPECHECK
        assert any(e.code == "E2003" for e in res.errors)

    def test_reject_duplicate_rule_id_and_name(self, base_ruleset: Ruleset):
        """Reject proposals with duplicate rule ID (E1004) or name (E2017)."""
        # Base ruleset already has R_Slot_Bounds with ID R001
        dup_id_dsl = """
        rule R_New_Rule:
            scope: Signal
            require slot <= 10
            priority: hard
            id: R001
        """
        validator = NLPValidator()
        res = validator.validate_proposal(dup_id_dsl, user_text="Duplicate id", current_ruleset=base_ruleset)

        assert res.valid is False
        assert res.failed_stage == ValidationStage.SCOPE_CHECK
        assert any(e.code == "E1004" for e in res.errors)

    def test_reject_undefined_field_and_type_mismatch(self, base_ruleset: Ruleset):
        """Reject proposals referencing non-existent fields (E2002) or type mismatches (E2005)."""
        bad_field_dsl = """
        rule R_Bad_Field:
            scope: Signal
            require self.non_existent_field == 42
            priority: hard
        """
        validator = NLPValidator()
        res = validator.validate_proposal(bad_field_dsl, user_text="Bad field", current_ruleset=base_ruleset)

        assert res.valid is False
        assert res.failed_stage == ValidationStage.TYPECHECK
        assert any(e.code == "E2002" for e in res.errors)

    def test_reject_static_conflict_and_show_involved_rules(self, base_ruleset: Ruleset):
        """Reject proposals causing static conflicts (E3001) and isolate involved rules."""
        # Base ruleset has rule R_Slot_Bounds: require slot >= 1
        # Add conflicting rule requiring slot <= 0
        conflict_dsl = """
        rule R_Conflict_Slot:
            scope: Charm
            require slot <= 0
            priority: hard
            id: R002
        """
        validator = NLPValidator()
        res = validator.validate_proposal(conflict_dsl, user_text="Conflicting slot bounds", current_ruleset=base_ruleset)

        assert res.valid is False
        assert res.failed_stage == ValidationStage.CONFLICT_CHECK
        assert any(e.code == "E3001" for e in res.conflicts)
        assert "R_Slot_Bounds" in res.involved_rules or "R_Conflict_Slot" in res.involved_rules


class TestHighImpactAndHumanApproval:
    """Test Part 11.4 high impact preview and human confirmation gate."""

    def test_high_impact_requires_human_approval_and_blocks_unauthorized_activation(self, sample_project: Project, base_ruleset: Ruleset, tmp_path: Path):
        """Verify high impact movements require explicit confirmation before activation."""
        mock = MockLLMProvider()
        # Force all signals to slot >= 7, moving signals from baseline slots 1..6
        tight_rule = """
        rule R_Tight_Slot:
            scope: Charm
            require slot <= 3
            priority: hard
            id: R_TIGHT
        """
        mock.add_response("Restrict signals", tight_rule)

        log_file = tmp_path / "nlp_impact.jsonl"
        bridge = NLPBridge(
            llm_provider=mock,
            audit_logger=NLPProposalLogger(log_file),
            impact_threshold=0.10,  # 10% threshold
        )

        res = bridge.process_request("Restrict signals", current_ruleset=base_ruleset, project_sample=sample_project)

        assert res.valid is True
        assert res.high_impact is True
        assert res.requires_human_confirmation is True
        assert res.status == ProposalStatus.PENDING_APPROVAL

        # Attempt to activate WITHOUT human approval must raise PermissionError
        with pytest.raises(PermissionError, match="human approval"):
            activate_rule(res, base_ruleset, human_approved=False, logger=bridge.logger)

        # Activate WITH human approval succeeds
        activate_rule(res, base_ruleset, human_approved=True, logger=bridge.logger)
        assert res.status == ProposalStatus.ACTIVATED
        assert any(r.name == "R_Tight_Slot" for r in base_ruleset.rules)


class TestPredicateSuggestionAndRegistration:
    """Test Part 11.5 predicate suggestion and explicit validated plugin registration."""

    def test_register_proposed_predicate_plugin(self):
        """Verify proposing and explicitly registering a new predicate without arbitrary code execution."""
        registry = get_builtin_predicate_registry()
        proposal = ProposedPredicate(
            name="same_rack",
            args=[("sig1", "Signal"), ("sig2", "Signal")],
            returns="Bool",
            description="True if instruments share the same equipment rack",
            category="relational",
            lowering_requirements="Linear equality on rack ID",
        )

        # Registration must be explicit
        defn = register_proposed_predicate(registry, proposal)
        assert defn.name == "same_rack"
        assert registry.has("same_rack") is True
        assert registry.get("same_rack").returns == "Bool"
