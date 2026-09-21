#!/usr/bin/env python3
"""Path-stable entry for launchd, systemd, Task Scheduler, and the wrappers.

Logic lives in watcher.py (daemon) and cli.py (subcommands). This filename
must not change: services ExecStart it by absolute path.
"""

from __future__ import annotations

from watcher import main

if __name__ == "__main__":
    raise SystemExit(main())
