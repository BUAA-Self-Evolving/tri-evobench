"""Restore a GitHub archive and independently verify the pinned Git tree/commit.

This reconstructs original Git objects, not a replacement local source commit.
Both SHA-1 object IDs must match the upstream GitHub commit metadata exactly.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def git(path, *args, input=None):
    return subprocess.run(["git", "-C", str(path), *args], input=input,
                          capture_output=True, check=True).stdout.decode().strip()


def original_commit(metadata):
    expected = metadata["sha"]
    if metadata["verification"].get("signature"):
        raise ValueError("signed commits require a signed-object restoration path")
    offsets = ["+0000", "+0800"]
    for minutes in range(-12 * 60, 14 * 60 + 1, 15):
        offset = ("+" if minutes >= 0 else "-") + f"{abs(minutes)//60:02d}{abs(minutes)%60:02d}"
        if offset not in offsets:
            offsets.append(offset)
    header = "tree " + metadata["tree"]["sha"] + "\n"
    header += "".join("parent " + parent["sha"] + "\n" for parent in metadata["parents"])
    people = []
    for role in ("author", "committer"):
        person = metadata[role]
        epoch = int(datetime.datetime.fromisoformat(person["date"].replace("Z", "+00:00")).timestamp())
        people.append(f"{role} {person['name']} <{person['email']}> {epoch}")
    for author_offset in offsets:
        for committer_offset in offsets:
            for ending in ("\n", ""):
                raw = (header + people[0] + " " + author_offset + "\n" + people[1] + " " +
                       committer_offset + "\n\n" + metadata["message"] + ending).encode()
                digest = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                if digest == expected:
                    return raw
    raise ValueError("could not reconstruct the exact upstream commit object")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    raw_commit = original_commit(metadata)
    dest = args.destination.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("destination must be empty")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.archive, "r:gz") as archive:
        members = archive.getmembers()  # Also rejects truncated archives before extracting.
        prefixes = {member.name.split("/")[0] for member in members}
        if len(prefixes) != 1:
            raise ValueError("archive has multiple roots")
        for member in members:
            relative = member.name.partition("/")[2]
            if not relative:
                continue
            member.name = relative
            archive.extract(member, dest, filter="data")
    git(dest, "init", "-q")
    git(dest, "config", "core.autocrlf", "false")
    git(dest, "add", "-f", "--all")
    actual_tree = git(dest, "write-tree")
    if actual_tree != metadata["tree"]["sha"]:
        raise ValueError("archive tree differs from pinned upstream Git tree")
    actual_commit = git(dest, "hash-object", "-t", "commit", "-w", "--stdin", input=raw_commit)
    if actual_commit != metadata["sha"]:
        raise ValueError("commit object identity mismatch")
    git(dest, "update-ref", "HEAD", actual_commit)
    if git(dest, "status", "--porcelain"):
        raise ValueError("restored source is dirty")
    print(json.dumps({"revision": actual_commit, "tree": actual_tree, "source_verified": True}))


if __name__ == "__main__":
    main()
