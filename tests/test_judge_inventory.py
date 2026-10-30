import hashlib
import json
from trievo_bench.judge_inventory import canonical, decode_paths, encode, path_trie


def test_path_trie_roundtrips_all_names_and_nested_prefixes():
    paths = {'a', 'a/b', 'a/b/c', 'deps/a.py', 'deps/空 格.py', 'README.md'}
    assert decode_paths(path_trie(paths)) == paths


def test_large_inventory_retains_all_paths_changes_and_full_digest_audit(tmp_path):
    before = {f'deps/pkg{i}/old.py': str(i) * 64 for i in range(1000)}
    after = {f'deps/pkg{i}/new.py': str(i) * 64 for i in range(1000)}
    before['retained'] = after['retained'] = 'a' * 64
    before['modified'] = 'a' * 64
    after['modified'] = 'b' * 64
    result = encode(before, after, tmp_path)
    assert decode_paths(result['before']['paths']) == set(before)
    assert decode_paths(result['after']['paths']) == set(after)
    data = __import__('pathlib').Path(result['artifact']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == result['sha256']
    saved = json.loads(data)
    assert saved['before'] == before and saved['after'] == after
    assert saved['modified'] == ['modified']
    assert result['diff']['changed_count'] == 2001
    assert len(canonical(result)) < len(canonical([before, after]))


def test_small_inventory_uses_original_payload_and_writes_no_artifact(tmp_path):
    assert encode({'a': '1'}, {'a': '2'}, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_mostly_modified_inventory_encodes_exact_complement(tmp_path):
    before = {f'pkg/{i}.py': 'a' * 64 for i in range(2000)}
    after = {p: 'b' * 64 for p in before}
    after['pkg/0.py'] = before['pkg/0.py']
    result = encode(before, after, tmp_path)
    descriptor = result['diff']['changed_files']['modified']
    excluded = decode_paths(descriptor['excluding_paths'])
    assert (decode_paths(result['before']['paths']) & decode_paths(result['after']['paths'])) - excluded == {
        p for p in before if before[p] != after[p]}
