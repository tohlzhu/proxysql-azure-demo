#!/usr/bin/env python3
"""Run tests with the task's local Redis when its environment is supplied."""

import os
import subprocess
import sys
from urllib.parse import quote

if __name__ == "__main__":
    if "TEST_REDIS_URL" not in os.environ and os.environ.get("REDIS_PASSWORD"):
        password = quote(os.environ["REDIS_PASSWORD"], safe="")
        port = int(os.getenv("REDIS_PORT", "16379"))
        os.environ["TEST_REDIS_URL"] = f"redis://:{password}@127.0.0.1:{port}/15"
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "pytest", "-c", "pytest.ini", *sys.argv[1:]])
    )
