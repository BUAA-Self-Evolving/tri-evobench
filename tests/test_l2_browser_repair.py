from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "tools" / "repair_l2_browser_oracles.py"
SPEC = importlib.util.spec_from_file_location("repair_l2_browser_oracles", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
repair_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair_tool)


class L2BrowserRepairTest(unittest.TestCase):
    def test_canonical_browser_verifiers_ignore_ephemeral_cleanup_races(self) -> None:
        repo = Path(__file__).parents[1]
        for stream in repair_tool.STREAMS:
            for verifier in (repo / "benchmarks/l2_demo/streams" / stream / "episodes").glob(
                "*/evaluation_assets/verifier.py"
            ):
                self.assertIn("ignore_cleanup_errors=True", verifier.read_text(encoding="utf-8"))

    def test_current_l2_package_inspects_as_taskstream(self) -> None:
        repo = Path(__file__).parents[1]
        package = repo / "benchmarks/current/L2_Demo_V4"
        from trievo_bench.l2_compat import inspect_l2_package

        inventory = inspect_l2_package(package)
        self.assertEqual(len(inventory), 30)
        self.assertEqual(sum(item.episode_count for item in inventory), 580)

    def test_materialized_native_timeout_is_a_scored_task_failure(self) -> None:
        assets = (
            Path(__file__).parents[1]
            / "benchmarks/current/L2_Demo_V4/streams/skillsbench_data_to_d3_001"
            / "turn1/evaluation_assets"
        )
        previous_native = sys.modules.pop("native_semantics", None)
        sys.path.insert(0, str(assets))
        try:
            spec = importlib.util.spec_from_file_location(
                "materialized_skillsbench_verifier_cli", assets / "verifier_cli.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.normalize_result(
                "browser-verifier",
                SimpleNamespace(category="timeout", score=0.0, details={"duration_ms": 300000}),
                {"evaluation": {"evaluation_profile": "general"}},
            )
        finally:
            sys.path.pop(0)
            sys.modules.pop("native_semantics", None)
            if previous_native is not None:
                sys.modules["native_semantics"] = previous_native
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["score"], 0.0)
        self.assertIsNone(result["error"])

    def _package(self, root: Path) -> None:
        oracle = repair_tool.ORACLE_REPLACEMENTS[0][0]
        verifier = "\n".join(old for old, _ in repair_tool.VERIFIER_REPLACEMENTS)
        for stream in repair_tool.STREAMS:
            assets = root / "streams" / stream / "episodes" / "E001" / "evaluation_assets"
            assets.mkdir(parents=True)
            (assets / "oracle_solution.sh").write_text(oracle, encoding="utf-8")
            (assets / "native_test_outputs.py").write_text(verifier, encoding="utf-8")

    def test_repair_updates_all_browser_streams_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            self._package(package)

            changed = repair_tool.repair(package, check=False)
            self.assertEqual(len(changed), 4)
            self.assertEqual(repair_tool.repair(package, check=True), [])

            for path in changed:
                text = path.read_text(encoding="utf-8")
                replacements = (
                    repair_tool.ORACLE_REPLACEMENTS
                    if path.name == "oracle_solution.sh"
                    else repair_tool.VERIFIER_REPLACEMENTS
                )
                for _, new in replacements:
                    self.assertIn(new, text)
