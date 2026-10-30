"""Guarded image-only instrumentation of pinned native admission boundaries."""
from pathlib import Path
import sys

HOOK = '(globalThis as { __trievoBudget?: { admit(kind: "model" | "tool"): void; outputCap(): number | undefined } }).__trievoBudget'


def replace_one(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f"pinned native hook anchor changed: {path.name}")
    path.write_text(text.replace(old, new))


def install(root: Path) -> None:
    marker = root / ".trievo-hooks-installed"
    if marker.exists():
        raise ValueError("pinned native hook anchor changed: already installed")
    packages = root / "apps/zcode-cli/packages"
    model = packages / "core/src/runtime/methods/runtime-model.ts"
    for method in ["generateText", "streamText"]:
        replace_one(model, f"    {method}(request) {{", f'''    {method}(request) {{
      // 嵌套模型包装层只收紧输出；实际请求在 adapter admission 唯一计数。
      const cap = {HOOK}?.outputCap();
      if (cap !== undefined) request = {{ ...request, options: {{ ...request.options,
        maxOutputTokens: Math.min(request.options?.maxOutputTokens ?? cap, cap) }} }};''')
    request = packages / "adapters/src/model/request-admission.ts"
    replace_one(request, "  if (input.admission === undefined) return NO_ADMISSION;",
                f'  {HOOK}?.admit("model");\n  if (input.admission === undefined) return NO_ADMISSION;')
    tool = packages / "core/src/tool/executor/call-runner.ts"
    replace_one(tool, "      const output = await executeWithTimeout(",
                f'      {HOOK}?.admit("tool");\n      const output = await executeWithTimeout(')
    cwd = packages / "adapters/src/exec/cwd-capture.ts"
    replace_one(cwd, "  const cwdCaptureDir = tmpdir();",
                "  const cwdCaptureDir = process.env.TRIEVO_CWD_CAPTURE_DIR ?? tmpdir();")
    marker.write_text("adapter-only model/tool admission and output cap\n")


if __name__ == "__main__":
    install(Path(sys.argv[1]))
