# L2 V5 offline task image

Build this image during source qualification, then record the immutable image
ID returned by `docker image inspect` in `runtime_profiles.json`:

```bash
docker build -t trievo-l2-v5:0.1 -f sandbox/l2-v5/Dockerfile .
docker image inspect trievo-l2-v5:0.1 --format '{{.Id}}'
```

The image contains only runtime and verifier dependencies. Source-task assets,
private expected outputs, and learner state remain package- or run-scoped. All
formal task and verifier containers run with Docker network mode `none`.
