# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/registry.py
────────────────────────────
The Capability Registry — the heart of the plugin system.

It maps:  format  →  operation  →  [Capability backends, ordered by priority].

Plugins call `register(...)` to add their capabilities. The router queries
`resolve(format, operation)` to get the ordered backend list to try. Adding a
new format/operation is purely additive — no core file changes.
"""

from __future__ import annotations

from collections import defaultdict

from agent2.fileintel.base import Capability
from agent2.fileintel.detector import FORMAT_CATEGORY, normalize_format


class CapabilityRegistry:
    """Holds every plugin's declared capabilities.

    Deliberately a plain object (not a global) so it can be dependency-injected
    and so tests can build isolated registries with fake plugins.
    """

    def __init__(self) -> None:
        # format -> operation -> list[Capability]
        self._caps: dict[str, dict[str, list[Capability]]] = defaultdict(lambda: defaultdict(list))
        self._plugins: list[str] = []

    # ── Registration ─────────────────────────────────────────────────────────
    def register(self, plugin_name: str, formats: list[str],
                 capabilities: list[Capability]) -> None:
        """Register one plugin's capabilities for a set of formats."""
        if plugin_name not in self._plugins:
            self._plugins.append(plugin_name)
        for fmt in formats:
            fmt = normalize_format(fmt)
            for cap in capabilities:
                self._caps[fmt][cap.operation].append(cap)
                # keep each op's backends ordered: lowest priority first
                self._caps[fmt][cap.operation].sort(key=lambda c: c.priority)

    # ── Queries ──────────────────────────────────────────────────────────────
    def resolve(self, fmt: str, operation: str) -> list[Capability]:
        """Ordered backend list for (format, operation); [] if none."""
        return list(self._caps.get(normalize_format(fmt), {}).get(operation, []))

    def operations_for(self, fmt: str) -> list[str]:
        """All operations available for a format."""
        return sorted(self._caps.get(normalize_format(fmt), {}).keys())

    def capabilities_for(self, fmt: str) -> dict[str, list[str]]:
        """operation -> [backend labels] for a format (for detect_file output)."""
        fmt = normalize_format(fmt)
        return {op: [c.backend for c in caps]
                for op, caps in self._caps.get(fmt, {}).items()}

    def formats_in_category(self, category: str) -> list[str]:
        return sorted(f for f, c in FORMAT_CATEGORY.items()
                      if c == category and f in self._caps)

    def known_formats(self) -> list[str]:
        return sorted(self._caps.keys())

    def summary(self) -> dict:
        """A compact overview used by the import-check verification step."""
        return {
            "plugins": list(self._plugins),
            "formats": len(self._caps),
            "operations": sum(len(ops) for ops in self._caps.values()),
            "by_format": {f: sorted(ops) for f, ops in sorted(self._caps.items())},
        }
