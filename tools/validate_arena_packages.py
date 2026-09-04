"""Offline integration checks: every declared turn, transition and verifier."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trievo_bench.l1_arena_compat import load_l1_arena_stream
from trievo_bench.arena_runtime import apply_updates

root = Path(sys.argv[1]).resolve()
count = 0
for manifest in root.glob('stream-*/stream.json'):
    data = json.loads(manifest.read_text(encoding='utf-8'))
    spec = load_l1_arena_stream(root, manifest.parent.name, [t['turn_id'] for t in data['turns']], 'docker', 'trievo-ubuntu:22.04')
    with tempfile.TemporaryDirectory() as tmp:
        for turn in spec.flows[0].turns:
            apply_updates(turn.setup, Path(tmp))
            evaluation = turn.verifier['evaluation']
            declarations = [evaluation.get('exec_check'), evaluation.get('preference_check'), evaluation.get('gt', {}).get('workspace_gt', {}).get('task_verifier')]
            for declaration in declarations:
                if declaration:
                    match = re.search(r'(check_[A-Za-z0-9_]+\.py)', declaration['command'])
                    assert match and (Path(turn.verifier['script_root']) / match.group(1)).is_file(), declaration
            count += 1
print(f'Validated {count} turns: conversion, ordered updates, checksum, verifier paths.')
