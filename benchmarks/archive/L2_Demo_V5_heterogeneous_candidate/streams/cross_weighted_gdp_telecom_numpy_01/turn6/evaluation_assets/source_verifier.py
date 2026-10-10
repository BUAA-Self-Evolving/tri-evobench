import json, subprocess, sys, tempfile
from pathlib import Path
def verify(submission, turn_dir):
    if not isinstance(submission, str) or not submission.strip(): return False,{"reason":"empty_submission"}
    item=json.loads((turn_dir/"evaluation_assets/source_item.json").read_text())
    with tempfile.TemporaryDirectory(prefix="l2-sr-") as raw:
        root=Path(raw); (root/"candidate.py").write_text(submission)
        (root/"runner.py").write_text("import runpy\nglobals().update(runpy.run_path('candidate.py'))\n"+item["eval_data"]["test"]+"\nimport unittest\nunittest.main()\n")
        run=subprocess.run([sys.executable,"runner.py"],cwd=root,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
    return run.returncode==0,{"carrier":"sr_exact_answer_native_tests/1.0","exit_code":run.returncode,"output_tail":run.stdout[-4000:]}
