"""
ARL Command-Line Interface (CLI).

Implements Part 12 and Part 13 authoritative CLI specification:
  - `arl compile <ruleset.arl>`
  - `arl check <ruleset.arl> --data <mdb>`
  - `arl allocate <ruleset.arl> --mdb <xlsx> --template <xlsx> --out <xlsx>`
  - `arl revise <ruleset.arl> --revA <xlsx> --revB <xlsx> --out <xlsx>`
  - `arl diff-rules <a.arl> <b.arl>`
  - `arl diff-alloc <a.xlsx> <b.xlsx>`
  - `arl predicates list`
  - `arl predicates register <file.yaml>`

Exit codes:
  - 0: success
  - 1: compile error (lex, parse, typecheck, static conflict, dag cycle)
  - 2: data error (E4xxx MDB data quality errors)
  - 3: solver fallback (when solving required greedy fallback, degraded=True)
  - 4: internal error (unhandled exceptions, system failure)

Exit-Code Precedence Order:
  Internal Error (4) > Compile Error (1) > Data Error (2) > Solver Fallback (3) > Success (0)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence
import yaml

from arl.conflict import ConflictChecker
from arl.data_quality import check_data_quality
from arl.errors import ArlError, ErrorCollector, Phase, Severity
from arl.excel_ingest import read_excel_mdb
from arl.excel_output import ExcelWriter, create_default_template
from arl.parser import parse_ruleset
from arl.pipeline import run_allocation_pipeline
from arl.predicates import PredicateRegistry, get_builtin_predicate_registry
from arl.report import build_run_report
from arl.resolve import Resolver
from arl.revision import (
    diff_allocations,
    diff_rulesets,
    read_allocation_from_excel as read_alloc_wb,
    run_revision_pipeline,
)
from arl.schema import Schema, get_base_schema
from arl.typecheck import TypeChecker


# =========================================================================
# Exit Code Constants
# =========================================================================

EXIT_SUCCESS = 0
EXIT_COMPILE_ERROR = 1
EXIT_DATA_ERROR = 2
EXIT_SOLVER_FALLBACK = 3
EXIT_INTERNAL_ERROR = 4


def print_errors(errors: Sequence[ArlError], stream: Any = sys.stderr) -> None:
    """Format and print structured errors."""
    for err in errors:
        loc = f" at line {err.location.line}:{err.location.column}" if err.location and err.location.line else ""
        rule = f" [rule: {err.rule_id}]" if err.rule_id else ""
        print(f"[{err.severity.value.upper()}] {err.code}: {err.message}{rule}{loc}", file=stream)
        if err.suggestion:
            print(f"       Suggestion: {err.suggestion}", file=stream)


# =========================================================================
# Command Implementations
# =========================================================================

def cmd_compile(ruleset_path: Path, schema: Optional[Schema] = None) -> int:
    """Compile ruleset and run full typecheck, resolution, and static conflict validation."""
    if not ruleset_path.exists():
        print(f"Error: Ruleset file not found: {ruleset_path}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    schema = schema or get_base_schema()
    col = ErrorCollector()

    try:
        source = ruleset_path.read_text(encoding="utf-8")
        rs = parse_ruleset(source, source_file=str(ruleset_path), collector=col)
    except Exception as ex:
        print(f"Parse error: {ex}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    if col.has_fatal:
        print_errors(col.errors)
        return EXIT_COMPILE_ERROR

    # Typecheck & Resolve
    tc = TypeChecker(schema, collector=col)
    tc.check_ruleset(rs)

    resolver = Resolver(schema, collector=col)
    resolver.resolve(rs)

    # Static conflict check
    checker = ConflictChecker(collector=col)
    conflict_res = checker.check(rs)

    if col.has_fatal or not conflict_res.can_solve:
        print_errors(col.errors)
        return EXIT_COMPILE_ERROR

    print(f"Compiled successfully: '{rs.name}' ({len(rs.rules)} rules, {len(rs.objectives)} objectives, {len(rs.overrides)} overrides)")
    return EXIT_SUCCESS


def cmd_check(ruleset_path: Path, data_path: Path, schema: Optional[Schema] = None) -> int:
    """Dry-run check: validate ruleset compilation, MDB data quality, and static conflicts."""
    schema = schema or get_base_schema()

    # 1. Compile ruleset
    if not ruleset_path.exists():
        print(f"Error: Ruleset file not found: {ruleset_path}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    col = ErrorCollector()
    try:
        source = ruleset_path.read_text(encoding="utf-8")
        rs = parse_ruleset(source, source_file=str(ruleset_path), collector=col)
    except Exception as ex:
        print(f"Parse error: {ex}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    if col.has_fatal:
        print_errors(col.errors)
        return EXIT_COMPILE_ERROR

    tc = TypeChecker(schema, collector=col)
    tc.check_ruleset(rs)
    resolver = Resolver(schema, collector=col)
    resolver.resolve(rs)

    checker = ConflictChecker(collector=col)
    conflict_res = checker.check(rs)

    if col.has_fatal or not conflict_res.can_solve:
        print_errors(col.errors)
        return EXIT_COMPILE_ERROR

    # 2. Ingest and check data
    if not data_path.exists():
        print(f"Error: MDB file not found: {data_path}", file=sys.stderr)
        return EXIT_DATA_ERROR

    try:
        project, _, _, _ = read_excel_mdb([data_path], schema=schema, collector=col)
    except Exception as ex:
        print(f"Data ingestion error: {ex}", file=sys.stderr)
        return EXIT_DATA_ERROR

    dq_col = ErrorCollector()
    dq_valid = check_data_quality(project, collector=dq_col)

    if dq_col.has_fatal or not dq_valid:
        print_errors(dq_col.errors)
        return EXIT_DATA_ERROR

    print(f"Dry-run check passed: ruleset '{rs.name}' and data '{data_path.name}' ({len(project.get('signals', []))} signals) are valid.")
    return EXIT_SUCCESS


def cmd_allocate(
    ruleset_path: Path,
    mdb_path: Path,
    template_path: Path,
    out_path: Path,
    report_path: Optional[Path] = None,
    solver_mode: str = "cpsat",
    schema: Optional[Schema] = None,
) -> int:
    """Execute end-to-end allocation pipeline."""
    schema = schema or get_base_schema()

    # 1. Validate ruleset path
    if not ruleset_path.exists():
        print(f"Error: Ruleset file not found: {ruleset_path}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    # 2. Validate data path
    if not mdb_path.exists():
        print(f"Error: MDB data file not found: {mdb_path}", file=sys.stderr)
        return EXIT_DATA_ERROR

    # 3. Validate template path
    if not template_path.exists():
        print(f"Error: Template file not found: {template_path}", file=sys.stderr)
        return EXIT_DATA_ERROR

    try:
        res = run_allocation_pipeline(
            mdb_paths=[mdb_path],
            template_path=template_path,
            output_path=out_path,
            ruleset_path=ruleset_path,
            report_path=report_path,
            schema=schema,
            solver_backend=solver_mode,
        )
    except Exception as ex:
        print(f"Internal solver failure: {ex}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    # Check precedence: Compile Error (1) > Data Error (2) > Solver Fallback (3) > Success (0)
    col = res.collector
    if col:
        has_compile_errors = any(e.phase in (Phase.LEX, Phase.PARSE, Phase.TYPECHECK, Phase.RESOLVE, Phase.CONFLICT_CHECK) and e.severity in (Severity.FATAL, Severity.ERROR) for e in col.errors)
        if has_compile_errors:
            print_errors(col.errors)
            return EXIT_COMPILE_ERROR

        has_data_errors = any((e.phase == Phase.DATA or str(e.code).startswith("E4")) and e.severity in (Severity.FATAL, Severity.ERROR) for e in col.errors)
        if has_data_errors:
            print_errors(col.errors)
            return EXIT_DATA_ERROR

    if res.allocation and res.allocation.degraded:
        print(f"Allocation completed with fallback (degraded=True): {out_path}", file=sys.stderr)
        return EXIT_SOLVER_FALLBACK

    if not res.success:
        print("Allocation failed to place all signals.", file=sys.stderr)
        return EXIT_SOLVER_FALLBACK

    print(f"Allocation successfully exported to '{out_path}'. Total signals placed: {len(res.allocation.allocations)}.")
    return EXIT_SUCCESS


def cmd_revise(
    ruleset_path: Path,
    rev_a_path: Path,
    rev_b_path: Path,
    out_path: Path,
    max_moves: Optional[int] = None,
    schema: Optional[Schema] = None,
) -> int:
    """Execute revision comparison, pinning, and move minimization pipeline."""
    schema = schema or get_base_schema()

    if not ruleset_path.exists():
        print(f"Error: Ruleset file not found: {ruleset_path}", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    if not rev_a_path.exists() or not rev_b_path.exists():
        print(f"Error: Rev A or Rev B workbook missing", file=sys.stderr)
        return EXIT_DATA_ERROR

    try:
        result = run_revision_pipeline(
            rev_a_mdb=rev_a_path,
            rev_b_mdb=rev_b_path,
            rev_a_allocations=rev_a_path,
            ruleset_b=ruleset_path,
            output_path=out_path,
            max_moves=max_moves,
            schema=schema,
        )
    except Exception as ex:
        print(f"Revision pipeline failure: {ex}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    if not result.success:
        if result.budget_exceeded:
            print("Move budget exceeded", file=sys.stderr)
            return EXIT_SOLVER_FALLBACK
        return EXIT_DATA_ERROR

    if result.allocation_result and result.allocation_result.degraded:
        return EXIT_SOLVER_FALLBACK

    unchanged_count = len(result.mdb_diff.unchanged) if result.mdb_diff else 0
    print(f"Revision successfully written to '{out_path}'. Total moved: {result.move_count}, unchanged: {unchanged_count}.")
    return EXIT_SUCCESS


def cmd_diff_rules(path_a: Path, path_b: Path) -> int:
    """Compute and display rule differences between two .arl files."""
    if not path_a.exists() or not path_b.exists():
        print("Error: One or both rule files do not exist", file=sys.stderr)
        return EXIT_COMPILE_ERROR

    rs_a = parse_ruleset(path_a.read_text(encoding="utf-8"), source_file=str(path_a))
    rs_b = parse_ruleset(path_b.read_text(encoding="utf-8"), source_file=str(path_b))

    diff = diff_rulesets(rs_a, rs_b)

    print(f"Ruleset Diff: '{rs_a.name}' -> '{rs_b.name}'")
    print(f"  Added rules ({len(diff.added_rules)}): {', '.join(r.name for r in diff.added_rules) or 'None'}")
    print(f"  Removed rules ({len(diff.removed_rules)}): {', '.join(r.name for r in diff.removed_rules) or 'None'}")
    print(f"  Modified rules ({len(diff.modified_rules)}): {', '.join(r.name for r in diff.modified_rules) or 'None'}")
    print(f"  Overridden rules ({len(diff.overridden_rules)}): {', '.join(r.name for r in diff.overridden_rules) or 'None'}")
    return EXIT_SUCCESS


def cmd_diff_alloc(path_a: Path, path_b: Path) -> int:
    """Compute and display allocation differences between two Excel workbooks."""
    if not path_a.exists() or not path_b.exists():
        print("Error: One or both allocation workbooks do not exist", file=sys.stderr)
        return EXIT_DATA_ERROR

    alloc_a = read_alloc_wb(path_a)
    alloc_b = read_alloc_wb(path_b)

    diff = diff_allocations(alloc_a, alloc_b)

    print(f"Allocation Diff: '{path_a.name}' -> '{path_b.name}'")
    print(f"  Moved signals: {diff['moved_count']}")
    print(f"  Unchanged signals: {diff['unchanged_count']}")
    print(f"  Cabinets affected: {', '.join(sorted(list(diff['cabinets_hit']))) or 'None'}")
    return EXIT_SUCCESS


def cmd_predicates_list(registry: Optional[PredicateRegistry] = None) -> int:
    """List all registered predicates in the typed registry."""
    reg = registry or get_builtin_predicate_registry()
    local_yaml = Path("predicates.yaml")
    if local_yaml.exists():
        try:
            custom_reg = PredicateRegistry.from_yaml(local_yaml)
            for p in custom_reg.all():
                if not reg.has(p.name):
                    reg.register(p.name, p.args, p.returns, p.description, category=p.category, allow_override=True)
        except Exception:
            pass

    preds = reg.all()

    print(f"Registered ARL Predicates ({len(preds)} total):")
    print("-" * 75)
    for p in sorted(preds, key=lambda x: (x.category, x.name)):
        arg_str = ", ".join(f"{name}: {t}" for name, t in p.args)
        print(f"[{p.category.upper():<11}] {p.name}({arg_str}) -> {p.returns}")
        if p.description:
            print(f"              {p.description}")
    return EXIT_SUCCESS


def cmd_predicates_register(file_path: Path, registry: Optional[PredicateRegistry] = None) -> int:
    """Register predicates from a YAML file into the predicate registry."""
    if not file_path.exists():
        print(f"Error: Predicate definition file not found: {file_path}", file=sys.stderr)
        return EXIT_DATA_ERROR

    reg = registry or get_builtin_predicate_registry()
    local_yaml = Path("predicates.yaml")
    if local_yaml.exists():
        try:
            custom_reg = PredicateRegistry.from_yaml(local_yaml)
            for p in custom_reg.all():
                if not reg.has(p.name):
                    reg.register(p.name, p.args, p.returns, p.description, category=p.category, allow_override=True)
        except Exception:
            pass

    try:
        content = file_path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            raise ValueError("Predicate YAML must be a mapping of predicate names to definitions.")

        count = 0
        for name, pdata in data.items():
            args = []
            for a in pdata.get("args", []):
                if isinstance(a, dict):
                    args.append((a.get("name", "arg"), a.get("type", "Any")))
                elif isinstance(a, (list, tuple)) and len(a) >= 2:
                    args.append((str(a[0]), str(a[1])))

            reg.register(
                name=name,
                args=args,
                returns=pdata.get("returns", "Bool"),
                description=pdata.get("description", ""),
                category=pdata.get("category", "general"),
                allow_override=True,
            )
            count += 1

        reg.save_yaml(local_yaml)
        print(f"Successfully registered {count} predicate(s) from '{file_path.name}'.")
        return EXIT_SUCCESS
    except Exception as ex:
        print(f"Predicate registration error: {ex}", file=sys.stderr)
        return EXIT_DATA_ERROR


# =========================================================================
# Argument Parser & CLI Entry Point
# =========================================================================

def build_parser() -> argparse.ArgumentParser:
    """Build exact argument parser specified in Part 12."""
    parser = argparse.ArgumentParser(
        prog="arl",
        description="Allocation Rule Language (ARL) v2 CLI Compiler & Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. arl compile <ruleset.arl>
    compile_parser = subparsers.add_parser("compile", help="Compile and typecheck an ARL ruleset")
    compile_parser.add_argument("ruleset", type=Path, help="Path to .arl ruleset file")

    # 2. arl check <ruleset.arl> --data <mdb>
    check_parser = subparsers.add_parser("check", help="Dry-run check ruleset against MDB data")
    check_parser.add_argument("ruleset", type=Path, help="Path to .arl ruleset file")
    check_parser.add_argument("--data", type=Path, required=True, help="Path to MDB Excel file")

    # 3. arl allocate <ruleset.arl> --mdb <xlsx> --template <xlsx> --out <xlsx>
    alloc_parser = subparsers.add_parser("allocate", help="Allocate IO signals into DCS hardware")
    alloc_parser.add_argument("ruleset", type=Path, help="Path to .arl ruleset file")
    alloc_parser.add_argument("--mdb", type=Path, required=True, help="Input Excel MDB workbook")
    alloc_parser.add_argument("--template", type=Path, required=True, help="Output template workbook")
    alloc_parser.add_argument("--out", type=Path, required=True, help="Destination allocation workbook")
    alloc_parser.add_argument("--report", type=Path, default=None, help="Optional run report JSON path")
    alloc_parser.add_argument("--solver", choices=["cpsat", "greedy"], default="cpsat", help="Solver backend")

    # 4. arl revise <ruleset.arl> --revA <xlsx> --revB <xlsx> --out <xlsx>
    revise_parser = subparsers.add_parser("revise", help="Minimally revise allocation from Rev A to Rev B")
    revise_parser.add_argument("ruleset", type=Path, help="Path to .arl ruleset file for Rev B")
    revise_parser.add_argument("--revA", type=Path, required=True, help="Rev A allocation workbook")
    revise_parser.add_argument("--revB", type=Path, required=True, help="Rev B MDB workbook")
    revise_parser.add_argument("--out", type=Path, required=True, help="Destination revised workbook")
    revise_parser.add_argument("--max-moves", type=int, default=None, help="Maximum allowed signal moves")

    # 5. arl diff-rules <a.arl> <b.arl>
    diff_rules_parser = subparsers.add_parser("diff-rules", help="Diff two ARL ruleset files")
    diff_rules_parser.add_argument("a", type=Path, help="Base ruleset (.arl)")
    diff_rules_parser.add_argument("b", type=Path, help="Target ruleset (.arl)")

    # 6. arl diff-alloc <a.xlsx> <b.xlsx>
    diff_alloc_parser = subparsers.add_parser("diff-alloc", help="Diff two allocation workbooks")
    diff_alloc_parser.add_argument("a", type=Path, help="Rev A allocation workbook")
    diff_alloc_parser.add_argument("b", type=Path, help="Rev B allocation workbook")

    # 7. arl predicates <list|register>
    pred_parser = subparsers.add_parser("predicates", help="Predicate management")
    pred_sub = pred_parser.add_subparsers(dest="pred_command", required=True)
    pred_sub.add_parser("list", help="List all registered predicates")
    pred_reg = pred_sub.add_parser("register", help="Register predicates from YAML file")
    pred_reg.add_argument("file", type=Path, help="YAML file containing predicate definitions")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point dispatching commands with strict exit codes."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as ex:
        return ex.code if isinstance(ex.code, int) else EXIT_COMPILE_ERROR

    try:
        if args.command == "compile":
            return cmd_compile(args.ruleset)
        elif args.command == "check":
            return cmd_check(args.ruleset, args.data)
        elif args.command == "allocate":
            return cmd_allocate(
                ruleset_path=args.ruleset,
                mdb_path=args.mdb,
                template_path=args.template,
                out_path=args.out,
                report_path=args.report,
                solver_mode=args.solver,
            )
        elif args.command == "revise":
            return cmd_revise(
                ruleset_path=args.ruleset,
                rev_a_path=args.revA,
                rev_b_path=args.revB,
                out_path=args.out,
                max_moves=args.max_moves,
            )
        elif args.command == "diff-rules":
            return cmd_diff_rules(args.a, args.b)
        elif args.command == "diff-alloc":
            return cmd_diff_alloc(args.a, args.b)
        elif args.command == "predicates":
            if args.pred_command == "list":
                return cmd_predicates_list()
            elif args.pred_command == "register":
                return cmd_predicates_register(args.file)
        return EXIT_INTERNAL_ERROR
    except Exception as ex:
        print(f"Internal unexpected error: {ex}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
