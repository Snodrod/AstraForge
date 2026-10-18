"""Shared test runner for AstraForge Blender steps (contract §10).

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- [module ...] [-k text]

Discovers test_*.py next to this file (a module filter like `core` or `test_core` keeps only
those files), imports each with scripts/blender and scripts/blender/tests on sys.path, calls
af_common.reset_scene() before every zero-argument test_* function and prints one line per test:

    PASS  test_core.test_cleanup_synthetic  3.21s
    FAIL  test_core.test_x  0.40s  AssertionError: ...
    SKIP  test_core.test_real_griffin  0.00s  file missing: ...

A test is SKIP when it raises unittest.SkipTest or returns a string starting with "skip"
(e.g. `return "skip: asset missing"`); a plain `return` counts as PASS. The last line is
`TESTRESULT {json}` with counts and per-test entries; the exit code is 1 if anything failed
(including a module that does not import), 0 otherwise. Options: -k <substring> filters test
names, --list prints the discovered tests without running them.
"""
import importlib
import json
import os
import sys
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER_DIR = os.path.dirname(HERE)
for _p in (HERE, BLENDER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    modules, keyword, listing = [], None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-k" and i + 1 < len(argv):
            keyword = argv[i + 1]
            i += 2
            continue
        if a == "--list":
            listing = True
        elif a:
            for part in a.split(","):
                part = part.strip()
                if part:
                    modules.append(part[5:] if part.startswith("test_") else part)
        i += 1
    return modules, keyword, listing


def discover(modules):
    names = sorted(f[:-3] for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py"))
    if modules:
        wanted = {"test_" + m.replace(".py", "") for m in modules}
        missing = sorted(wanted - set(names))
        names = [n for n in names if n in wanted]
        return names, missing
    return names, []


def _short(exc):
    text = "%s: %s" % (type(exc).__name__, exc)
    return text.replace("\n", " ")[:500]


def main():
    modules, keyword, listing = parse_args(sys.argv)
    names, missing = discover(modules)
    results = []
    t_all = time.time()
    import af_common as af  # noqa: E402  (needs bpy, so only inside Blender)

    for m in missing:
        results.append({"module": m, "test": "<module>", "status": "FAIL", "seconds": 0.0,
                        "error": "no such test module in %s" % HERE})
        print("FAIL  %s.<module>  0.00s  no such test module" % m, flush=True)
    for mod_name in names:
        try:
            mod = importlib.import_module(mod_name)
        except BaseException as exc:  # a broken module is a failure, not a crash of the run
            results.append({"module": mod_name, "test": "<import>", "status": "FAIL",
                            "seconds": 0.0, "error": _short(exc),
                            "trace": traceback.format_exc()[-3000:]})
            print("FAIL  %s.<import>  0.00s  %s" % (mod_name, _short(exc)), flush=True)
            continue
        tests = sorted(n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n)))
        if keyword:
            tests = [n for n in tests if keyword in n]
        for name in tests:
            if listing:
                print("LIST  %s.%s" % (mod_name, name), flush=True)
                continue
            status, error, trace = "PASS", None, None
            t0 = time.time()
            try:
                af.reset_scene()
                ret = getattr(mod, name)()
                if isinstance(ret, str) and ret.lower().startswith("skip"):
                    status, error = "SKIP", ret
            except unittest.SkipTest as exc:
                status, error = "SKIP", str(exc)
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                status, error = "FAIL", _short(exc)
                trace = traceback.format_exc()[-3000:]
            dt = round(time.time() - t0, 2)
            item = {"module": mod_name, "test": name, "status": status, "seconds": dt}
            if error:
                item["error"] = error
            if trace:
                item["trace"] = trace
            results.append(item)
            line = "%s  %s.%s  %.2fs" % (status, mod_name, name, dt)
            if error:
                line += "  " + error
            print(line, flush=True)
            if trace:
                print(trace, flush=True)
    summary = {
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "skipped": sum(1 for r in results if r["status"] == "SKIP"),
        "modules": names,
        "seconds": round(time.time() - t_all, 2),
        "tests": [{k: v for k, v in r.items() if k != "trace"} for r in results],
    }
    print("TESTRESULT " + json.dumps(summary, ensure_ascii=False), flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
