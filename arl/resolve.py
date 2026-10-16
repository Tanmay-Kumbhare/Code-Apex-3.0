"""
ARL Resolution and Dependency Analysis.

Performs:
  - Inheritance resolution: extends target resolution (E2018)
  - Rule reference resolution: require rule IDENT (E2010)
  - Dependency DAG build and cycle detection (E2009)
  - Override resolution and scope verification (E2012, E2013)
  - Duplicate field writer detection (E2011)
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from arl.ast import AssignEffect, Override, RequireEffect, Rule, RuleRef, Ruleset
from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error
from arl.schema import Schema


class DependencyDAG:
    """Directed Acyclic Graph representing rule dependencies."""

    def __init__(self) -> None:
        self.adj: Dict[str, Set[str]] = {}
        self.rules: Dict[str, Rule] = {}

    def add_rule(self, rule: Rule) -> None:
        self.rules[rule.name] = rule
        if rule.name not in self.adj:
            self.adj[rule.name] = set()

    def add_dependency(self, from_rule: str, to_rule: str) -> None:
        if from_rule not in self.adj:
            self.adj[from_rule] = set()
        self.adj[from_rule].add(to_rule)

    def detect_cycles(self) -> List[List[str]]:
        """Detect all cycles in the rule dependency graph using DFS."""
        visited: Dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
        cycles: List[List[str]] = []
        path: List[str] = []

        def dfs(node: str) -> None:
            visited[node] = 1
            path.append(node)

            for neighbor in self.adj.get(node, []):
                if visited.get(neighbor, 0) == 1:
                    # Cycle detected: extract subpath from neighbor to end
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif visited.get(neighbor, 0) == 0:
                    dfs(neighbor)

            path.pop()
            visited[node] = 2

        for node in self.adj.keys():
            if visited.get(node, 0) == 0:
                dfs(node)

        return cycles


class Resolver:
    """Resolves names, inheritance, rule dependencies, overrides, and writers."""

    def __init__(
        self,
        schema: Schema,
        known_rulesets: Optional[Dict[str, Ruleset]] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> None:
        self.schema = schema
        self.known_rulesets = known_rulesets or {}
        self.collector = collector or ErrorCollector()
        self.dag = DependencyDAG()

    def resolve(self, ruleset: Ruleset) -> DependencyDAG:
        """Execute full resolution pass on ruleset."""
        self.known_rulesets[ruleset.name] = ruleset

        # 1. Check extends target (E2018)
        if ruleset.extends:
            if ruleset.extends not in self.known_rulesets:
                self.collector.add_error(
                    code="E2018",
                    message=f"Ruleset '{ruleset.name}' extends unknown ruleset '{ruleset.extends}'",
                    location=ruleset.location,
                    phase=Phase.RESOLVE,
                )

        # 2. Index all rules in DAG
        rule_map: Dict[str, Rule] = {}
        for rule in ruleset.rules:
            self.dag.add_rule(rule)
            rule_map[rule.name] = rule

        # 3. Resolve rule references and build dependency edges
        for rule in ruleset.rules:
            for effect in rule.effects:
                if isinstance(effect, RequireEffect) and isinstance(effect.target, RuleRef):
                    ref_name = effect.target.rule_name
                    if ref_name not in rule_map:
                        # E2010: Unknown rule reference
                        self.collector.add_error(
                            code="E2010",
                            message=f"Rule '{rule.name}' references unknown rule '{ref_name}'",
                            rule_id=rule.name,
                            location=effect.location,
                            phase=Phase.RESOLVE,
                            suggestion=f"Known rules in ruleset: {', '.join(sorted(rule_map.keys()))}",
                        )
                    else:
                        self.dag.add_dependency(rule.name, ref_name)

        # 4. Check for cyclic rule references (E2009)
        cycles = self.dag.detect_cycles()
        if cycles:
            for cycle in cycles:
                cycle_str = " -> ".join(cycle)
                self.collector.add_error(
                    code="E2009",
                    message=f"Cyclic rule reference detected: {cycle_str}",
                    rule_id=cycle[0],
                    location=rule_map[cycle[0]].location if cycle[0] in rule_map else ruleset.location,
                    phase=Phase.DAG_BUILD,
                )

        # 5. Resolve overrides (E2012, E2013)
        for override in ruleset.overrides:
            target = override.target_rule
            if target not in rule_map:
                # E2013: Undefined override target
                self.collector.add_error(
                    code="E2013",
                    message=f"Override targets undefined rule '{target}'",
                    location=override.location,
                    phase=Phase.OVERRIDE_APPLY,
                    suggestion=f"Available rules to override: {', '.join(sorted(rule_map.keys()))}",
                )
            else:
                target_rule = rule_map[target]
                # Check for scope/effect compatibility: E2012
                # If override has an assign effect, ensure field belongs to target rule's scope
                entity_def = self.schema.get_entity(target_rule.scope)
                for effect in override.effects:
                    if isinstance(effect, AssignEffect):
                        if not entity_def or effect.field_name not in entity_def.fields:
                            self.collector.add_error(
                                code="E2012",
                                message=f"Override scope mismatch: target rule '{target}' has scope '{target_rule.scope}', but assign targets field '{effect.field_name}'",
                                location=override.location,
                                phase=Phase.OVERRIDE_APPLY,
                            )

        # 6. Check duplicate field writers (E2011)
        writers: Dict[Tuple[str, str], List[str]] = {}  # (scope, field_name) -> [rule_name]
        for rule in ruleset.rules:
            for effect in rule.effects:
                if isinstance(effect, AssignEffect):
                    key = (rule.scope, effect.field_name)
                    if key not in writers:
                        writers[key] = []
                    writers[key].append(rule.name)

        for (scope, fname), rule_names in writers.items():
            if len(rule_names) > 1:
                # E2011: Duplicate field writer
                self.collector.add_error(
                    code="E2011",
                    message=f"Duplicate field writer: rules {rule_names} all assign field '{fname}' on entity '{scope}'",
                    rule_id=rule_names[0],
                    location=rule_map[rule_names[1]].location,
                    phase=Phase.TYPECHECK,
                    suggestion="Merge the rules or use an override declaration",
                )

        return self.dag
