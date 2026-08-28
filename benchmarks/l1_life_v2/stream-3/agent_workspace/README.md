# Agent workspace for l1_life_stream_3

This is the single persistent writable workspace shared by all 60 turns in this stream. Copy `state/seed_state.json` and `tool_state/seed_state.json` to their `current_state.json` counterparts at the start of a fresh stream run. Evolving/Raw-Replay/Oracle conditions carry current state forward; Reset/Frozen conditions follow their declared reset or write-blocking policy. `facts.json` outside this workspace is sealed evaluator truth and must never be copied into Agent-visible state.
