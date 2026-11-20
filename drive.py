"""The archive drive's side: what runs on the Mac the drive is plugged into.

The instance on that Mac imports it and calls `op()` in its own process. An instance on any other
Mac sends this file over SSH (`ssh <mac> /usr/bin/python3 -`) with one request appended, and reads
the answer from the last line it prints, so both Macs run the same code against the drive and
nothing has to be installed beside it. It runs on the Python macOS ships (3.9), standard library
only, and never deletes anything on the drive.

The drive's layout (Archive/README.txt says it too):
  Archive/<mac>/<path relative to that Mac's home>   what was moved, where it was
  Archive/manifest.jsonl                             one line per file moved: mac, original,
                                                     archive, bytes, sha256, moved (and mtime,
                                                     item, job; a link has symlink in place of
                                                     archive, bytes and sha256)
  Archive/restores.jsonl                             one line per item copied back
"""

import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time

CHUNK = 4 << 20
LOCK = "/tmp/archive-drive.lock"
"""Every write to the drive's lists takes this lock on the drive's own Mac (exFAT can't lock)."""


def archive_root(volume):
    return os.path.join(volume, "Archive")


def manifest_path(volume):
    return os.path.join(archive_root(volume), "manifest.jsonl")


def sha256(path, nocache=True):
    """A file's sha256, read from the disk rather than memory where macOS lets it (F_NOCACHE)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if nocache and hasattr(fcntl, "F_NOCACHE"):
            try:
                fcntl.fcntl(f.fileno(), fcntl.F_NOCACHE, 1)
            except OSError:
                pass
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def manifest(volume):
    """Every line of the manifest, in order (a torn last line is left out)."""
    lines = []
    try:
        with open(manifest_path(volume), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return lines


def group(entries):
    """Manifest lines grouped by item (what was asked to be moved), in the order they were moved.
    Lines from before items were written down share their mac and moment, and their item is what
    their originals share."""
    groups = {}
    for e in entries:
        key = (e.get("mac"), e.get("item") or "", e.get("job") or e.get("moved"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"mac": e.get("mac"), "item": e.get("item"), "job": e.get("job"), "moved": e.get("moved"),
                               "files": 0, "links": 0, "bytes": 0, "originals": [], "one": None}
        if e.get("archive") and g["one"] is None:
            g["one"] = (e.get("original") or "", e["archive"])
        if e.get("symlink") is not None:
            g["links"] += 1
        else:
            g["files"] += 1
            g["bytes"] += int(e.get("bytes") or 0)
        g["originals"].append(e.get("original") or "")
    out = []
    for g in groups.values():
        if not g["item"]:
            g["item"] = os.path.commonpath(g["originals"]) if len(g["originals"]) > 1 else g["originals"][0]
        # Where the item is on the drive: any of its files' archive path, less what follows the item.
        original, archive = g["one"] or ("", None)
        tail = original[len(g["item"]):]
        g["archive"] = archive[:len(archive) - len(tail)] if archive and archive.endswith(tail) else archive
        del g["originals"], g["one"]
        out.append(g)
    return out


def addresses():
    """Where another Mac can reach this one over SSH, best first: its LAN addresses, its Tailscale
    address (the CGNAT range) and its Bonjour name. Read from ifconfig, which needs nothing else."""
    try:
        said = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        said = ""
    lan, tailnet = [], []
    for ip in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", said):
        a, b = (int(x) for x in ip.split(".")[:2])
        if a == 127 or (a == 169 and b == 254):
            continue
        if a == 100 and 64 <= b <= 127:
            tailnet.append(ip)
        elif a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            lan.append(ip)
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        name = ""
    name = name or socket.gethostname().split(".")[0]
    found = []
    for ip in lan + tailnet + ([f"{name}.local"] if re.match(r"^[A-Za-z0-9-]+$", name or "") else []):
        if ip not in found:
            found.append(ip)
    return found


def usage(volume):
    try:
        du = shutil.disk_usage(volume)
        return {"size_bytes": du.total, "free_bytes": du.free, "used_bytes": du.used}
    except OSError:
        return {}


# What another Mac (or this one's instance) asks of the drive.

def op_status(req):
    volume = req["volume"]
    mounted = is_mounted(volume)
    out = {"volume": volume, "mounted": mounted, "host": socket.gethostname().split(".")[0],
           "user": os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))}
    if req.get("addresses"):
        out["addresses"] = addresses()
    if not mounted:
        return out
    out.update(usage(volume))
    out["archive"] = os.path.isdir(archive_root(volume))
    if req.get("totals", True):
        per = {}
        lines = manifest(volume)
        for e in lines:
            if e.get("symlink") is not None:
                continue
            p = per.setdefault(e.get("mac") or "?", {"files": 0, "bytes": 0, "last_moved": None})
            p["files"] += 1
            p["bytes"] += int(e.get("bytes") or 0)
            if e.get("moved") and (p["last_moved"] is None or e["moved"] > p["last_moved"]):
                p["last_moved"] = e["moved"]
        out["macs"] = per
        out["manifest_lines"] = len(lines)
        out["archived_bytes"] = sum(p["bytes"] for p in per.values())
    return out


def is_mounted(volume):
    """Mounted, or a folder that already holds Archive/ (a drive shared as a folder, or a plain disk folder)."""
    return os.path.ismount(volume) or os.path.isdir(archive_root(volume))


def need_mounted(volume):
    if not is_mounted(volume):
        raise RuntimeError(f"{os.path.basename(volume)} isn't plugged in to {socket.gethostname().split('.')[0]} (nothing is mounted at {volume})")


def op_prepare(req):
    """Makes the folders a move lands in, and says whether the drive has room for it, and which of
    its paths are already there: as an archived file of record (in the manifest) or a leftover."""
    volume = req["volume"]
    need_mounted(volume)
    for d in req.get("dirs") or []:
        os.makedirs(d, exist_ok=True)
    out = usage(volume)
    paths = req.get("paths") or []
    if paths:
        recorded = {}
        for e in manifest(volume):
            if e.get("archive"):
                recorded[e["archive"]] = e
        there = {}
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            e = recorded.get(p)
            there[p] = {"bytes": st.st_size, "mtime": int(st.st_mtime), "recorded": bool(e),
                        "sha256": e.get("sha256") if e else None}
        out["there"] = there
    return out


def op_hash(req):
    """Each path's size and sha256 as the drive holds it, or why it can't be read."""
    out = {}
    for p in req.get("paths") or []:
        try:
            out[p] = [os.path.getsize(p), sha256(p)]
        except Exception as e:
            out[p] = [None, f"{type(e).__name__}: {e}"]
    return {"hashes": out}


def op_append(req):
    """Appends lines to one of the drive's lists, under the drive's lock, and flushes them to it."""
    volume = req["volume"]
    need_mounted(volume)
    name = req.get("file") or "manifest.jsonl"
    if name not in ("manifest.jsonl", "restores.jsonl"):
        raise ValueError(f"no list called {name}")
    os.makedirs(archive_root(volume), exist_ok=True)
    text = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in req.get("lines") or [])
    with open(LOCK, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(os.path.join(archive_root(volume), name), "a", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    return {"appended": len(req.get("lines") or []), "file": name}


def matches(words, text):
    text = text.lower()
    return all(w in text for w in words)


def op_search(req):
    """The items archived whose original path has all of these words, newest first."""
    volume = req["volume"]
    need_mounted(volume)
    words = [w.lower() for w in (req.get("query") or "").split() if w]
    mac = req.get("mac")
    entries = [e for e in manifest(volume) if (not mac or e.get("mac") == mac)]
    groups = group(entries)
    root = archive_root(volume)
    hits = []
    for g in groups:
        if words and not matches(words, g["item"] or ""):
            continue
        hits.append(g)
    restores = {}
    try:
        with open(os.path.join(root, "restores.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                restores[(r.get("mac"), r.get("item"))] = r
    except FileNotFoundError:
        pass
    for g in hits:
        r = restores.get((g["mac"], g["item"]))
        if r:
            g["restored"] = {"to": r.get("to"), "at": r.get("restored")}
    hits.sort(key=lambda g: g.get("moved") or "", reverse=True)
    limit = int(req.get("limit") or 25)
    return {"items": hits[:limit], "found": len(hits)}


def op_entries(req):
    """The manifest lines of one item (everything moved from under its original path, in its
    latest move), for copying it back."""
    volume = req["volume"]
    need_mounted(volume)
    item, mac = req["item"].rstrip("/"), req.get("mac")
    lines = [e for e in manifest(volume) if e.get("mac") == mac and
             ((e.get("original") or "") == item or (e.get("original") or "").startswith(item + "/"))]
    latest = {}
    for e in lines:
        latest[e["original"]] = e  # a later move of the same file wins
    return {"entries": list(latest.values())}


def op_verify(req):
    """Reads archived files back off the drive and checks each against its manifest line: all of
    an item's (item), or a random sample (sample), up to max_bytes read."""
    volume = req["volume"]
    need_mounted(volume)
    mac, item = req.get("mac"), (req.get("item") or "").rstrip("/")
    words = [w.lower() for w in (req.get("query") or "").split() if w]
    files = [e for e in manifest(volume) if e.get("archive") and e.get("sha256") and (not mac or e.get("mac") == mac)]
    if item:
        files = [e for e in files if e["original"] == item or e["original"].startswith(item + "/")]
    if words:
        files = [e for e in files if matches(words, e.get("original") or "")]
    latest = {}
    for e in files:
        latest[e["archive"]] = e
    files = list(latest.values())
    sample = int(req.get("sample") or 0)
    if sample and len(files) > sample:
        files = random.sample(files, sample)
    budget = int(req.get("max_bytes") or 0)
    checked, ok, missing, changed, read, skipped = 0, 0, [], [], 0, 0
    started = time.time()
    for e in files:
        size = int(e.get("bytes") or 0)
        if budget and read + size > budget:
            skipped += 1
            continue
        checked += 1
        try:
            got = sha256(e["archive"])
        except FileNotFoundError:
            missing.append(e["archive"])
            continue
        except Exception as err:
            missing.append(f"{e['archive']} ({type(err).__name__}: {err})")
            continue
        read += size
        if got == e["sha256"] and os.path.getsize(e["archive"]) == size:
            ok += 1
        else:
            changed.append(e["archive"])
        if checked % 200 == 0:
            progress(f"checked {checked} of {len(files)} files ({read / 1e9:.1f} GB)")
    return {"checked": checked, "ok": ok, "missing": len(missing), "changed": len(changed),
            "examples": (missing + changed)[:10], "skipped_over_budget": skipped,
            "bytes_read": read, "seconds": round(time.time() - started, 1)}


def op_find(req):
    """The archived files (latest manifest lines) whose sha256 is each of these: {sha256: [archive paths]}."""
    volume = req["volume"]
    need_mounted(volume)
    wanted = set(req.get("sha256s") or [])
    found = {}
    for e in manifest(volume):
        if e.get("archive") and e.get("sha256") in wanted:
            paths = found.setdefault(e["sha256"], [])
            if e["archive"] not in paths:
                paths.append(e["archive"])
    return {"found": found}


def op_backups(req):
    """Each backup's newest file on the drive: {name, root, files, newest, bytes, mtime, partials}. A backup is a
    folder (path, on the drive or absolute) and a pattern its files match (default *, ** for any depth); names
    starting with a dot or ending in .partial are copies under way, counted apart."""
    volume = req["volume"]
    need_mounted(volume)
    import glob
    out = []
    for b in req.get("backups") or []:
        root = b["path"] if os.path.isabs(b["path"]) else os.path.join(volume, b["path"])
        pattern = b.get("pattern") or "*"
        found = {"name": b["name"], "root": root, "files": 0, "newest": None, "bytes": None, "mtime": None, "partials": 0,
                 "exists": os.path.isdir(root)}
        for p in glob.glob(os.path.join(glob.escape(root), pattern), recursive=True):
            name = os.path.basename(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if name.startswith(".") or name.endswith(".partial"):
                if time.time() - st.st_mtime < 3600:
                    found["partials"] += 1
                continue
            found["files"] += 1
            if found["mtime"] is None or st.st_mtime > found["mtime"]:
                size = st.st_size
                if os.path.isdir(p):
                    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs
                               if not os.path.islink(os.path.join(r, f)))
                found.update(newest=os.path.relpath(p, root), bytes=size, mtime=st.st_mtime)
        out.append(found)
    return {"backups": out, "now": time.time()}


def op_free(req):
    return usage(req["volume"])


OPS = {"status": op_status, "prepare": op_prepare, "hash": op_hash, "append": op_append, "search": op_search,
       "entries": op_entries, "verify": op_verify, "free": op_free, "find": op_find,
       "backups": op_backups}


def progress(words):
    """A progress line for whoever reads this over SSH."""
    print("PROGRESS " + json.dumps(words), flush=True)


def op(req):
    """Does one request here, in this process."""
    fn = OPS.get(req.get("op"))
    if fn is None:
        raise ValueError(f"the drive has no op {req.get('op')!r}")
    return fn(req)


def _main(text):
    """Over SSH: one request in, one answer out, as the last line."""
    try:
        answer = {"ok": True, "result": op(json.loads(text))}
    except Exception as e:
        answer = {"ok": False, "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__}
    sys.stdout.write("ANSWER " + json.dumps(answer, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__" and len(sys.argv) > 1:
    _main(sys.argv[1])
