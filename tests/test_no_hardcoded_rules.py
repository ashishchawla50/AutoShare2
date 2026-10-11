"""Guard: decision code must read every rule number from config/config.py.

On 2026-10-11 a hardcoded 7% stop in strategy/multi_timeframe.py survived the switch
to a 10% stop, and the VCP tightness gate (50) was typed in three places that ignored
config.VCP_MIN_TIGHTNESS_SCORE. All such values now live in config. This test fails
if a new numeric literal appears in the live decision path or the backtest engine.

Allowed without listing: 0, 1, -1, 2 and 100 (arithmetic, percentages, indexing).
Anything else must be in ALLOWED below with a reason. A trading threshold never is:
add it to config.py instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCANNED = [
    "main.py", "scheduler.py", "strategy", "exit", "regime", "risk", "scanner", "broker",
    "portfolio", "backtest/fast/engine.py", "backtest/fast/signals.py",
    "backtest/regime_sim.py",
]
TRIVIAL = {0, 1, -1, 2, 100}

# file -> {value: reason}. Only non-rule numbers belong here.
ALLOWED: dict[str, dict[float, str]] = {
    "main.py": {10: "ISO date slice [:10]", 6: "order tag symbol slice", 60: "log banner width",
                200: "error message truncation"},
    "strategy/multi_timeframe.py": {3: "show first 3 failing conditions in a message"},
    "strategy/stage_analysis.py": {3: "Stage enum value", 4: "Stage enum value"},
    "strategy/vcp_detector.py": {3: "round() display precision"},
    "regime/regime_detector.py": {3: "regime rank order", 4: "regime rank order",
                                  5: "regime rank order"},
    "scanner/stock_filter.py": {1e7: "rupees per crore"},
    "broker/auth_server.py": {400: "HTTP status", 403: "HTTP status", 500: "HTTP status",
                              10: "token log truncation"},
    "broker/kite_client.py": {60: "seconds per minute"},
    "broker/portfolio_manager.py": {10: "ISO date slice [:10]", 999: "sentinel: never traded"},
    "backtest/fast/engine.py": {5: "np.bincount minlength = number of verdict codes"},
    "backtest/fast/signals.py": {3: "verdict code", 4: "verdict code", 12: "hash prefix length",
                                 16: "hash prefix length"},
}


def _files() -> list[Path]:
    out: list[Path] = []
    for entry in SCANNED:
        p = REPO / entry
        out.extend([p] if p.is_file() else sorted(p.rglob("*.py")))
    return [f for f in out if "__pycache__" not in f.parts]


def test_no_hardcoded_rule_numbers():
    offenders = []
    for f in _files():
        rel = f.relative_to(REPO).as_posix()
        allowed = ALLOWED.get(rel, {})
        src = f.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Constant) and type(node.value) in (int, float)):
                continue
            v = node.value
            if v in TRIVIAL or v in allowed:
                continue
            offenders.append(f"{rel}:{node.lineno}  {v!r}  {lines[node.lineno - 1].strip()[:90]}")
    assert not offenders, (
        "Hardcoded numbers in decision code — move them to config/config.py "
        "(or, if truly not a rule, allowlist with a reason):\n" + "\n".join(offenders)
    )
