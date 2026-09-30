"""Run tests/test_discover.py without pytest.

The verifier host has pyyaml and jsonschema but no pytest, and verification runs with
no network, so nothing can be installed. This runner provides the three fixtures the
suite uses (tmp_path, monkeypatch, built) and prints a pytest-style tail:
`N passed` or `N passed, M failed`.
"""
import importlib
import inspect
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# Always the shim, never the real pytest, so the runner behaves the same on every host.
shim = types.ModuleType("pytest")
shim.fixture = lambda f=None, **kw: f if f else (lambda g: g)
sys.modules["pytest"] = shim


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, value in reversed(self._undo):
            setattr(target, name, value)
        self._undo.clear()


def main() -> int:
    mod = importlib.import_module("test_discover")
    fixtures = {n: f for n, f in vars(mod).items() if inspect.isfunction(f) and n in ("built",)}
    passed, failed = 0, []
    for cname, cls in sorted(vars(mod).items()):
        if not (inspect.isclass(cls) and cname.startswith("Test")):
            continue
        for mname, meth in sorted(vars(cls).items()):
            if not mname.startswith("test_"):
                continue
            tmp = Path(tempfile.mkdtemp(prefix="discover-test-"))
            mp = MonkeyPatch()
            try:
                args = {}
                for p in list(inspect.signature(meth).parameters)[1:]:
                    if p == "tmp_path":
                        args[p] = tmp
                    elif p == "monkeypatch":
                        args[p] = mp
                    elif p in fixtures:
                        args[p] = fixtures[p](tmp, mp)
                    else:
                        raise RuntimeError(f"unknown fixture {p}")
                meth(cls(), **args)
                passed += 1
            except Exception:
                failed.append(f"{cname}::{mname}\n{traceback.format_exc()}")
            finally:
                mp.undo()
                shutil.rmtree(tmp, ignore_errors=True)
    for f in failed:
        print("FAILED", f)
    print(f"{passed} passed" + (f", {len(failed)} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
