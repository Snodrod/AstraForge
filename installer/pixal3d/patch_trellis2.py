"""AstraForge patches for ComfyUI-Trellis2 (idempotent, each patch has its own marker).

1. DINOv3: upstream (nodes.py, class Trellis2LoadModel) silently downloads the DINOv3 weights from the third-party mirror
   `visualbruno/dinov3-vitl16-pretrain-lvd1689m` when `models/facebook/dinov3-vitl16-pretrain-lvd1689m/model.safetensors`
   is missing. AstraForge accepts only the official weights (timm/vit_large_patch16_dinov3.lvd1689m converted with
   tools/dinov3_timm_to_hf.py, or the gated facebook repo), so the node must fail loudly instead. A `raise` is inserted
   right after `if not os.path.exists(dinov3_model_path):`; the old download code stays as dead code.
2. meshlib: upstream imports `meshlib` (MeshLib, AMV Consulting — non-commercial / educational licence only) at module
   load. AstraForge fills holes with CuMesh (MIT) and does not install meshlib, so the imports become optional; only the
   meshlib nodes (never used by AstraForge) fail when called.

    python_embeded\\python.exe -I patch_trellis2.py <ComfyUI-Trellis2 folder>      # exit 0 = patched or already patched
    python_embeded\\python.exe -I patch_trellis2.py <ComfyUI-Trellis2 folder> --check
"""
import re
import sys
from pathlib import Path

DINO_DONE = "Automatic download from a third-party mirror is disabled"
MARKER_MESHLIB = "ASTRAFORGE PATCH meshlib"
ANCHOR = re.compile(r"^(?P<indent>[ \t]*)if not os\.path\.exists\(dinov3_model_path\):[ \t]*(?P<nl>\r?\n)", re.M)
MESHLIB_IMPORTS = re.compile(r"^import meshlib\.(?P<mod>mrmesh\w+) as (?P<alias>\w+)[ \t]*(?P<nl>\r?\n)", re.M)


def patch_dinov3(text):
    if DINO_DONE in text:
        return text, "dinov3: already"
    m = ANCHOR.search(text)
    if not m:
        raise SystemExit("anchor `if not os.path.exists(dinov3_model_path):` not found: upstream changed, re-check by hand")
    ind, nl = m.group("indent") + "    ", m.group("nl")
    lines = [
        "# ASTRAFORGE PATCH: upstream silently downloads DINOv3 from a third-party mirror",
        "# (visualbruno/dinov3-vitl16-pretrain-lvd1689m). We only accept the official weights, placed by the installer",
        "# (see ASTRAFORGE_README.md), so fail loudly instead of downloading.",
        "raise Exception('DINOv3 weights missing: expected ' + dinov3_model_path +",
        "                ' (official facebook/dinov3-vitl16-pretrain-lvd1689m or the converted timm copy). '",
        "                '" + DINO_DONE + " in this install.')",
    ]
    block = "".join(ind + ln + nl for ln in lines)
    return text[: m.end()] + block + text[m.end():], "dinov3: patched"


def patch_meshlib(text):
    if MARKER_MESHLIB in text:
        return text, "meshlib: already"
    found = list(MESHLIB_IMPORTS.finditer(text))
    if not found:
        return text, "meshlib: no top-level imports (nothing to do)"
    for m in reversed(found):
        nl = m.group("nl")
        repl = ("try:  # " + MARKER_MESHLIB + ": meshlib is non-commercial only and not installed by AstraForge" + nl +
                "    import meshlib." + m.group("mod") + " as " + m.group("alias") + nl +
                "except ImportError:" + nl +
                "    " + m.group("alias") + " = None" + nl)
        text = text[: m.start()] + repl + text[m.end():]
    return text, "meshlib: patched %d imports" % len(found)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    node_dir = Path(sys.argv[1])
    check_only = "--check" in sys.argv[2:]
    path = node_dir / "nodes.py"
    if not path.is_file():
        print(f"nodes.py not found in {node_dir}")
        return 2
    text = path.read_bytes().decode("utf-8")
    # meshlib is imported at module level in several files (nodes.py, trellis2/representations/mesh/base.py, ...)
    others = [f for f in sorted(node_dir.rglob("*.py")) if f != path and "__pycache__" not in f.parts]
    if check_only:
        ok = DINO_DONE in text and all(
            MARKER_MESHLIB in t or not MESHLIB_IMPORTS.search(t)
            for t in [text] + [f.read_bytes().decode("utf-8", "replace") for f in others])
        print("patched" if ok else "NOT patched")
        return 0 if ok else 1
    text, r1 = patch_dinov3(text)
    text, r2 = patch_meshlib(text)
    path.write_bytes(text.encode("utf-8"))
    print(r1 + "; " + r2, path)
    for f in others:
        t = f.read_bytes().decode("utf-8")
        t2, r = patch_meshlib(t)
        if t2 != t:
            f.write_bytes(t2.encode("utf-8"))
            print(r, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
