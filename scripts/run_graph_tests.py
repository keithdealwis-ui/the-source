"""Run tests/test_graph.py without pytest.

The verifier host has pyyaml and jsonschema but no pytest, and verification runs with no
network. This runner provides what the suite uses (the `inputs` fixture, tmp_path,
pytest.raises, pytest.mark.skipif) and prints a pytest-style tail: `N passed` or
`N passed, M failed`.
"""
import contextlib
import importlib
import inspect
import re
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

shim = types.ModuleType("pytest")
shim.fixture = lambda f=None, **kw: f if f else (lambda g: g)


@contextlib.contextmanager
def raises(exc, match=None):
    try:
        yield
    except exc as e:
        if match and not re.search(match, str(e)):
            raise AssertionError(f"{exc.__name__} raised, but {match!r} not in {e}") from e
    else:
        raise AssertionError(f"{exc.__name__} not raised")


def skipif(cond, reason=""):
    def mark(obj):
        obj.__skip__ = reason if cond else None
        return obj
    return mark


shim.raises = raises
shim.mark = types.SimpleNamespace(skipif=skipif)
sys.modules["pytest"] = shim


def main() -> int:
    mod = importlib.import_module("test_graph")
    fixtures = {"inputs": mod.inputs}
    passed, skipped, failed = 0, 0, []
    for cname, cls in sorted(vars(mod).items()):
        if not (inspect.isclass(cls) and cname.startswith("Test")):
            continue
        for mname, meth in sorted(vars(cls).items()):
            if not mname.startswith("test_"):
                continue
            if getattr(cls, "__skip__", None):
                skipped += 1
                continue
            tmp = Path(tempfile.mkdtemp(prefix="graph-test-"))
            try:
                args = {}
                for p in list(inspect.signature(meth).parameters)[1:]:
                    if p == "tmp_path":
                        args[p] = tmp
                    elif p in fixtures:
                        args[p] = fixtures[p]()
                    else:
                        raise RuntimeError(f"unknown fixture {p}")
                meth(cls(), **args)
                passed += 1
            except Exception:
                failed.append(f"{cname}::{mname}\n{traceback.format_exc()}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    for f in failed:
        print("FAILED", f)
    print(f"{passed} passed" + (f", {len(failed)} failed" if failed else "") + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
