"""
ARL Allocation Pipeline.

Coordinates the end-to-end pipeline:
  Excel Ingestion -> Normalization -> Compilation -> Conflict Check -> Solver -> Validation -> Template Output -> Audit Log & Report
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import Ruleset
from arl.audit import EventLogger
from arl.conflict import ConflictChecker, ConflictCheckResult
from arl.dag import RuleDAG
from arl.entities import Project
from arl.data_quality import check_data_quality
from arl.errors import ErrorCollector
from arl.excel_ingest import IngestionReport, read_excel_mdb
from arl.excel_output import ExcelWriter
from arl.normalize import normalize_ruleset
from arl.override import apply_overrides
from arl.parser import parse_ruleset
from arl.report import RunReport, build_run_report
from arl.resolve import Resolver
from arl.schema import Schema, get_base_schema
from arl.solver_greedy import AllocationResult, GreedySolver
from arl.tq import TQ, TQManager
from arl.typecheck import TypeChecker
from arl.validate import ValidationResult, Validator


class PipelineConfig(BaseModel):
    """Configuration options for the allocation pipeline."""
    solver_backend: str = "cpsat"
    time_limit_seconds: float = 30.0
    enable_fallback: bool = True
    column_mapping: Optional[Dict[str, str]] = None
    audit_log_path: Optional[Union[str, Path]] = None
    report_path: Optional[Union[str, Path]] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class PipelineResult(BaseModel):
    """Result of an end-to-end allocation run."""
    success: bool
    output_path: Optional[str] = None
    total_signals: int = 0
    placed_signals: int = 0
    unplaced_signals: int = 0
    solver_invoked: bool = True
    validation_passed: bool = False
    tqs: List[TQ] = Field(default_factory=list)
    ingestion_report: Optional[IngestionReport] = None
    run_report: Optional[RunReport] = None
    conflict_result: Optional[ConflictCheckResult] = None
    allocation: Optional[AllocationResult] = None
    collector: Optional[ErrorCollector] = None
    stats: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)


def run_allocation_pipeline(
    mdb_paths: Union[str, Path, List[Union[str, Path]]],
    output_path: Optional[Union[str, Path]] = None,
    template_path: Optional[Union[str, Path]] = None,
    ruleset_path: Optional[Union[str, Path]] = None,
    column_mapping: Optional[Dict[str, str]] = None,
    schema: Optional[Schema] = None,
    audit_log_path: Optional[Union[str, Path]] = None,
    report_path: Optional[Union[str, Path]] = None,
    solver_backend: str = "cpsat",
    time_limit_seconds: float = 30.0,
    enable_fallback: bool = True,
) -> PipelineResult:
    """
    Execute full end-to-end IO allocation workflow with static conflict check and audit logging.
    """
    logger = EventLogger(log_file=audit_log_path)
    logger.log(phase="init", event_type="pipeline_start", outcome="success")

    sch = schema or get_base_schema()
    col = ErrorCollector()
    tqs = TQManager()

    # 1. Ingestion & Normalization
    logger.log(phase="ingest", event_type="phase_start")
    project, tqs, col, ingest_rep = read_excel_mdb(
        file_paths=mdb_paths,
        column_mapping=column_mapping,
        schema=sch,
        collector=col,
        tq_manager=tqs,
    )
    logger.log(
        phase="ingest",
        event_type="phase_end",
        outcome="success" if not col.has_fatal else "warning",
        details={"signals": len(project.get("signals", [])), "cables": len(project.get("cables", []))},
    )

    # 1.5. Data Quality Validation (E4xxx)
    dq_valid = check_data_quality(project, collector=col, tq_manager=tqs)
    if col.has_fatal or not dq_valid:
        run_rep = build_run_report(
            ruleset=None,
            collector=col,
            tq_manager=tqs,
            has_conflict=False,
        )
        if report_path:
            run_rep.save_json(report_path)
        return PipelineResult(
            success=False,
            output_path=None,
            total_signals=len(project.get("signals", [])),
            placed_signals=0,
            unplaced_signals=len(project.get("signals", [])),
            solver_invoked=False,
            validation_passed=False,
            tqs=tqs.get_all(),
            ingestion_report=ingest_rep,
            run_report=run_rep,
            conflict_result=None,
            allocation=None,
            collector=col,
            stats={"reason": "Fatal data quality errors"},
        )

    # 2. Ruleset Processing (if provided)
    ruleset: Optional[Ruleset] = None
    if ruleset_path:
        is_file = False
        try:
            is_file = Path(ruleset_path).exists() and Path(ruleset_path).is_file()
        except (OSError, ValueError):
            is_file = False

        if is_file:
            logger.log(phase="parse", event_type="phase_start", details={"file": str(ruleset_path)})
            with open(ruleset_path, "r", encoding="utf-8") as rf:
                src = rf.read()
        else:
            src = str(ruleset_path)
            logger.log(phase="parse", event_type="phase_start", details={"source": "inline"})

        raw_ruleset = parse_ruleset(src, collector=col)
        logger.log(phase="parse", event_type="phase_end", outcome="success")

        logger.log(phase="typecheck", event_type="phase_start")
        tc = TypeChecker(schema=sch, collector=col)
        tc.check_ruleset(raw_ruleset)
        logger.log(phase="typecheck", event_type="phase_end")

        logger.log(phase="resolve", event_type="phase_start")
        resolver = Resolver(schema=sch, collector=col)
        resolver.resolve(raw_ruleset)
        logger.log(phase="resolve", event_type="phase_end")

        logger.log(phase="normalize", event_type="phase_start")
        norm_ruleset = normalize_ruleset(raw_ruleset)
        logger.log(phase="normalize", event_type="phase_end")

        logger.log(phase="dag_build", event_type="phase_start")
        dag = RuleDAG.from_ruleset(norm_ruleset, collector=col)
        logger.log(phase="dag_build", event_type="phase_end")

        logger.log(phase="override_apply", event_type="phase_start")
        ruleset = apply_overrides(norm_ruleset, collector=col)
        logger.log(phase="override_apply", event_type="phase_end")

    # 3. Static Conflict Checking: HARD GATE before solver
    conflict_res = None
    if ruleset:
        logger.log(phase="conflict_check", event_type="phase_start")
        checker = ConflictChecker(collector=col)
        conflict_res = checker.check(ruleset, project)
        if not conflict_res.can_solve:
            logger.log(
                phase="conflict_check",
                event_type="solver_blocked",
                outcome="blocked",
                details={
                    "conflicts": [e.code for e in conflict_res.conflicts],
                    "unsat_cores": conflict_res.unsat_cores,
                },
            )
            # Create TQ for fatal conflict E3001/E3002
            for conf_err in conflict_res.conflicts:
                tqs.create_tq(
                    code=conf_err.code,
                    title=f"Static Conflict: {conf_err.code}",
                    severity="critical",
                    evidence=conf_err.message,
                    suggestions=[conf_err.suggestion or "Adjust conflicting rules"],
                )

            # Build report and return WITHOUT running solver
            run_rep = build_run_report(
                ruleset=ruleset,
                collector=col,
                tq_manager=tqs,
                has_conflict=True,
            )
            if report_path:
                run_rep.save_json(report_path)

            return PipelineResult(
                success=False,
                output_path=None,
                total_signals=len(project.get("signals", [])),
                placed_signals=0,
                unplaced_signals=len(project.get("signals", [])),
                solver_invoked=False,  # PROVES SOLVER WAS NOT INVOKED
                validation_passed=False,
                tqs=tqs.get_all(),
                ingestion_report=ingest_rep,
                run_report=run_rep,
                conflict_result=conflict_res,
                allocation=None,
                collector=col,
                stats={"reason": "Solver execution blocked by fatal E3xxx conflicts"},
            )
        else:
            logger.log(phase="conflict_check", event_type="phase_end", outcome="success")

    # 4. Solver Backend (CP-SAT with deterministic Greedy fallback, or Greedy)
    logger.log(phase="solve", event_type="phase_start", details={"solver": solver_backend})
    if solver_backend == "cpsat":
        from arl.solver_cpsat import CPSATSolver
        solver = CPSATSolver(
            schema=sch,
            tq_manager=tqs,
            collector=col,
            time_limit_seconds=time_limit_seconds,
            enable_fallback=enable_fallback,
        )
    else:
        solver = GreedySolver(schema=sch, tq_manager=tqs, collector=col)

    alloc_result = solver.solve(project, ruleset=ruleset)
    logger.log(
        phase="solve",
        event_type="phase_end",
        outcome="success" if alloc_result.success else "partial",
        details=alloc_result.stats,
    )

    # 5. Independent Validation
    logger.log(phase="validate", event_type="phase_start")
    validator = Validator()
    val_result = validator.validate(project, alloc_result, ruleset=ruleset)
    logger.log(
        phase="validate",
        event_type="phase_end",
        outcome="success" if val_result.valid else "failure",
        details={"checks_run": val_result.checks_run, "violations": len(val_result.violations)},
    )

    # 6. Excel Template-Preserving Output (if output_path is provided)
    out_file_str = None
    if output_path:
        logger.log(phase="output", event_type="phase_start", details={"path": str(output_path)})
        writer = ExcelWriter()
        out_file = writer.write_allocation_workbook(
            output_path=output_path,
            allocation_result=alloc_result,
            project=project,
            template_path=template_path,
            ruleset=ruleset,
            validation_result=val_result,
        )
        out_file_str = str(out_file)
        logger.log(phase="output", event_type="phase_end", outcome="success")

    # 7. Generate Run Report
    run_rep = build_run_report(
        ruleset=ruleset,
        collector=col,
        tq_manager=tqs,
        has_conflict=False,
    )
    if report_path:
        run_rep.save_json(report_path)

    logger.log(phase="report", event_type="report_generated", outcome=run_rep.status)

    is_overall_success = alloc_result.success and val_result.valid

    return PipelineResult(
        success=is_overall_success,
        output_path=out_file_str,
        total_signals=len(project.get("signals", [])),
        placed_signals=len(alloc_result.allocations),
        unplaced_signals=len(alloc_result.unplaced_signals),
        solver_invoked=True,
        validation_passed=val_result.valid,
        tqs=tqs.get_all(),
        ingestion_report=ingest_rep,
        run_report=run_rep,
        conflict_result=conflict_res,
        allocation=alloc_result,
        collector=col,
        stats={
            "alloc_stats": alloc_result.stats,
            "validation_checks": val_result.checks_run,
            "violations_count": len(val_result.violations),
        },
    )
