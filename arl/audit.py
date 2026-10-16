"""
ARL Audit and Observability Event Logger.

Implements Part 6.5 and Part 13 append-only structured JSON Lines logging:
  - Records each compiler phase transition (parse, typecheck, resolve, normalize, dag_build,
    override_apply, lower, conflict_check, solve, validate, report).
  - Captures rule decisions, solver attempts, fallback events, validation outcomes, and output results.
  - Generates deterministic event streams with ISO 8601 timestamp metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import uuid
from pydantic import BaseModel, ConfigDict, Field


class AuditEvent(BaseModel):
    """Structured audit event conforming to Part 6.5 & Part 13."""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: str
    phase: str
    event_type: str
    outcome: str = "success"  # success, warning, failure, quarantined, blocked, info
    rule_id: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict())


class EventLogger:
    """Append-only audit event logger."""

    def __init__(self, run_id: Optional[str] = None, log_file: Optional[Union[str, Path]] = None) -> None:
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.log_file = Path(log_file) if log_file else None
        self.events: List[AuditEvent] = []

    def log(
        self,
        phase: str,
        event_type: str,
        outcome: str = "success",
        rule_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        """Create and append an audit event."""
        event = AuditEvent(
            run_id=self.run_id,
            phase=phase,
            event_type=event_type,
            outcome=outcome,
            rule_id=rule_id,
            details=details or {},
        )
        self.events.append(event)

        # Append to log file if configured
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(event.to_json_line() + "\n")

        return event

    def get_events(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.events]

    def to_jsonl(self) -> str:
        return "\n".join(e.to_json_line() for e in self.events) + "\n"

    def save_to_file(self, path: Union[str, Path]) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.to_jsonl())
        return p

    @classmethod
    def read_jsonl(cls, path: Union[str, Path]) -> List[Dict[str, Any]]:
        """Read and parse events from an existing JSONL audit log."""
        p = Path(path)
        events = []
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        return events


# Standard alias for EventLogger
AuditLogger = EventLogger

