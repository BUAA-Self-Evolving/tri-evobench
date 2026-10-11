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

### Live tau2 sessions (from the next package build)

Profiles with a `live_session` block (`tau2_live_cpu_1800s_v5`) need, per
Interaction (implemented in `trievo_bench/tau2_live/launcher.py`):

1. Start a session container from the profile's task image with the Stream
   Workspace volume at `/workspace` and default networking. Pass
   `DEEPSEEK_API_KEY` (and any proxy variables) by name only. Copy the Turn's
   `evaluation_assets/tau2_task.json` into the container, never into the
   Workspace, and run `python -m tau2_session.service`. It is ready when
   `/workspace/.tau2/session.sock` exists.
2. Run the agent. Its own sandbox keeps `network: disabled`; it reaches the
   conversation only through `/workspace/.tau2/tau2.py`.
3. After the agent returns, before syncing the Workspace back, run
   `python -m tau2_session.ctl finish` in the container, copy
   `/out/record.json` out and remove the container. The service deletes
   `/workspace/.tau2`. Pass the record inline to the native verifier for the
   `interaction_record` / `tau2_live_session` binding, and keep it in the
   checkpoint evidence (`tau2_live_record`) so a resume can re-verify it.
4. A missing record or a session that failed for infrastructure reasons is an
   evaluation error.

The agent needs one terminal command per move: the profile sets
`max_tool_calls: 60`, and the baseline's own `l2_agent_limits` must allow that
(the smoke used `max_tool_calls` 60, `max_model_calls` 80,
`episode_timeout_seconds` 1800).

### Known runtime cost

tau2 Turns are token-heavy. Live, the agent's context grows with the
conversation; a DeepSeek smoke of `intra_tau2_interleaved_01` turn1–turn2 used
3.71M tokens (mostly cache reads) for two passing Turns. The user simulator
re-reads the conversation on every reply (260k prompt tokens on the telecom
Turn).
