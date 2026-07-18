"""Fleet scenario runner: launches the full demo fleet as threads.

Usage:  python demo/run_demo.py            # all five agents
        python demo/run_demo.py looper spender   # subset
"""
from __future__ import annotations

import sys
import threading
import time

from agents import AGENTS

STAGGER = 2.0  # seconds between launches so the dashboard tells a story


def main() -> None:
    names = sys.argv[1:] or ["healthy", "looper", "spender", "violator", "wanderer"]
    threads = []
    for n in names:
        fn = AGENTS[n]
        t = threading.Thread(target=fn, name=n, daemon=True)
        t.start()
        print(f"launched: {n}")
        threads.append(t)
        time.sleep(STAGGER)
    for t in threads:
        t.join(timeout=300)
    print("demo complete")


if __name__ == "__main__":
    main()
