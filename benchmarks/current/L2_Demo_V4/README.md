# L2 Demo V4 - diversified independent Streams

This package preserves all six frozen L2 Demo V3 parent Streams and adds 24 independent 10/20-Turn Streams. The short-stream main panel and parent diagnostic panel must be reported separately because they reuse the same 290 parent Turns. There are 30 Streams and 580 Turn placements, not 580 unique source tasks.

Each shard starts from its own copied blank `agent_workspace`, carries all task and private verifier assets, and has no runtime dependency on its parent. See `shard_design.json`, each shard's `selection.json`, and `panels.json`.

Validate after fresh extraction with `python validate_pilot.py`. Add `--native-smoke` for all red/green fixtures.
