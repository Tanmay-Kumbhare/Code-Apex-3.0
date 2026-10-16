"""
ARL Override Application and Effective Rule Construction.

Transforms the bound AST by applying overrides to produce an effective ruleset.
Enforces writer uniqueness and detects if an override removes the only writer (E3003).
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Set, Tuple

from arl.ast import (
    AssignEffect,
    BinaryOp,
    Effect,
    Expr,
    Override,
    Rule,
    RuleMeta,
    Ruleset,
)
from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error
from arl.schema import Schema


def apply_overrides(
    ruleset: Ruleset,
    schema: Optional[Schema] = None,
    collector: Optional[ErrorCollector] = None,
) -> Ruleset:
    """
    Applies overrides to produce effective rules.
    Returns a new effective Ruleset.
    """
    col = collector or ErrorCollector()
    rule_map: Dict[str, Rule] = {r.name: copy.deepcopy(r) for r in ruleset.rules}

    # Record initial writers before overrides
    initial_writers: Dict[Tuple[str, str], Set[str]] = {}
    for r in ruleset.rules:
        for eff in r.effects:
            if isinstance(eff, AssignEffect):
                key = (r.scope, eff.field_name)
                if key not in initial_writers:
                    initial_writers[key] = set()
                initial_writers[key].add(r.name)

    # Apply overrides
    for override in ruleset.overrides:
        target_name = override.target_rule
        if target_name not in rule_map:
            col.add_error(
                code="E2013",
                message=f"Override targets undefined rule '{target_name}'",
                location=override.location,
                phase=Phase.OVERRIDE_APPLY,
            )
            continue

        target_rule = rule_map[target_name]

        # Verify scope compatibility: E2012
        if schema:
            entity_def = schema.get_entity(target_rule.scope)
            for eff in override.effects:
                if isinstance(eff, AssignEffect):
                    if not entity_def or eff.field_name not in entity_def.fields:
                        col.add_error(
                            code="E2012",
                            message=f"Override scope mismatch: rule '{target_name}' has scope '{target_rule.scope}', but override assigns field '{eff.field_name}'",
                            location=override.location,
                            phase=Phase.OVERRIDE_APPLY,
                        )

        # Merge when condition
        if override.when:
            if target_rule.when:
                target_rule.when = BinaryOp(
                    left=target_rule.when,
                    op="and",
                    right=override.when,
                    location=override.when.location,
                )
            else:
                target_rule.when = override.when

        # Override replaces target effects
        if override.effects:
            target_rule.effects = copy.deepcopy(override.effects)

        # Update metadata if specified
        if override.meta.priority:
            target_rule.meta.priority = override.meta.priority
        if override.meta.weight is not None:
            target_rule.meta.weight = override.meta.weight
        if override.meta.message:
            target_rule.meta.message = override.meta.message
        if override.meta.tags:
            target_rule.meta.tags = sorted(list(set(target_rule.meta.tags + override.meta.tags)))

    # Compute effective writers after overrides
    effective_writers: Dict[Tuple[str, str], Set[str]] = {}
    for r in rule_map.values():
        for eff in r.effects:
            if isinstance(eff, AssignEffect):
                key = (r.scope, eff.field_name)
                if key not in effective_writers:
                    effective_writers[key] = set()
                effective_writers[key].add(r.name)

    # Check E3003: Override removes only writer of required field
    for (scope, fname), orig_rules in initial_writers.items():
        if (scope, fname) not in effective_writers or len(effective_writers[(scope, fname)]) == 0:
            col.add_error(
                code="E3003",
                message=f"Override removes the only writer of field '{fname}' on entity '{scope}'",
                phase=Phase.OVERRIDE_APPLY,
            )

    # Check E2011: Multiple writers check on effective rules
    for (scope, fname), writers in effective_writers.items():
        if len(writers) > 1:
            col.add_error(
                code="E2011",
                message=f"Duplicate field writer in effective rules: rules {sorted(list(writers))} all assign field '{fname}' on entity '{scope}'",
                phase=Phase.TYPECHECK,
            )

    return Ruleset(
        name=ruleset.name,
        extends=ruleset.extends,
        includes=copy.deepcopy(ruleset.includes),
        rules=list(rule_map.values()),
        objectives=copy.deepcopy(ruleset.objectives),
        overrides=copy.deepcopy(ruleset.overrides),
        location=ruleset.location,
    )
