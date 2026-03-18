from __future__ import annotations

VALID_STRATEGIES = ["plain", "bluegreen"]

def is_valid_strategy(s: str) -> bool:
    return s in VALID_STRATEGIES
