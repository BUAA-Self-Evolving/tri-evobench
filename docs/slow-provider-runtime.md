# Slow-provider L2 runtime

Use this opt-in profile for a high-concurrency model service whose responses and
transient failures routinely exceed the original short RPC deadlines. Retry only
the current model request: never restart an Interaction, tool execution, or Turn
as a transport retry. HTTP 408/409/429/5xx and connection/timeouts are retryable;
authentication, ordinary 4xx, and invalid task answers are not.

Generate a separate config directory from the existing frozen Docker configs:

```bash
uv run --frozen python tools/configure_slow_provider.py \
  --source /absolute/existing/config \
  --output /absolute/new/config-slow
```

The defaults are 600 seconds per API attempt, 20 extra retries (21 attempts),
21,600 seconds per Interaction and 14,400 seconds per evolution. Backoff is
bounded and jittered. These finite outer limits allow slow model/tool rounds and
a complete failed request retry series; they do not promise eventual success if
the service stays unavailable. Tool timeout, tool count, output-token limits,
state-freeze checks and the actual model remain unchanged. Terminal execution is
never retried automatically. SDK retries are disabled for SkillOpt/Trace2Skill;
Hermes uses its existing per-request retry loop with SDK retries disabled.
Hermes' 90-second stale-response detector is aligned with the request timeout,
including newly constructed background-review agents and restored sessions.

The config overlays set and pass `TRIEVO_SLOW_PROVIDER=1`,
`TRIEVO_API_TIMEOUT_SECONDS`, and `TRIEVO_API_MAX_RETRIES` into each Docker
adapter. Set these same variables for the host-side Judge/User Simulator client.
The script accepts explicit replacement image tags with `--hermes-image`,
`--skillopt-image`, and `--trace2skill-image`; build the patched code into those
images before launch. Existing baseline Dockerfiles support full builds. Local
incremental builds can instead extend pinned existing image IDs and replace only
the changed Python sources; record those parent IDs and recipe separately.

Run independent jobs with:

```bash
export TRIEVO_SLOW_PROVIDER=1
export TRIEVO_API_TIMEOUT_SECONDS=600
export TRIEVO_API_MAX_RETRIES=20
uv run --frozen python tools/run_new_package_matrix.py \
  --root /absolute/existing/results --level l2 \
  --l2-package /absolute/L2_Demo_V3 \
  --l2-condition reset --l2-condition frozen --l2-condition evolving \
  --config-dir /absolute/new/config-slow \
  --concurrency 10 --job-error-policy continue
```

`continue` keeps admitting independent jobs after an error. The failed job remains
`paused_error`; the final process returns nonzero if any job failed. It does not
retry the failed job. Existing complete results are reused. An interrupted
Interaction without a saved reply remains blocked by the checkpoint safety
check; it is not silently replayed to fill a slot. With `--gate`, only the gate
selection runs, and errors cannot certify a completed gate. The default error
policy remains `stop`.

This profile explicitly overrides the dataset's 900-second wall limit. Every new
Turn and run records its timing profile. Preserved old results are not rewritten;
a mixture of old and relaxed timing profiles cannot produce a formal matched
aggregate. Use a separately frozen run for comparisons requiring identical time
budgets. Longer request waiting and extra attempts can increase provider cost;
requests that time out may have been processed remotely, but no returned tool
call from a failed request is executed.
