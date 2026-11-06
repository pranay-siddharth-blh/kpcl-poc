"""
The app only reads its inputs (Cescom POC's locked-inputs rule): the warehouse file, the semantic layer and the
worked examples must hash exactly as recorded in webapp/integrity_manifest.json when the layer was built.
Run after the other tests: if any code path had written to them, this fails.
"""
import hashlib
import json
from pathlib import Path

from app.config import PROJECT_ROOT, settings

MANIFEST = PROJECT_ROOT / "integrity_manifest.json"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def test_inputs_unchanged():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    root = PROJECT_ROOT.parent
    for rel, digest in manifest["files"].items():
        assert sha256(root / rel) == digest, f"{rel} changed since the manifest was written"


def test_db_opened_read_only(db):
    assert db.path == settings.db_path
    assert db._conn.execute("PRAGMA query_only").fetchone()[0] == 1
