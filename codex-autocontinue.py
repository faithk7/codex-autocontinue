#!/usr/bin/env python3
"""Path-stable entry for launchd, systemd, Task Scheduler, and the wrappers.

Logic lives in src/watcher.py (daemon) and src/cli.py (subcommands). This
filename must not change: services ExecStart it by absolute path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from watcher import main

if __name__ == "__main__":
    raise SystemExit(main())
