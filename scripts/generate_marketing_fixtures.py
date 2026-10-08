"""Generate development and acceptance inputs only; never regenerate frozen expectations."""

import json
from pathlib import Path

from llm_ops_lab.marketing.fixtures import make_fixture

if __name__ == "__main__":
    root = Path("data/marketing/v1")
    root.mkdir(parents=True, exist_ok=True)
    for split in ("development", "acceptance"):
        target = root / f"{split}.json"
        data = (json.dumps(make_fixture(split), indent=2) + "\n").encode()
        if target.exists() and target.read_bytes() != data:
            raise SystemExit(f"Refusing to overwrite changed versioned fixture: {target}")
        if not target.exists():
            target.write_bytes(data)
