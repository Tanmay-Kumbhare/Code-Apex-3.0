"""
ARL Dependency DAG and Cycle Detection.

Builds dependency graph of rules and includes, performs cycle detection (E2009, E3004),
validates hard-to-soft rule references (E3005), and provides topological sorting.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from arl.ast import RequireEffect, Rule, RuleRef, Ruleset
from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error


class RuleDAG:
    """Dependency graph for rules within one or more rulesets."""

    def __init__(self, collector: Optional[ErrorCollector] = None) -> None:
        self.collector = collector or ErrorCollector()
        self.rules: Dict[str, Rule] = {}
        self.dependencies: Dict[str, Set[str]] = {}  # from_rule -> set of required_rules

    def add_rule(self, rule: Rule) -> None:
        self.rules[rule.name] = rule
        if rule.name not in self.dependencies:
            self.dependencies[rule.name] = set()

        for effect in rule.effects:
            if isinstance(effect, RequireEffect) and isinstance(effect.target, RuleRef):
                ref_name = effect.target.rule_name
                self.dependencies[rule.name].add(ref_name)

    def build_from_ruleset(self, ruleset: Ruleset) -> None:
        """Populate DAG from a ruleset."""
        for rule in ruleset.rules:
            self.add_rule(rule)

    @classmethod
    def from_ruleset(cls, ruleset: Ruleset, collector: Optional[ErrorCollector] = None) -> RuleDAG:
        dag = cls(collector=collector)
        dag.build_from_ruleset(ruleset)
        return dag

    def detect_cycles(self) -> List[List[str]]:
        """Detect all cycles using DFS. Emits E2009 for each cycle."""
        visited: Dict[str, int] = {}  # 0: unvisited, 1: visiting, 2: visited
        cycles: List[List[str]] = []
        path: List[str] = []

        def dfs(node: str) -> None:
            visited[node] = 1
            path.append(node)

            for neighbor in self.dependencies.get(node, []):
                if visited.get(neighbor, 0) == 1:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)
                    self.collector.add_error(
                        code="E2009",
                        message=f"Cyclic rule reference detected: {' -> '.join(cycle)}",
                        rule_id=cycle[0],
                        location=self.rules[cycle[0]].location if cycle[0] in self.rules else None,
                        phase=Phase.DAG_BUILD,
                    )
                elif visited.get(neighbor, 0) == 0:
                    dfs(neighbor)

            path.pop()
            visited[node] = 2

        for r_name in list(self.dependencies.keys()):
            if visited.get(r_name, 0) == 0:
                dfs(r_name)

        return cycles

    def check_hard_soft_invariants(self) -> None:
        """
        E3005: Hard rule references soft-only rule.
        A hard rule cannot require a rule declared with priority: soft.
        """
        for r_name, rule in self.rules.items():
            is_hard = (rule.meta.priority or "hard") == "hard"
            if not is_hard:
                continue

            for req_name in self.dependencies.get(r_name, []):
                target_rule = self.rules.get(req_name)
                if target_rule:
                    target_priority = target_rule.meta.priority or "hard"
                    if target_priority == "soft":
                        self.collector.add_error(
                            code="E3005",
                            message=f"Hard rule '{r_name}' cannot require soft-only rule '{req_name}'",
                            rule_id=r_name,
                            location=rule.location,
                            phase=Phase.TYPECHECK,
                            suggestion=f"Change rule '{req_name}' priority to 'hard' or make '{r_name}' soft",
                        )

    def topological_order(self) -> List[str]:
        """
        Return rules in topological order (dependencies first).
        If A requires B, B is ordered before A.
        """
        dependents_of: Dict[str, Set[str]] = {k: set() for k in self.rules}
        in_degree: Dict[str, int] = {k: 0 for k in self.rules}

        for u, reqs in self.dependencies.items():
            valid_reqs = {r for r in reqs if r in self.rules}
            in_degree[u] = len(valid_reqs)
            for v in valid_reqs:
                dependents_of[v].add(u)

        queue = [u for u, deg in in_degree.items() if deg == 0]
        order: List[str] = []

        while queue:
            queue.sort()  # Deterministic ordering
            u = queue.pop(0)
            order.append(u)

            for dependent in dependents_of.get(u, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        return order


def detect_include_cycles(
    include_graph: Dict[str, List[str]],
    collector: Optional[ErrorCollector] = None,
) -> List[List[str]]:
    """
    Detect include cycles across files (E3004).
    include_graph: file_path -> [included_file_paths]
    """
    col = collector or ErrorCollector()
    visited: Dict[str, int] = {}
    cycles: List[List[str]] = []
    path: List[str] = []

    def dfs(node: str) -> None:
        visited[node] = 1
        path.append(node)

        for neighbor in include_graph.get(node, []):
            if visited.get(neighbor, 0) == 1:
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                cycles.append(cycle)
                col.add_error(
                    code="E3004",
                    message=f"Include cycle detected: {' -> '.join(cycle)}",
                    phase=Phase.RESOLVE,
                )
            elif visited.get(neighbor, 0) == 0:
                dfs(neighbor)

        path.pop()
        visited[node] = 2

    for fpath in list(include_graph.keys()):
        if visited.get(fpath, 0) == 0:
            dfs(fpath)

    return cycles
