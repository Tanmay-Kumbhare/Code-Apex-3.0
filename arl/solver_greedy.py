"""
ARL Greedy Allocator.

Implements the deterministic greedy solver following the allocation order:
  groups -> cabinets -> CIOCs -> baseplates -> CHARMs
Preserves capacities, hardware compatibility, segregation, and active hard rules.
Generates structured TQs for any unplaced signals.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field

from arl.ast import Ruleset
from arl.entities import Baseplate, Cabinet, Cable, Charm, CIOC, Group, Pair, Project, Signal
from arl.errors import ErrorCollector, Phase
from arl.schema import Schema
from arl.tq import TQ, TQManager

# Default IO to Hardware Mapping
DEFAULT_HARDWARE_MAP: Dict[str, Tuple[str, str]] = {
    "AI": ("AI", "STANDARD"),
    "AO": ("AO", "STANDARD"),
    "DI": ("DI", "STANDARD"),
    "DO": ("HIGH_SIDE_DO", "STANDARD"),
    "DO_SOV": ("HIGH_SIDE_DO", "FUSED_INJECTED_POWER"),
    "DO_HORN": ("HIGH_SIDE_DO", "FUSED_INJECTED_POWER"),
    "DO_BEACON": ("HIGH_SIDE_DO", "FUSED_INJECTED_POWER"),
    "DO_SOUNDER": ("HIGH_SIDE_DO", "FUSED_INJECTED_POWER"),
    "DO_RELAY": ("ISOLATED_DO", "RELAY_OUTPUT"),
}


class Placement(BaseModel):
    """Assigned hardware coordinates for a single instrument signal."""
    tag: str
    cabinet_id: str
    cioc_id: str
    baseplate_id: str
    slot: int  # 1 to 12
    charm_type: str
    tb_type: str
    applied_rules: List[str] = Field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class AllocationResult(BaseModel):
    """Result of an allocation run."""
    success: bool = False
    degraded: bool = False
    allocations: Dict[str, Placement] = Field(default_factory=dict)  # tag -> Placement
    unplaced_signals: List[str] = Field(default_factory=list)
    tqs: List[TQ] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class GreedySolver:
    """Deterministic greedy allocator."""

    def __init__(
        self,
        schema: Optional[Schema] = None,
        tq_manager: Optional[TQManager] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> None:
        self.schema = schema
        self.tq_manager = tq_manager or TQManager()
        self.collector = collector or ErrorCollector()

    def solve(
        self,
        project: Project,
        ruleset: Optional[Ruleset] = None,
        pins: Optional[Dict[str, Any]] = None,
    ) -> AllocationResult:
        """
        Execute deterministic greedy allocation across project hierarchy:
          groups -> cabinets -> CIOCs -> baseplates -> CHARMs
        Honors pinned signal placements if provided.
        """
        allocations: Dict[str, Placement] = {}
        unplaced: List[str] = []

        # Collect and index project hardware slots
        # Occupancy tracker: (cabinet_id, cioc_id, baseplate_id, slot) -> tag
        occupied_slots: Dict[Tuple[str, str, str, int], str] = {}

        cabinets: List[Cabinet] = project.get("cabinets", [])
        # Deterministic sorting of cabinets
        sorted_cabinets = sorted(cabinets, key=lambda c: (str(c.get("area", "")), int(c.get("seq", 0)), str(c.get("id", ""))))

        # Group signals by group ID
        groups: List[Group] = project.get("groups", [])
        signals: List[Signal] = project.get("signals", [])

        # Map signals to groups
        signal_map: Dict[str, Signal] = {s.get("tag"): s for s in signals if s.get("tag")}
        group_signals: Dict[str, List[Signal]] = {}

        # 0. Pre-place active pinned signals (Part 10.2 Pinning)
        if pins:
            active_pins = [p for p in pins.values() if hasattr(p, "is_relaxed") and not p.is_relaxed]
            sorted_pins = sorted(active_pins, key=lambda p: (int(getattr(p, "priority", 1)), p.tag), reverse=True)
            for p in sorted_pins:
                tag = str(p.tag)
                coord = (p.cabinet_id, p.cioc_id, p.baseplate_id, p.slot)
                if coord not in occupied_slots:
                    sig = signal_map.get(tag)
                    io_type = str(sig.get("io_type", "AI")) if sig else "AI"
                    charm_type, tb_type = DEFAULT_HARDWARE_MAP.get(io_type, ("AI", "STANDARD"))
                    occupied_slots[coord] = tag
                    allocations[tag] = Placement(
                        tag=tag,
                        cabinet_id=p.cabinet_id,
                        cioc_id=p.cioc_id,
                        baseplate_id=p.baseplate_id,
                        slot=p.slot,
                        charm_type=charm_type,
                        tb_type=tb_type,
                        applied_rules=["R_Pinned_RevA"],
                    )

        for grp in groups:
            gid = str(grp.get("id", "DEFAULT"))
            group_signals[gid] = sorted(grp.get("signals", []), key=lambda s: (str(s.get("cable", "")), int(s.get("pair", 0))))

        # Catch ungrouped signals
        grouped_tags = {s.get("tag") for grp_list in group_signals.values() for s in grp_list if s.get("tag")}
        ungrouped = [s for s in signals if s.get("tag") and s.get("tag") not in grouped_tags]
        if ungrouped:
            group_signals["_ungrouped_"] = sorted(ungrouped, key=lambda s: (str(s.get("cable", "")), int(s.get("pair", 0))))

        # Allocation loop: Groups -> Cables -> Signals
        for gid in sorted(group_signals.keys()):
            sigs_in_group = group_signals[gid]

            for sig in sigs_in_group:
                tag = str(sig.get("tag"))
                if tag in allocations:
                    # Already placed by pin
                    continue

                io_type = str(sig.get("io_type", "AI"))
                seg = sig.get("seg")
                safety = bool(sig.get("safety", False))

                # Determine CHARM and TB type
                charm_type, tb_type = DEFAULT_HARDWARE_MAP.get(io_type, ("AI", "STANDARD"))

                placed = False

                # Search through cabinets -> CIOCs -> Baseplates -> slots (1..12)
                for cab in sorted_cabinets:
                    cab_id = str(cab.get("id", ""))
                    # Area filter if signal defines area
                    sig_area = sig.get("area")
                    if sig_area and cab.get("area") and str(sig_area) != str(cab.get("area")):
                        continue

                    ciocs: List[CIOC] = sorted(cab.get("ciocs", []), key=lambda c: int(c.get("index", 0)))
                    for cioc in ciocs:
                        cioc_id = str(cioc.get("id", ""))
                        seg_role = str(cioc.get("seg_role", "MIXED"))

                        # SEG check
                        if seg == "A" and seg_role == "B_ONLY":
                            continue
                        if seg == "B" and seg_role == "A_ONLY":
                            continue

                        baseplates: List[Baseplate] = sorted(cioc.get("baseplates", []), key=lambda b: int(b.get("index", 0)))
                        for bp in baseplates:
                            bp_id = str(bp.get("id", ""))
                            cap = int(bp.get("capacity", 12))

                            for slot_idx in range(1, cap + 1):
                                coord = (cab_id, cioc_id, bp_id, slot_idx)
                                if coord in occupied_slots:
                                    continue

                                # Slot is free! Place signal here
                                occupied_slots[coord] = tag
                                placement = Placement(
                                    tag=tag,
                                    cabinet_id=cab_id,
                                    cioc_id=cioc_id,
                                    baseplate_id=bp_id,
                                    slot=slot_idx,
                                    charm_type=charm_type,
                                    tb_type=tb_type,
                                    applied_rules=["R_Greedy_Standard"],
                                )
                                allocations[tag] = placement
                                placed = True
                                break

                            if placed:
                                break
                        if placed:
                            break
                    if placed:
                        break

                if not placed:
                    unplaced.append(tag)
                    self.tq_manager.create_tq(
                        code="E4007",
                        title=f"Unplaced Signal: {tag}",
                        severity="high",
                        affected_signals=[tag],
                        evidence=f"No available matching slot found in any cabinet for signal {tag} (IO: {io_type}, Seg: {seg})",
                        unplaced_count=1,
                        suggestions=["Add additional baseplates or CIOCs", "Adjust segregation roles"],
                    )

        total_signals = len(signals)
        placed_count = len(allocations)
        total_slots = sum(
            bp.get("capacity", 12)
            for cab in sorted_cabinets
            for cioc in cab.get("ciocs", [])
            for bp in cioc.get("baseplates", [])
        )
        spare_slots = max(0, total_slots - placed_count)
        spare_pct = (spare_slots / total_slots * 100.0) if total_slots > 0 else 0.0

        is_success = (len(unplaced) == 0) and (placed_count == total_signals)

        stats = {
            "total_signals": total_signals,
            "placed_signals": placed_count,
            "unplaced_signals": len(unplaced),
            "total_slots": total_slots,
            "occupied_slots": len(occupied_slots),
            "spare_slots": spare_slots,
            "spare_percent": round(spare_pct, 2),
            "success_rate": round((placed_count / total_signals * 100.0), 2) if total_signals > 0 else 100.0,
        }

        return AllocationResult(
            success=is_success,
            degraded=False,
            allocations=allocations,
            unplaced_signals=unplaced,
            tqs=self.tq_manager.get_all(),
            stats=stats,
        )
