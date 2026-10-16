"""
ARL Natural-Language Bridge.

Implements Part 11 of the authoritative ARL v2 specification:
  - 11.1 Pipeline:
      User text -> Intent classifier -> DSL draft (LLM)
        -> Parser -> Typecheck -> Scope check -> Static conflict check
        -> Dry-run on sample -> Impact preview -> Human approval -> Activate
  - 11.2 Guardrails:
      - Grammar-constrained decoding & provider interface (mockable/disableable).
      - Reject tokens outside grammar, unknown predicates, duplicate rule IDs,
        undefined entities/fields, type errors, scope violations, and static conflicts.
      - Log every LLM proposal and its disposition.
  - 11.3 Deterministic fallback:
      - Fully functional offline form-based rule builder and direct ARL authoring.
      - No feature is LLM-only.
  - 11.4 Failure handling:
      - Ambiguous intent -> clarifying question with 2-3 concrete options.
      - Invalid output -> structured errors with form UI / direct DSL editing fallback.
      - Conflicts -> involved rules and evidence.
      - High impact -> explicit human confirmation required.
  - 11.5 Predicate suggestion:
      - Suggest missing predicate signature, description, and lowering requirements.
      - Explicit, validated one-time plugin registration without arbitrary code execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import uuid
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import BinaryOp, Effect, Expr, ForbidEffect, LiteralExpr, RequireEffect, Rule, RuleMeta, Ruleset
from arl.conflict import ConflictChecker, ConflictCheckResult
from arl.dag import RuleDAG
from arl.entities import Project, Signal
from arl.errors import ArlError, ErrorCollector, Phase, Severity, make_error
from arl.parser import parse_ruleset
from arl.predicates import PredicateDefinition, PredicateRegistry, get_builtin_predicate_registry
from arl.resolve import Resolver
from arl.schema import Schema, get_base_schema
from arl.solver_cpsat import CPSATSolver
from arl.solver_greedy import AllocationResult, GreedySolver
from arl.typecheck import TypeChecker


# =========================================================================
# Enums and Data Models
# =========================================================================

class IntentType(str, Enum):
    CREATE_RULE = "create_rule"
    MODIFY_RULE = "modify_rule"
    PREDICATE_REQUEST = "predicate_request"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"
    ACTIVATED = "activated"


class ValidationStage(str, Enum):
    PARSER = "parser"
    SCOPE_CHECK = "scope_check"
    TYPECHECK = "typecheck"
    CONFLICT_CHECK = "conflict_check"
    DRY_RUN = "dry_run"


class IntentResult(BaseModel):
    """Result of intent classification."""
    intent: IntentType
    confidence: float = 1.0
    rule_type: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    is_ambiguous: bool = False
    clarification_question: Optional[str] = None
    options: List[str] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class FormRuleSpec(BaseModel):
    """Deterministic form-based rule specification (LLM-independent fallback)."""
    rule_name: str
    scope: str = "Signal"
    condition: Optional[str] = None
    effect_kind: str = "require"  # 'require', 'forbid', 'assign', 'prefer'
    effect_expr: str
    priority: str = "hard"
    weight: Optional[float] = None
    message: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    rule_id: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ProposedPredicate(BaseModel):
    """Specification of a proposed predicate when missing."""
    name: str
    args: List[Tuple[str, str]] = Field(default_factory=list)  # (name, type)
    returns: str = "Bool"
    description: str = ""
    category: str = "custom"
    lowering_requirements: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ProposalValidationResult(BaseModel):
    """Comprehensive validation outcome of an NLP-proposed rule."""
    proposal_id: str = Field(default_factory=lambda: f"prop_{uuid.uuid4().hex[:8]}")
    user_text: str
    dsl_text: str
    valid: bool = False
    status: ProposalStatus = ProposalStatus.DRAFT
    rule_obj: Optional[Rule] = None
    failed_stage: Optional[ValidationStage] = None
    errors: List[ArlError] = Field(default_factory=list)
    conflicts: List[ArlError] = Field(default_factory=list)
    involved_rules: List[str] = Field(default_factory=list)
    dry_run_impact: Optional[Dict[str, Any]] = None
    high_impact: bool = False
    requires_human_confirmation: bool = False
    predicate_suggestion: Optional[ProposedPredicate] = None
    clarification_question: Optional[str] = None
    clarification_options: List[str] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


# =========================================================================
# LLM Provider Interface (11.2)
# =========================================================================

class LLMProvider(ABC):
    """Abstract interface for LLM integrations allowing mocking and disabling."""

    @abstractmethod
    def generate(self, prompt: str, grammar: Optional[str] = None) -> str:
        """Generate response text from prompt, optionally constrained by grammar."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the LLM provider is active and available."""
        pass


class DisabledLLMProvider(LLMProvider):
    """Disabled LLM provider enforcing pure deterministic offline fallback."""

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, grammar: Optional[str] = None) -> str:
        raise RuntimeError(
            "LLM provider is disabled. Use deterministic form-based rule builder or direct ARL authoring."
        )


class MockLLMProvider(LLMProvider):
    """Configurable mock LLM provider for unit and regression testing."""

    def __init__(self, responses: Optional[Dict[str, str]] = None) -> None:
        self.responses: Dict[str, str] = responses or {}
        self.call_history: List[Dict[str, Any]] = []
        self._enabled: bool = True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def is_available(self) -> bool:
        return self._enabled

    def add_response(self, prompt_substring: str, response: str) -> None:
        self.responses[prompt_substring] = response

    def generate(self, prompt: str, grammar: Optional[str] = None) -> str:
        if not self._enabled:
            raise RuntimeError("Mock LLM provider is disabled.")

        self.call_history.append({"prompt": prompt, "grammar": grammar})

        # Match longest matching substring
        for key, resp in self.responses.items():
            if key in prompt:
                return resp

        # Default fallback response if no exact match
        return "rule R_Default:\n    scope: Signal\n    require slot >= 1\n    priority: hard"


# =========================================================================
# Deterministic Form-Based Rule Builder (11.3)
# =========================================================================

def build_rule_from_form(spec: FormRuleSpec) -> str:
    """
    Deterministic rule generator that produces clean, valid ARL syntax
    without invoking an LLM. Satisfies Part 11.3 deterministic fallback.
    """
    lines = [f"rule {spec.rule_name}:"]
    lines.append(f"    scope: {spec.scope}")

    if spec.condition:
        lines.append(f"    when: {spec.condition}")

    lines.append(f"    {spec.effect_kind} {spec.effect_expr}")
    lines.append(f"    priority: {spec.priority}")

    if spec.weight is not None:
        lines.append(f"    weight: {spec.weight}")

    if spec.message:
        lines.append(f'    message: "{spec.message}"')

    if spec.tags:
        tags_str = ", ".join(spec.tags)
        lines.append(f"    tags: [{tags_str}]")

    if spec.rule_id:
        lines.append(f"    id: {spec.rule_id}")

    return "\n".join(lines)


# =========================================================================
# Intent Classification & Ambiguity Detection (11.1 & 11.4)
# =========================================================================

def classify_intent(user_text: str, provider: Optional[LLMProvider] = None) -> IntentResult:
    """
    Classify user request into intent types.
    Detects ambiguity (11.4) and missing predicate requirements (11.5).
    """
    text_lower = user_text.lower().strip()

    # 1. Ambiguity Detection (Part 11.4)
    # Generic "segregate" without target dimension is inherently ambiguous in DeltaV engineering
    if ("segregate" in text_lower or "separate" in text_lower) and not any(
        k in text_lower for k in ("baseplate", "cioc", "cabinet", "charm", "group")
    ):
        return IntentResult(
            intent=IntentType.AMBIGUOUS,
            is_ambiguous=True,
            confidence=0.9,
            clarification_question="How should the signals be segregated in the hardware?",
            options=[
                "Segregate by Baseplate (same_baseplate)",
                "Segregate by CIOC (same_cioc)",
                "Segregate by Cabinet (same_cabinet)",
            ],
        )

    if ("keep together" in text_lower or "group" in text_lower) and not any(
        k in text_lower for k in ("baseplate", "cioc", "cabinet", "cable")
    ):
        return IntentResult(
            intent=IntentType.AMBIGUOUS,
            is_ambiguous=True,
            confidence=0.85,
            clarification_question="At which hardware level should these signals be co-located?",
            options=[
                "Co-locate on same Baseplate",
                "Co-locate on same CIOC",
                "Co-locate in same Cabinet",
            ],
        )

    # 2. Predicate Request Detection (Part 11.5)
    # Check for engineering concepts not in the default Part 4 predicate registry
    missing_predicates_map = {
        "rack": ProposedPredicate(
            name="same_rack",
            args=[("sig1", "Signal"), ("sig2", "Signal")],
            returns="Bool",
            description="True if both signals reside in the same physical rack",
            category="relational",
            lowering_requirements="Linear equality on cabinet rack identifier",
        ),
        "interlock": ProposedPredicate(
            name="is_interlocked",
            args=[("sig1", "Signal"), ("sig2", "Signal")],
            returns="Bool",
            description="True if signals share safety interlock group",
            category="relational",
            lowering_requirements="Boolean implication constraint",
        ),
        "redundant_pair": ProposedPredicate(
            name="is_redundant_pair",
            args=[("sig", "Signal")],
            returns="Bool",
            description="True if instrument has partner redundant signal",
            category="hardware",
            lowering_requirements="Check redundancy partner slot delta",
        ),
    }

    for keyword, pred in missing_predicates_map.items():
        if keyword in text_lower and not any(k in text_lower for k in ("baseplate", "cioc", "cabinet", "slot")):
            return IntentResult(
                intent=IntentType.PREDICATE_REQUEST,
                confidence=0.95,
                parameters={"proposed_predicate": pred.model_dump()},
            )

    # 3. Known Rule Patterns
    if any(k in text_lower for k in ("safety", "segregat", "baseplate", "cioc", "cabinet", "slot", "forbid", "require", "limit")):
        return IntentResult(
            intent=IntentType.CREATE_RULE,
            confidence=0.9,
            parameters={"text": user_text},
        )

    return IntentResult(
        intent=IntentType.UNKNOWN,
        confidence=0.3,
    )


# =========================================================================
# DSL Draft Generation (11.1)
# =========================================================================

def generate_dsl_draft(
    user_text: str,
    intent: IntentResult,
    provider: Optional[LLMProvider] = None,
) -> str:
    """
    Generate DSL rule draft from natural language.
    Uses LLM provider if available; otherwise uses deterministic pattern templates.
    """
    if provider and provider.is_available():
        prompt = (
            f"Convert the following industrial DCS allocation requirement into an ARL v2 rule:\n"
            f"Requirement: \"{user_text}\"\n"
            f"Syntax rules: rule <Name>: scope: <Entity> [when: <Cond>] require|forbid <Expr> priority: hard|soft [id: <ID>]"
        )
        return provider.generate(prompt)

    # Deterministic pattern-matching templates (Offline Fallback)
    text_lower = user_text.lower()
    if "safety" in text_lower and "baseplate" in text_lower:
        return build_rule_from_form(
            FormRuleSpec(
                rule_name="R_Safety_Baseplate",
                scope="Signal",
                condition="self.safety == true",
                effect_kind="require",
                effect_expr="same_baseplate(self)",
                priority="hard",
                rule_id="R_SAFE_01",
                message="Safety signals must be co-located on the same baseplate",
                tags=["safety", "segregation"],
            )
        )
    elif "safety" in text_lower and "cioc" in text_lower:
        return build_rule_from_form(
            FormRuleSpec(
                rule_name="R_Safety_CIOC",
                scope="Signal",
                condition="self.safety == true",
                effect_kind="require",
                effect_expr="same_cioc(self)",
                priority="hard",
                rule_id="R_SAFE_02",
                message="Safety signals must connect to the same CIOC",
                tags=["safety"],
            )
        )
    elif "slot" in text_lower and ("limit" in text_lower or "max" in text_lower or "<=" in text_lower):
        return build_rule_from_form(
            FormRuleSpec(
                rule_name="R_Slot_Limit",
                scope="Signal",
                effect_kind="require",
                effect_expr="self.slot <= 6",
                priority="hard",
                rule_id="R_SLOT_LIM",
            )
        )
    elif "forbid" in text_lower or "not allow" in text_lower:
        return build_rule_from_form(
            FormRuleSpec(
                rule_name="R_Forbid_Slot",
                scope="Signal",
                effect_kind="forbid",
                effect_expr="self.slot == 12",
                priority="hard",
                rule_id="R_FORBID_12",
            )
        )

    # Default conservative rule
    return build_rule_from_form(
        FormRuleSpec(
            rule_name="R_Default_Auto",
            scope="Signal",
            effect_kind="require",
            effect_expr="self.slot >= 1",
            priority="hard",
            rule_id="R_AUTO_01",
        )
    )


# =========================================================================
# Guardrails & Validation Engine (11.1, 11.2, 11.4)
# =========================================================================

class NLPValidator:
    """
    Executes the full compiler validation path on proposed rules:
      Parser -> Typecheck -> Scope check -> Static conflict check -> Dry-run impact
    """

    def __init__(
        self,
        schema: Optional[Schema] = None,
        predicate_registry: Optional[PredicateRegistry] = None,
        impact_threshold: float = 0.10,  # 10% movement threshold for human confirmation
    ) -> None:
        self.schema = schema or get_base_schema()
        self.predicates = predicate_registry or get_builtin_predicate_registry()
        self.impact_threshold = impact_threshold

    def validate_proposal(
        self,
        dsl_text: str,
        user_text: str,
        current_ruleset: Optional[Ruleset] = None,
        project_sample: Optional[Project] = None,
    ) -> ProposalValidationResult:
        """
        Validate a proposed rule through all 5 compiler gate stages.
        """
        col = ErrorCollector()
        result = ProposalValidationResult(
            user_text=user_text,
            dsl_text=dsl_text,
            valid=False,
        )

        # Wrap rule in synthetic ruleset if authored standalone
        full_dsl = dsl_text
        if "ruleset " not in dsl_text:
            full_dsl = f"ruleset ProposedRuleset {{\n{dsl_text}\n}}"

        # Stage 1: Parser & Lexer (Tokens outside grammar, block syntax)
        try:
            parsed_rs = parse_ruleset(full_dsl, collector=col)
        except Exception as ex:
            col.add_error(
                code="E1001",
                message=f"Parser rejected proposed rule syntax: {ex}",
                phase=Phase.PARSE,
                severity=Severity.FATAL,
            )
            result.failed_stage = ValidationStage.PARSER
            result.errors = col.errors
            result.status = ProposalStatus.REJECTED
            return result

        if col.has_fatal or not parsed_rs.rules:
            result.failed_stage = ValidationStage.PARSER
            result.errors = col.errors
            result.status = ProposalStatus.REJECTED
            return result

        proposed_rule = parsed_rs.rules[0]
        result.rule_obj = proposed_rule

        # Stage 2: Scope Check (Known entity types)
        valid_scopes = {"Signal", "Charm", "CIOC", "Baseplate", "Cabinet", "Group", "Cable", "Pair", "Project"}
        if proposed_rule.scope not in valid_scopes:
            col.add_error(
                code="E2001",
                message=f"Invalid scope '{proposed_rule.scope}'. Scope must be one of {sorted(list(valid_scopes))}",
                phase=Phase.TYPECHECK,
                severity=Severity.FATAL,
            )
            result.failed_stage = ValidationStage.SCOPE_CHECK
            result.errors = col.errors
            result.status = ProposalStatus.REJECTED
            return result

        # Check duplicate rule IDs against current ruleset
        if current_ruleset:
            existing_names = {r.name for r in current_ruleset.rules}
            existing_ids = {r.id for r in current_ruleset.rules if r.id}

            if proposed_rule.name in existing_names:
                col.add_error(
                    code="E2017",
                    message=f"Duplicate rule name: '{proposed_rule.name}' is already defined in ruleset",
                    phase=Phase.RESOLVE,
                    severity=Severity.FATAL,
                )
            if proposed_rule.id and proposed_rule.id in existing_ids:
                col.add_error(
                    code="E1004",
                    message=f"Duplicate rule ID: '{proposed_rule.id}' is already used by another rule",
                    phase=Phase.RESOLVE,
                    severity=Severity.FATAL,
                )

        if col.has_fatal:
            result.failed_stage = ValidationStage.SCOPE_CHECK
            result.errors = col.errors
            result.status = ProposalStatus.REJECTED
            return result

        # Stage 3: Typecheck & Predicates Check
        tc = TypeChecker(schema=self.schema, collector=col)
        tc.check_ruleset(parsed_rs)

        resolver = Resolver(schema=self.schema, collector=col)
        resolver.resolve(parsed_rs)

        if col.has_fatal:
            result.failed_stage = ValidationStage.TYPECHECK
            result.errors = col.errors
            result.status = ProposalStatus.REJECTED
            return result

        # Stage 4: Static Conflict Check (E3001, E3002)
        # Combine current ruleset + proposed rule
        combined_rules = list(current_ruleset.rules) if current_ruleset else []
        combined_rules.append(proposed_rule)
        combined_rs = Ruleset(name="ValidationCombined", rules=combined_rules)

        checker = ConflictChecker(collector=col)
        dummy_project = project_sample or Project(name="DummyProj", signals=[], cabinets=[])
        conflict_res = checker.check(combined_rs, dummy_project)

        if not conflict_res.can_solve or conflict_res.has_conflicts:
            result.failed_stage = ValidationStage.CONFLICT_CHECK
            result.conflicts = conflict_res.conflicts
            result.errors.extend(conflict_res.conflicts)
            # Find involved rules
            involved: Set[str] = set()
            for c_err in conflict_res.conflicts:
                if c_err.rule_id:
                    involved.add(c_err.rule_id)
            for core in conflict_res.unsat_cores:
                involved.update(core)
            result.involved_rules = sorted(list(involved))
            result.status = ProposalStatus.REJECTED
            return result

        # Stage 5: Dry-Run on Sample Data & Impact Preview
        if project_sample and project_sample.get("signals"):
            solver = CPSATSolver(schema=self.schema, time_limit_seconds=5.0)
            # Baseline run
            baseline_alloc = solver.solve(project_sample, ruleset=current_ruleset)
            # Proposed run
            test_alloc = solver.solve(project_sample, ruleset=combined_rs)

            total_sigs = len(project_sample.get("signals", []))
            moved = 0
            for tag, pl_test in test_alloc.allocations.items():
                if tag in baseline_alloc.allocations:
                    pl_base = baseline_alloc.allocations[tag]
                    if (pl_test.cabinet_id != pl_base.cabinet_id or
                        pl_test.cioc_id != pl_base.cioc_id or
                        pl_test.baseplate_id != pl_base.baseplate_id or
                        pl_test.slot != pl_base.slot):
                        moved += 1

            unplaced_delta = len(test_alloc.unplaced_signals) - len(baseline_alloc.unplaced_signals)
            impact_ratio = (moved / total_sigs) if total_sigs > 0 else 0.0

            result.dry_run_impact = {
                "total_signals": total_sigs,
                "moved_signals": moved,
                "unplaced_delta": unplaced_delta,
                "movement_ratio": round(impact_ratio * 100.0, 1),
                "baseline_success": baseline_alloc.success,
                "test_success": test_alloc.success,
            }

            if impact_ratio > self.impact_threshold or unplaced_delta > 0:
                result.high_impact = True
                result.requires_human_confirmation = True

        result.valid = True
        result.status = ProposalStatus.PENDING_APPROVAL if result.requires_human_confirmation else ProposalStatus.VALIDATED
        return result


# =========================================================================
# Proposal Audit Logger (11.2 & Part 13)
# =========================================================================

class NLPProposalLogger:
    """
    Append-only logger for natural language proposals, validation results,
    rejections, and human activation decisions.
    """

    def __init__(self, log_path: Optional[Union[str, Path]] = None) -> None:
        self.log_path = Path(log_path) if log_path else Path("nlp_proposals.jsonl")

    def log_proposal(
        self,
        proposal: ProposalValidationResult,
        disposition: str,
        human_approved: Optional[bool] = None,
    ) -> None:
        """Write structured proposal event to JSONL."""
        event = {
            "proposal_id": proposal.proposal_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_text": proposal.user_text,
            "dsl_text": proposal.dsl_text,
            "status": proposal.status.value,
            "valid": proposal.valid,
            "failed_stage": proposal.failed_stage.value if proposal.failed_stage else None,
            "errors": [str(e) for e in proposal.errors],
            "involved_rules": proposal.involved_rules,
            "high_impact": proposal.high_impact,
            "requires_human_confirmation": proposal.requires_human_confirmation,
            "human_approved": human_approved,
            "disposition": disposition,
            "dry_run_impact": proposal.dry_run_impact,
        }

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")


# =========================================================================
# Rule Activation (11.1)
# =========================================================================

def activate_rule(
    proposal: ProposalValidationResult,
    target_ruleset: Ruleset,
    human_approved: bool = True,
    logger: Optional[NLPProposalLogger] = None,
) -> Ruleset:
    """
    Activate a validated rule into the target ruleset.
    Enforces human approval when high impact is flagged (Part 11.4).
    """
    if not proposal.valid or not proposal.rule_obj:
        raise ValueError("Cannot activate invalid or unvalidated rule proposal.")

    if proposal.requires_human_confirmation and not human_approved:
        if logger:
            logger.log_proposal(proposal, disposition="Activation rejected: human approval required", human_approved=False)
        proposal.status = ProposalStatus.REJECTED
        raise PermissionError("Rule activation blocked: high-impact proposal requires explicit human approval.")

    # Add rule to ruleset
    target_ruleset.rules.append(proposal.rule_obj)
    proposal.status = ProposalStatus.ACTIVATED

    if logger:
        logger.log_proposal(proposal, disposition="Rule activated successfully", human_approved=human_approved)

    return target_ruleset


# =========================================================================
# Predicate Suggestion & Registration API (11.5)
# =========================================================================

def register_proposed_predicate(
    registry: PredicateRegistry,
    proposal: ProposedPredicate,
    lower_fn: Optional[Callable[..., Any]] = None,
) -> PredicateDefinition:
    """
    Explicit, validated, one-time plugin registration of a suggested predicate.
    Does not execute dynamic unverified Python code (Part 11.5).
    """
    return registry.register(
        name=proposal.name,
        args=proposal.args,
        returns=proposal.returns,
        description=proposal.description,
        category=proposal.category,
        lower=lower_fn,
    )


# =========================================================================
# Natural-Language Bridge Orchestrator (Part 11)
# =========================================================================

class NLPBridge:
    """
    End-to-end NLP Bridge orchestrating:
      User text -> Intent classifier -> DSL draft -> Validator -> Impact -> Activation
    """

    def __init__(
        self,
        schema: Optional[Schema] = None,
        predicate_registry: Optional[PredicateRegistry] = None,
        llm_provider: Optional[LLMProvider] = None,
        audit_logger: Optional[NLPProposalLogger] = None,
        impact_threshold: float = 0.10,
    ) -> None:
        self.schema = schema or get_base_schema()
        self.predicates = predicate_registry or get_builtin_predicate_registry()
        self.provider = llm_provider or DisabledLLMProvider()
        self.logger = audit_logger or NLPProposalLogger()
        self.validator = NLPValidator(
            schema=self.schema,
            predicate_registry=self.predicates,
            impact_threshold=impact_threshold,
        )

    def process_request(
        self,
        user_text: str,
        current_ruleset: Optional[Ruleset] = None,
        project_sample: Optional[Project] = None,
    ) -> ProposalValidationResult:
        """
        Execute full natural-language bridge pipeline.
        """
        # 1. Intent Classification
        intent_res = classify_intent(user_text, provider=self.provider)

        # Ambiguous handling
        if intent_res.is_ambiguous:
            result = ProposalValidationResult(
                user_text=user_text,
                dsl_text="",
                valid=False,
                status=ProposalStatus.DRAFT,
                clarification_question=intent_res.clarification_question,
                clarification_options=intent_res.options,
            )
            self.logger.log_proposal(result, disposition="Clarification requested from user")
            return result

        # Predicate suggestion handling
        if intent_res.intent == IntentType.PREDICATE_REQUEST:
            pred_data = intent_res.parameters.get("proposed_predicate", {})
            pred_prop = ProposedPredicate(**pred_data)
            result = ProposalValidationResult(
                user_text=user_text,
                dsl_text="",
                valid=False,
                status=ProposalStatus.DRAFT,
                predicate_suggestion=pred_prop,
            )
            self.logger.log_proposal(result, disposition=f"Suggested missing predicate: {pred_prop.name}")
            return result

        # 2. Draft Generation
        dsl_draft = generate_dsl_draft(user_text, intent_res, provider=self.provider)

        # 3. Validation Pipeline
        val_result = self.validator.validate_proposal(
            dsl_text=dsl_draft,
            user_text=user_text,
            current_ruleset=current_ruleset,
            project_sample=project_sample,
        )

        # Log proposal
        disposition = "Passed validation" if val_result.valid else f"Failed at {val_result.failed_stage}"
        self.logger.log_proposal(val_result, disposition=disposition)

        return val_result
