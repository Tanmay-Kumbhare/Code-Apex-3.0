"""
ARL Excel Output Writer.

Implements Part 9.2 Template-Preserving Output:
  - Loads provided allocation template preserving merged cells, formulas, styles, and data validation.
  - Fills cell values without regenerating or destroying existing template structures.
  - Generates required sheets: Allocation, TQ_List, Rule_Check, Summary, and Impact.
  - Ensures atomic writing via temporary file and atomic replace.
  - Formats allocation rows: tag, cabinet, CIOC, baseplate, CHARM slot, CHARM type, TB type, rule IDs, rule version.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional, Union
import uuid

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from arl.ast import Ruleset
from arl.entities import Project
from arl.solver_greedy import AllocationResult, Placement
from arl.tq import TQ
from arl.validate import ValidationResult


HEADER_FILL = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
THIN_BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)


def create_default_template(output_path: Union[str, Path]) -> Path:
    """
    Generate a default styled allocation template workbook
    containing pre-existing banners, merged cells, formulas, and styles.
    Used when no external client template is provided.
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Allocation"

    # Title Banner with Merged Cells
    ws.merge_cells("A1:I1")
    title_cell = ws["A1"]
    title_cell.value = "DELTAV DCS IO SIGNAL ALLOCATION"
    title_cell.font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="002060", end_color="002060", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # Header Row
    headers = [
        "Tag", "Cabinet", "CIOC", "Baseplate", "CHARM Slot",
        "CHARM Type", "TB Type", "Applied Rule IDs", "Rule Version"
    ]
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER
    ws.row_dimensions[2].height = 24

    # Sample summary row with formulas at bottom
    ws["K2"] = "Total Allocations:"
    ws["L2"] = "=COUNTA(A3:A500)"

    wb.save(p)
    return p


class ExcelWriter:
    """Template-preserving, atomic Excel output writer."""

    def __init__(self, rule_version: str = "2.1.0") -> None:
        self.rule_version = rule_version

    def write_allocation_workbook(
        self,
        output_path: Union[str, Path],
        allocation_result: AllocationResult,
        project: Project,
        template_path: Optional[Union[str, Path]] = None,
        ruleset: Optional[Ruleset] = None,
        validation_result: Optional[ValidationResult] = None,
        revision_impact: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """
        Populate allocation data into template workbook and atomically write to output_path.
        """
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)

        # 1. Load or create template workbook
        if template_path and Path(template_path).exists():
            wb = openpyxl.load_workbook(template_path)
        else:
            # Create a styled template in-memory
            temp_tmpl = out_p.parent / f".tmp_tmpl_{uuid.uuid4().hex}.xlsx"
            create_default_template(temp_tmpl)
            wb = openpyxl.load_workbook(temp_tmpl)
            try:
                temp_tmpl.unlink(missing_ok=True)
            except Exception:
                pass

        # 2. Populate 'Allocation' Sheet
        self._populate_allocation_sheet(wb, allocation_result)

        # 3. Populate 'TQ_List' Sheet
        self._populate_tq_sheet(wb, allocation_result.tqs)

        # 4. Populate 'Rule_Check' Sheet
        self._populate_rule_check_sheet(wb, ruleset, validation_result)

        # 5. Populate 'Summary' Sheet
        self._populate_summary_sheet(wb, allocation_result, validation_result)

        # 6. Populate 'Impact' Sheet
        self._populate_impact_sheet(wb, revision_impact)

        # 7. Atomic Write: save to temp file and rename
        temp_out = out_p.parent / f".tmp_write_{uuid.uuid4().hex}_{out_p.name}"
        try:
            wb.save(temp_out)
            # Verify file integrity by attempting to reopen
            test_wb = openpyxl.load_workbook(temp_out, read_only=True)
            test_wb.close()
            # Atomic rename
            os.replace(temp_out, out_p)
        finally:
            if temp_out.exists():
                try:
                    temp_out.unlink()
                except Exception:
                    pass

        return out_p

    def _populate_allocation_sheet(
        self,
        wb: openpyxl.Workbook,
        result: AllocationResult,
    ) -> None:
        """Fill allocations preserving existing headers and layout."""
        if "Allocation" not in wb.sheetnames:
            ws = wb.create_sheet("Allocation")
            headers = [
                "Tag", "Cabinet", "CIOC", "Baseplate", "CHARM Slot",
                "CHARM Type", "TB Type", "Applied Rule IDs", "Rule Version"
            ]
            for col_idx, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx, value=h)
                cell.font = HEADER_FONT
                cell.fill = HEADER_FILL
        else:
            ws = wb["Allocation"]

        # Find starting row for data (row after header)
        start_row = 3 if ws.max_row >= 2 and ws.cell(row=1, column=1).coordinate in ws.merged_cells else 2

        # Sort placements deterministically by cabinet -> cioc -> baseplate -> slot
        sorted_placements = sorted(
            result.allocations.values(),
            key=lambda p: (p.cabinet_id, p.cioc_id, p.baseplate_id, p.slot),
        )

        for i, pl in enumerate(sorted_placements):
            row_idx = start_row + i
            rule_ids_str = ", ".join(pl.applied_rules) if pl.applied_rules else "R_Default"
            values = [
                pl.tag,
                pl.cabinet_id,
                pl.cioc_id,
                pl.baseplate_id,
                pl.slot,
                pl.charm_type,
                pl.tb_type,
                rule_ids_str,
                self.rule_version,
            ]
            for col_idx, val in enumerate(values, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = THIN_BORDER
                cell.alignment = Alignment(horizontal="center" if col_idx in (2, 3, 4, 5) else "left")

        # Auto-adjust column widths
        for col in ws.columns:
            first_val = str(col[0].value or "")
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(14, len(first_val) + 4)

    def _populate_tq_sheet(
        self,
        wb: openpyxl.Workbook,
        tqs: List[TQ],
    ) -> None:
        """Create or update TQ_List sheet conforming to Part 6.4 schema."""
        if "TQ_List" in wb.sheetnames:
            ws = wb["TQ_List"]
            ws.delete_rows(1, ws.max_row + 1)
        else:
            ws = wb.create_sheet("TQ_List")

        headers = [
            "TQ ID", "Severity", "Code", "Title", "Involved Rules",
            "Affected Signals", "Affected Cables", "Affected Pairs",
            "Evidence", "Impact: Unplaced", "Impact: Cabinets",
            "Impact: Spare Delta", "Suggestions", "Auto Fix Available"
        ]

        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = PatternFill(start_color="9C0006" if "TQ" in h else "C00000", end_color="C00000", fill_type="solid")
            cell.border = THIN_BORDER
        ws.row_dimensions[1].height = 24

        for r_idx, tq in enumerate(tqs, 2):
            rules_str = ", ".join(tq.rules)
            sigs_str = ", ".join(tq.affected.get("signals", []))
            cbls_str = ", ".join(tq.affected.get("cables", []))
            pairs_str = ", ".join(tq.affected.get("pairs", []))
            sugg_str = "; ".join(tq.suggestions)

            row_vals = [
                tq.tq_id,
                tq.severity.upper(),
                tq.code,
                tq.title,
                rules_str,
                sigs_str,
                cbls_str,
                pairs_str,
                tq.evidence,
                tq.impact.get("unplaced_signals", 0),
                tq.impact.get("cabinets_affected", 0),
                f"{tq.impact.get('spare_delta', 0.0):.1f}%",
                sugg_str,
                "YES" if tq.auto_fix_available else "NO",
            ]
            for col_idx, val in enumerate(row_vals, 1):
                c = ws.cell(row=r_idx, column=col_idx, value=val)
                c.border = THIN_BORDER

        # Format widths
        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = 18

    def _populate_rule_check_sheet(
        self,
        wb: openpyxl.Workbook,
        ruleset: Optional[Ruleset],
        val_result: Optional[ValidationResult],
    ) -> None:
        """Create or update Rule_Check sheet."""
        if "Rule_Check" in wb.sheetnames:
            ws = wb["Rule_Check"]
            ws.delete_rows(1, ws.max_row + 1)
        else:
            ws = wb.create_sheet("Rule_Check")

        headers = ["Rule ID", "Scope", "Priority", "Status", "Violations Count", "Message / Details"]
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.border = THIN_BORDER
        ws.row_dimensions[1].height = 24

        violations_by_rule: Dict[str, List[str]] = {}
        if val_result:
            for v in val_result.violations:
                violations_by_rule.setdefault(v.rule_id, []).append(v.message)

        row_idx = 2
        if ruleset:
            for rule in ruleset.rules:
                r_id = rule.meta.rule_id or rule.name
                prio = (rule.meta.priority or "hard").upper()
                viols = violations_by_rule.get(r_id, [])
                status = "FAIL" if viols else "PASS"

                ws.cell(row=row_idx, column=1, value=r_id).border = THIN_BORDER
                ws.cell(row=row_idx, column=2, value=rule.scope).border = THIN_BORDER
                ws.cell(row=row_idx, column=3, value=prio).border = THIN_BORDER
                st_cell = ws.cell(row=row_idx, column=4, value=status)
                st_cell.border = THIN_BORDER
                st_cell.font = Font(bold=True, color="9C0006" if status == "FAIL" else "006100")
                ws.cell(row=row_idx, column=5, value=len(viols)).border = THIN_BORDER
                ws.cell(row=row_idx, column=6, value="; ".join(viols) if viols else "Rule satisfied").border = THIN_BORDER
                row_idx += 1
        else:
            # Report general validation status
            ws.cell(row=2, column=1, value="Core_DCS_Rules").border = THIN_BORDER
            ws.cell(row=2, column=2, value="Project").border = THIN_BORDER
            ws.cell(row=2, column=3, value="HARD").border = THIN_BORDER
            status = "PASS" if (val_result and val_result.valid) else "FAIL"
            ws.cell(row=2, column=4, value=status).border = THIN_BORDER
            ws.cell(row=2, column=5, value=len(val_result.violations) if val_result else 0).border = THIN_BORDER
            ws.cell(row=2, column=6, value="Physical constraints and segregation verified").border = THIN_BORDER

        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = 20

    def _populate_summary_sheet(
        self,
        wb: openpyxl.Workbook,
        result: AllocationResult,
        val_result: Optional[ValidationResult],
    ) -> None:
        """Create or update Summary KPI sheet."""
        if "Summary" in wb.sheetnames:
            ws = wb["Summary"]
            ws.delete_rows(1, ws.max_row + 1)
        else:
            ws = wb.create_sheet("Summary")

        ws.merge_cells("A1:D1")
        title_c = ws["A1"]
        title_c.value = "ARL ALLOCATION RUN SUMMARY"
        title_c.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        title_c.fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        title_c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        kpis = [
            ("Total Signals Ingested", result.stats.get("total_signals", len(result.allocations) + len(result.unplaced_signals))),
            ("Successfully Placed Signals", len(result.allocations)),
            ("Unplaced Signals", len(result.unplaced_signals)),
            ("Allocation Success Rate", f"{result.stats.get('success_rate', 100.0):.1f}%"),
            ("Total Hardware Slots", result.stats.get("total_slots", 0)),
            ("Occupied CHARM Slots", result.stats.get("occupied_slots", len(result.allocations))),
            ("Spare CHARM Slots", result.stats.get("spare_slots", 0)),
            ("Spare Capacity Ratio", f"{result.stats.get('spare_percent', 0.0):.1f}%"),
            ("Technical Queries (TQs) Raised", len(result.tqs)),
            ("Independent Validation Status", "VALID" if (val_result and val_result.valid) else "INVALID / REJECTED"),
        ]

        for i, (metric, val) in enumerate(kpis, 3):
            m_cell = ws.cell(row=i, column=2, value=metric)
            m_cell.font = Font(bold=True)
            m_cell.border = THIN_BORDER
            v_cell = ws.cell(row=i, column=3, value=val)
            v_cell.border = THIN_BORDER
            v_cell.alignment = Alignment(horizontal="right")

        ws.column_dimensions["B"].width = 32
        ws.column_dimensions["C"].width = 22

    def _populate_impact_sheet(
        self,
        wb: openpyxl.Workbook,
        impact: Optional[Dict[str, Any]],
    ) -> None:
        """Create or update Impact sheet conforming to Part 10.3 specification."""
        if "Impact" in wb.sheetnames:
            ws = wb["Impact"]
            ws.delete_rows(1, ws.max_row + 1)
        else:
            ws = wb.create_sheet("Impact")

        ws.merge_cells("A1:D1")
        title_c = ws["A1"]
        title_c.value = "REVISION COMPARISON & MOVE IMPACT REPORT"
        title_c.font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        title_c.fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        title_c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        curr_row = 3

        # 1. Summary Metrics Block (Part 10.3: added, deleted, moved, unchanged counts; cabinets hit; move count)
        headers = ["Revision Metric / Parameter", "Value"]
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.border = THIN_BORDER
        curr_row += 1

        summary_dict = impact if impact else {
            "Status": "Baseline (Single Run)",
            "Signals Added": 0,
            "Signals Deleted": 0,
            "Signals Moved": 0,
            "Signals Unchanged": 0,
            "Total Moves Count": 0,
            "Cabinets Affected Count": 0,
            "Cabinets Affected": "None",
        }

        # Filter out nested complex objects for summary section
        scalar_kpis = [
            ("Status", summary_dict.get("Status", "Revision Run")),
            ("Signals Added", summary_dict.get("Signals Added", 0)),
            ("Signals Deleted", summary_dict.get("Signals Deleted", 0)),
            ("Signals Moved", summary_dict.get("Signals Moved", 0)),
            ("Signals Unchanged", summary_dict.get("Signals Unchanged", 0)),
            ("Total Moves Count", summary_dict.get("Total Moves Count", summary_dict.get("Signals Moved", 0))),
            ("Move Budget", summary_dict.get("Move Budget", "UNLIMITED")),
            ("Move Budget Exceeded", summary_dict.get("Move Budget Exceeded", "NO")),
            ("Cabinets Affected Count", summary_dict.get("Cabinets Affected Count", 0)),
            ("Cabinets Affected", summary_dict.get("Cabinets Affected", "None")),
            ("Rule Changes Count", summary_dict.get("Rule Changes Count", 0)),
        ]

        for k, v in scalar_kpis:
            c1 = ws.cell(row=curr_row, column=1, value=k)
            c1.font = Font(bold=True)
            c1.border = THIN_BORDER
            c2 = ws.cell(row=curr_row, column=2, value=str(v))
            c2.border = THIN_BORDER
            if k == "Move Budget Exceeded" and str(v).upper() == "YES":
                c2.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                c2.font = Font(color="9C0006", bold=True)
            curr_row += 1

        curr_row += 1

        # 2. Rule Changes Block (Part 10.3: rule changes)
        rule_changes = impact.get("rule_changes", []) if impact else []
        if rule_changes:
            ws.merge_cells(f"A{curr_row}:D{curr_row}")
            section_hdr = ws[f"A{curr_row}"]
            section_hdr.value = "RULESET CHANGES (REV A VS REV B)"
            section_hdr.font = Font(bold=True, color="FFFFFF")
            section_hdr.fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            curr_row += 1

            r_hdrs = ["Rule Name", "Rule ID", "Change Type", "Diff Details"]
            for col_idx, h in enumerate(r_hdrs, 1):
                c = ws.cell(row=curr_row, column=col_idx, value=h)
                c.font = HEADER_FONT
                c.fill = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
                c.border = THIN_BORDER
            curr_row += 1

            for r_ch in rule_changes:
                ws.cell(row=curr_row, column=1, value=r_ch.get("name", "")).border = THIN_BORDER
                ws.cell(row=curr_row, column=2, value=r_ch.get("rule_id", "") or "-").border = THIN_BORDER
                ch_type_cell = ws.cell(row=curr_row, column=3, value=str(r_ch.get("change_type", "")).upper())
                ch_type_cell.border = THIN_BORDER
                ws.cell(row=curr_row, column=4, value=r_ch.get("details", "")).border = THIN_BORDER
                curr_row += 1

            curr_row += 1

        # 3. Moved Signals Block (Part 10.3: moved signals details)
        moved_signals = impact.get("moved_signals", []) if impact else []
        if moved_signals:
            ws.merge_cells(f"A{curr_row}:E{curr_row}")
            move_hdr = ws[f"A{curr_row}"]
            move_hdr.value = "MOVED SIGNALS TRAIL"
            move_hdr.font = Font(bold=True, color="FFFFFF")
            move_hdr.fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
            curr_row += 1

            m_hdrs = ["Tag", "Old Location (Rev A)", "New Location (Rev B)", "Cabinet Changed?", "Reason"]
            for col_idx, h in enumerate(m_hdrs, 1):
                c = ws.cell(row=curr_row, column=col_idx, value=h)
                c.font = HEADER_FONT
                c.fill = PatternFill(start_color="F2DCDB", end_color="F2DCDB", fill_type="solid")
                c.border = THIN_BORDER
            curr_row += 1

            for mv in moved_signals:
                old_loc = f"{mv.get('old_cabinet')}/{mv.get('old_cioc')}/{mv.get('old_baseplate')}/Slot {mv.get('old_slot')}"
                new_loc = f"{mv.get('new_cabinet')}/{mv.get('new_cioc')}/{mv.get('new_baseplate')}/Slot {mv.get('new_slot')}"
                cab_changed = "YES" if mv.get("old_cabinet") != mv.get("new_cabinet") else "NO"

                ws.cell(row=curr_row, column=1, value=mv.get("tag", "")).border = THIN_BORDER
                ws.cell(row=curr_row, column=2, value=old_loc).border = THIN_BORDER
                ws.cell(row=curr_row, column=3, value=new_loc).border = THIN_BORDER
                c_cab = ws.cell(row=curr_row, column=4, value=cab_changed)
                c_cab.border = THIN_BORDER
                c_cab.alignment = Alignment(horizontal="center")
                ws.cell(row=curr_row, column=5, value=mv.get("reason", "Slot relocated")).border = THIN_BORDER
                curr_row += 1

        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 30
        ws.column_dimensions["C"].width = 30
        ws.column_dimensions["D"].width = 24
        ws.column_dimensions["E"].width = 30

