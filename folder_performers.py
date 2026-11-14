#!/usr/bin/env python3
"""
Folder Performers - Stash plugin

For every subfolder of the configured root directories:
  * find (or create) a performer named after the folder
  * add that performer to every scene, image and gallery whose files live
    anywhere inside that folder (including nested subfolders)

Runs as a manual task, and as a hook on newly scanned scenes/images/galleries.
Uses only the Python standard library.
"""
import base64
import json
import os
import ssl
import sys
import urllib.request

PLUGIN_ID = "folder_performers"  # must match the .yml file name
CHUNK = 500


# ---------------------------------------------------------------- logging ---
def _log(level, msg):
    sys.stderr.write(f"\x01{level}\x02{msg}\n")
    sys.stderr.flush()


def debug(m): _log("d", m)
def info(m): _log("i", m)
def warn(m): _log("w", m)
def error(m): _log("e", m)
def progress(p): _log("p", f"{min(max(p, 0.0), 1.0):.3f}")


# ---------------------------------------------------------------- graphql ---
class Stash:
    def __init__(self, conn):
        scheme = conn.get("Scheme") or "http"
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "localhost"
        port = conn.get("Port") or 9999
        self.url = f"{scheme}://{host}:{port}/graphql"
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        cookie = conn.get("SessionCookie")
        if cookie and cookie.get("Name"):
            self.headers["Cookie"] = f"{cookie['Name']}={cookie['Value']}"
        api_key = conn.get("ApiKey")
        if api_key:
            self.headers["ApiKey"] = api_key
        self.ctx = ssl._create_unverified_context() if scheme == "https" else None

    def q(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.url, data=body, headers=self.headers)
        with urllib.request.urlopen(req, context=self.ctx) as r:
            res = json.load(r)
        if res.get("errors"):
            raise RuntimeError(json.dumps(res["errors"]))
        return res["data"]


# ------------------------------------------------------------- definitions ---
KINDS = {
    #          find op         filter arg       filter type         result key   fields
    "scene":   ("findScenes",    "scene_filter",   "SceneFilterType",   "scenes",
                "files { path }"),
    "image":   ("findImages",    "image_filter",   "ImageFilterType",   "images",
                "visual_files { ... on ImageFile { path } ... on VideoFile { path } }"),
    "gallery": ("findGalleries", "gallery_filter", "GalleryFilterType", "galleries",
                "folder { path } files { path }"),
}
FIND_ONE = {"scene": "findScene", "image": "findImage", "gallery": "findGallery"}
BULK = {
    "scene":   ("bulkSceneUpdate",   "BulkSceneUpdateInput!"),
    "image":   ("bulkImageUpdate",   "BulkImageUpdateInput!"),
    "gallery": ("bulkGalleryUpdate", "BulkGalleryUpdateInput!"),
}


def item_paths(kind, item):
    paths = []
    if kind == "scene":
        paths = [f["path"] for f in item.get("files") or []]
    elif kind == "image":
        paths = [f["path"] for f in item.get("visual_files") or [] if f and f.get("path")]
    elif kind == "gallery":
        paths = [f["path"] for f in item.get("files") or []]
        if item.get("folder"):
            paths.append(item["folder"]["path"])
    return paths


def norm(p):
    return os.path.normpath(p).rstrip("/\\")


def inside(path, folder):
    """True if path is the folder itself or anything beneath it."""
    return (norm(path) + os.sep).startswith(norm(folder) + os.sep)


# ---------------------------------------------------------------- helpers ---
def get_roots(stash):
    cfg = stash.q("query { configuration { plugins } }")["configuration"]["plugins"] or {}
    raw = (cfg.get(PLUGIN_ID) or {}).get("rootDirectories") or ""
    return [norm(r.strip()) for r in raw.split(",") if r.strip()]


def find_in_folder(stash, kind, folder):
    op, farg, ftype, key, fields = KINDS[kind]
    query = (f"query($f: {ftype}, $p: FindFilterType) "
             f"{{ {op}(filter: $p, {farg}: $f) {{ {key} {{ id {fields} }} }} }}")
    data = stash.q(query, {"f": {"path": {"value": folder, "modifier": "INCLUDES"}},
                           "p": {"per_page": -1}})
    # INCLUDES is a substring match, so confirm each hit really lives in the folder
    return [it["id"] for it in data[op][key]
            if any(inside(p, folder) for p in item_paths(kind, it))]


def get_or_create_performer(stash, name):
    q = ("query($f: PerformerFilterType) { findPerformers(performer_filter: $f, "
         "filter: {per_page: 1}) { performers { id name } } }")
    for field in ("name", "aliases"):
        hits = stash.q(q, {"f": {field: {"value": name, "modifier": "EQUALS"}}})
        perf = hits["findPerformers"]["performers"]
        if perf:
            return perf[0]["id"], False
    created = stash.q("mutation($i: PerformerCreateInput!) { performerCreate(input: $i) { id } }",
                      {"i": {"name": name}})
    return created["performerCreate"]["id"], True


def add_performer(stash, kind, ids, performer_id):
    op, itype = BULK[kind]
    for i in range(0, len(ids), CHUNK):
        stash.q(f"mutation($i: {itype}) {{ {op}(input: $i) {{ id }} }}",
                {"i": {"ids": ids[i:i + CHUNK],
                       "performer_ids": {"ids": [performer_id], "mode": "ADD"},
                       "organized": True}})


COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}


def first_image(folder):
    """First image by name: files directly in the folder first, then subfolders."""
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=str.lower)
        for fn in sorted(filenames, key=str.lower):
            if os.path.splitext(fn)[1].lower() in COVER_TYPES and not fn.startswith("."):
                return os.path.join(dirpath, fn)
    return None


def set_cover_if_missing(stash, performer_id, folder):
    perf = stash.q("query($id: ID!) { findPerformer(id: $id) { image_path } }",
                   {"id": performer_id})["findPerformer"]
    if perf and perf.get("image_path") and "default=true" not in perf["image_path"]:
        return False  # already has a real cover - leave it alone
    img = first_image(folder)
    if not img:
        return False
    with open(img, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    mime = COVER_TYPES[os.path.splitext(img)[1].lower()]
    stash.q("mutation($i: PerformerUpdateInput!) { performerUpdate(input: $i) { id } }",
            {"i": {"id": performer_id, "image": f"data:{mime};base64,{data}"}})
    info(f"Set cover for performer {performer_id} from {img}")
    return True


# ------------------------------------------------------------------- task ---
def process_folder(stash, name, folder):
    found = {kind: find_in_folder(stash, kind, folder) for kind in KINDS}
    if not any(found.values()):
        debug(f"'{name}': nothing in Stash under {folder} yet - skipped (scan first?)")
        return
    pid, created = get_or_create_performer(stash, name)
    for kind, ids in found.items():
        if ids:
            add_performer(stash, kind, ids, pid)
    try:
        set_cover_if_missing(stash, pid, folder)
    except Exception as e:
        warn(f"'{name}': could not set cover image: {e}")
    info(f"{'Created' if created else 'Updated'} performer '{name}': "
         f"{len(found['scene'])} scenes, {len(found['image'])} images, "
         f"{len(found['gallery'])} galleries")


def run_task(stash, roots):
    folders = []
    for root in roots:
        if not os.path.isdir(root):
            warn(f"Root directory not found: {root}")
            continue
        for entry in sorted(os.listdir(root), key=str.lower):
            path = os.path.join(root, entry)
            if os.path.isdir(path) and not entry.startswith("."):
                folders.append((entry, path))
    info(f"Found {len(folders)} performer folder(s)")
    for i, (name, path) in enumerate(folders):
        progress(i / max(len(folders), 1))
        try:
            process_folder(stash, name, path)
        except Exception as e:
            error(f"'{name}': {e}")
    progress(1.0)


# ------------------------------------------------------------------- hook ---
def performer_folder_for(path, roots):
    """Return (performer name, performer folder path) for a file, or (None, None)."""
    for root in roots:
        if inside(path, root) and norm(path) != root:
            rel = os.path.relpath(norm(path), root)
            first = rel.split(os.sep)[0]
            # a file directly in the root has no performer folder
            if os.sep in rel or os.path.isdir(path):
                return first, os.path.join(root, first)
    return None, None


def run_hook(stash, roots, ctx):
    kind = {"Scene": "scene", "Image": "image", "Gallery": "gallery"}.get(
        (ctx.get("type") or "").split(".")[0])
    if not kind:
        return
    item_id = str(ctx.get("id"))
    op = FIND_ONE[kind]
    fields = KINDS[kind][4]
    item = stash.q(f"query($id: ID!) {{ {op}(id: $id) {{ id {fields} }} }}", {"id": item_id})[op]
    if not item:
        return
    for path in item_paths(kind, item):
        name, folder = performer_folder_for(path, roots)
        if name:
            pid, created = get_or_create_performer(stash, name)
            add_performer(stash, kind, [item_id], pid)
            try:
                set_cover_if_missing(stash, pid, folder)
            except Exception as e:
                warn(f"'{name}': could not set cover image: {e}")
            info(f"{kind} {item_id} -> performer '{name}'" + (" (created)" if created else ""))
            return


# ------------------------------------------------------------------- main ---
def main():
    payload = json.loads(sys.stdin.read() or "{}")
    stash = Stash(payload.get("server_connection") or {})
    args = payload.get("args") or {}

    roots = get_roots(stash)
    if not roots:
        msg = "No root directory set. Go to Settings > Plugins > Folder Performers."
        error(msg)
        print(json.dumps({"error": msg}))
        return

    ctx = args.get("hookContext")
    if ctx:
        run_hook(stash, roots, ctx)
    else:
        run_task(stash, roots)
    print(json.dumps({"output": "ok"}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error(str(e))
        print(json.dumps({"error": str(e)}))
