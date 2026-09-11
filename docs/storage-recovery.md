# L2 shared-storage pause and recovery

The matrix now supports a cooperative storage gate. It is separate from SETA's
build-cache preflight and Recuris' training-process guard: none of these is a
kernel quota or a global interception of every Docker write.

Use the same artifact root and frozen slow-provider configs:

```bash
export TRIEVO_STORAGE_GUARD=1
export TRIEVO_STORAGE_STATE_ROOT=/absolute/matrix/_storage
export TRIEVO_STORAGE_PATH=/ebs/docker  # must match this daemon's DockerRootDir
export TRIEVO_STORAGE_MIN_FREE_GIB=20
export TRIEVO_STORAGE_RESUME_FREE_GIB=25
export TRIEVO_STORAGE_RECOVERY_SECONDS=30
export TRIEVO_STORAGE_INTERVAL=2
uv run --frozen python tools/run_new_package_matrix.py \
  --root /absolute/matrix --level l2 --l2-package /absolute/L2_Demo_V3 \
  --l2-condition reset --l2-condition frozen --l2-condition evolving \
  --config-dir /absolute/config-slow-provider \
  --concurrency 10 --job-error-policy continue \
  --storage-guard --recover-storage-errors --storage-retry-limit 5
```

Keep the existing model credentials and slow-provider timeout/retry exports from
the original launcher. No baseline image rebuild is needed for this host-side
storage change. `--storage-guard` fails closed if the guard cannot initialize;
it never silently runs without the requested protection.

## Admission and ongoing work

The dispatcher samples health even with all ten workers occupied. Below the
20 GiB floor, below 10,000 free inodes, or on a reported storage/overlay fault,
it stops admitting jobs and persists `paused_storage`. Pending and deferred
jobs remain queued. Recovery requires >=25 GiB, stable health for 30 seconds,
and a real Docker create/cp/read-back probe. Each probe removes only its own
fresh exact container; no global prune, daemon restart, or external cleanup.

At least ten safely runnable jobs plus healthy storage means the dispatcher
refills to ten. Low storage can deliberately leave slots vacant. At the end of
the matrix, fewer than ten remaining independent jobs cannot legitimately
occupy ten slots without duplicating work.

Workers wait before the next Interaction/evolution and volume synchronization;
they are not killed mid-tool-call. An already executing arbitrary tool may still
write before it reaches a safe boundary: polling is not a filesystem write ban.
The gate does not stop other workloads sharing the Docker filesystem.

Volume push/pull storage failures retry the same copy in the original process,
with fresh staging on pull and the prior complete host workspace preserved.
Health waiting does not consume a copy attempt; five real copy failures exhaust
the local budget. Cleanup does not wait on the gate. A received model reply is
saved before any Docker copy, but `reply_received` without a synchronized
workspace is explicitly not a safe resume checkpoint.

## Recovery queue

`--recover-storage-errors` reclassifies old storage failures only after checking
their original logs, job identity and complete checkpoint history:

- Valid saved adjudication/Turn checkpoint: resume that safe state.
- No checkpoint history and a traceback proving failure during initial volume
  setup before Agent execution: restart environment initialization.
- Any unsafe or corrupt checkpoint: `blocked_recovery`; never fall back to a
  clean restart. A valid zero-score answer is not rerun to improve its score.
- Other errors (authentication, Judge empty content, protocol errors) are not
  automatically converted to storage retries.

Per-job retry counts and attempt history survive dispatcher restarts. A job that
recovers and completes no longer counts as failed. Five deferred recovery
attempts exhaust the job budget and produce `needs_attention`. Verified surviving
workers from the same output root are adopted on full parallel dispatcher resume,
not duplicated. The exclusive output lock remains required.

## Inspectable state

`matrix_status.json` records target/active concurrency, pending/deferred IDs,
blocked jobs, storage health, attempt history and retry counters. The gate keeps
`_storage/health.json` and transition-only `_storage/events.jsonl`. These are
operational states, not Agent scores. An incomplete matrix still exits nonzero;
healthy storage and ten processes do not certify benchmark or learning success.

This change does not fix the separately identified Trace2Skill Action versus
TASK_COMPLETE protocol conflict. That remains a distinct qualification issue.
