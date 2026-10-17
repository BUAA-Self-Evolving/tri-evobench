"""Historical input comparison must reject byte, mode, link and path changes."""
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from l2_v5_native_reuse_inputs import _same_tree


@pytest.mark.parametrize('mutation', ['bytes', 'mode', 'link', 'extra_path', 'missing_path'])
def test_historical_tree_rejects_changed_actual_input(tmp_path, mutation):
    source, prior = tmp_path / 'source', tmp_path / 'captured'
    source.mkdir()
    (source / 'nested').mkdir()
    (source / 'nested/data').write_bytes(b'original source')
    (source / 'alias').symlink_to('nested/data')
    shutil.copytree(source, prior, symlinks=True)
    _same_tree(source, prior)
    if mutation == 'bytes':
        (source / 'nested/data').write_bytes(b'changed source')
    elif mutation == 'mode':
        path = source / 'nested/data'
        os.chmod(path, (path.stat().st_mode & 0o777) ^ 0o100)
    elif mutation == 'link':
        (source / 'alias').unlink()
        (source / 'alias').symlink_to('other')
    elif mutation == 'extra_path':
        (source / 'extra').write_bytes(b'new')
    else:
        (source / 'nested/data').unlink()
    with pytest.raises(ValueError, match='historical input'):
        _same_tree(source, prior)


def test_only_root_skill_stub_is_excluded_from_historical_context(tmp_path):
    source, prior = tmp_path / 'source', tmp_path / 'captured'
    (source / 'nested/skills').mkdir(parents=True)
    (source / 'nested/skills/data').write_bytes(b'original')
    shutil.copytree(source, prior)
    (prior / 'skills').mkdir()
    _same_tree(source, prior, ignored={'skills'})
    (source / 'nested/skills/data').write_bytes(b'changed')
    with pytest.raises(ValueError, match='historical input'):
        _same_tree(source, prior, ignored={'skills'})
