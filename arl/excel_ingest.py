"""
ARL Excel IO MDB Ingestion.

Supports:
  - Ingestion of one or multiple .xlsx workbook files.
  - Inspectable and configurable fuzzy column header matching with manual override.
  - Optional LLM-assisted column mapping (disabled by default, with deterministic fallback).
  - Normalization of workbook rows into the schema-driven entity model (Project, Signal, Cable, Pair, Group).
  - Rigorous data quality validation checking codes E4001-E4010 and generating structured TQs.
"""

from __future__ import annotations

import difflib
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import pandas as pd
from pydantic import BaseModel, Field

from arl.data_quality import check_data_quality
from arl.entities import (
    Baseplate,
    Cabinet,
    Cable,
    CIOC,
    Group,
    Pair,
    Project,
    Signal,
)
from arl.errors import ErrorCollector, Phase
from arl.schema import Schema, get_base_schema
from arl.tq import TQManager

# Canonical schema fields for Signal and their common aliases in IO MDBs
CANONICAL_FIELD_ALIASES: Dict[str, List[str]] = {
    "tag": [
        "tag", "tag_name", "tagname", "instrument_tag", "signal_tag",
        "loop_name", "tag no", "instrument no", "signal", "tag_id",
        "instrument_number", "tag_number", "device_tag"
    ],
    "io_type": [
        "io_type", "iotype", "io", "i/o", "signal_type", "type", "io type",
        "channel_type", "i/o type", "signal io type", "card_type"
    ],
    "cable": [
        "cable", "cable_id", "cable_no", "cable_number", "cable tag",
        "multicore", "cbl", "home_run_cable", "trunk_cable", "cable_name"
    ],
    "pair": [
        "pair", "pair_no", "pair_number", "pair_index", "pair_id",
        "core", "pair #", "pair no.", "cable_pair", "pair_idx"
    ],
    "seg": [
        "seg", "segregation", "seg_role", "separation", "channel_seg",
        "seg_group", "segregation_group", "seg A/B"
    ],
    "voltage": [
        "voltage", "voltage_class", "signal_voltage", "volt", "level"
    ],
    "safety": [
        "safety", "is_safety", "sis", "sil", "safety_flag", "safety_critical"
    ],
    "redundancy": [
        "redundancy", "redundant", "redundancy_kind", "red", "is_redundant"
    ],
    "area": [
        "area", "unit_area", "plant_area", "location", "area_code", "sub_area"
    ],
    "unit": [
        "unit", "process_unit", "plant_unit", "unit_id", "facility"
    ],
    "group": [
        "group", "group_id", "bundle", "loop_group", "system", "group_name"
    ],
    "cable_size": [
        "cable_size", "size", "cablesize", "cable_pairs", "number_of_pairs", "pairs_count"
    ],
    "rev": [
        "rev", "revision", "rev_id", "version", "revision_id", "issue"
    ],
}


class ColumnMapping(BaseModel):
    """Inspectable mapping between raw workbook header and canonical entity field."""
    raw_column: str
    canonical_field: str
    confidence: float = 1.0
    method: str = "exact"  # 'manual', 'exact', 'alias', 'fuzzy', 'llm'


class IngestionReport(BaseModel):
    """Diagnostics and metadata from workbook ingestion."""
    files_processed: List[str] = Field(default_factory=list)
    total_rows_read: int = 0
    signals_created: int = 0
    cables_created: int = 0
    groups_created: int = 0
    column_mappings: List[ColumnMapping] = Field(default_factory=list)
    unmapped_columns: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


def _normalize_header(header: Any) -> str:
    """Normalize column header string for comparison."""
    if header is None:
        return ""
    s = str(header).strip().lower()
    # Replace punctuation and whitespace with single space
    s = re.sub(r"[_\-\./\\#,]+", " ", s)
    return " ".join(s.split())


def resolve_column_mapping(
    columns: List[str],
    manual_overrides: Optional[Dict[str, str]] = None,
    use_llm: bool = False,
    llm_mapper: Optional[Callable[[List[str]], Dict[str, str]]] = None,
) -> Tuple[Dict[str, str], List[ColumnMapping], List[str]]:
    """
    Resolve raw dataframe columns to canonical schema fields.
    Returns:
      (raw_to_canonical_map, list_of_mappings, unmapped_columns)
    """
    manual_map = {str(k).strip(): str(v).strip() for k, v in (manual_overrides or {}).items()}
    resolved: Dict[str, str] = {}
    mapping_details: List[ColumnMapping] = []
    unmapped: List[str] = []

    # Priority 1: User-specified manual overrides
    remaining_cols = []
    for col in columns:
        col_str = str(col).strip()
        if col_str in manual_map:
            canonical = manual_map[col_str]
            resolved[col_str] = canonical
            mapping_details.append(
                ColumnMapping(
                    raw_column=col_str,
                    canonical_field=canonical,
                    confidence=1.0,
                    method="manual",
                )
            )
        else:
            remaining_cols.append(col_str)

    # Priority 2: LLM-assisted mapping (if enabled and hook provided)
    if use_llm and llm_mapper is not None and remaining_cols:
        try:
            llm_results = llm_mapper(remaining_cols)
            for raw, canonical in llm_results.items():
                if raw in remaining_cols and canonical in CANONICAL_FIELD_ALIASES:
                    resolved[raw] = canonical
                    mapping_details.append(
                        ColumnMapping(
                            raw_column=raw,
                            canonical_field=canonical,
                            confidence=0.9,
                            method="llm",
                        )
                    )
                    remaining_cols.remove(raw)
        except Exception:
            # Fall back safely on deterministic mapping
            pass

    # Priority 3: Exact & Alias Matching
    still_unmapped = []
    for raw in remaining_cols:
        norm = _normalize_header(raw)
        matched_field = None
        method = "none"

        # Check exact canonical name match
        for canon, aliases in CANONICAL_FIELD_ALIASES.items():
            if norm == _normalize_header(canon):
                matched_field = canon
                method = "exact"
                break

        # Check aliases
        if not matched_field:
            for canon, aliases in CANONICAL_FIELD_ALIASES.items():
                for alias in aliases:
                    if norm == _normalize_header(alias):
                        matched_field = canon
                        method = "alias"
                        break
                if matched_field:
                    break

        # Priority 4: Fuzzy matching with cutoff 0.75
        if not matched_field:
            best_score = 0.0
            best_field = None
            for canon, aliases in CANONICAL_FIELD_ALIASES.items():
                candidates = [canon] + aliases
                for cand in candidates:
                    norm_cand = _normalize_header(cand)
                    score = difflib.SequenceMatcher(None, norm, norm_cand).ratio()
                    if score > best_score:
                        best_score = score
                        best_field = canon
            if best_score >= 0.75 and best_field:
                matched_field = best_field
                method = "fuzzy"

        if matched_field:
            resolved[raw] = matched_field
            mapping_details.append(
                ColumnMapping(
                    raw_column=raw,
                    canonical_field=matched_field,
                    confidence=1.0 if method in ("exact", "alias") else 0.8,
                    method=method,
                )
            )
        else:
            still_unmapped.append(raw)

    return resolved, mapping_details, still_unmapped


def read_excel_mdb(
    file_paths: Union[str, Path, List[Union[str, Path]]],
    column_mapping: Optional[Dict[str, str]] = None,
    use_llm: bool = False,
    llm_mapper: Optional[Callable[[List[str]], Dict[str, str]]] = None,
    schema: Optional[Schema] = None,
    collector: Optional[ErrorCollector] = None,
    tq_manager: Optional[TQManager] = None,
    auto_generate_hardware: bool = True,
) -> Tuple[Project, TQManager, ErrorCollector, IngestionReport]:
    """
    Ingest one or multiple Excel IO MDB workbooks and normalize into an ARL Project.
    Runs data quality validation (E4001-E4010) and populates TQs.
    """
    col = collector or ErrorCollector()
    tqs = tq_manager or TQManager()
    sch = schema or get_base_schema()

    if isinstance(file_paths, (str, Path)):
        file_paths = [file_paths]

    report = IngestionReport()
    dfs: List[pd.DataFrame] = []

    for fp in file_paths:
        p = Path(fp)
        report.files_processed.append(str(p))
        if not p.exists():
            col.add_error(code="E4001", message=f"Input file not found: '{p}'", phase=Phase.DATA)
            continue

        try:
            with pd.ExcelFile(p) as excel_file:
                for sheet_name in excel_file.sheet_names:
                    df = pd.read_excel(excel_file, sheet_name=sheet_name)
                    if not df.empty and len(df.columns) > 1:
                        df["_source_file"] = p.name
                        df["_source_sheet"] = sheet_name
                        dfs.append(df)
        except Exception as ex:
            col.add_error(
                code="E4001",
                message=f"Failed to read Excel file '{p}': {str(ex)}",
                phase=Phase.DATA,
            )

    if not dfs:
        # Return empty project
        empty_proj = Project(name="Empty_Project", schema=sch)
        return empty_proj, tqs, col, report

    combined_df = pd.concat(dfs, ignore_index=True)
    report.total_rows_read = len(combined_df)

    # Resolve Column Headers
    raw_cols = [str(c) for c in combined_df.columns if not str(c).startswith("_")]
    col_map, mapping_details, unmapped = resolve_column_mapping(
        raw_cols,
        manual_overrides=column_mapping,
        use_llm=use_llm,
        llm_mapper=llm_mapper,
    )
    report.column_mappings = mapping_details
    report.unmapped_columns = unmapped

    # Rename columns in combined_df
    mapped_df = combined_df.rename(columns=col_map)

    # Build Signal entities
    signals: List[Signal] = []
    cables_dict: Dict[str, Dict[str, Any]] = {}
    groups_dict: Dict[str, List[Signal]] = {}

    for idx, row in mapped_df.iterrows():
        # Clean string/value representation
        tag_val = row.get("tag")
        if pd.isna(tag_val) or not str(tag_val).strip():
            tag_str = None
        else:
            tag_str = str(tag_val).strip()

        io_val = row.get("io_type")
        io_str = str(io_val).strip() if (io_val is not None and not pd.isna(io_val)) else None

        cable_val = row.get("cable")
        cable_str = str(cable_val).strip() if (cable_val is not None and not pd.isna(cable_val)) else None

        pair_val = row.get("pair")
        pair_int = None
        if pair_val is not None and not pd.isna(pair_val):
            try:
                pair_int = int(pair_val)
            except (ValueError, TypeError):
                pair_int = None

        seg_val = row.get("seg")
        seg_str = str(seg_val).strip() if (seg_val is not None and not pd.isna(seg_val)) else None
        if seg_str:
            # Normalize Seg string: "A" or "B"
            if "a" in seg_str.lower() and "b" not in seg_str.lower():
                seg_str = "A"
            elif "b" in seg_str.lower() and "a" not in seg_str.lower():
                seg_str = "B"

        area_val = row.get("area")
        area_str = str(area_val).strip() if (area_val is not None and not pd.isna(area_val)) else None

        unit_val = row.get("unit")
        unit_str = str(unit_val).strip() if (unit_val is not None and not pd.isna(unit_val)) else None

        group_val = row.get("group")
        group_str = str(group_val).strip() if (group_val is not None and not pd.isna(group_val)) else "DEFAULT_GRP"

        safety_val = row.get("safety")
        safety_bool = False
        if safety_val is not None and not pd.isna(safety_val):
            safety_str = str(safety_val).lower().strip()
            safety_bool = safety_str in ("true", "1", "yes", "y", "sis", "sil")

        red_val = row.get("redundancy")
        red_str = str(red_val).strip() if (red_val is not None and not pd.isna(red_val)) else None

        rev_val = row.get("rev")
        rev_str = str(rev_val).strip() if (rev_val is not None and not pd.isna(rev_val)) else "A"

        # Instantiate Signal
        sig = Signal(
            schema=sch,
            tag=tag_str,
            io_type=io_str,
            cable=cable_str,
            pair=pair_int,
            seg=seg_str,
            area=area_str,
            unit=unit_str,
            group=group_str,
            safety=safety_bool,
            redundancy=red_str,
            rev=rev_str,
        )
        signals.append(sig)

        # Track Group
        if group_str not in groups_dict:
            groups_dict[group_str] = []
        groups_dict[group_str].append(sig)

        size_val = row.get("cable_size")
        size_int = None
        if size_val is not None and not pd.isna(size_val):
            try:
                size_int = int(size_val)
            except (ValueError, TypeError):
                size_int = size_val

        # Track Cable and Pairs
        if cable_str:
            if cable_str not in cables_dict:
                cables_dict[cable_str] = {
                    "id": cable_str,
                    "pairs": set(),
                    "max_pair": 0,
                    "signals": [],
                    "declared_size": size_int,
                }
            if size_int is not None:
                cables_dict[cable_str]["declared_size"] = size_int
            if pair_int is not None:
                cables_dict[cable_str]["pairs"].add(pair_int)
                cables_dict[cable_str]["max_pair"] = max(cables_dict[cable_str]["max_pair"], pair_int)
            cables_dict[cable_str]["signals"].append(sig)

    report.signals_created = len(signals)

    # Build Cable entities and calculate standard cable sizes (2, 6, 12, 24)
    cables: List[Cable] = []
    for cid, cinfo in sorted(cables_dict.items()):
        max_p = cinfo["max_pair"]
        # Standard sizing: fit max pair index into smallest standard size
        if max_p <= 2:
            std_size = 2
        elif max_p <= 6:
            std_size = 6
        elif max_p <= 12:
            std_size = 12
        else:
            std_size = 24

        actual_size = cinfo.get("declared_size") if cinfo.get("declared_size") is not None else std_size

        pair_objs: List[Pair] = []
        # Add pairs for every signal in the cable
        for s in cinfo["signals"]:
            p_val = s.get("pair")
            if p_val is not None:
                pair_objs.append(
                    Pair(
                        schema=sch,
                        cable=cid,
                        index=p_val,
                        signal=s.get("tag"),
                        wired_spare=False,
                    )
                )

        # Add wired spares for any remaining unused indices within cable size
        used_indices = {p.get("index") for p in pair_objs}
        try:
            cable_size_int = int(actual_size)
            for p_idx in range(1, cable_size_int + 1):
                if p_idx not in used_indices:
                    pair_objs.append(
                        Pair(
                            schema=sch,
                            cable=cid,
                            index=p_idx,
                            signal=None,
                            wired_spare=True,
                        )
                    )
        except (ValueError, TypeError):
            pass

        cable_entity = Cable(
            schema=sch,
            id=cid,
            size=actual_size,
            pairs=pair_objs,
        )
        cables.append(cable_entity)

    report.cables_created = len(cables)

    # Build Group entities
    groups: List[Group] = []
    for gid, g_sigs in sorted(groups_dict.items()):
        grp = Group(
            schema=sch,
            id=gid,
            signals=g_sigs,
            cables=[c for c in cables if any(s.get("cable") == c.get("id") for s in g_sigs)],
        )
        groups.append(grp)
    report.groups_created = len(groups)

    # Build standard project hardware hierarchy if requested
    cabinets: List[Cabinet] = []
    if auto_generate_hardware:
        # Determine total slots needed with a 20% spare capacity buffer
        total_signals = len(signals)
        slots_needed = max(24, int(total_signals * 1.25) + 12)
        baseplates_needed = (slots_needed + 11) // 12

        # 8 baseplates per CIOC
        baseplates: List[Baseplate] = []
        ciocs: List[CIOC] = []

        bp_counter = 1
        cioc_counter = 1

        while baseplates_needed > 0:
            current_cioc_bps = min(8, baseplates_needed)
            cioc_bps: List[Baseplate] = []
            cioc_id = f"CIOC-{cioc_counter:02d}"

            for i in range(1, current_cioc_bps + 1):
                bp = Baseplate(
                    schema=sch,
                    id=f"BP-{bp_counter:02d}",
                    cioc=cioc_id,
                    index=i,
                    capacity=12,
                )
                cioc_bps.append(bp)
                baseplates.append(bp)
                bp_counter += 1

            cioc = CIOC(
                schema=sch,
                id=cioc_id,
                cabinet="CAB-01",
                index=cioc_counter,
                seg_role="MIXED",
                baseplates=cioc_bps,
            )
            ciocs.append(cioc)
            cioc_counter += 1
            baseplates_needed -= current_cioc_bps

        cab = Cabinet(
            schema=sch,
            id="CAB-01",
            area="AREA_01",
            seq=1,
            ciocs=ciocs,
            spare_target_percent=10.0,
        )
        cabinets.append(cab)

    project = Project(
        schema=sch,
        name="Ingested_Project",
        cabinets=cabinets,
        signals=signals,
        cables=cables,
        groups=groups,
    )

    # Run complete Data Quality Checks (E4001 - E4010)
    check_data_quality(project, schema=sch, collector=col, tq_manager=tqs)

    return project, tqs, col, report
