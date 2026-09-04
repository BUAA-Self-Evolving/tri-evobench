import hashlib
import json
import unittest
from pathlib import Path


L2_ROOT = Path(__file__).resolve().parents[2]
STREAM_ROOT = (
    L2_ROOT / "streams" / "skilllearn_stock_data_visualization_001"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sorted_hash_manifest(directory: Path) -> str:
    manifest = "".join(
        f"{sha256(path)}  {path.name}\n"
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file()
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


class SkillLearnSourceIntegrityTests(unittest.TestCase):
    def test_every_native_sibling_matches_its_integrity_manifest(self):
        data_locations = {
            "E001": STREAM_ROOT / "workspace" / "data",
            "E002": STREAM_ROOT / "workspace" / "episodes" / "E002" / "data",
            "E003": STREAM_ROOT / "workspace" / "episodes" / "E003" / "data",
        }
        descriptions = {
            "E001": "stock-descriptions.csv",
            "E002": "stock-descriptions.json",
            "E003": "stock-descriptions.tsv",
        }
        for episode_id, data_dir in data_locations.items():
            with self.subTest(episode_id=episode_id):
                episode_dir = STREAM_ROOT / "episodes" / episode_id
                private_dir = episode_dir / "evaluation_assets"
                manifest = json.loads(
                    (private_dir / "source_integrity.json").read_text()
                )
                self.assertEqual(
                    sha256(private_dir / "source_item" / "instruction.md"),
                    manifest["source_request_sha256"],
                )
                self.assertEqual(
                    sha256(private_dir / "native_test_outputs.py"),
                    manifest["native_tests_sha256"],
                )
                self.assertEqual(
                    sha256(private_dir / "oracle_solution.sh"),
                    manifest["oracle_solution_sha256"],
                )
                self.assertEqual(
                    sha256(data_dir / descriptions[episode_id]),
                    manifest["stock_descriptions_sha256"],
                )
                stock_dir = data_dir / "indiv-stock"
                self.assertEqual(
                    len(list(stock_dir.iterdir())),
                    manifest["individual_stock_file_count"],
                )
                self.assertEqual(
                    sorted_hash_manifest(stock_dir),
                    manifest["individual_stock_sorted_hash_manifest_sha256"],
                )
                self.assertEqual(
                    sha256(episode_dir / "assets" / "request.md"),
                    manifest["adapted_request_sha256"],
                )


if __name__ == "__main__":
    unittest.main()
