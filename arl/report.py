"""
ARL Run Report.

Implements the Part 6.3 structured run report schema:
  - ruleset name and version
  - status: 'success', 'compiled_with_warnings', 'rejected', 'failed'
  - summary: rules_total, rules_active, rules_quarantined, errors, warnings, tqs
  - quarantine details
  - warnings and TQs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import Ruleset
from arl.errors import ArlError, ErrorCollector, Severity
from arl.tq import TQ, TQManager


class QuarantinedRuleInfo(BaseModel):
    """Details of a rule quarantined during syntax or semantic phases."""
    rule_name: str
    rule_id: Optional[str] = None
    code: str
    message: str
    phase: str

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class ReportSummary(BaseModel):
    """Summary KPI metrics for the compilation / execution run."""
    rules_total: int = 0
    rules_active: int = 0
    rules_quarantined: int = 0
    errors: int = 0
    warnings: int = 0
    tqs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class RunReport(BaseModel):
    """Structured Run Report conforming to Part 6.3."""
    ruleset: str
    version: str = "2.1.0"
    status: str = "success"  # 'success', 'compiled_with_warnings', 'rejected', 'failed'
    summary: ReportSummary = Field(default_factory=ReportSummary)
    quarantine: List[QuarantinedRuleInfo] = Field(default_factory=list)
    warnings: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    tqs: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save_json(self, path: Union[str, Path]) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        return p


def build_run_report(
    ruleset: Optional[Ruleset] = None,
    collector: Optional[ErrorCollector] = None,
    tq_manager: Optional[TQManager] = None,
    ruleset_name: str = "DefaultRuleset",
    version: str = "2.1.0",
    has_conflict: bool = False,
    quarantined_rules: Optional[List[QuarantinedRuleInfo]] = None,
) -> RunReport:
    """Construct structured run report from compilation state."""
    col = collector or ErrorCollector()
    tqs = tq_manager or TQManager()

    total_rules = len(ruleset.rules) if ruleset else 0
    quarantine_list = list(quarantined_rules or [])

    # Deduce quarantined rules from collector syntax / semantic errors if not passed explicitly
    if not quarantine_list:
        for err in col.errors:
            if err.rule_id and err.code.startswith(("E1", "E2")):
                quarantine_list.append(
                    QuarantinedRuleInfo(
                        rule_name=err.rule_id,
                        rule_id=err.rule_id,
                        code=err.code,
                        message=err.message,
                        phase=err.phase.value if hasattr(err.phase, "value") else str(err.phase),
                    )
                )

    quarantined_count = len(quarantine_list)
    active_rules = max(0, total_rules - quarantined_count)

    err_list = [e.to_dict() for e in col.errors if e.severity in (Severity.ERROR, Severity.FATAL)]
    warn_list = [e.to_dict() for e in col.errors if e.severity == Severity.WARNING]
    tq_list = tqs.to_list()

    # Determine status
    if has_conflict or any(e.code.startswith("E3") for e in col.errors):
        status = "rejected"
    elif len(err_list) > 0:
        status = "failed"
    elif len(warn_list) > 0 or len(tq_list) > 0 or quarantined_count > 0:
        status = "compiled_with_warnings"
    else:
        status = "success"

    summary = ReportSummary(
        rules_total=total_rules,
        rules_active=active_rules,
        rules_quarantined=quarantined_count,
        errors=len(err_list),
        warnings=len(warn_list),
        tqs=len(tq_list),
    )

    name = ruleset.name if ruleset else ruleset_name

    return RunReport(
        ruleset=name,
        version=version,
        status=status,
        summary=summary,
        quarantine=quarantine_list,
        warnings=warn_list,
        errors=err_list,
        tqs=tq_list,
    )
