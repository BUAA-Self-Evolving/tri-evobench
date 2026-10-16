# Native task environments (r3)

New pool records opt in with `runtime.environment_version: l2_environment/1.0`.
The composer hashes their full execution profile and rejects conflicting profile
IDs. Legacy records keep their old profile IDs and launch behavior.

File tasks also carry `runtime.native_environment` with schema version
`l2_native_environment/1.0`, native `cwd`, `python_executable`, idle shell
`entrypoint`, original image `user`, and `path_mappings`. Each mapping has
`native_path` and `workspace_relative_path`, the latter relative to the current
`/workspace/tasks/<turn_id>`. Directory targets must be materialized in the seed;
file outputs can initially be absent. Whole home directories, interpreter/tool
installations, private evaluation roots and Workspace itself cannot be aliased.

New profiles require enforced cgroup limits: two CPUs, 4 GiB RAM and 256 PIDs.
The launcher rejects a daemon with no cgroup driver before starting work, then
checks the actual container controller files and ancestor limits. Docker
HostConfig values alone are not proof of enforcement. These checks also run
before new-profile private verification and source qualification. A rootless
daemon without cgroups cannot qualify or execute new profiles; legacy profiles
retain their historical behavior.

Startup checks immutable image identity and offline state, installs only declared
aliases, and reads a generated marker back with `docker cp`. A Docker endpoint
that reports successful `exec` without performing it fails startup. Use a Docker
endpoint supporting real exec and named volumes; no machine-specific endpoint is
hardcoded by the runtime. Only Workspace files and baseline state persist between
Turns. Frozen retains files but disables evolution; reset reseeds each Turn.

New profiles require the baseline to advertise `native_task_execution`. Hermes
and SkillOpt use a scoped reverse NDJSON request handled by the host, which binds
each command to the current task container. They receive no Docker socket.
Unsupported adapters fail explicitly for new profiles. Legacy profiles do not
require this capability. Native evaluator containers receive a private Workspace
snapshot, so verifier writes never change the live Workspace.

The native builder preserves upstream Docker builds and test runner bodies,
records build-time setup adaptations and image configuration, strips skills and
private content from the flattened distributable filesystem, and uses a fresh
container for each fixture. An infrastructure error is not a meaningful red.
Private oracle helpers are only permitted by an explicit task adaptation; never
copy them into workspace seeds or gold output fixtures. All runtime task inputs,
private verifier assets and provenance must be materialized before admission.

Verification commands (set paths in your environment):

```bash
uv run --frozen python -m pytest tests -q
TRIEVO_TEST_NATIVE_DOCKER=1 uv run --frozen python -m pytest tests/test_native_environment.py tests/test_native_runner_integration.py tests/test_native_package_qualification.py -q
uv run --frozen python tools/l2_v5_r3_handoff.py --old-pool "$OLD_POOL" --new-pool "$NEW_POOL" --r2-package "$R2_PACKAGE" --out "$ARTIFACTS"
```

Positive Docker integration tests require a daemon with working cgroup resource
enforcement. Unit tests use synthetic controller trees for both valid and
invalid limits; they do not replace real enforcement evidence.

Docker tests require the existing `trievo-l2-v5:0.6-native` and
`trievo-l2-v5:0.7-native` fixture images, create
small local test derivatives and clean their containers/volumes. They exercise
image switching in all conditions, Workspace checkpoint restoration, non-root
users, private snapshot isolation and negative controls. They do not establish
native-source task admission or model score improvements. The actual runner
tests cover both legacy and new profiles through the process adapter protocol,
with no model calls. Versioned package qualification uses container copies and
fresh containers; it does not require the daemon to see host bind paths.

Offline delivery uses exact saved image configuration IDs, not a re-import of a
filesystem tar with a new timestamp. The native builder maintains its own safe
image archives. Export the admitted SRA/tau images separately with:

```bash
uv run --frozen python tools/l2_v5_export_images.py --pool "$NEW_POOL" --family sra --family tau2 --out "$IMAGE_ARCHIVES"
```

`IMAGE_ARCHIVES.json` records the archive SHA-256, configuration, layers and
restore command. Archive configuration bytes are checked against the declared
image ID. Rejected candidates and private intermediate images are not exported.
For new native profiles, rebuild the Hermes bridge with
`baseline/adapters/hermes/Dockerfile.native-runtime` over a verified existing
Hermes base and select its new digest via `baseline_overrides.options.image`.
Do not overwrite the r2 image tag or change legacy manifests.
