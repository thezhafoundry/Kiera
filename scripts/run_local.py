"""Run Keira's tokenless local development server on loopback only."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)
    sys.path.insert(0, str(project_root))
    host = os.getenv("KEIRA_LOCAL_BIND_HOST", "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS:
        raise SystemExit("Local development server must bind to a loopback host")
    port = int(os.getenv("KEIRA_LOCAL_PORT", "8000"))
    os.environ["KEIRA_LOCAL_BIND_HOST"] = host
    os.environ["KEIRA_LOCAL_LAUNCHER"] = "1"
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
