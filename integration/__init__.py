"""Anvil 内置记忆、视觉与工单运行时。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for component in ("hr", "vision"):
    path = str(ROOT / "vendor" / component)
    if path not in sys.path:
        sys.path.insert(0, path)
