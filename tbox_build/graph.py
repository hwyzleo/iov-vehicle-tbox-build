"""Dependency graph analysis for TBOX Build services.

Provides topological sorting, cycle detection, missing-dependency
detection and transitive-closure computation over the service
dependency graph declared in the service manifest.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable

from .errors import CycleError, DependencyError
from .manifest import Service, ServiceManifest


class DependencyGraph:
    """Build and analyse the service dependency graph.

    Edges: if service *A* lists *B* in ``build.dependencies``, then
    *B* must be built before *A*.  In graph terms the directed edge
    runs **B -> A** (B enables A).
    """

    def __init__(self, services: dict[str, Service] | ServiceManifest):
        if isinstance(services, ServiceManifest):
            services = services.services
        self._services: dict[str, Service] = services
        # _deps[sid] = list of service IDs that *sid* depends on
        self._deps: dict[str, list[str]] = {}
        for sid, svc in services.items():
            self._deps[sid] = list(svc.build.dependencies)

    # -- queries -----------------------------------------------------------

    def all_service_ids(self) -> list[str]:
        return list(self._services.keys())

    def dependencies(self, service_id: str) -> list[str]:
        """Direct dependencies of *service_id*."""
        return list(self._deps.get(service_id, []))

    def dependents(self, service_id: str) -> list[str]:
        """Services that depend on *service_id*."""
        return [sid for sid, deps in self._deps.items() if service_id in deps]

    def closure(self, service_ids: Iterable[str]) -> set[str]:
        """Return the transitive closure of *service_ids* (including themselves)."""
        result: set[str] = set()
        stack = list(service_ids)
        while stack:
            sid = stack.pop()
            if sid in result:
                continue
            result.add(sid)
            for dep in self._deps.get(sid, []):
                if dep not in result:
                    stack.append(dep)
        return result

    # -- analysis ----------------------------------------------------------

    def find_missing_dependencies(self) -> dict[str, list[str]]:
        """Return ``{service_id: [missing_dep, ...]}`` for deps not declared."""
        missing: dict[str, list[str]] = {}
        for sid, deps in self._deps.items():
            missing_deps = [d for d in deps if d not in self._services]
            if missing_deps:
                missing[sid] = missing_deps
        return missing

    def detect_cycles(self) -> list[list[str]]:
        """Detect cycles via DFS; return a list of cycles (each a path of IDs)."""
        WHITE, GRAY, BLACK = 0, 1, 2
        colour: dict[str, int] = {sid: WHITE for sid in self._services}
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            colour[node] = GRAY
            path.append(node)
            for dep in self._deps.get(node, []):
                if dep not in self._services:
                    continue  # missing dep, handled separately
                if colour[dep] == GRAY:
                    # Found a cycle: extract from first occurrence of dep
                    idx = path.index(dep)
                    cycles.append(path[idx:] + [dep])
                elif colour[dep] == WHITE:
                    dfs(dep, path)
            path.pop()
            colour[node] = BLACK

        for sid in sorted(self._services):
            if colour[sid] == WHITE:
                dfs(sid, [])
        return cycles

    def topological_sort(self, service_ids: Iterable[str] | None = None) -> list[str]:
        """Return services in dependency-build order (Kahn's algorithm).

        If *service_ids* is given, only the transitive closure of those
        services is sorted.  Raises :class:`CycleError` if a cycle is
        detected.
        """
        if service_ids is None:
            service_ids = list(self._services.keys())

        closure = self.closure(service_ids)

        # in_degree[sid] = number of *sid*'s dependencies that are in closure
        in_degree: dict[str, int] = {sid: 0 for sid in closure}
        forward: dict[str, list[str]] = {sid: [] for sid in closure}
        for sid in closure:
            for dep in self._deps.get(sid, []):
                if dep in closure:
                    forward[dep].append(sid)
                    in_degree[sid] += 1

        queue: deque[str] = deque(sorted(s for s in closure if in_degree[s] == 0))
        result: list[str] = []
        while queue:
            sid = queue.popleft()
            result.append(sid)
            for dependent in sorted(forward[sid]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(closure):
            remaining = closure - set(result)
            cycles = self._find_cycles_in(remaining)
            raise CycleError(
                f"Dependency cycle detected involving: {sorted(remaining)}",
                cycles,
            )
        return result

    def _find_cycles_in(self, subset: set[str]) -> list[list[str]]:
        """Detect cycles restricted to *subset*."""
        WHITE, GRAY, BLACK = 0, 1, 2
        colour: dict[str, int] = {sid: WHITE for sid in subset}
        cycles: list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            colour[node] = GRAY
            path.append(node)
            for dep in self._deps.get(node, []):
                if dep not in subset:
                    continue
                if colour[dep] == GRAY:
                    idx = path.index(dep)
                    cycles.append(path[idx:] + [dep])
                elif colour[dep] == WHITE:
                    dfs(dep, path)
            path.pop()
            colour[node] = BLACK

        for sid in sorted(subset):
            if colour[sid] == WHITE:
                dfs(sid, [])
        return cycles

    def validate(self) -> None:
        """Run all graph-level validations.

        Raises :class:`DependencyError` for missing dependencies and
        :class:`CycleError` for cycles.
        """
        missing = self.find_missing_dependencies()
        if missing:
            details = [f"{sid} -> {deps}" for sid, deps in missing.items()]
            raise DependencyError(
                f"Missing dependencies detected ({len(missing)} service(s))",
                details,  # type: ignore[arg-type]
            )
        cycles = self.detect_cycles()
        if cycles:
            raise CycleError(
                f"Dependency cycles detected ({len(cycles)} cycle(s))",
                cycles,
            )

    def build_order(self, service_ids: Iterable[str] | None = None) -> list[str]:
        """Convenience: validate then topological_sort."""
        self.validate()
        return self.topological_sort(service_ids)
