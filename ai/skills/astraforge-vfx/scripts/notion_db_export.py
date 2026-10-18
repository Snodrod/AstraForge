"""Export a public Notion database (a page on *.notion.site) to JSON and CSV without authentication.

Example (Simon Schreibt's "Textures for VFX" database, generators only):
  py -3 -I notion_db_export.py https://simonschreibt.notion.site/Textures-for-VFX-Database-2c72eccccfa84a0eae927d778ad746cc out --filter Type=generator

Writes to out/: database.json and database.csv (all rows of the first database found on the page) and, if --filter
is given, filtered.json. Talks only to the internal API of the public Notion site: loadCachedPageChunkV2 -> queryCollection.
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def post(site, method, body):
    req = urllib.request.Request(f"{site}/api/v3/{method}", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "user-agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def value(rec):
    v = rec.get("value", {})
    return v.get("value", v)


def page_id(url):
    m = re.search(r"([0-9a-f]{32})(?:[?#].*)?$", url.replace("-", ""))
    if not m:
        sys.exit("The URL has no Notion page id (32 hex characters)")
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def text(prop):
    return "".join(part[0] for part in prop) if prop else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--filter", help="Property=value for multi_select/select/text, e.g. Type=generator")
    ap.add_argument("--limit", type=int, default=5000)
    a = ap.parse_args()
    site = re.match(r"https?://[^/]+", a.url).group(0)
    pid = page_id(a.url)
    chunk = post(site, "loadCachedPageChunkV2", {"page": {"id": pid}, "limit": 100, "cursor": {"stack": []},
                                                  "chunkNumber": 0, "verticalColumns": False})
    blocks = chunk["recordMap"]["block"]
    space = value(blocks[pid])["space_id"]
    views = [(value(b).get("collection_id"), (value(b).get("view_ids") or [None])[0]) for b in blocks.values()
             if value(b).get("type") in ("collection_view", "collection_view_page")]
    views = [v for v in views if v[0] and v[1]]
    if not views:
        sys.exit("The page has no embedded database (collection_view)")
    # the database with the most rows is usually the "full" table; the others are its views
    best = None
    for cid, vid in views:
        q = post(site, "queryCollection?src=initial_load", {
            "source": {"type": "collection", "id": cid, "spaceId": space},
            "collectionView": {"id": vid, "spaceId": space},
            "loader": {"type": "reducer", "reducers": {"collection_group_results": {"type": "results", "limit": a.limit}},
                       "sort": [], "searchQuery": "", "userTimeZone": "UTC"}})
        ids = q["result"]["reducerResults"]["collection_group_results"]["blockIds"]
        if not best or len(ids) > len(best[1]):
            best = (q, ids, cid)
    q, ids, cid = best
    coll = value(q["recordMap"]["collection"][cid])
    schema, rows_blocks = coll["schema"], q["recordMap"]["block"]
    rows = []
    for bid in ids:
        if bid not in rows_blocks:
            continue
        props = value(rows_blocks[bid]).get("properties", {})
        row = {"id": bid}
        for key, s in schema.items():
            val = text(props.get(key))
            row[s["name"]] = [x for x in val.split(",") if x] if s["type"] == "multi_select" else val
        rows.append(row)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "database.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    names = [s["name"] for s in schema.values()]
    with open(out / "database.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(names + ["id"])
        for r in rows:
            w.writerow([", ".join(r.get(n, [])) if isinstance(r.get(n), list) else r.get(n, "") for n in names] + [r["id"]])
    msg = f"{len(rows)} rows, properties: {', '.join(names)}"
    if a.filter:
        k, v = a.filter.split("=", 1)
        sel = [r for r in rows if (v in r[k] if isinstance(r.get(k), list) else r.get(k) == v)]
        (out / "filtered.json").write_text(json.dumps(sel, ensure_ascii=False, indent=1), encoding="utf-8")
        msg += f"; matching filter {a.filter}: {len(sel)}"
    print(msg)


if __name__ == "__main__":
    main()
