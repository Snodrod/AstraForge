"""QA of paint_from_concept: contact sheet, numeric verdict and an optional local vision-model review.

  * build_sheet()  - labelled tiles (concept front | model front | concept back | model back | 3/4 left |
                     3/4 right | side) in a grid, 5x7 bitmap font (no PIL in Blender's Python);
  * judge()        - metric thresholds -> list of problems (the step's `texture_qa` warning);
  * run_vlm()      - Ollama /api/chat with the sheet as an image; never raises: an unavailable model, a busy
                     ComfyUI (the GPU is shared with mesh generation) or a bad answer is reported as `skipped`;
  * parse_review() - tolerant JSON extraction (code fences, prose around the object, clamped score).

HTTP goes through _request() only (urllib, no third-party packages), so tests replace that one function.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

import numpy as np

# ---------------------------------------------------------------------- thresholds (qa.json -> thresholds)
THRESHOLDS = {
    "iou_aligned": 0.80,        # silhouette of the model vs the (aligned) concept
    "color_mae": 0.10,          # flat render through the camera vs the concept, common silhouette
    "warp_p95_pct": 4.0,        # % of the subject height: a big warp = the mesh deviates from the concept
    "grazing_share": 0.35,      # concept-coloured texels on faces tilted > 60 deg away from the camera
    "fill_share_back": 0.60,    # with a back reference: texels no reference sees (pure guess)
    "jacobian_min": 0.10,       # the warp nearly folds the concept somewhere (determinant of p + d(p))
    "squeezed_share": 0.05,     # share of the silhouette where the warp squeezes the concept below half
}

# ---------------------------------------------------------------------- bitmap font
_FONT = {
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#", "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".####/#..../#..../#..../#..../#..../.####", "D": "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E": "#####/#..../#..../####./#..../#..../#####", "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".####/#..../#..../#..##/#...#/#...#/.###.", "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": "#####/..#../..#../..#../..#../..#../#####", "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#", "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#", "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.", "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#", "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.", "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.", "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#", "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..", "Z": "#####/....#/...#./..#../.#.../#..../#####",
    "0": ".###./#...#/#..##/#.#.#/##..#/#...#/.###.", "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####", "3": "####./....#/....#/.###./....#/....#/####.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.", "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": ".###./#..../#..../####./#...#/#...#/.###.", "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.", "9": ".###./#...#/#...#/.####/....#/....#/.###.",
    "/": "....#/....#/...#./..#../.#.../#..../#....", "-": "...../...../...../#####/...../...../.....",
    ".": "...../...../...../...../...../.##../.##..", ":": "...../.##../.##../...../.##../.##../.....",
    "+": "...../..#../..#../#####/..#../..#../.....", " ": "...../...../...../...../...../...../.....",
}
_GLYPH = {k: np.array([[c == "#" for c in row] for row in v.split("/")], dtype=bool) for k, v in _FONT.items()}


def draw_text(img, text, x, y, scale=2, color=(1.0, 1.0, 1.0)):
    """Draw upper-case text into an (h, w, 3) top-first float image in place; unknown characters are skipped."""
    h, w = img.shape[:2]
    cx = int(x)
    for ch in str(text).upper():
        g = _GLYPH.get(ch)
        if g is None:
            continue
        big = np.kron(g, np.ones((scale, scale), dtype=bool))
        gh, gw = big.shape
        if cx + gw > w or y + gh > h:
            break
        region = img[y:y + gh, cx:cx + gw]
        region[big] = color
        cx += (5 + 1) * scale
    return img


def label_tile(tile, text, color, label_h=22, scale=2):
    """(h, w, 3) top-first tile with a label bar above it."""
    h, w = tile.shape[:2]
    bar = np.empty((label_h, w, 3), dtype=np.float32)
    bar[:] = np.asarray(color, dtype=np.float32) * 0.35
    draw_text(bar, text, 6, (label_h - 7 * scale) // 2, scale, (1.0, 1.0, 1.0))
    return np.concatenate([bar, tile.astype(np.float32)], axis=0)


def build_sheet(tiles, cols=4, gap=6, bg=(0.82, 0.82, 0.84), label_h=22):
    """tiles: list of (label, kind, tile (h, w, 3) top-first); kind 'ref' (reference painting) or 'model'
    colours the label bar. Returns the grid as (H, W, 3) top-first float32."""
    colors = {"ref": (0.2, 0.55, 0.95), "model": (0.95, 0.55, 0.15)}
    cells = [label_tile(t, lab, colors.get(kind, (0.5, 0.5, 0.5)), label_h) for lab, kind, t in tiles]
    if not cells:
        return np.ones((8, 8, 3), dtype=np.float32)
    ch = max(c.shape[0] for c in cells)
    cw = max(c.shape[1] for c in cells)
    rows = []
    for r in range(0, len(cells), cols):
        parts = []
        for k in range(cols):
            cell = np.empty((ch, cw, 3), dtype=np.float32)
            cell[:] = bg
            if r + k < len(cells):
                c = cells[r + k]
                cell[:c.shape[0], :c.shape[1]] = c
            parts.append(cell)
            if k < cols - 1:
                parts.append(np.full((ch, gap, 3), bg, dtype=np.float32))
        rows.append(np.concatenate(parts, axis=1))
        if r + cols < len(cells):
            rows.append(np.full((gap, rows[-1].shape[1], 3), bg, dtype=np.float32))
    return np.concatenate(rows, axis=0)


# ---------------------------------------------------------------------- verdict from the metrics
def judge(metrics, thresholds=None, has_back=False):
    """Metric problems -> list of {"metric", "value", "limit", "note"}. The fill share only counts with a back
    reference (a front-only run guesses 70-80 % of the texture by construction)."""
    t = dict(THRESHOLDS, **(thresholds or {}))
    out = []

    def add(name, value, limit, note, bad):
        if value is not None and bad:
            out.append({"metric": name, "value": value, "limit": limit, "note": note})

    for side in ("front", "back"):
        iou = metrics.get("iou_%s_aligned" % side)
        add("iou_%s_aligned" % side, iou, t["iou_aligned"], "silhouette of the model differs from the %s concept" % side,
            iou is not None and iou < t["iou_aligned"])
        mae = metrics.get("color_mae_%s" % side)
        add("color_mae_%s" % side, mae, t["color_mae"], "colours of the model differ from the %s concept" % side,
            mae is not None and mae > t["color_mae"])
        wp = (metrics.get("warp_%s" % side) or {}).get("p95_pct")
        add("warp_%s_p95_pct" % side, wp, t["warp_p95_pct"],
            "the concept had to be bent a lot: the mesh deviates from the %s concept" % side,
            wp is not None and wp > t["warp_p95_pct"])
        jm = (metrics.get("warp_%s" % side) or {}).get("jacobian_min")
        add("warp_%s_jacobian_min" % side, jm, t["jacobian_min"], "the %s warp nearly folds the concept" % side,
            jm is not None and jm < t["jacobian_min"])
        sq = (metrics.get("warp_%s" % side) or {}).get("squeezed_share")
        add("warp_%s_squeezed_share" % side, sq, t["squeezed_share"],
            "the %s warp squeezes the concept over a large area" % side, sq is not None and sq > t["squeezed_share"])
    g = metrics.get("grazing_share")
    add("grazing_share", g, t["grazing_share"], "much colour sits on faces seen at a grazing angle (smears)",
        g is not None and g > t["grazing_share"])
    fs = (metrics.get("texels") or {}).get("fill_share")
    add("fill_share", fs, t["fill_share_back"], "most of the texture is a guess although a back reference exists",
        has_back and fs is not None and fs > t["fill_share_back"])
    return out


# ---------------------------------------------------------------------- HTTP + the vision model
def _request(method, url, payload=None, timeout=30):
    """JSON over HTTP (urllib). Raises on any failure; the only network entry point of this module."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def build_prompt(has_back):
    back_a = ("CONCEPT BACK is the reference painting seen from behind and MODEL BACK is the model rendered "
              "from the same back camera with the texture projected from it. "
              if has_back else
              "There is no back reference: MODEL BACK is a plain rear view of the model, judge only whether it "
              "looks plausible and consistent with the front (no stripes, no front face or emblem smeared on "
              "the back). ")
    return (
        "You are a strict QA reviewer of a texture painted onto a 3D game character. The image is a contact "
        "sheet of tiles with a label bar each. Blue labels are REFERENCE paintings, orange labels are "
        "renders of the 3D MODEL. Tiles: CONCEPT FRONT is the reference painting (front view); MODEL FRONT is "
        "the model rendered from the same camera with the texture projected from that painting. " + back_a +
        "3/4 LEFT, 3/4 RIGHT and SIDE are the model only, seen from other angles, no reference exists for "
        "them, so judge them against the front (and back) reference. Lighting is flat, so you see the raw "
        "texture colours.\n"
        "Check: (1) every part has the reference's colour in the right place: face / skull, hands, helmet / "
        "head, sleeves, belt, boots, cloak, armour plates; (2) no smears, long stripes or streaks, especially "
        "on faces turned away from the front camera and in the 3/4 and SIDE views; (3) the back matches the "
        "back reference; (4) no colour bleeding between parts (sleeve colour on hands, face colour on the "
        "helmet rim, cloak colour on legs). Ignore a thin mismatch exactly at the silhouette edge and small "
        "differences in shading; the geometry may deviate slightly from the painting.\n"
        "Report only problems you can clearly see in a tile, never the same problem for several tiles and "
        "nothing for a tile that looks right. Answer with strict JSON only, no other text, keys in this order "
        "(look for the issues first, then judge): "
        '{"issues": [{"view": "<tile name>", "part": "<character part>", "problem": "<one short sentence>"}], '
        '"score": <integer 0-10: 10 flawless, 8 minor flaws, 6 acceptable for a game, 4 clearly wrong colours or '
        'smears, 2 broken>, "verdict": "ok" or "fix"}. '
        "At most 8 issues, most severe first; an empty list when there are none."
    )


def parse_review(text):
    """Model answer -> {"score": 0..10 int, "verdict": "ok"|"fix", "issues": [...]} or None."""
    if not text:
        return None
    s = str(text).strip()
    if "```" in s:                                   # strip code fences
        parts = s.split("```")
        inner = [p for p in parts[1::2]] or parts
        s = inner[0]
        if s.lower().startswith("json"):
            s = s[4:]
    obj = None
    a = s.find("{")
    while a != -1 and obj is None:                   # first balanced {...} that parses and carries a score
        depth = 0
        in_str = False
        esc = False
        for i in range(a, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        cand = json.loads(s[a:i + 1])
                    except ValueError:
                        cand = None
                    if isinstance(cand, dict) and "score" in cand:
                        obj = cand
                    break
        a = s.find("{", a + 1)
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    try:
        score = int(round(float(obj["score"])))
    except (TypeError, ValueError):
        return None
    score = max(0, min(10, score))
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in ("ok", "fix"):
        verdict = "ok" if score >= 6 else "fix"
    issues = []
    for it in (obj.get("issues") or [])[:12]:
        if isinstance(it, dict):
            issues.append({k: str(it.get(k, ""))[:240] for k in ("view", "part", "problem")})
        elif isinstance(it, str):
            issues.append({"view": "", "part": "", "problem": it[:240]})
    return {"score": score, "verdict": verdict, "issues": issues}


def _model_listed(tags, model):
    names = set()
    for m in (tags or {}).get("models", []) or []:
        for k in ("name", "model"):
            if m.get(k):
                names.add(str(m[k]))
    return model in names or (":" not in model and model + ":latest" in names)


def comfy_state(url):
    """-> (busy True/False/None, info). None = ComfyUI not reachable (nothing runs there)."""
    try:
        q = _request("GET", url.rstrip("/") + "/queue", timeout=5)
    except Exception as exc:                       # noqa: BLE001 - any failure means "not reachable"
        return None, "not reachable (%s)" % type(exc).__name__
    running = len(q.get("queue_running") or [])
    pending = len(q.get("queue_pending") or [])
    return (running + pending) > 0, "%d running, %d pending" % (running, pending)


def run_vlm(sheet_path, p, has_back, log=None):
    """Vision-model review of the sheet. Returns a dict that always has "mode" and "ran"; on success also
    "model", "score", "verdict", "issues", "seconds"; otherwise "skipped" (reason) and the caller adds the
    warning `qa_vlm_unavailable`. Never raises."""
    mode = str(p.get("qa_vlm", "auto")).lower()
    res = {"mode": mode, "ran": False}
    if mode in ("off", "none", "false", "0", "no"):
        res["skipped"] = "qa_vlm off"
        res["off"] = True
        return res
    url = str(p.get("qa_url") or os.environ.get("ASTRA_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = str(p.get("qa_model") or os.environ.get("ASTRA_QA_VLM") or "qwen3-vl:8b")
    comfy = str(p.get("qa_comfy_url") or os.environ.get("ASTRA_COMFY_URL") or "http://127.0.0.1:8190").rstrip("/")
    res.update({"model": model, "url": url})
    t0 = time.time()
    try:
        tags = _request("GET", url + "/api/tags", timeout=5)
    except Exception as exc:                       # noqa: BLE001
        res["skipped"] = "Ollama not reachable at %s (%s)" % (url, type(exc).__name__)
        return res
    if not _model_listed(tags, model):
        res["skipped"] = "model %s is not installed in Ollama (ollama pull %s)" % (model, model)
        return res
    # the GPU is shared with ComfyUI (mesh generation): never unload / compete while it works
    wait = max(0.0, float(p.get("qa_wait_comfy_s", 0) or 0))
    busy, info = comfy_state(comfy)
    waited = 0.0
    while busy and waited < wait:
        time.sleep(min(15.0, wait - waited))
        waited = time.time() - t0
        busy, info = comfy_state(comfy)
    res["comfy"] = info
    if busy:
        res["skipped"] = "ComfyUI is busy (%s): the GPU is not free for the review" % info
        return res
    if p.get("qa_free_comfy", True) and busy is not None:
        try:
            _request("POST", comfy + "/free", {"unload_models": True, "free_memory": True}, timeout=15)
            res["comfy_freed"] = True
        except Exception as exc:                   # noqa: BLE001 - freeing is best effort
            res["comfy_freed"] = False
            res["comfy_free_error"] = type(exc).__name__
    with open(sheet_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    # qwen3-vl is a thinking model: left alone it reasons for thousands of tokens over every tile and never
    # reaches the answer (measured: 8000 tokens, empty reply). Prefilling the reply with the first JSON key skips the
    # reasoning (seconds instead of minutes); other vision models just continue the JSON.
    prefill = '{"issues":'
    messages = [{"role": "user", "content": build_prompt(has_back), "images": [b64]},
                {"role": "assistant", "content": prefill}]
    payload = {"model": model, "stream": False, "keep_alive": 0, "think": False,
               "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1200}, "messages": messages}
    timeout = float(p.get("qa_timeout_s", 300))
    if log:
        log("qa_vlm", model=model)
    try:
        ans = _request("POST", url + "/api/chat", payload, timeout=timeout)
    except Exception as exc:                       # noqa: BLE001
        res["skipped"] = "chat call failed (%s: %s)" % (type(exc).__name__, str(exc)[:160])
        res["seconds"] = round(time.time() - t0, 1)
        return res
    msg = (ans or {}).get("message") or {}
    text = prefill + (msg.get("content") or "")
    review = parse_review(text) or parse_review(msg.get("thinking") or "")
    res["seconds"] = round(time.time() - t0, 1)
    if review is None:
        res["skipped"] = "unreadable answer: %r" % str(text)[:160]
        res["raw"] = str(text)[:1000]
        return res
    res.update(review)
    res["ran"] = True
    return res
