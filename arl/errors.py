"""
ARL Error Control Framework.

Defines structured error types, codes, severity levels, phases,
and collector for the ARL compiler pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Severity(str, Enum):
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Phase(str, Enum):
    LEX = "lex"
    PARSE = "parse"
    TYPECHECK = "typecheck"
    RESOLVE = "resolve"
    NORMALIZE = "normalize"
    DAG_BUILD = "dag_build"
    OVERRIDE_APPLY = "override_apply"
    LOWER = "lower"
    CONFLICT_CHECK = "conflict_check"
    SOLVE = "solve"
    VALIDATE = "validate"
    REPORT = "report"
    SCHEMA = "schema"
    DATA = "data"
    REVISION = "revision"


class SourceLocation(BaseModel):
    file: Optional[str] = None
    line: int = 1
    column: int = 1
    end_line: Optional[int] = None
    end_column: Optional[int] = None

    def __str__(self) -> str:
        loc = f"{self.line}:{self.column}"
        if self.file:
            loc = f"{self.file}:{loc}"
        return loc


class ArlError(BaseModel):
    code: str
    severity: Severity = Severity.ERROR
    phase: Phase
    rule_id: Optional[str] = None
    location: Optional[SourceLocation] = None
    message: str
    context: Dict[str, Any] = Field(default_factory=dict)
    suggestion: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def __str__(self) -> str:
        loc = f" at {self.location}" if self.location else ""
        rule = f" in rule '{self.rule_id}'" if self.rule_id else ""
        sug = f" Suggestion: {self.suggestion}" if self.suggestion else ""
        return f"[{self.code}] ({self.severity.value.upper()}) {self.message}{rule}{loc}.{sug}"


# Standard Error Code Catalog (E1xxx to E6xxx)
ERROR_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    # Syntax (E1xxx)
    "E1001": {"message": "Unexpected token", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E1002": {"message": "Unterminated string", "phase": Phase.LEX, "severity": Severity.FATAL},
    "E1003": {"message": "Missing colon after rule name", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E1004": {"message": "Duplicate rule ID in ruleset", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E1005": {"message": "Invalid block structure", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E1006": {"message": "Invalid numeric literal", "phase": Phase.LEX, "severity": Severity.FATAL},
    "E1007": {"message": "Invalid escape sequence", "phase": Phase.LEX, "severity": Severity.FATAL},

    # Semantic (E2xxx)
    "E2001": {"message": "Unknown entity type", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2002": {"message": "Unknown field on entity", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2003": {"message": "Unknown predicate", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2004": {"message": "Predicate arity mismatch", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2005": {"message": "Type mismatch", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2006": {"message": "Missing Option unwrap", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2007": {"message": "Field access on non-entity", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2008": {"message": "Aggregate over non-collection", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2009": {"message": "Cyclic rule reference", "phase": Phase.DAG_BUILD, "severity": Severity.FATAL},
    "E2010": {"message": "Unknown rule reference", "phase": Phase.RESOLVE, "severity": Severity.FATAL},
    "E2011": {"message": "Duplicate field writer", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2012": {"message": "Override scope mismatch", "phase": Phase.OVERRIDE_APPLY, "severity": Severity.FATAL},
    "E2013": {"message": "Override targets undefined rule", "phase": Phase.OVERRIDE_APPLY, "severity": Severity.FATAL},
    "E2014": {"message": "Objective references unknown weight", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2015": {"message": "Invalid regex", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},
    "E2016": {"message": "Reserved keyword used as identifier", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E2017": {"message": "Duplicate rule name in same ruleset", "phase": Phase.PARSE, "severity": Severity.FATAL},
    "E2018": {"message": "Extends target not found", "phase": Phase.RESOLVE, "severity": Severity.FATAL},

    # Conflict (E3xxx)
    "E3001": {"message": "Unsat core: rules conflict on data", "phase": Phase.CONFLICT_CHECK, "severity": Severity.FATAL},
    "E3002": {"message": "Hard rule contradicts assignment", "phase": Phase.CONFLICT_CHECK, "severity": Severity.FATAL},
    "E3003": {"message": "Override removes only writer of required field", "phase": Phase.OVERRIDE_APPLY, "severity": Severity.FATAL},
    "E3004": {"message": "Include cycle", "phase": Phase.RESOLVE, "severity": Severity.FATAL},
    "E3005": {"message": "Hard rule references soft-only rule", "phase": Phase.TYPECHECK, "severity": Severity.FATAL},

    # Data (E4xxx)
    "E4001": {"message": "Missing required field", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4002": {"message": "Cable size unknown or inconsistent", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4003": {"message": "Orphan pair", "phase": Phase.DATA, "severity": Severity.WARNING},
    "E4004": {"message": "Duplicate pair index", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4005": {"message": "A/B mismatch within group", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4006": {"message": "IO type not in CHARM map", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4007": {"message": "Capacity overflow", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4008": {"message": "Naming collision", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4009": {"message": "Duplicate tag", "phase": Phase.DATA, "severity": Severity.ERROR},
    "E4010": {"message": "Pair index out of range for cable size", "phase": Phase.DATA, "severity": Severity.ERROR},

    # Solver (E5xxx)
    "E5001": {"message": "Solver timeout", "phase": Phase.SOLVE, "severity": Severity.WARNING},
    "E5002": {"message": "Infeasible after relaxation", "phase": Phase.SOLVE, "severity": Severity.FATAL},
    "E5003": {"message": "Objective unbounded", "phase": Phase.SOLVE, "severity": Severity.ERROR},
    "E5004": {"message": "Memory limit exceeded", "phase": Phase.SOLVE, "severity": Severity.FATAL},
    "E5005": {"message": "Invalid IR node", "phase": Phase.SOLVE, "severity": Severity.ERROR},

    # Revision (E6xxx)
    "E6001": {"message": "Pinned slot no longer valid", "phase": Phase.REVISION, "severity": Severity.WARNING},
    "E6002": {"message": "New rule invalidates pins", "phase": Phase.REVISION, "severity": Severity.WARNING},
    "E6003": {"message": "Move budget exceeded", "phase": Phase.REVISION, "severity": Severity.ERROR},
    "E6004": {"message": "Rev A allocation missing", "phase": Phase.REVISION, "severity": Severity.ERROR},
}


def make_error(
    code: str,
    message: Optional[str] = None,
    rule_id: Optional[str] = None,
    location: Optional[SourceLocation] = None,
    context: Optional[Dict[str, Any]] = None,
    suggestion: Optional[str] = None,
    severity: Optional[Severity] = None,
    phase: Optional[Phase] = None,
) -> ArlError:
    """Factory for standard ArlError instances."""
    definition = ERROR_DEFINITIONS.get(code, {})
    default_msg = definition.get("message", f"Unknown error code {code}")
    default_phase = definition.get("phase", Phase.TYPECHECK)
    default_sev = definition.get("severity", Severity.ERROR)

    return ArlError(
        code=code,
        severity=severity or default_sev,
        phase=phase or default_phase,
        rule_id=rule_id,
        location=location,
        message=message or default_msg,
        context=context or {},
        suggestion=suggestion,
    )


class ErrorCollector:
    """Accumulates errors across compiler phases without throwing."""

    def __init__(self) -> None:
        self.errors: List[ArlError] = []

    def add(self, error: ArlError) -> None:
        self.errors.append(error)

    def add_error(
        self,
        code: str,
        message: Optional[str] = None,
        rule_id: Optional[str] = None,
        location: Optional[SourceLocation] = None,
        context: Optional[Dict[str, Any]] = None,
        suggestion: Optional[str] = None,
        severity: Optional[Severity] = None,
        phase: Optional[Phase] = None,
    ) -> ArlError:
        err = make_error(
            code=code,
            message=message,
            rule_id=rule_id,
            location=location,
            context=context,
            suggestion=suggestion,
            severity=severity,
            phase=phase,
        )
        self.add(err)
        return err

    @property
    def has_fatal(self) -> bool:
        return any(e.severity == Severity.FATAL for e in self.errors)

    @property
    def has_errors(self) -> bool:
        return any(e.severity in (Severity.ERROR, Severity.FATAL) for e in self.errors)

    def has_code(self, code: str) -> bool:
        return any(e.code == code for e in self.errors)

    def for_rule(self, rule_id: str) -> List[ArlError]:
        return [e for e in self.errors if e.rule_id == rule_id]

    def clear(self) -> None:
        self.errors.clear()
