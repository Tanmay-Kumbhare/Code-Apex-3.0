"""
ARL Technical Query (TQ) Framework.

Implements structured TQ generation according to Part 6.4 schema.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class TQ(BaseModel):
    """
    Technical Query schema matching Part 6.4:
    {
      "tq_id": "TQ-0142",
      "severity": "high",
      "code": "E3001",
      "title": "...",
      "rules": ["R-001", "R-002"],
      "affected": {"signals": [], "cables": [], "pairs": []},
      "evidence": "...",
      "impact": {"unplaced_signals": 0, "cabinets_affected": 0, "spare_delta": 0.0},
      "suggestions": [],
      "auto_fix_available": false
    }
    """
    tq_id: str
    severity: str = "high"  # "critical", "high", "medium", "low"
    code: str
    title: str
    rules: List[str] = Field(default_factory=list)
    affected: Dict[str, List[str]] = Field(default_factory=lambda: {"signals": [], "cables": [], "pairs": []})
    evidence: str = ""
    impact: Dict[str, Any] = Field(default_factory=lambda: {"unplaced_signals": 0, "cabinets_affected": 0, "spare_delta": 0.0})
    suggestions: List[str] = Field(default_factory=list)
    auto_fix_available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    @property
    def affected_signals(self) -> List[str]:
        return self.affected.get("signals", [])

    @property
    def suggested_action(self) -> str:
        return self.suggestions[0] if self.suggestions else ""



class TQManager:
    """Manages generation, tracking, and export of Technical Queries."""

    def __init__(self, prefix: str = "TQ-") -> None:
        self.prefix = prefix
        self.counter = 0
        self.tqs: List[TQ] = []

    def create_tq(
        self,
        code: str,
        title: str,
        severity: str = "high",
        rules: Optional[List[str]] = None,
        affected_signals: Optional[List[str]] = None,
        affected_cables: Optional[List[str]] = None,
        affected_pairs: Optional[List[str]] = None,
        evidence: str = "",
        unplaced_count: int = 0,
        cabinets_affected: int = 0,
        spare_delta: float = 0.0,
        suggestions: Optional[List[str]] = None,
        auto_fix_available: bool = False,
    ) -> TQ:
        self.counter += 1
        tq_id = f"{self.prefix}{self.counter:04d}"

        tq = TQ(
            tq_id=tq_id,
            severity=severity,
            code=code,
            title=title,
            rules=rules or [],
            affected={
                "signals": affected_signals or [],
                "cables": affected_cables or [],
                "pairs": affected_pairs or [],
            },
            evidence=evidence,
            impact={
                "unplaced_signals": unplaced_count,
                "cabinets_affected": cabinets_affected,
                "spare_delta": spare_delta,
            },
            suggestions=suggestions or [],
            auto_fix_available=auto_fix_available,
        )
        self.tqs.append(tq)
        return tq

    def get_all(self) -> List[TQ]:
        return list(self.tqs)

    def to_list(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self.tqs]
