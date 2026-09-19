# L2 Demo V4 panel contract

The V3 Task and Stream documents remain `l2_taskstream/1.0`. V4 adds a package-level `l2_taskstream/1.1` manifest so selection and aggregation can distinguish two non-independent views of the same 290 parent Turns.

| Panel | Purpose | Streams | Turn placements |
|---|---|---:|---:|
| `short_stream_main_panel` | Primary matched matrix | 24 shards | 290 |
| `parent_stream_diagnostic` | Long-stream diagnostic | 6 parents | 290 |

A launcher must select exactly one `panel_id`, verify the `panels.json` SHA-256 bound by the root manifest, and carry both values into reports and checkpoints. Aggregation must reject records with missing, partial, or mixed panel bindings. The two panels may be reported side by side but their denominators and overall means must never be combined.

`evolution_reference.py` and `validate_contract.py` preserve the V3 base-contract fixtures. V4 panel-aware runs use `evolution_panel_reference.py` and profile `l2_evolution_aggregation/1.1`; its default entrypoint rejects records without a panel binding.
