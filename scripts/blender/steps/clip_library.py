"""clip_library - the Mixamo clip library, contract section 7 (data/library/animations).

params.action:
  "scan"    - params.folder (+ params.pattern, default "**/*.fbx") or params.files -> metadata of
              every FBX (frames, fps, skeleton, skin, category/tags guess, sha1, library match,
              motion duplicates) in <out_dir>/scan.json; nothing is copied.
  "import"  - same inputs (files may be [path | {path, name, category, tags, source}]): copies new
              clips to <library>/<source>/<id>.fbx with id "<source>-<slug>", writes
              <source>/<id>.poses.json and maintains index.json. source = files[].source, else
              "mixamo" for a Mixamo-named skeleton (mixamorig:/bare), else "fbx" (UE, Rigify...).
              Dedupe: identical SHA1, or identical motion (12 sampled world joint positions within
              3 mm, same frame count) - the smaller file without skin is kept, the others are
              recorded in `also_found`.
  "remove"  - params.ids: drop entries and their files.
  "preview" - params.ids (default: all): (re)write <id>.poses.json (~12 sampled world positions of
              the key joints for UI stick figures).
  "list"    - return the index.
params.library: default ASTRA_FORGE_LIBRARY/animations, else C:/AstraForge/library/animations on Windows.

This step writes into params.library by design (the library is shared state of the forge);
result.json and scan.json go to out_dir as usual. Licence note: index.json["licence"].
"""
import json
import os
import shutil
import time

import af_common as af

try:
    from steps import _anim_lib as lib
except ImportError:
    import _anim_lib as lib


def _inputs(P):
    files = []
    for f in P.get("files") or []:
        files.append(f if isinstance(f, dict) else {"path": f})
    if P.get("folder"):
        for p in lib.gather_fbx(P["folder"], P.get("pattern", "**/*.fbx")):
            files.append({"path": p})
    seen, out = set(), []
    for f in files:
        p = os.path.normcase(os.path.abspath(f["path"]))
        if p in seen:
            continue
        seen.add(p)
        f = dict(f, path=os.path.abspath(f["path"]))
        if not os.path.exists(f["path"]):
            raise FileNotFoundError(f["path"])
        out.append(f)
    if not out:
        raise ValueError("no FBX files given (params.files or params.folder)")
    return out


def _describe(f):
    meta, pose = lib.fbx_metadata(f["path"])
    if f.get("name"):
        meta["name"] = f["name"]
        meta["category"] = lib.guess_category(f["name"])
        meta["tags"] = lib.guess_tags(f["name"])
    if f.get("category"):
        meta["category"] = f["category"]
    if f.get("tags"):
        meta["tags"] = sorted(set(meta["tags"]) | set(f["tags"]))
    meta["pretty"] = meta["name"][:1].upper() + meta["name"][1:]
    pj = lib.poses_json(meta, pose, None)
    return meta, pj


def _rest_kind(elev):
    if elev is None:
        return None
    if abs(elev) < 10:
        return "T"
    if -70 < elev < -25:
        return "A"
    return "other"


def _load_poses(library, entry):
    p = os.path.join(library, entry.get("poses") or "")
    if entry.get("poses") and os.path.exists(p):
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return None


def _write_poses(library, clip_id, pj, folder="mixamo"):
    rel = "%s/%s.poses.json" % (folder, clip_id)
    pj = dict(pj, id=clip_id)
    path = os.path.join(library, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pj, fh, ensure_ascii=False, separators=(",", ":"))
    return rel


def _source_of(f, meta):
    """One value drives the entry's `source`, its id prefix and its sub-folder: an explicit
    files[].source (slug), else 'mixamo' for a Mixamo-named skeleton, else 'fbx'."""
    src = lib.slug(str(f.get("source"))) if f.get("source") else None
    return src or ("mixamo" if meta["skeleton"] == "mixamo" else "fbx")


def _sort_key(item):
    meta = item[1]
    p = meta["path"].replace("\\", "/").lower()
    return (bool(meta["has_skin"]), ("superseded" in p) or p.endswith("_prev.fbx"),
            meta["size"], len(p))


def _counts(entries):
    out = {}
    for e in entries:
        out[e["category"]] = out.get(e["category"], 0) + 1
    return dict(sorted(out.items()))


def run(req):
    P = req["params"]
    os.makedirs(req["out_dir"], exist_ok=True)
    action = P.get("action", "scan")
    library = os.path.abspath(P.get("library") or lib.DEFAULT_LIBRARY)
    outputs, warnings, checks = {}, [], []

    if action == "list":
        idx = lib.load_index(library)
        return {"outputs": {"index": lib.index_path(library)},
                "stats": {"clips": len(idx["clips"]), "by_category": _counts(idx["clips"])},
                "index": idx}

    if action in ("scan", "import"):
        files = _inputs(P)
        idx = lib.load_index(library)
        by_sha = {e.get("sha1"): e for e in idx["clips"]}
        described = []
        for f in files:
            af.log("describe", file=f["path"])
            try:
                described.append((f, *_describe(f)))
            except Exception as exc:  # a broken FBX must not stop a batch
                warnings.append("skipped %s: %s" % (f["path"], exc))
        described.sort(key=lambda it: _sort_key((it[0], it[1])))
        # ---- phase A: group duplicates (identical SHA1 or identical sampled motion)
        groups = []          # {"members": [(f, meta, pj)], "library": entry or None}
        lib_poses = {}
        for f, meta, pj in described:
            home = None
            if meta["sha1"] in by_sha:
                home = ("library", by_sha[meta["sha1"]], "sha1")
            if home is None:
                for g in groups:
                    if lib.poses_match(pj, g["members"][0][2]):
                        home = ("group", g, "motion")
                        break
            if home is None:
                for e in idx["clips"]:
                    if e.get("frames") != meta["frames"]:
                        continue
                    if e["id"] not in lib_poses:
                        lib_poses[e["id"]] = _load_poses(library, e)
                    pz = lib_poses[e["id"]]
                    if pz is not None and lib.poses_match(pj, pz):
                        home = ("library", e, "motion")
                        break
            if home is None:
                groups.append({"members": [(f, meta, pj)], "library": None, "why": None})
            elif home[0] == "group":
                home[1]["members"].append((f, meta, pj))
                home[1]["why"] = home[2]
            else:
                g = next((g for g in groups if g["library"] is home[1]), None)
                if g is None:
                    g = {"members": [], "library": home[1], "why": home[2]}
                    groups.append(g)
                g["members"].append((f, meta, pj))
        # ---- phase B: one entry per new group; file = smallest skinless member (sorted first),
        # name = production member (not superseded/_prev, shallowest path, shortest stem)
        results = []

        def name_key(m):
            p = m[1]["path"].replace("\\", "/").lower()
            sup = ("superseded" in p) or p.endswith("_prev.fbx") or "_prev" in p
            return (sup, "/candidates/" in p, len(os.path.basename(p)))

        for g in groups:
            members = g["members"]
            rows = []
            for f, meta, pj in members:
                row = {k: meta[k] for k in ("path", "name", "category", "tags", "skeleton",
                                            "bones", "frames", "fps", "duration", "has_skin",
                                            "root_motion", "hips_travel_m",
                                            "rest_arm_elevation_deg", "size", "sha1")}
                row["rest_pose"] = _rest_kind(meta["rest_arm_elevation_deg"])
                rows.append(row)
            if g["library"] is not None:
                e = g["library"]
                for row in rows:
                    row["duplicate_of"], row["duplicate_by"] = e["id"], g["why"]
                    if action == "import" and row["path"] != e.get("imported_from"):
                        also = e.setdefault("also_found", [])
                        if row["path"] not in also:
                            also.append(row["path"])
                results += rows
                continue
            file_m = members[0]
            name_m = min(members, key=name_key)
            for row in rows[1:]:
                row["duplicate_of"], row["duplicate_by"] = rows[0]["path"], g["why"]
            if action == "scan":
                results += rows
                continue
            meta = dict(file_m[1])
            explicit = file_m[0].get("name") or name_m[0].get("name")
            meta["name"] = explicit or name_m[1]["name"]
            meta["pretty"] = meta["name"][:1].upper() + meta["name"][1:]
            meta["category"] = (file_m[0].get("category") or name_m[0].get("category") or
                                lib.guess_category(meta["name"]))
            tags = set()
            for m in members:
                tags |= set(m[1]["tags"])
            ids = {e["id"] for e in idx["clips"]}
            source = _source_of(file_m[0], meta)
            base = source + "-" + lib.slug(meta["name"])
            cid, k = base, 2
            while cid in ids:
                cid, k = "%s-%d" % (base, k), k + 1
            rel = "%s/%s.fbx" % (source, cid)
            dst = os.path.join(library, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(meta["path"], dst)
            entry = {"id": cid, "name": meta["pretty"], "category": meta["category"],
                     "source": source,
                     "file": rel, "skeleton": meta["skeleton"], "bones": meta["bones"],
                     "frames": meta["frames"], "fps": meta["fps"], "duration": meta["duration"],
                     "frame_range": meta["frame_range"], "root_motion": meta["root_motion"],
                     "hips_travel_m": meta["hips_travel_m"], "tags": sorted(tags),
                     "imported_from": meta["path"], "has_skin": meta["has_skin"],
                     "rest_pose": rows[0]["rest_pose"], "size": meta["size"],
                     "sha1": meta["sha1"], "imported": time.strftime("%Y-%m-%dT%H:%M:%S")}
            aliases = sorted({m[1]["name"] for m in members} - {meta["name"]})
            if aliases:
                entry["aliases"] = aliases
            if len(members) > 1:
                entry["also_found"] = [m[1]["path"] for m in members[1:]]
            if P.get("preview", True):
                entry["poses"] = _write_poses(library, cid, file_m[2], source)
            idx["clips"].append(entry)
            by_sha[meta["sha1"]] = entry
            rows[0]["id"] = cid
            rows[0]["name"] = meta["name"]
            results += rows
        if action == "import":
            outputs["index"] = lib.save_index(library, idx)
        scan_path = af.out_path(req, "%s.json" % action)
        with open(scan_path, "w", encoding="utf-8") as fh:
            json.dump(af._jsonable({"library": library, "files": results}), fh,
                      ensure_ascii=False, indent=1)
        outputs[action] = scan_path
        new = [r for r in results if r.get("id")]
        dups = [r for r in results if r.get("duplicate_of")]
        stats = {"files": len(files), "described": len(described), "new": len(new),
                 "duplicates": len(dups), "library_clips": len(idx["clips"]),
                 "by_category": _counts(idx["clips"]) if action == "import" else
                 _counts([r for r in results if not r.get("duplicate_of")])}
        checks.append(af.check("all_described", len(described) == len(files),
                               len(described), len(files), severity="warning"))
        return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}

    if action == "remove":
        ids = set(P.get("ids") or [])
        if not ids:
            raise ValueError("params.ids is empty")
        idx = lib.load_index(library)
        removed = []
        for e in list(idx["clips"]):
            if e["id"] in ids:
                for key in ("file", "poses"):
                    if e.get(key):
                        p = os.path.join(library, e[key])
                        if os.path.exists(p):
                            os.remove(p)
                idx["clips"].remove(e)
                removed.append(e["id"])
        missing = sorted(ids - set(removed))
        if missing:
            warnings.append("not in library: %s" % ", ".join(missing))
        outputs["index"] = lib.save_index(library, idx)
        return {"outputs": outputs, "stats": {"removed": removed,
                                              "library_clips": len(idx["clips"])},
                "warnings": warnings}

    if action == "preview":
        idx = lib.load_index(library)
        ids = set(P.get("ids") or [e["id"] for e in idx["clips"]])
        done = []
        for e in idx["clips"]:
            if e["id"] not in ids:
                continue
            path = os.path.join(library, e["file"])
            meta, pose = lib.fbx_metadata(path)
            meta["name"] = e["name"]
            pj = lib.poses_json(meta, pose, e["id"])
            e["poses"] = _write_poses(library, e["id"], pj,
                                      os.path.dirname(e["file"]).replace("\\", "/") or "mixamo")
            done.append(e["id"])
        outputs["index"] = lib.save_index(library, idx)
        return {"outputs": outputs, "stats": {"previews": len(done), "ids": done}}

    raise ValueError("unknown action %r (scan|import|remove|preview|list)" % action)
