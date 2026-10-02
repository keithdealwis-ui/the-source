"""Run tests/test_canonical.py and tests/test_daily.py without pytest (KEI-807 / KEI-848
verifier route). Pass module names to run only those.

Verification hosts carry python3 + pyyaml + jsonschema but not pytest. This runner
supplies the small part of pytest the tests use (mark.skipif, mark.parametrize, raises,
and the tmp_path / monkeypatch fixtures) and runs every test. Prints "<n> passed" or
"<n> passed, <m> failed" as its last line and exits non-zero on any failure.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class _Mark:
    def skipif(self, cond, reason=""):
        def deco(fn):
            fn._skip = (bool(cond), reason)
            return fn
        return deco

    def parametrize(self, names, values):
        def deco(fn):
            fn._params = ([n.strip() for n in names.split(",")], values)
            return fn
        return deco


@contextlib.contextmanager
def _raises(exc):
    try:
        yield
    except exc:
        return
    raise AssertionError(f"did not raise {exc.__name__}")


class MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)


fake = types.ModuleType("pytest")
fake.mark = _Mark()
fake.raises = _raises
sys.modules["pytest"] = fake


def main() -> int:
    mods = sys.argv[1:] or ["test_canonical", "test_daily"]
    passed = failed = skipped = 0
    tests = []
    for m in mods:
        mod = importlib.import_module(m)
        tests += sorted(inspect.getmembers(mod, inspect.isfunction), key=lambda x: x[1].__code__.co_firstlineno)
    for name, fn in tests:
        if not name.startswith("test_"):
            continue
        if getattr(fn, "_skip", (False, ""))[0]:
            skipped += 1
            print(f"SKIP {name}: {fn._skip[1]}")
            continue
        names, values = getattr(fn, "_params", ([], [()]))
        for vals in values:
            vals = vals if isinstance(vals, tuple) else (vals,)
            kwargs = dict(zip(names, vals))
            mp = MonkeyPatch()
            with tempfile.TemporaryDirectory() as tmp:
                sig = inspect.signature(fn).parameters
                if "tmp_path" in sig:
                    kwargs["tmp_path"] = Path(tmp)
                if "monkeypatch" in sig:
                    kwargs["monkeypatch"] = mp
                label = f"{name}[{vals}]" if names else name
                try:
                    fn(**kwargs)
                    passed += 1
                    print(f"ok   {label}")
                except Exception:
                    failed += 1
                    print(f"FAIL {label}\n{traceback.format_exc()}")
                finally:
                    mp.undo()
    print(f"{passed} passed" + (f", {failed} failed" if failed else "") + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
