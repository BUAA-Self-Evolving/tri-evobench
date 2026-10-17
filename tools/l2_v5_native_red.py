"""Prepare submission-only red candidates; fresh native tests decide validity."""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


def _copy(source: Path, dest: Path) -> None:
    if dest.is_symlink():
        raise ValueError("red destination must not be a symlink")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True)


def _rewrite_zip(path: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info, replacements.get(info.filename, archive.read(info)))
                   for info in archive.infolist()]
        comment = archive.comment
    temporary = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w") as archive:
        archive.comment = comment
        for info, content in entries:
            archive.writestr(info, content)
    temporary.replace(path)


def _offer(dest: Path) -> dict[str, Any]:
    root = dest / "rootfs/root"
    name = json.loads((root / "employee_data.json").read_text())["CANDIDATE_FULL_NAME"]
    wrong = "Incorrect Candidate Name"
    if not isinstance(name, str) or not name or name == wrong:
        raise ValueError("gold candidate name unavailable")
    path = root / "offer_letter_filled.docx"
    replacements = {}
    for_name = escape(name).encode()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not (info.filename.startswith("word/") and info.filename.endswith(".xml")):
                continue
            content = archive.read(info)
            xml = ET.fromstring(content)
            count = sum((node.text or "").count(name) for node in xml.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
            if count:
                if content.count(for_name) != count:
                    raise ValueError("gold name is not literal XML text")
                replacements[info.filename] = content.replace(for_name, escape(wrong).encode())
    if not replacements:
        raise ValueError("gold name not found")
    _rewrite_zip(path, replacements)
    return {"kind": "wrong_offer_candidate_name", "field": "CANDIDATE_FULL_NAME"}


def _gdp(source_id: str, dest: Path) -> dict[str, Any]:
    path = dest / "rootfs/root/gdp.xlsx"
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell = "H43" if source_id == "skilllearnbench:weighted-gdp-calculation-5" else "H50"
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets = [s for s in workbook.findall("s:sheets/s:sheet", ns)
                  if s.attrib.get("name") == "Task"]
        if len(sheets) != 1:
            raise ValueError("gold Task sheet unavailable")
        relation = sheets[0].attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = [r.attrib["Target"] for r in relations if r.attrib.get("Id") == relation]
        if len(targets) != 1:
            raise ValueError("gold Task relationship unavailable")
        target = targets[0]
        sheet = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        content = archive.read(sheet)
    cells = [c for c in ET.fromstring(content).iter("{" + ns["s"] + "}c")
             if c.attrib.get("r") == cell]
    if len(cells) != 1 or cells[0].attrib.get("t") not in (None, "n") or not list(cells[0]):
        raise ValueError("gold weighted result unavailable")
    hits = list(re.finditer(rb'<c\b[^>]*\br="' + cell.encode() + rb'"[^>]*>.*?</c>',
                           content, re.S))
    if len(hits) != 1:
        raise ValueError("gold weighted result XML unavailable")
    hit = hits[0]
    opening = hit.group().split(b">", 1)[0] + b">"
    changed = content[:hit.start()] + opening + b"<v>30</v></c>" + content[hit.end():]
    ET.fromstring(changed)
    _rewrite_zip(path, {sheet: changed})
    return {"kind": "wrong_weighted_result", "sheet": "Task", "cell": cell, "value": 30}


def _organize(dest: Path) -> dict[str, Any]:
    for source in (dest / "rootfs/root").rglob("2401.10034v3.pdf"):
        if source.parent.name != "LLM" or source.is_symlink() or not source.is_file():
            continue
        folder = source.parent.parent / "black_hole"
        target = folder / source.name
        if folder.is_dir() and not target.exists():
            source.rename(target)
            return {"kind": "wrong_document_subject", "filename": source.name,
                    "from": "LLM", "to": "black_hole"}
    raise ValueError("gold subject PDF unavailable")


def prepare_broken(source_id: str, gold_dir: Path, dest: Path,
                   existing_work: Path | None = None) -> dict[str, Any] | None:
    """Return a mutation note, or None so the caller can use its seed fallback.

    Existing candidates are hints only: require matching source ID and a present
    candidate directory, then copy them without trusting an old test result.
    """
    gold_dir, dest = Path(gold_dir), Path(dest)
    gold, output = gold_dir.resolve(), dest.resolve()
    if gold == output or gold in output.parents or output in gold.parents:
        raise ValueError("red destination must be separate from gold")
    if existing_work is not None:
        work = Path(existing_work)
        slug = source_id.replace(":", "__").replace("/", "__")
        for record_path in (work / slug / "red-recovery/candidate-result.json",
                            work / "red-recovery/candidate-result.json"):
            try:
                record = json.loads(record_path.read_text())
                candidate = Path(record["candidate"])
                if not candidate.is_absolute():
                    candidate = record_path.parent / candidate
                if record.get("source_id") == source_id and candidate.is_dir():
                    if candidate.resolve() != output:
                        if candidate.resolve() in output.parents or output in candidate.resolve().parents:
                            continue
                        _copy(candidate, dest)
                    return {"kind": "reused_red_candidate", "candidate": str(candidate),
                            "mutation": record.get("mutation")}
            except (OSError, ValueError, KeyError, TypeError):
                continue
    mutation = (_offer if "offer-letter-generator" in source_id else
                _organize if "organize-messy-files" in source_id else
                _gdp if "weighted-gdp" in source_id else None)
    if mutation is None:
        return None
    try:
        _copy(gold_dir, dest)
        return mutation(source_id, dest) if mutation is _gdp else mutation(dest)
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile):
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        return None
