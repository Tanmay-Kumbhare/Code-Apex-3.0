"""
ARL Data Quality Checks.

Detects data-level errors E4001-E4010 and raises Technical Queries (TQs) or structured errors.
Preserves recoverability for business data before passing to solvers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from arl.entities import Cable, Pair, Project, Signal
from arl.errors import ArlError, ErrorCollector, Phase, Severity
from arl.schema import Schema
from arl.tq import TQManager

VALID_IO_TYPES = {
    "AI", "AO", "DI", "DO", "DO_SOV", "DO_HORN", "DO_BEACON", "DO_SOUNDER", "DO_RELAY"
}
VALID_CABLE_SIZES = {2, 6, 12, 24}


def check_data_quality(
    project: Project,
    schema: Optional[Schema] = None,
    collector: Optional[ErrorCollector] = None,
    tq_manager: Optional[TQManager] = None,
) -> bool:
    """
    Run data-quality checks on Project data model.
    Emits E4001-E4010 and creates TQs where recoverable.
    Returns True if data has no fatal errors.
    """
    col = collector or ErrorCollector()
    tqs = tq_manager or TQManager()
    passed = True

    # 1. Signals checks: Duplicate tag (E4009), Missing required fields (E4001), Unknown IO type (E4006)
    seen_tags: Set[str] = set()
    signals: List[Signal] = project.get("signals", [])

    for sig in signals:
        tag = sig.get("tag")
        if not tag:
            col.add_error(
                code="E4001",
                message="Signal is missing required 'tag' field",
                phase=Phase.DATA,
            )
            passed = False
            continue

        if tag in seen_tags:
            col.add_error(
                code="E4009",
                message=f"Duplicate signal tag detected: '{tag}'",
                phase=Phase.DATA,
            )
            tqs.create_tq(
                code="E4009",
                title=f"Duplicate Signal Tag: {tag}",
                affected_signals=[tag],
                evidence=f"Tag '{tag}' occurs multiple times in project MDB",
                suggestions=["Rename one of the duplicate tags", "Remove redundant signal"],
            )
            passed = False
        seen_tags.add(tag)

        # Check required fields
        for req_f in ("io_type", "cable", "pair"):
            if sig.get(req_f) is None:
                col.add_error(
                    code="E4001",
                    message=f"Signal '{tag}' is missing required field '{req_f}'",
                    phase=Phase.DATA,
                )
                passed = False

        # Check IO type in CHARM map: E4006
        io_type = sig.get("io_type")
        if io_type and str(io_type) not in VALID_IO_TYPES:
            col.add_error(
                code="E4006",
                message=f"Signal '{tag}' has unsupported IO type '{io_type}'",
                phase=Phase.DATA,
            )
            tqs.create_tq(
                code="E4006",
                title=f"Invalid IO Type: {io_type} for {tag}",
                affected_signals=[tag],
                evidence=f"IO type '{io_type}' is not mapped to any known CHARM type",
                suggestions=["Map IO type to supported DeltaV CHARM (AI, AO, DI, DO)", "Clarify instrument specification"],
            )
            passed = False

    # Check for duplicate pair assignments across signals (E4004)
    seen_signal_pairs: Dict[Tuple[str, int], str] = {}
    for sig in signals:
        cbl = sig.get("cable")
        pair_val = sig.get("pair")
        sig_tag = sig.get("tag")
        if cbl and pair_val is not None:
            pair_key = (str(cbl), int(pair_val))
            if pair_key in seen_signal_pairs:
                prev_tag = seen_signal_pairs[pair_key]
                col.add_error(
                    code="E4004",
                    message=f"Duplicate pair assignment: signals '{prev_tag}' and '{sig_tag}' both claim cable '{cbl}' pair {pair_val}",
                    phase=Phase.DATA,
                )
                tqs.create_tq(
                    code="E4004",
                    title=f"Duplicate Pair Index {pair_val} in Cable {cbl}",
                    affected_signals=[prev_tag, str(sig_tag)],
                    affected_cables=[str(cbl)],
                    affected_pairs=[f"{cbl}:{pair_val}"],
                    evidence=f"Signals '{prev_tag}' and '{sig_tag}' both claim pair {pair_val} in cable {cbl}",
                    suggestions=["Renumber pairs consecutively or assign to distinct cables"],
                )
                passed = False
            else:
                seen_signal_pairs[pair_key] = str(sig_tag)

    # 2. Cable and Pair checks
    cables: List[Cable] = project.get("cables", [])
    seen_cable_ids: Set[str] = set()

    for cable in cables:
        cid = cable.get("id")
        if not cid:
            col.add_error(code="E4001", message="Cable missing required 'id'", phase=Phase.DATA)
            passed = False
            continue

        if cid in seen_cable_ids:
            col.add_error(code="E4008", message=f"Duplicate Cable ID: '{cid}'", phase=Phase.DATA)
            passed = False
        seen_cable_ids.add(cid)

        # Check cable size: E4002
        csize = cable.get("size")
        try:
            csize_int = int(csize)
            if csize_int not in VALID_CABLE_SIZES:
                col.add_error(
                    code="E4002",
                    message=f"Cable '{cid}' has invalid or non-standard size {csize}",
                    phase=Phase.DATA,
                )
                passed = False
        except (ValueError, TypeError):
            col.add_error(
                code="E4002",
                message=f"Cable '{cid}' size is unknown or non-numeric: {csize}",
                phase=Phase.DATA,
            )
            passed = False
            csize_int = 0

        # Check pairs within cable
        pairs: List[Pair] = cable.get("pairs", [])
        seen_pair_indices: Set[int] = set()

        for p in pairs:
            p_idx = p.get("index")
            if p_idx is None:
                col.add_error(code="E4001", message=f"Pair in cable '{cid}' missing index", phase=Phase.DATA)
                passed = False
                continue

            # Duplicate pair index: E4004
            if p_idx in seen_pair_indices:
                col.add_error(
                    code="E4004",
                    message=f"Duplicate pair index {p_idx} in cable '{cid}'",
                    phase=Phase.DATA,
                )
                tqs.create_tq(
                    code="E4004",
                    title=f"Duplicate Pair Index {p_idx} in Cable {cid}",
                    affected_cables=[cid],
                    affected_pairs=[f"{cid}:{p_idx}"],
                    evidence=f"Pair index {p_idx} appears multiple times in cable {cid}",
                    suggestions=["Renumber pairs consecutively"],
                )
                passed = False
            seen_pair_indices.add(p_idx)

            # Pair index out of range for cable size: E4010
            if csize_int > 0 and (p_idx < 1 or p_idx > csize_int):
                col.add_error(
                    code="E4010",
                    message=f"Pair index {p_idx} out of range for cable '{cid}' (size {csize_int})",
                    phase=Phase.DATA,
                )
                tqs.create_tq(
                    code="E4010",
                    title=f"Pair Index {p_idx} Out of Range for Cable {cid}",
                    affected_cables=[cid],
                    affected_pairs=[f"{cid}:{p_idx}"],
                    evidence=f"Cable size is {csize_int}, but pair index is {p_idx}",
                    suggestions=["Correct pair index to be within 1 and cable size", "Upgrade cable size"],
                )
                passed = False

            # Orphan pair: E4003 (pair has signal or is wired spare without valid cable ref)
            if not p.get("cable") and not cid:
                col.add_error(code="E4003", message=f"Orphan pair {p_idx} has no parent cable", phase=Phase.DATA)
                passed = False

    # 3. Check Capacity Overflow (E4007)
    total_slots = 0
    for cab in project.get("cabinets", []):
        for cioc in cab.get("ciocs", []):
            for bp in cioc.get("baseplates", []):
                total_slots += bp.get("capacity", 12)

    total_signals = len(signals)
    if total_slots > 0 and total_signals > total_slots:
        col.add_error(
            code="E4007",
            message=f"Capacity overflow: project has {total_signals} signals but only {total_slots} available slots",
            phase=Phase.DATA,
        )
        tqs.create_tq(
            code="E4007",
            title="Project Slot Capacity Overflow",
            severity="critical",
            evidence=f"Total signals ({total_signals}) exceeds total hardware slots ({total_slots})",
            unplaced_count=total_signals - total_slots,
            suggestions=["Add additional cabinets or CIOCs", "Expand baseplate counts"],
        )
        passed = False

    # 4. Check Group Segregation Mismatch (E4005)
    for grp in project.get("groups", []):
        gid = grp.get("id", "GRP")
        segs: Set[str] = set()
        grp_sigs: List[Signal] = grp.get("signals", [])
        for s in grp_sigs:
            seg_val = s.get("seg")
            if seg_val:
                segs.add(str(seg_val))
        if len(segs) > 1 and "A" in segs and "B" in segs:
            col.add_error(
                code="E4005",
                message=f"A/B mismatch within group '{gid}': contains both Seg A and Seg B signals",
                phase=Phase.DATA,
            )
            tqs.create_tq(
                code="E4005",
                title=f"A/B Segregation Mismatch in Group {gid}",
                severity="high",
                evidence=f"Group {gid} contains both Seg A and Seg B signals",
                suggestions=["Split group into dedicated Seg A and Seg B groups"],
            )
            passed = False

    return passed
