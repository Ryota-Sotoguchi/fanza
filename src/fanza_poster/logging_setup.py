from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path


def setup_logging(log_dir: Path, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{date.today():%Y-%m-%d}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(sh)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
