"""Shared paths, config loading and deterministic serialisation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("THE_SOURCE_ROOT", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config"
RAW = ROOT / "raw"
DATA = ROOT / "data"
STAGING = DATA / "staging"
LIVE = DATA / "live"
DATASET = DATA / "dataset"
REPORTS = DATASET / "reports"
STATE = DATA / "state"
INTAKE = DATA / "intake"
SCHEMA = ROOT / "schema"

SCHEMA_VERSION = "1.0.0"


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_sources(overlay: bool = True) -> dict:
    """config/sources.yaml, with pins advanced by the research cycle applied.

    The reviewed decision (licence, conditions, licence-file hash) lives only in the YAML.
    data/state/source_pins.json records a newer commit for a source ONLY after research.py
    proved its licence file byte-identical to the reviewed one (KEI-807).
    """
    cfg = load_yaml(CONFIG / "sources.yaml")
    pins = STATE / "source_pins.json"
    if overlay and pins.exists():
        advanced = read_json(pins).get("pins") or {}
        for s in cfg["sources"]:
            if s["source_id"] in advanced:
                s["pinned_commit"] = advanced[s["source_id"]]["pinned_commit"]
    return cfg


def intake_ledger_version() -> str:
    """Content hash of the intake ledger, in the 40-hex shape every source commit has."""
    ledger = INTAKE / "admitted"
    files = sorted(ledger.glob("*.json")) if ledger.exists() else []
    if not files:
        return "0" * 40
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:40]


def load_policy() -> dict:
    return load_yaml(CONFIG / "policy.yaml")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dumps(obj) -> str:
    """Canonical JSON: sorted keys, fixed separators, UTF-8 kept readable."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(dumps(row) + "\n")


def read_jsonl(path: Path) -> list:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True, ensure_ascii=False, indent=2)
        fh.write("\n")


def read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_manifest(as_of: str) -> dict:
    """Hash every dataset file. Called last, after the validation report is written."""
    files = {}
    for path in sorted(DATASET.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.json":
            files[str(path.relative_to(DATASET))] = sha256_file(path)
    manifest = {"as_of": as_of, "schema_version": SCHEMA_VERSION,
                "publication_status": "internal_not_published", "sha256": files}
    write_json(DATASET / "MANIFEST.json", manifest)
    return manifest


class NetworkRefused(RuntimeError):
    pass


def refuse_network(url: str) -> None:
    """With THE_SOURCE_NO_NETWORK set, any attempt to reach a host is an error.

    Lets a verifier prove that an offline rebuild and the test suite touch no network,
    rather than take it on trust.
    """
    if os.environ.get("THE_SOURCE_NO_NETWORK"):
        raise NetworkRefused(f"network access attempted with THE_SOURCE_NO_NETWORK set: {url}")
