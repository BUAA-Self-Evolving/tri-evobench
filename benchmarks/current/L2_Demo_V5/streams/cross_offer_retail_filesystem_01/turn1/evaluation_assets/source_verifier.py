import os, shutil, subprocess, sys
from pathlib import Path
def verify(submission, turn_dir):
    if not isinstance(submission,Path) or not submission.is_dir(): return False,{"reason":"workspace_required"}
    tests=turn_dir/"evaluation_assets/native_test_files/test_outputs.py"
    if not tests.is_file(): return False,{"reason":"upstream_test_outputs_missing"}
    # Upstream task images conventionally expose the task workspace at /root
    # or /app. The verifier container is disposable, so mirror only public
    # submission files into those compatibility locations before pytest.
    compatibility_root=Path(os.environ.get("L2_VERIFIER_COMPAT_ROOT","/"))
    root_dir=compatibility_root/"root"; app_dir=compatibility_root/"app"
    for destination in (root_dir,app_dir):
        destination.mkdir(parents=True,exist_ok=True)
        for child in submission.iterdir():
            target=destination/child.name
            if child.is_dir(): shutil.copytree(child,target,dirs_exist_ok=True)
            else: shutil.copy2(child,target)
    env=dict(os.environ); env.update({"HOME":str(root_dir),"L2_AGENT_WORKSPACE":str(submission)})
    run=subprocess.run([sys.executable,"-m","pytest","-q",str(tests)],cwd=app_dir,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
    return run.returncode==0,{"carrier":"upstream_pytest_native/1.0","exit_code":run.returncode,"output_tail":run.stdout[-4000:]}
