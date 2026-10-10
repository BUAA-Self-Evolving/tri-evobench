# L2 V5 offline images

| Dockerfile | Tag | Used by |
|---|---|---|
| `Dockerfile.native-0.7` | `trievo-l2-v5:0.7-native` | SR-Agents, SkillLearnBench, SkillsBench task and verifier |
| `Dockerfile.tau2` | `trievo-l2-v5-tau2:0.1` | tau2 task and verifier (upstream tau2 installed for replay) |
| `Dockerfile.autoresearch-cpu` | `trievo-l2-v5-ar-cpu:0.1` | AutoResearch CPU tasks; verifier for all AutoResearch tasks |
| `Dockerfile.autoresearch-gpu` | `trievo-l2-v5-ar-gpu:0.1` | AutoResearch GPU task sandbox (built on the CPU image) |

The images form one chain; build them in order: `Dockerfile`
(`trievo-l2-v5:0.4`, the September candidate's image) → `Dockerfile.native`
(`0.5-native`) → `Dockerfile.native-0.6` (`0.6-native`) → `Dockerfile.native-0.7`
and `Dockerfile.autoresearch-cpu` → `Dockerfile.autoresearch-gpu`.
`Dockerfile.tau2` builds on `0.4`.

Build an image, then record its immutable ID in the pool records and
`runtime_profiles.json`:

```bash
docker build -t trievo-l2-v5:0.7-native -f sandbox/l2-v5/Dockerfile.native-0.7 sandbox/l2-v5
docker image inspect trievo-l2-v5:0.7-native --format '{{.Id}}'
```

On PHAI, pass the Tsinghua PyPI mirror with
`--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`. The image
IDs pinned by L2 Demo V5 are listed in `benchmarks/current/L2_Demo_V5/README.md`.

The images contain only runtime and verifier dependencies. Source-task assets,
private expected outputs, and learner state stay package- or run-scoped. All
task and verifier containers run with Docker network mode `none`.
