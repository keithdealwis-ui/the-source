import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pins", ROOT / "scripts" / "check_local_licence_pins.py")
pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pins)


def test_committed_local_licence_pins_match():
    assert pins.main() == 0


def test_an_edited_local_licence_file_is_drift():
    src = [{"source_id": "job_match", "kind": "job_match", "licence_file": "docs/JOB-MATCH.md",
            "licence_file_sha256": "199e9c1378d77da147e67bbc9465d65492650b458b77aafb627444c75a4110c7"},
           {"source_id": "altstack", "kind": "upstream", "licence_file": "LICENSE", "licence_file_sha256": "x"}]
    bad = pins.drift(src)
    assert [b[0] for b in bad] == ["job_match"]  # upstream sources are research.py's job, not this check's
