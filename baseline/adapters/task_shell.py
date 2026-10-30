#!/usr/bin/env python3
"""Configured native shell executable for the V5 task-container transport."""
from trievo_bench.task_terminal import shell_proxy_main

if __name__ == "__main__":
    raise SystemExit(shell_proxy_main())
