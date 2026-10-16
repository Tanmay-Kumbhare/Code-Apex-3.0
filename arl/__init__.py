"""
Allocation Rule Language (ARL) v2.

A declarative, typed, extensible domain-specific language for industrial
IO signal allocation in DeltaV DCS projects.
"""

from arl.ast import (
    AggregateExpr,
    AsExpr,
    AssignEffect,
    AstNode,
    BinaryOp,
    CallExpr,
    Effect,
    Expr,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    Include,
    IndexExpr,
    IsNoneExpr,
    IsSomeExpr,
    ListLiteralExpr,
    LiteralExpr,
    Objective,
    Override,
    PreferEffect,
    QuantifiedExpr,
    RequireEffect,
    Rule,
    RuleMeta,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
)
from arl.entities import (
    Baseplate,
    Cabinet,
    Cable,
    Charm,
    CIOC,
    Entity,
    Group,
    Pair,
    Project,
    Signal,
)
from arl.errors import (
    ArlError,
    ErrorCollector,
    Phase,
    Severity,
    SourceLocation,
    make_error,
)
from arl.dag import RuleDAG, detect_include_cycles
from arl.ir import (
    AllDifferent,
    And,
    Contiguous,
    Count,
    Eq,
    Exists,
    ForAll,
    Gt,
    Gte,
    IRNode,
    Implies,
    Lt,
    Lte,
    Neq,
    Not,
    Objective as IRObjective,
    Or,
    Soft,
    Sum,
    Var,
)
from arl.lexer import Lexer, Token, TokenType
from arl.lower import Lowerer
from arl.normalize import normalize_expr, normalize_ruleset
from arl.override import apply_overrides
from arl.parser import Parser, parse_ruleset, parse_expr
from arl.predicates import (
    PredicateDefinition,
    PredicateRegistry,
    get_builtin_predicate_registry,
)
from arl.resolve import DependencyDAG, Resolver
from arl.schema import (
    EntityDefinition,
    EnumDefinition,
    FieldDefinition,
    Schema,
    get_base_schema,
)
from arl.typecheck import TypeChecker, TypeEnvironment
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
    TupleType,
    can_coerce,
    parse_type_string,
)
# Stage 5: TQ, Data Quality, Solver, Validator
from arl.tq import TQ, TQManager
from arl.data_quality import check_data_quality
from arl.solver_greedy import (
    DEFAULT_HARDWARE_MAP,
    AllocationResult,
    GreedySolver,
    Placement,
)
from arl.validate import ValidationResult, ValidationViolation, Validator
# Stage 6: Excel Ingestion, Output, and Pipeline
from arl.excel_ingest import (
    ColumnMapping,
    IngestionReport,
    read_excel_mdb,
    resolve_column_mapping,
)
from arl.excel_output import ExcelWriter, create_default_template
from arl.pipeline import PipelineResult, run_allocation_pipeline
# Stage 7: Conflicts, Run Report, and Audit Logger
from arl.conflict import ConflictChecker, ConflictCheckResult
from arl.report import (
    QuarantinedRuleInfo,
    ReportSummary,
    RunReport,
    build_run_report,
)
from arl.audit import AuditEvent, EventLogger
# Stage 8: CP-SAT Solver and Unsat Cores
from arl.solver_cpsat import CPSATSolver, PhysicalSlot, UnsatCore
# Stage 9: Revision Comparison and Move Minimization
from arl.revision import (
    MDBDiff,
    MoveRecord,
    PinKind,
    PinManager,
    RelaxationPriority,
    RevisionResult,
    RuleChangeType,
    RuleDiff,
    RulesetDiff,
    SignalChangeType,
    SignalDiff,
    SignalPin,
    diff_allocations,
    diff_mdb,
    diff_rulesets,
    read_allocation_from_excel,
    run_revision_pipeline,
)
# Stage 10: Natural-Language Bridge
from arl.nlp import (
    DisabledLLMProvider,
    FormRuleSpec,
    IntentResult,
    IntentType,
    LLMProvider,
    MockLLMProvider,
    NLPBridge,
    NLPProposalLogger,
    NLPValidator,
    ProposalStatus,
    ProposalValidationResult,
    ProposedPredicate,
    ValidationStage,
    activate_rule,
    build_rule_from_form,
    classify_intent,
    generate_dsl_draft,
    register_proposed_predicate,
)

__version__ = "2.1.0"

__all__ = [
    "__version__",
    "Schema",
    "FieldDefinition",
    "EntityDefinition",
    "EnumDefinition",
    "get_base_schema",
    "ArlError",
    "ErrorCollector",
    "Phase",
    "Severity",
    "SourceLocation",
    "make_error",
    "ArlType",
    "PrimitiveType",
    "DomainType",
    "EnumType",
    "EntityType",
    "ReferenceType",
    "OptionType",
    "SetType",
    "ListType",
    "TupleType",
    "can_coerce",
    "parse_type_string",
    "Entity",
    "Signal",
    "Cable",
    "Pair",
    "Group",
    "Baseplate",
    "Charm",
    "CIOC",
    "Cabinet",
    "Project",
    # AST
    "AstNode",
    "Ruleset",
    "Rule",
    "RuleMeta",
    "RuleRef",
    "Include",
    "Objective",
    "Override",
    "Effect",
    "RequireEffect",
    "ForbidEffect",
    "AssignEffect",
    "PreferEffect",
    "Expr",
    "BinaryOp",
    "UnaryOp",
    "LiteralExpr",
    "ListLiteralExpr",
    "IdentifierExpr",
    "SelfExpr",
    "FieldAccessExpr",
    "CallExpr",
    "IndexExpr",
    "QuantifiedExpr",
    "AggregateExpr",
    "AsExpr",
    "IsSomeExpr",
    "IsNoneExpr",
    # Lexer & Parser
    "Lexer",
    "Token",
    "TokenType",
    "Parser",
    "parse_ruleset",
    "parse_expr",
    # Typecheck, Resolve, Normalize
    "TypeChecker",
    "TypeEnvironment",
    "Resolver",
    "DependencyDAG",
    "normalize_ruleset",
    "normalize_expr",
    # Stage 4: IR, Predicates, DAG, Override, Lower
    "IRNode",
    "Var",
    "Eq",
    "Neq",
    "Lt",
    "Lte",
    "Gt",
    "Gte",
    "And",
    "Or",
    "Not",
    "Implies",
    "AllDifferent",
    "Count",
    "Sum",
    "Contiguous",
    "ForAll",
    "Exists",
    "Soft",
    "IRObjective",
    "PredicateDefinition",
    "PredicateRegistry",
    "get_builtin_predicate_registry",
    "RuleDAG",
    "detect_include_cycles",
    "apply_overrides",
    "Lowerer",
    # Stage 5: Solver & Validation
    "TQ",
    "TQManager",
    "check_data_quality",
    "Placement",
    "AllocationResult",
    "GreedySolver",
    "DEFAULT_HARDWARE_MAP",
    "Validator",
    "ValidationResult",
    "ValidationViolation",
    # Stage 6: Excel Ingest, Output & Pipeline
    "read_excel_mdb",
    "resolve_column_mapping",
    "ColumnMapping",
    "IngestionReport",
    "ExcelWriter",
    "create_default_template",
    "run_allocation_pipeline",
    "PipelineResult",
    # Stage 7: Conflicts, Report & Audit
    "ConflictChecker",
    "ConflictCheckResult",
    "RunReport",
    "ReportSummary",
    "QuarantinedRuleInfo",
    "build_run_report",
    "AuditEvent",
    "EventLogger",
    # Stage 8: CP-SAT Solver
    "CPSATSolver",
    "UnsatCore",
    "PhysicalSlot",
    # Stage 9: Revision
    "SignalChangeType",
    "SignalDiff",
    "MDBDiff",
    "RuleChangeType",
    "RuleDiff",
    "RulesetDiff",
    "PinKind",
    "RelaxationPriority",
    "SignalPin",
    "MoveRecord",
    "RevisionResult",
    "diff_mdb",
    "diff_rulesets",
    "diff_allocations",
    "PinManager",
    "read_allocation_from_excel",
    "run_revision_pipeline",
    # Stage 10: NLP Bridge
    "IntentType",
    "IntentResult",
    "FormRuleSpec",
    "ProposedPredicate",
    "ProposalStatus",
    "ValidationStage",
    "ProposalValidationResult",
    "LLMProvider",
    "DisabledLLMProvider",
    "MockLLMProvider",
    "NLPValidator",
    "NLPProposalLogger",
    "NLPBridge",
    "build_rule_from_form",
    "classify_intent",
    "generate_dsl_draft",
    "activate_rule",
    "register_proposed_predicate",
    # Stage 11: CLI, UI & Lifecycle
    "LifecycleManager",
    "RulesetVersionRecord",
    "RulesetStatus",
    "SemVer",
    "cli_main",
    "EXIT_SUCCESS",
    "EXIT_COMPILE_ERROR",
    "EXIT_DATA_ERROR",
    "EXIT_SOLVER_FALLBACK",
    "EXIT_INTERNAL_ERROR",
    "create_app",
    # Pipeline
    "run_allocation_pipeline",
    "PipelineConfig",
    "PipelineResult",
    # Audit
    "AuditEvent",
    "AuditLogger",
    "EventLogger",
]

from arl.lifecycle import LifecycleManager, RulesetVersionRecord, RulesetStatus, SemVer
from arl.cli import (
    main as cli_main,
    EXIT_SUCCESS,
    EXIT_COMPILE_ERROR,
    EXIT_DATA_ERROR,
    EXIT_SOLVER_FALLBACK,
    EXIT_INTERNAL_ERROR,
)
from arl.ui import create_app
from arl.pipeline import run_allocation_pipeline, PipelineConfig, PipelineResult
from arl.audit import AuditEvent, AuditLogger, EventLogger



