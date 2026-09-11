from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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

    def test_materialized_browser_verifiers_and_receipt_are_current(self) -> None:
        repo = Path(__file__).parents[1]
        package = repo / "benchmarks/current/L2_Demo_V2"
        receipt = package / "runtime-repair.json"
        from tools.repair_l2_runtime_contracts import REPAIR_VERSION, repair

        self.assertEqual(json.loads(receipt.read_text())["repair_version"], REPAIR_VERSION)
        self.assertEqual(repair(package, check=True)["repair_version"], REPAIR_VERSION)

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
