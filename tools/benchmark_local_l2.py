"""Bounded local Docker numerical throughput probe; no model or benchmark reruns."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid


WORK = """
import json,time,numpy as np
from threadpoolctl import threadpool_info
start=time.monotonic()
rng=np.random.default_rng(17)
a=rng.normal(size=(2300,2300))
checksum=0.
for i in range(120):
    b=a@a.T
    checksum+=float(b[0,0])
print(json.dumps({'seconds':time.monotonic()-start,'checksum':checksum,'threads':threadpool_info()}))
"""


def benchmark(image: str, output: Path) -> dict:
    if output.exists():
        raise ValueError("choose a new benchmark evidence file")
    docker = shutil.which("docker") or str(Path.home() / ".local/share/trievo-container-tools/bin/docker")
    info = json.loads(subprocess.check_output([docker, "info", "--format", "{{json .}}"], text=True))
    results = []
    def run(index):
        name = "trievo-perf-" + uuid.uuid4().hex[:12]
        try:
            command = [docker, "run", "--rm", "--name", name, "--network", "none", "--cpus", "4",
                       "--memory", "2g", "--pids-limit", "128", "--entrypoint", "python3"]
            for key in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"]:
                command += ["-e", key + "=4"]
            process = subprocess.run([*command, image, "-c", WORK], text=True,
                                     capture_output=True, timeout=120)
            if process.returncode:
                raise RuntimeError("local numerical probe failed, code " + str(process.returncode))
            return json.loads(process.stdout)
        finally:
            subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(-1)  # Warm the image and BLAS before comparing concurrency levels.
    for concurrency in [1, 2, 3, 4]:
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            jobs = list(pool.map(run, range(concurrency)))
        elapsed = time.monotonic() - start
        results.append({"workers": concurrency, "elapsed_seconds": elapsed,
                        "jobs_per_hour": 3600 * concurrency / elapsed, "jobs": jobs})
    result = {"image": image, "real_api_calls": 0, "task_cpus": 4, "task_memory_gib": 2,
              "hardware": {"docker_cpus": info["NCPU"], "docker_memory_bytes": info["MemTotal"]},
              "results": results, "limit": "Numerical saturation probe only. Does not reproduce all task distributions, model latency, peak memory or full-matrix speedup."}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark(args.image, args.output)
    print(json.dumps([{k:v for k,v in row.items() if k != "jobs"} for row in result["results"]], indent=2))
