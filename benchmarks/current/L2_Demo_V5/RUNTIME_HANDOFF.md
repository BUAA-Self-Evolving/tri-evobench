## Runtime handoff

What the runtime has to do to run L2 Demo V5, and what the current runtime
(`trievo_bench/l2_agent.py` on branch `claude/l2-v5-rebuild`) already does.

### Required behavior

1. Select one pool and its panel explicitly (`--pool-id`, `--panel-id`) and
   verify the manifest, pool, panel, and runtime-profile SHA-256 bindings before
   the run. Never average across pools.
2. Keep one Workspace for the whole Stream. Before each Turn, copy the Turn's
   `workspace_seed/` to `agent_workspace/tasks/<turn_id>/` and keep all earlier
   files (`continue_with_patch`). Implemented: `workspace_persistence: stream`
   selects this path, and the validator rejects `task_local_seed` transitions in
   a stream-persistent package.
3. Run the native verifier before the Judge, in the profile's
   `verifier_image_digest`, with network disabled. For workspace submissions the
   verifier binding is the Turn's task directory
   (`agent_workspace/tasks/<turn_id>`); the verifier resolves the declared
   artifact inside it. A verifier error is an evaluation error.
4. Resolve Judge rubric routes relative to the package root
   (`prompts/rubrics/*.md`).
5. Run `requires_gpu: true` profiles only with GPU passthrough. The current
   runtime starts task containers without GPU devices and with a 4 GB / 2 CPU
   limit, so `auto_research_cpu_gpu_interleaved_01` is not runnable yet and must
   be reported as not run rather than scored on CPU. PHAI's rootless Docker does
   expose GPUs: `docker run --gpus device=0 trievo-l2-v5-ar-gpu:0.1` sees an
   A100 from both torch and JAX.
6. The explicit-dependency Stream needs the persistent Workspace: stage-2 Turns
   read their inputs from the stage-1 Turn directory. Run it only under
   `evolving` or `frozen`; under `reset` its stage-2 Turns have no inputs.

### Turn budgets

`runtime_profiles.json` sets `turn_timeout_seconds` per profile from the
upstream limit (SR-Agents 900 s; SkillLearnBench/SkillsBench the upstream agent
timeout, 600–7200 s; tau2 1800 s; AutoResearch the upstream training timeout,
1800 or 3600 s). Native verifier timeouts are in each Turn's
`evaluation.native_verifier.timeout_seconds`. AutoResearch timeouts assume the
upstream compute budget; with the current 2-CPU limit, the CPU tasks are
feasible but slower than upstream intended.

### Docker on PHAI

Use the rootless daemon socket for runtime runs
(`DOCKER_HOST=unix:///run/user/1000/docker.sock`) with
`TRIEVO_DOCKER_PROXY_BRIDGE=off` and `TRIEVO_DOCKER_VOLUME_TRANSPORT=required`.
The proxy socket at `/var/run/docker.sock` accepts host bind mounts from
`/data_storage` but drops `docker exec` and container stdout, and it rejects the
`-v <named volume>` mounts that the Hermes, opencode, and prime-agent baseline
manifests use. Offline tooling that only needs bind mounts (for example
`tools/l2_v5_qualify_package.py`) can use the proxy socket and reads results
from files.

### Known runtime cost

tau2 Turns are token-heavy. The agent reads the policy, database, and tool
documentation from the Workspace and writes a full trajectory; a DeepSeek smoke
of `intra_tau2_interleaved_01` used about 3.4M tokens in two Turns before
hitting a 3M budget. Plan budgets for tau2 Streams accordingly.
