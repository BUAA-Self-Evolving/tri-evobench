"""Use the pinned project's documented sequential CLI build order."""
import subprocess

for package in ["shared-types", "contracts", "dynamic-workflow", "dynamic-workflow-runtime",
                "core", "adapters", "i18n", "telemetry", "bootstrap", "cli"]:
    print("Building native package", package, flush=True)
    subprocess.run(["pnpm", "--filter", "@zcode/" + package, "build"], check=True)
