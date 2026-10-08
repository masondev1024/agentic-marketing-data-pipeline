"""Validate the AC1 requirement index and accepted seven-deliverable evidence."""

import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from check_marketing_docs import validate as validate_docs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INDEX = "evidence/marketing-requirements.json"
BRIEF = "work/marketing-aws-implementation-brief.md"


def validate(root=ROOT):
    result = validate_docs(root)
    if result["inference_source_drift"]:
        raise ValueError("Current model implementation lacks matching inference evidence")
    index = json.loads((root / INDEX).read_text())
    entries = index["requirement_evidence"]
    lines = [entry["line"] for entry in entries]
    if lines != [1, 3, 5, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 25]:
        raise ValueError("Incomplete authoritative brief coverage")
    for entry in entries:
        if not 0 < len(entry["text"]) <= 2000 or entry["source"] != BRIEF:
            raise ValueError("Invalid requirement evidence boundary")
        if not entry["references"]:
            raise ValueError("Missing implementation/verification references")
        for name in entry["references"]:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
                raise ValueError(f"Invalid evidence reference: {name}")
    authority = root / BRIEF
    if authority.exists():
        content = authority.read_bytes()
        if hashlib.sha256(content).hexdigest() != index["authority_sha256"]:
            raise ValueError("Authoritative brief changed")
        source_lines = content.decode().splitlines()
        if lines != [i for i, line in enumerate(source_lines, 1) if line.strip()]:
            raise ValueError("Unmapped authoritative requirement")
        for entry in entries:
            digest = hashlib.sha256(source_lines[entry["line"] - 1].encode()).hexdigest()
            if digest != entry["source_line_sha256"]:
                raise ValueError("Source requirement changed")
    return {**result, "requirements_checked": len(entries),
            "authority_available": authority.exists(), "status": "release_references_verified"}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(), ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
