"""Lossless workspace path sets with a commitment to the full digest manifest.

Only large inventories opt into this representation. Every path is retained;
individual SHA256 values remain in the private audit artifact. Replies, tools,
native verdicts and rubric text are never shortened here.
"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def path_trie(paths) -> dict:
    tree = {}
    for path in sorted(paths):
        node = tree
        for component in path.split('/'):
            node = node.setdefault(component, {})
        node[''] = True
    def compact(node):
        if node == {'': True}:
            return 0
        return {name: compact(child) if isinstance(child, dict) else child
                for name, child in node.items()}
    return compact(tree)


def decode_paths(tree, prefix='') -> set[str]:
    result = set()
    for name, child in tree.items():
        if name == '':
            result.add(prefix)
        elif child == 0:
            result.add(prefix + '/' + name if prefix else name)
        else:
            result.update(decode_paths(child, prefix + '/' + name if prefix else name))
    return result


def encode(before: dict, after: dict, directory: Path) -> dict | None:
    if len(canonical([before, after])) <= 128000:
        return None
    before_paths, after_paths = set(before), set(after)
    common = before_paths & after_paths
    modified = {p for p in common if before[p] != after[p]}
    # Set references avoid repeating thousands of newly installed paths.
    excluded = common - modified
    modified_description = ({'intersection_of': ['workspace_before.paths', 'workspace_after.paths'],
                             'excluding_paths': path_trie(excluded)}
                            if len(excluded) < len(modified) else {'paths': path_trie(modified)})
    full = {'before': before, 'after': after,
            'added': sorted(after_paths - before_paths), 'removed': sorted(before_paths - after_paths),
            'modified': sorted(modified)}
    directory.mkdir(parents=True, exist_ok=True)
    data = canonical(full)
    digest = hashlib.sha256(data).hexdigest()
    artifact = directory / (uuid4().hex + '.json')
    with artifact.open('xb') as handle:
        handle.write(data)
    def inventory(value):
        return {'representation': 'complete_path_trie/1.0', 'file_count': len(value),
                'full_path_sha256_manifest_sha256': hashlib.sha256(canonical(value)).hexdigest(),
                'paths': path_trie(value)}
    return {'before': inventory(before), 'after': inventory(after),
            'diff': {'representation': 'exact_path_set_operations/1.0',
                     'changed_files': {'added': {'set_difference': ['workspace_after.paths', 'workspace_before.paths']},
                                       'removed': {'set_difference': ['workspace_before.paths', 'workspace_after.paths']},
                                       'modified': modified_description},
                     'added_count': len(full['added']), 'removed_count': len(full['removed']),
                     'modified_count': len(modified), 'changed_count': len(full['added']) + len(full['removed']) + len(modified),
                     'full_manifest_sha256': digest,
                     'interpretation': 'All paths are retained exactly: dictionary keys are path components, zero values mark files (an empty-name key marks a file that is also a path prefix). Set operations specify every changed path. Individual content digests are bound by the full manifest SHA256; native verifier results remain authoritative.'},
            'artifact': str(artifact), 'sha256': digest}
