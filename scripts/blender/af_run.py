"""AstraForge Blender worker entry point.

    blender.exe -b --factory-startup --python scripts/blender/af_run.py -- <step> <request.json>

Loads scripts/blender/steps/<step>.py, calls run(req) and reports the result through
af_common.finish(): <out_dir>/result.json plus a final stdout line `AFRESULT {json}`.
Any exception prints `AFERROR {json}` and exits with code 1. See docs/FORGE-CONTRACT.md §3.
"""
import importlib
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import af_common as af  # noqa: E402


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 2:
        raise SystemExit("usage: af_run.py -- <step> <request.json>")
    step, req_path = argv[0], argv[1]
    if not step.replace("_", "").isalnum():
        raise SystemExit("bad step name: %r" % step)
    req = af.load_request(req_path)
    req.setdefault("step", step)
    try:
        module = importlib.import_module("steps." + step)
        blend = req.get("inputs", {}).get("blend")
        if blend:
            if not os.path.isfile(blend):
                raise FileNotFoundError("inputs.blend not found: %s" % blend)
            af.open_blend(blend)
        result = module.run(req)
        if result is None:
            raise RuntimeError("step %s returned nothing" % step)
        af.finish(req, result)
    except SystemExit:
        raise
    except BaseException as exc:  # report every failure in the protocol format
        payload = {"step": step, "error": "%s: %s" % (type(exc).__name__, exc),
                   "trace": traceback.format_exc()[-6000:]}
        try:
            out_dir = req.get("out_dir")
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
                    json.dump(dict(payload, ok=False), fh, ensure_ascii=False, indent=2)
        finally:
            print("AFERROR " + json.dumps(payload, ensure_ascii=False), flush=True)
            sys.stdout.flush()
            os._exit(1)


if __name__ == "__main__":
    main()
