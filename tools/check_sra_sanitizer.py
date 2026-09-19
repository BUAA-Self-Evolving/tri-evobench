"""Offline Docker regression for pinned sanitizer compatibility overlay."""
import importlib.util
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


root = Path("/repo")
stream = root / "benchmarks/current/L2_Demo_V4/streams/sra_bigcodebench_pandas_dataframe_viz_001"
original = load("old_sanitize", stream / "turn5/evaluation_assets/sra_sanitize.py")
patched = load("new_sanitize", root / "trievo_bench/native_patches/sra_sanitize.py")
example = "a, b = (2, 3)\ndef task_func():\n    return a + b\n"
try:
    original.sanitize(example, "task_func")
    raise AssertionError("Expected original tuple-assignment crash")
except ValueError as error:
    assert "no identifier" in str(error)
sanitized = patched.sanitize(example, "task_func")
scope = {}
exec(sanitized, scope)
assert scope["task_func"]() == 5
assert sanitized.count("a, b =") == 1
assert "a, b =" not in patched.sanitize("def task_func():\n    return 1\na, b = (2, 3)\n", "task_func")
count = 0
for path in sorted(stream.glob("turn*/evaluation_assets/known_correct_response.py")):
    code = path.read_text()
    assert original.sanitize(code, "task_func") == patched.sanitize(code, "task_func"), path
    count += 1
assert count == 100, count
pathological = ("<thought\nLet me inspect the workspace. response\n\n" * 800) + "<thought\nLet"
assert len(pathological.splitlines()) > 1000
assert patched.sanitize(pathological, "task_func") == pathological
print(f"PASS: tuple bindings retained once, pathological prose rejected, all {count} reference extractions unchanged")
