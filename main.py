"""archive: moves files off a Mac onto an archive drive, verified file by file with sha256, with a manifest
on the drive; brings them back the same way; says whether a path has a verified copy there before it's
deleted; and minds the backups the user keeps on that drive.

On the Mac the drive is plugged into it copies straight onto it; from any other Mac it sends them over SSH
with rsync, by the fastest route it measures, two streams at once. Either way each file's size and sha256
are read back off the drive and must match the original before the original is removed, and then only the
files it checked go: anything that appeared meanwhile stays. Moves and restores are jobs that outlast the
call that started them and pick up again after a restart.

Its config is config.json in its home (see the README): the drive's volume, where else to reach it, and
the backups to mind.
"""

import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid

import drive as drivelib
import sbmcp

NAME = "archive"
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = sbmcp.home()
"""Where it keeps its jobs, state and config."""


CONFIG_PATH = os.path.join(HOME, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4", "-o", "StrictHostKeyChecking=accept-new"]
config_seen = None
"""config.json's (mtime, size) when it was last read, so a change to it takes effect without a restart."""


def apply_config(config):
    """Sets everything config.json decides. Its keys are in the README."""
    global CONFIG, USER_HOME, VOLUME, DRIVE_NAME, DRIVE, MAC, SSH, RSYNC, REMOTE_PYTHON, HOSTS
    global DRIVE_EVERY_S, WAIT_FOR_DRIVE_S, MARGIN, BACKUPS, BACKUP_EVERY_S
    CONFIG = config
    USER_HOME = os.path.expanduser(config.get("user_home") or "~")
    """The home folder whose files it moves; Archive/<mac>/ mirrors it."""
    VOLUME = (config.get("volume") or "").rstrip("/") or None
    """Where the archive drive mounts, like /Volumes/Archive."""
    DRIVE_NAME = config.get("drive_name") or (os.path.basename(VOLUME) if VOLUME else "the archive drive")
    DRIVE = "archive-drive:" + (re.sub(r"[^a-z0-9]+", "-", DRIVE_NAME.lower()).strip("-") or "drive")
    MAC = config.get("mac") or socket.gethostname().split(".")[0].lower()
    """This Mac's name, as Archive/<mac>/ knows it."""
    SSH = shlex.split(config.get("ssh") or "ssh")
    RSYNC = config.get("rsync") or "rsync"
    REMOTE_PYTHON = config.get("remote_python") or "/usr/bin/python3"
    HOSTS = [h.strip() for h in config.get("hosts") or [] if h.strip()]
    """Where to reach the drive's Mac (user@address) when it isn't plugged in here."""
    DRIVE_EVERY_S = float(config.get("check_minutes") or 5) * 60
    WAIT_FOR_DRIVE_S = float(config.get("wait_minutes") or 30) * 60
    """How long a job waits for the drive's Mac to come back (asleep, off the network) before an item gives up,
    its originals kept."""
    MARGIN = float(config.get("margin_gb") or 1) * 1e9
    """Room a move leaves on the drive, at the least."""
    BACKUPS = [b for b in config.get("backups") or [] if b.get("name") and b.get("path")]
    """The backups it minds on the drive: {name, path (on the drive, or absolute), pattern, max_age_hours}."""
    BACKUP_EVERY_S = float(config.get("backup_check_minutes") or 15) * 60


def refresh_config():
    """Reads config.json again when it has changed since it was last read (or appeared, or went away). A config
    that doesn't parse leaves the last good one in place, and says so."""
    global config_seen, CONFIG_LOADED
    try:
        st = os.stat(CONFIG_PATH)
        seen = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        seen = None
    if seen == config_seen and CONFIG_LOADED:
        return
    try:
        config = load_config()
    except (OSError, ValueError) as e:
        if CONFIG_LOADED:
            raise RuntimeError(f"{CONFIG_PATH} doesn't read ({e}), so the last config it read stands") from e
        config = {}
    apply_config(config)
    config_seen = seen
    CONFIG_LOADED = True


def need_volume():
    """Fails clearly when no archive drive is set, before anything reaches for it."""
    if not VOLUME:
        raise Unreachable(f"no archive drive configured: put volume in {CONFIG_PATH} (like {{\"volume\": \"/Volumes/Archive\"}})")


CONFIG_LOADED = False
refresh_config()
SPOT_CHECK_EVERY_S = 24 * 3600
RECENT_S = 10 * 60
"""Something written to in the last ten minutes is likely in use, so it isn't moved unless forced."""
HEARTBEAT_S = 5 * 60
"""How often it says how it is: its manifest's heartbeat."""

STANDARD = {"Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public", "Applications", "Library"}
VIDEO = {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".braw", ".mxf", ".hevc", ".3gp"}
AUDIO = {".wav", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".caf"}
IMAGE = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".tif", ".tiff", ".psd", ".gif"}
ARCHIVES = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".zst"}
INSTALLERS = {".dmg", ".iso", ".pkg"}
BUNDLES = {".screenstudio": "Screen Studio project", ".fcpbundle": "Final Cut library", ".app": "app",
           ".logicx": "Logic project", ".band": "GarageBand project"}
PRUNE = {"node_modules", ".git", ".venv", "venv", "__pycache__", "DerivedData", ".next", "Pods", ".build", ".gradle", "target"}
NEVER = {".photoslibrary", ".musiclibrary", ".tvlibrary", ".photolibrary", ".imovielibrary", ".lrlibrary", ".lrdata",
         ".photoboothlibrary", ".aplibrary", ".migratedphotolibrary"}
APP_OWNED = {"Music/Music", "Music/iTunes", "Movies/TV", "Music/GarageBand", "Pictures/Photo Booth Library"}
"""Folders an app keeps its library in: never candidates, never moved (move what the app exports)."""


def app_managed(rel):
    """Whether a path (under home) is in a library an app manages."""
    parts = rel.split("/")
    return any(os.path.splitext(p)[1].lower() in NEVER or p.endswith(" Library") for p in parts) or \
        any(rel == owned or rel.startswith(owned + "/") for owned in APP_OWNED)


DATALESS = 0x40000000  # SF_DATALESS: an iCloud file whose bytes aren't on this Mac

app = sbmcp.Server(NAME, version="1", instructions=(
    "Moves files off this Mac onto the archive drive, verified file by file with sha256 and written in the drive's "
    "manifest before any original is removed, and copies them back the same way. Archive/<mac>/<path under home> "
    "mirrors where each came from. candidates says what's worth moving, move starts a job (it outlasts the call; job "
    "waits on it), where finds what was archived, restore brings an item back, verify reads archived files back off "
    "the drive, safe_to_delete says whether paths have a verified copy on the drive before they're deleted. status "
    "says how the drive and the user's backups on it stand; it emits backup_stale when a backup falls behind."))
app.event("job", "A move or restore: how it stands", stream="archive/jobs", kind="observation")
app.event("item", "An item moved to the drive or brought back", stream="archive/items", kind="observation")
app.event("drive", "The archive drive: where it's plugged in, how to reach it, its space", stream="archive/drives",
          kind="observation", retention="signal")
app.event("ended", "A move or restore that ended after the call that started it answered", stream="archive/jobs",
          kind="observation")
app.event("surprise", "Archived files that no longer match what was moved", stream="surprises", kind="surprise")
app.event("backup", "A backup that landed on the drive: its newest file, size and age", stream="archive/backups",
          kind="observation", retention="signal")
app.event("backup_stale", "A backup on the drive is older than expected, or missing", stream="surprises", kind="surprise")
app.event("heartbeat", "How it is itself", stream=f"vitals/{NAME}", kind="observation", retention="signal")

state = {"started_at": int(time.time() * 1000), "calls": 0, "failed": 0, "last_error": None}
lock = threading.RLock()
saving = threading.Lock()
jobs = {}
"""Every job this instance knows, by id: what it keeps in HOME/jobs/<id>.json."""
running = {}
"""Jobs with a runner thread now, by id, with their rsync processes and cancel flags."""
route_cache = {"target": None, "at": 0, "probes": []}
watching = {}
"""Calls waiting on a job now, by its id."""


def now_ms():
    return int(time.time() * 1000)


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def gb(n):
    return f"{(n or 0) / 1e9:.1f} GB" if (n or 0) >= 1e8 else f"{(n or 0) / 1e6:.0f} MB"


def expand(path):
    """A path as given (~/..., or full), made whole against the home folder whose files it moves."""
    path = path.strip()
    if path == "~" or path.startswith("~/"):
        path = USER_HOME + path[1:]
    return os.path.abspath(path).rstrip("/") or "/"


def tilde(path):
    return "~/" + os.path.relpath(path, USER_HOME) if path.startswith(USER_HOME + "/") else path


def failing(fn=None, *, needs_drive=True):
    """A tool that counts its calls, and keeps its last failure for status. Each call reads config.json again if it
    changed; one that reaches the drive (`needs_drive`) fails clearly when none is set."""
    if fn is None:
        return lambda f: failing(f, needs_drive=needs_drive)

    def wrapped(*a, **k):
        state["calls"] += 1
        try:
            refresh_config()
            if needs_drive:
                need_volume()
            return fn(*a, **k)
        except Exception as e:
            state["failed"] += 1
            state["last_error"] = f"{fn.__name__}: {type(e).__name__}: {e}"
            raise
    wrapped.__name__, wrapped.__doc__ = fn.__name__, fn.__doc__
    wrapped.__signature__ = inspect.signature(fn)
    return wrapped


# The drive: here, or on the Mac it's plugged into, over SSH.

class Unreachable(RuntimeError):
    """The drive's Mac didn't answer: asleep, off the network, or the drive unplugged."""


def mounted_here():
    return bool(VOLUME) and drivelib.is_mounted(VOLUME)


def targets():
    """Every user@address the drive's Mac might answer on (hosts in config)."""
    return list(HOSTS), {}


def ssh_cmd(target, *command):
    return [*SSH, *SSH_OPTS, target, *command]


def probe(target, sample=8_000_000):
    """How fast this Mac reaches the drive's Mac at target: whether it answers, then MB/s for a
    sample sent through SSH."""
    t0 = time.time()
    try:
        ok = subprocess.run(ssh_cmd(target, "test", "-d", shlex.quote(drivelib.archive_root(VOLUME))),
                            capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        return {"target": target, "ok": False}
    latency = time.time() - t0
    t0 = time.time()
    try:
        sent = subprocess.run(ssh_cmd(target, "cat > /dev/null"), input=b"\0" * sample, capture_output=True, timeout=60)
        ok = sent.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    seconds = max(time.time() - t0, 1e-3)
    return {"target": target, "ok": ok, "connect_s": round(latency, 2), "mb_s": round(sample / seconds / 1e6, 1) if ok else None}


def route(fresh=False):
    """The fastest way to the drive's Mac right now (cached ten minutes), or why there's none."""
    with lock:
        if not fresh and route_cache["target"] and time.time() - route_cache["at"] < 600:
            return route_cache["target"]
    found, facts = targets()
    if not found:
        where = f" (last seen on {facts.get('host')}, {'plugged in' if facts.get('mounted') else 'unplugged'})" if facts else ""
        raise Unreachable(f"{DRIVE_NAME} isn't plugged in to this Mac, and no hosts are set to reach it{where}")
    probes = [probe(t) for t in found]
    good = sorted((p for p in probes if p["ok"]), key=lambda p: -(p["mb_s"] or 0))
    with lock:
        route_cache.update(target=good[0]["target"] if good else None, at=time.time(), probes=probes)
    if not good:
        raise Unreachable(f"Can't reach {DRIVE_NAME}'s Mac ({facts.get('host')}) over SSH: tried {', '.join(found)}")
    return good[0]["target"]


def drive(op, timeout=900, on_progress=None, **req):
    """One request of the drive (drive.py's ops): in this process when it's plugged in here, else
    over SSH on its Mac, sending drive.py along."""
    if not VOLUME:
        raise Unreachable("No archive drive set (volume in config.json)")
    req = {**req, "op": op, "volume": VOLUME}
    if mounted_here():
        return drivelib.op(req)
    target = route()
    with open(os.path.join(HERE, "drive.py"), encoding="utf-8") as f:
        script = f.read() + f"\n_main({json.dumps(json.dumps(req))})\n"
    p = subprocess.Popen(ssh_cmd(target, REMOTE_PYTHON, "-"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    answer, err = None, []
    try:
        p.stdin.write(script)
        p.stdin.close()
        for line in p.stdout:
            if line.startswith("PROGRESS ") and on_progress:
                try:
                    on_progress(json.loads(line[9:]))
                except ValueError:
                    pass
            elif line.startswith("ANSWER "):
                answer = json.loads(line[7:])
        err = p.stderr.read()
        p.wait()
    finally:
        timer.cancel()
        for pipe in (p.stdout, p.stderr):
            pipe.close()
    if answer is None:
        with lock:
            route_cache["target"] = None
        raise Unreachable(f"{DRIVE_NAME}'s Mac didn't answer {op} over SSH ({target}, exit {p.returncode}): {err.strip()[-300:]}")
    if not answer["ok"]:
        raise RuntimeError(answer["error"])
    return answer["result"]


def dest_of(path):
    return os.path.join(drivelib.archive_root(VOLUME), MAC, os.path.relpath(path, USER_HOME))


# What's on this Mac.

def walk(src):
    """Everything an item holds: its files (size, mtime), links (target), folders, what it can't
    move (sockets and the like), and iCloud files whose bytes aren't here."""
    out = {"files": [], "links": [], "dirs": [], "special": [], "dataless": [], "bytes": 0, "newest": 0}

    def add_file(p, st):
        if getattr(st, "st_flags", 0) & DATALESS:
            out["dataless"].append(p)
        out["files"].append([p, st.st_size, st.st_mtime_ns])
        out["bytes"] += st.st_size
        out["newest"] = max(out["newest"], st.st_mtime)

    st = os.lstat(src)
    if os.path.islink(src):
        raise ValueError(f"{tilde(src)} is a link, not a file or folder")
    if not os.path.isdir(src):
        add_file(src, st)
        return out
    for root, dirs, files in os.walk(src):
        out["dirs"].append(root)
        for name in list(dirs):
            p = os.path.join(root, name)
            if os.path.islink(p):
                dirs.remove(name)
                out["links"].append([p, os.readlink(p)])
        for name in files:
            p = os.path.join(root, name)
            st = os.lstat(p)
            if os.path.islink(p):
                out["links"].append([p, os.readlink(p)])
            elif os.path.isfile(p):
                add_file(p, st)
            else:
                out["special"].append(p)
    return out


def refusal(path, force=False, allow_hidden=False):
    """Why a path can't be moved, or None."""
    if not path.startswith(USER_HOME + "/"):
        return "it isn't in the home folder"
    rel = os.path.relpath(path, USER_HOME)
    parts = rel.split("/")
    if parts[0] == "Library" or parts[0] == ".Trash":
        return "it's in ~/" + parts[0] + ", which apps and macOS keep (clear caches, don't archive them)"
    if len(parts) == 1 and parts[0] in STANDARD:
        return f"~/{parts[0]} is one of macOS's own folders: move what's in it instead"
    if app_managed(rel):
        return "it's in a library an app manages (Photos, Music, Lightroom, Photo Booth): move what the app exports instead"
    if not allow_hidden and any(p.startswith(".") for p in parts):
        return "it's a hidden folder or file (allow_hidden to move it anyway)"
    if not os.path.lexists(path):
        return "there's nothing there"
    if os.path.islink(path):
        return "it's a link, not a file or folder"
    return None


def plan(paths, force=False, allow_hidden=False):
    """Each path as an item to move, or why it can't be."""
    items, refused, seen = [], [], set()
    busy = {i["path"] for j in jobs.values() if j["state"] in ("queued", "running", "waiting") for i in j["items"]}
    for raw in paths:
        path = expand(raw)
        why = refusal(path, force, allow_hidden)
        if not why and (path in busy or any(path.startswith(b + "/") or b.startswith(path + "/") for b in busy)):
            why = "a job running now is already moving it"
        if not why and (path in seen or any(path.startswith(s + "/") or s.startswith(path + "/") for s in seen)):
            why = "it's already in this move (inside another path given, or holding one)"
        tree = None
        if not why:
            try:
                tree = walk(path)
            except Exception as e:
                why = f"can't read it: {e}"
        if not why and tree["dataless"]:
            why = f"{len(tree['dataless'])} of its files are only in iCloud, not on this Mac (download them first)"
        if not why and not force and tree["newest"] and time.time() - tree["newest"] < RECENT_S:
            why = "something in it changed in the last ten minutes, so it may be in use (force to move it anyway)"
        if why:
            refused.append({"path": tilde(path), "why": why})
            continue
        seen.add(path)
        items.append({"path": path, "rel": os.path.relpath(path, USER_HOME), "dest": dest_of(path),
                      "dir": os.path.isdir(path), "bytes": tree["bytes"], "files": len(tree["files"]),
                      "links": len(tree["links"]), "special": len(tree["special"]), "state": "pending",
                      "attempts": 0, "transferred": 0, "error": None})
    return items, refused


def what_of(items):
    by = {}
    for i in items:
        top = i["rel"].split("/")[0] if "/" in i["rel"] else "~"
        by[top] = by.get(top, 0) + 1
    parts = ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:3])
    more = "…" if len(by) > 3 else ""
    total = sum(i["bytes"] for i in items)
    return f"{gb(total)} in {len(items)} item{'s' if len(items) != 1 else ''} ({parts}{more})"


# Jobs.

def jobs_dir():
    d = os.path.join(HOME, "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def save(job):
    path = os.path.join(jobs_dir(), job["id"] + ".json")
    for _ in range(5):
        try:
            with lock:
                text = json.dumps(job, ensure_ascii=False)
            break
        except RuntimeError:  # a worker changed it mid-way: read it again
            time.sleep(0.05)
    with saving:  # two streams of a job save it at once
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".tmp", path)


def load_jobs():
    for name in sorted(os.listdir(jobs_dir())):
        if name.endswith(".json"):
            try:
                with open(os.path.join(jobs_dir(), name), encoding="utf-8") as f:
                    job = json.load(f)
                jobs[job["id"]] = job
            except (ValueError, OSError, KeyError):
                continue


ACTIVE = ("copying", "verifying", "removing")


def counts(job):
    c = {"pending": 0, "active": 0, "done": 0, "failed": 0}
    for i in job["items"]:
        s = i["state"]
        c["done" if s == "done" else "failed" if s in ("failed", "cancelled") else "pending" if s == "pending" else "active"] += 1
    return c


def rate_of(job):
    """Bytes a second this job has sent, over the time it spent sending (not waiting)."""
    sent = sum(i.get("transferred") or 0 for i in job["items"])
    if not job.get("started"):
        return 0
    end = job.get("ended") or now_ms()
    seconds = (end - job["started"]) / 1000 - job.get("waited_s", 0)
    return sent / seconds if seconds > 5 and sent else 0


def progress_of(job):
    total = sum(i["bytes"] for i in job["items"])
    moved = sum(i["bytes"] for i in job["items"] if i["state"] == "done")
    left = sum(i["bytes"] for i in job["items"] if i["state"] == "pending") + \
        sum(max(i["bytes"] - (i.get("transferred") or 0), 0) for i in job["items"] if i["state"] in ACTIVE)
    rate = rate_of(job)
    eta = int(left / rate) if rate > 0 and left > 0 and job["state"] in ("running", "waiting") else None
    return total, moved, left, rate, eta


def view(job):
    """A job as the asker sees it."""
    total, moved, left, rate, eta = progress_of(job)
    c = counts(job)
    out = {"job": job["id"], "op": job["op"], "mac": job["mac"], "state": job["state"], "what": job["what"],
           "items": len(job["items"]), "done": c["done"], "failed": c["failed"], "left": c["pending"] + c["active"],
           "bytes": total, "bytes_done": moved, "rate_mb_s": round(rate / 1e6, 1) if rate else None,
           "eta_minutes": round(eta / 60) if eta is not None else None, "route": job.get("route"),
           "started": job.get("started"), "ended": job.get("ended"), "summary": summary(job)}
    if job.get("free_after") is not None:
        out["free_on_mac"] = gb(job["free_after"])
    problems = [{"path": tilde(i["path"]), "why": i["error"]} for i in job["items"] if i["state"] == "failed"]
    if problems:
        out["problems"] = problems[:20]
    working = [{"path": tilde(i["path"]), "state": i["state"], "of": gb(i["bytes"]), "sent": gb(i.get("transferred") or 0)}
               for i in job["items"] if i["state"] in ACTIVE]
    if working:
        out["working_on"] = working
    if job.get("notes"):
        out["notes"] = job["notes"][-5:]
    return out


def summary(job):
    total, moved, _, rate, eta = progress_of(job)
    c = counts(job)
    verb = "Moved" if job["op"] == "move" else "Brought back"
    where = f"to {DRIVE_NAME}" if job["op"] == "move" else f"from {DRIVE_NAME}"
    if job["state"] in ("running", "waiting", "queued"):
        s = f"{verb} {gb(moved)} of {gb(total)} {where} so far ({c['done']} of {len(job['items'])} items)"
        if job["state"] == "waiting":
            s += f", waiting for {DRIVE_NAME}'s Mac to answer"
        elif eta is not None:
            s += f", about {max(1, round(eta / 60))} min left at {rate / 1e6:.1f} MB/s"
        return s + "."
    s = f"{verb} {gb(moved)} {where}, every file checked" if moved else f"Nothing {'moved' if job['op'] == 'move' else 'came back'}"
    if c["done"]:
        s += f" ({c['done']} item{'s' if c['done'] != 1 else ''})"
    if c["failed"]:
        s += f"; {c['failed']} item{'s' if c['failed'] != 1 else ''} didn't, and stayed {'where they were' if job['op'] == 'move' else 'on the drive'}"
    if job["state"] == "cancelled":
        s += "; cancelled"
    s += "."
    if job["op"] == "move" and job.get("free_after") is not None:
        s += f" {job['mac']} has {gb(job['free_after'])} free now."
    return s




def emit_job(job, force=False):
    """Says how the job stands in archive/jobs: at once when forced, else at most every five minutes."""
    with lock:
        if not force and time.time() - job.get("_emitted", 0) < 300:
            return
        job["_emitted"] = time.time()
    total, moved, _, rate, eta = progress_of(job)
    c = counts(job)
    state_text = {"queued": "Starting", "running": "Moving" if job["op"] == "move" else "Bringing back",
                  "waiting": "Waiting for the drive", "done": "Done", "failed": "Failed", "cancelled": "Cancelled"}.get(job["state"], job["state"])
    if job["state"] == "done" and c["failed"]:
        state_text = f"Done, {c['failed']} failed"
    if job["state"] in ("running", "waiting"):
        eta_text = f"about {max(1, round(eta / 60))} min" if eta is not None else ""
    else:
        eta_text = time.strftime("%a %H:%M UTC", time.gmtime(job["ended"] / 1000)) if job.get("ended") else ""
    app.emit("job", {"entity": f"archive-job:{job['id']}", "kind": "archive-job", "name": f"{job['op'].capitalize()} on {job['mac']}",
                     "summary": summary(job),
                     "facts": {"op": job["op"], "mac": job["mac"], "state": job["state"], "state_text": state_text,
                               "what": job["what"], "items": len(job["items"]), "items_done": c["done"], "items_failed": c["failed"],
                               "bytes": total, "bytes_done": moved, "progress_text": f"{gb(moved)} of {gb(total)}",
                               "eta_s": eta, "eta_text": eta_text, "rate_bps": int(rate) if rate else None, "route": job.get("route"),
                               "created": job["created"], "started": job.get("started"), "ended": job.get("ended"),
                               "work": job.get("work"), "drive": DRIVE}},
             about=[DRIVE])


def new_job(op, items, work=None, notify_after=True, streams=0, notes=None):
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    job = {"id": f"{stamp}-{uuid.uuid4().hex[:4]}", "op": op, "mac": MAC, "state": "queued", "items": items,
           "what": what_of(items) if op == "move" else f"{gb(sum(i['bytes'] for i in items))} back to {MAC}: {os.path.basename(items[0]['path'])}",
           "created": now_ms(), "started": None, "ended": None, "work": work, "notify": notify_after, "streams": streams,
           "notes": notes or [], "route": None, "probes": [], "waited_s": 0.0, "free_after": None, "_emitted": 0}
    with lock:
        jobs[job["id"]] = job
    save(job)
    return job


def start(job):
    with lock:
        if job["id"] in running:
            return
        running[job["id"]] = {"cancel": threading.Event(), "procs": set()}
    threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{job['id']}").start()


def run_job(job):
    ctl = running[job["id"]]
    keep_awake = None
    if shutil.which("caffeinate"):
        try:
            keep_awake = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
        except OSError:
            keep_awake = None
    try:
        job["state"] = "running"
        job["started"] = job.get("started") or now_ms()
        if not mounted_here():
            try:
                job["route"] = route(fresh=True)
            except Unreachable as e:
                job["notes"].append(str(e))
                if not wait_for_drive(job, ctl):
                    raise
            job["probes"] = route_cache["probes"]
        save(job)
        emit_job(job, force=True)
        streams = job.get("streams") or (1 if mounted_here() else 2)
        threads = [threading.Thread(target=worker, args=(job, ctl), daemon=True) for _ in range(max(1, min(streams, 4)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        c = counts(job)
        job["state"] = "cancelled" if ctl["cancel"].is_set() else "done" if c["done"] or not c["failed"] else "failed"
    except Exception as e:
        job["state"] = "failed"
        job["notes"].append(f"{type(e).__name__}: {e}")
        state["last_error"] = f"job {job['id']}: {e}"
        for i in job["items"]:
            if i["state"] in ("pending",) + ACTIVE:
                i["state"], i["error"] = "failed", str(e)
    finally:
        job["ended"] = now_ms()
        job["free_after"] = shutil.disk_usage(USER_HOME).free
        save(job)
        emit_job(job, force=True)
        if keep_awake:
            keep_awake.terminate()
            keep_awake.wait(5)
        with lock:
            running.pop(job["id"], None)
            watched = watching.get(job["id"], 0) > 0
    if job.get("notify") and not watched:
        c = counts(job)
        what = "Archive" if job["op"] == "move" else "Restore"
        how = "cancelled" if job["state"] == "cancelled" else "failed" if job["state"] == "failed" else \
            "done, with problems" if c["failed"] else "done"
        app.emit("ended", {"entity": f"archive-job:{job['id']}", "summary": f"{what} {how} on {job['mac']}: {summary(job)}",
                           "work": job.get("work")}, about=[DRIVE])


def worker(job, ctl):
    while not ctl["cancel"].is_set():
        with lock:
            item = next((i for i in job["items"] if i["state"] == "pending"), None)
            if item is None:
                return
            item["state"] = "copying"
        try:
            for attempt in range(3):
                try:
                    (move_item if job["op"] == "move" else restore_item)(job, item, ctl)
                    break
                except Unreachable:
                    if attempt == 2 or not wait_for_drive(job, ctl):
                        raise
                    item["state"] = "copying"
        except Cancelled:
            item["state"], item["error"] = "cancelled", "cancelled before it finished; nothing of it was removed"
        except Exception as e:
            item["state"], item["error"] = "failed", f"{e}"
            app.log(f"{job['id']} {item['path']}: {type(e).__name__}: {e}")
        save(job)
        emit_job(job, force=len(job["items"]) <= 30)


class Cancelled(Exception):
    pass


def check(ctl):
    if ctl["cancel"].is_set():
        raise Cancelled()


def wait_for_drive(job, ctl):
    """Waits until the drive's Mac answers again (it woke, the network came back), up to
    WAIT_FOR_DRIVE_S, saying so meanwhile. False if it never did."""
    deadline, t0 = time.time() + WAIT_FOR_DRIVE_S, time.time()
    was = job["state"]
    job["state"] = "waiting"
    save(job)
    emit_job(job, force=True)
    try:
        pause = 5
        while time.time() < deadline:
            ctl["cancel"].wait(min(pause, max(0.5, deadline - time.time())))
            check(ctl)
            try:
                job["route"] = route(fresh=True)
                return True
            except Unreachable:
                pause = min(pause * 2, 60)
        return False
    finally:
        with lock:
            job["waited_s"] = job.get("waited_s", 0) + time.time() - t0
            job["state"] = "running" if was == "waiting" else was
        save(job)
        emit_job(job, force=True)


PROGRESS = re.compile(rb"^\s*([\d,]+)\s+(\d+)%")
_quotes = {}


def remote(target, path):
    """A path on the drive's Mac as rsync is given it. macOS's openrsync hands it to the remote
    shell as it is, so it's quoted here; rsync 3.2.4 and later escapes it itself."""
    if "q" not in _quotes:
        try:
            said = subprocess.run([RSYNC, "--version"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            said = ""
        m = re.search(r"version (\d+)\.(\d+)\.(\d+)", said)
        _quotes["q"] = "openrsync" in said or not m or tuple(map(int, m.groups())) < (3, 2, 4)
    return f"{target}:{shlex.quote(path) if _quotes['q'] else path}"
NETWORK = re.compile(r"connect|timed? ?out|broken pipe|reset by peer|closed|unreachable|no route|resolve", re.I)


def rsync(job, item, ctl, build, retries=4):
    """Runs rsync (its arguments from build(target), the route to the drive's Mac) until it
    succeeds, waiting for that Mac when it can't be reached; what it sends counts into the item."""
    last, base = "", item.get("transferred") or 0
    for attempt in range(1, retries + 1):
        check(ctl)
        item["attempts"] = item.get("attempts", 0) + 1
        target = job.get("route") or route()
        cmd = [RSYNC, "-rt", "--partial", "--timeout=300", "--progress", "-e", " ".join(shlex.quote(a) for a in [*SSH, *SSH_OPTS])]
        cmd += build(target)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ctl["procs"].add(p)
        err = []
        reader = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        reader.start()
        done_bytes, cur, buf, tick = 0, 0, b"", time.time()
        while True:
            chunk = p.stdout.read1(65536)
            if not chunk:
                break
            *pieces, buf = re.split(rb"[\r\n]", buf + chunk)
            for piece in pieces:
                m = PROGRESS.match(piece)
                if m:
                    n, pct = int(m.group(1).replace(b",", b"")), int(m.group(2))
                    if pct >= 100:
                        done_bytes, cur = done_bytes + n, 0
                    else:
                        cur = n
            item["transferred"] = base + done_bytes + cur
            if time.time() - tick > 30:
                tick = time.time()
                emit_job(job)
        p.wait()
        reader.join(5)
        p.stdout.close()
        p.stderr.close()
        ctl["procs"].discard(p)
        base = item["transferred"] = base + done_bytes + cur
        check(ctl)
        if p.returncode == 0:
            return
        said = b"".join(e for e in err if e).decode("utf-8", "replace").strip().splitlines()
        last = (said or [f"rsync exit {p.returncode}"])[-1]
        network = p.returncode in (10, 12, 30, 35, 255) or NETWORK.search(last)
        if not network or attempt == retries:
            break
        with lock:
            route_cache["target"] = None
        job["route"] = None
        if not wait_for_drive(job, ctl):
            last = f"{DRIVE_NAME}'s Mac didn't answer for {round(WAIT_FOR_DRIVE_S / 60)} min ({last})"
            break
    raise RuntimeError(f"copying didn't finish: {last}; the originals are untouched")


def copy_file(src, dst, ctl, item):
    """Copies one file onto the drive here, hashing what it reads, and flushes it to the drive."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    h = hashlib.sha256()
    tmp = dst + ".archive-partial"
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            check(ctl)
            block = fi.read(drivelib.CHUNK)
            if not block:
                break
            h.update(block)
            fo.write(block)
            item["transferred"] = (item.get("transferred") or 0) + len(block)
        fo.flush()
        try:
            import fcntl
            fcntl.fcntl(fo.fileno(), getattr(fcntl, "F_FULLFSYNC", 51))
        except Exception:
            os.fsync(fo.fileno())
    os.replace(tmp, dst)
    st = os.stat(src)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    return h.hexdigest()


def hash_local(paths, ctl):
    out = {}
    for p in paths:
        check(ctl)
        out[p] = drivelib.sha256(p, nocache=False)
    return out


def conflicts(item, tree):
    """Archived files of record already where this item would land, and different from it: then
    it goes beside them, named for the day, so nothing archived is ever written over."""
    sizes = {item["dest"] + p[len(item["path"]):]: size for p, size, _ in tree["files"]}
    there = {}
    paths = list(sizes)
    for k in range(0, len(paths), 2000):
        there.update(drive("prepare", paths=paths[k:k + 2000]).get("there") or {})
    return [d for d, info in there.items() if info.get("recorded") and info.get("bytes") != sizes.get(d)]


def move_item(job, item, ctl):
    src = item["path"]
    if not os.path.lexists(src):
        entries = drive("entries", item=src, mac=MAC).get("entries") or []
        if entries and all(e.get("job") == job["id"] for e in entries):
            item["state"], item["error"] = "done", None  # it was moved before a restart, and its record kept
            return
        raise RuntimeError("it's no longer there")
    tree = walk(src)
    if tree["dataless"]:
        raise RuntimeError(f"{len(tree['dataless'])} of its files are only in iCloud now")
    item["bytes"], item["files"] = tree["bytes"], len(tree["files"])
    free = drive("free").get("free_bytes")
    if free is not None and free < tree["bytes"] + MARGIN:
        raise RuntimeError(f"{DRIVE_NAME} has {gb(free)} free, too little for {gb(tree['bytes'])}")
    if not item.get("renamed") and conflicts(item, tree):
        stem, ext = (item["dest"], "") if item["dir"] else os.path.splitext(item["dest"])
        item["dest"] = f"{stem} (archived {time.strftime('%Y-%m-%d %H%M', time.gmtime())}){ext}"
        item["renamed"] = True
        job["notes"].append(f"{tilde(src)} differs from what's archived at its place already, so it went beside it as {os.path.basename(item['dest'])}")
    dest = item["dest"]
    mapping = {p: dest + p[len(src):] for p, _, _ in tree["files"]}
    item["state"] = "copying"
    save(job)
    if mounted_here():
        item["transferred"] = 0
        for d in tree["dirs"]:
            os.makedirs(dest + d[len(src):], exist_ok=True)
        local_hashes = {p: copy_file(p, mapping[p], ctl, item) for p, _, _ in tree["files"]}
    else:
        drive("prepare", dirs=[os.path.dirname(dest)])
        if item["dir"]:
            rsync(job, item, ctl, lambda target: [src + "/", remote(target, dest + "/")])
        else:
            rsync(job, item, ctl, lambda target: [src, remote(target, dest)])
        item["state"] = "verifying"
        save(job)
        local_hashes = hash_local([p for p, _, _ in tree["files"]], ctl)
    item["state"] = "verifying"
    save(job)
    for p, size, mtime_ns in tree["files"]:
        st = os.lstat(p)
        if st.st_size != size or st.st_mtime_ns != mtime_ns:
            raise RuntimeError(f"{tilde(p)} changed while it was being moved; nothing was removed")
    hashes, paths = {}, list(mapping.values())
    for k in range(0, len(paths), 2000):
        hashes.update(drive("hash", paths=paths[k:k + 2000], timeout=3600)["hashes"])
    bad = []
    for p, size, _ in tree["files"]:
        got = hashes.get(mapping[p]) or [None, "missing"]
        if got[0] != size or got[1] != local_hashes[p]:
            bad.append(f"{tilde(p)} ({'not on the drive: ' + str(got[1]) if got[0] is None else 'differs on the drive'})")
    if bad:
        raise RuntimeError(f"{len(bad)} file{'s' if len(bad) != 1 else ''} didn't check out on {DRIVE_NAME}, so nothing was removed: " + "; ".join(bad[:3]))
    moved = iso()
    lines = [{"mac": MAC, "original": p, "archive": mapping[p], "bytes": size, "sha256": local_hashes[p], "moved": moved,
              "mtime": int(mtime_ns // 1_000_000_000), "item": src, "job": job["id"]} for p, size, mtime_ns in tree["files"]]
    lines += [{"mac": MAC, "original": p, "symlink": to, "moved": moved, "item": src, "job": job["id"]} for p, to in tree["links"]]
    for k in range(0, len(lines), 2000):
        drive("append", lines=lines[k:k + 2000])
    item["state"] = "removing"
    save(job)
    kept = remove_checked(tree)
    item["state"], item["error"], item["moved"] = "done", None, moved
    if kept:
        item["kept"] = kept[:20]
        job["notes"].append(f"{tilde(src)}: kept {len(kept)} thing{'s' if len(kept) != 1 else ''} that appeared while it moved, like {tilde(kept[0])}")
    app.emit("item", {"entity": item_entity(src), "kind": "archive-item", "name": tilde(src),
                      "summary": f"Moved {tilde(src)} from {MAC} ({gb(tree['bytes'])}, {len(tree['files'])} files) to {DRIVE_NAME}",
                      "facts": {"mac": MAC, "original": src, "archive": dest, "files": len(tree["files"]), "links": len(tree["links"]),
                                "bytes": tree["bytes"], "moved": moved, "job": job["id"], "restored": None}},
             about=[f"archive-job:{job['id']}", DRIVE])


def item_entity(original, mac=None):
    return f"archive-item:{mac or MAC}-{hashlib.sha1(original.encode()).hexdigest()[:12]}"


def remove_checked(tree):
    """Removes exactly what was checked on the drive (its files and links), then the folders that
    leaves empty. Anything else (a file that appeared meanwhile) stays, and is said."""
    for p, _, _ in tree["files"]:
        os.remove(p)
    for p, _ in tree["links"]:
        os.remove(p)
    kept = []
    for d in sorted(tree["dirs"], key=len, reverse=True):
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            continue
        if ".DS_Store" in names:  # Finder's, remade at will
            os.remove(os.path.join(d, ".DS_Store"))
        try:
            os.rmdir(d)
        except OSError:
            kept += [os.path.join(d, n) for n in os.listdir(d) if not os.path.isdir(os.path.join(d, n))]
    return kept


# Bringing things back.

def restore_item(job, item, ctl):
    entries = item["entries"]
    files = [e for e in entries if e.get("archive")]
    links = [e for e in entries if e.get("symlink") is not None]
    final, src_root = item["path"], item["archive"]
    staging = os.path.join(os.path.dirname(final), f".{os.path.basename(final)}.archive-restoring")
    if os.path.lexists(final):
        raise RuntimeError(f"{tilde(final)} is there already; nothing was copied over it")
    if os.path.isdir(staging) and not os.path.islink(staging):
        shutil.rmtree(staging)
    elif os.path.lexists(staging):
        os.remove(staging)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    to_local = {e["archive"]: staging + e["archive"][len(src_root):] for e in files}
    item["transferred"] = 0
    if mounted_here():
        for e in files:
            copy_file(e["archive"], to_local[e["archive"]], ctl, item)
    elif item["dir"]:
        os.makedirs(staging, exist_ok=True)
        rsync(job, item, ctl, lambda target: [remote(target, src_root + "/"), staging + "/"])
    else:
        rsync(job, item, ctl, lambda target: [remote(target, src_root), staging])
    item["state"] = "verifying"
    save(job)
    bad = []
    for e in files:
        local = to_local[e["archive"]]
        try:
            ok = os.path.getsize(local) == e["bytes"] and drivelib.sha256(local, nocache=False) == e["sha256"]
        except OSError:
            ok = False
        if not ok:
            bad.append(e["archive"])
        elif e.get("mtime"):
            os.utime(local, (e["mtime"], e["mtime"]))
    if bad:
        raise RuntimeError(f"{len(bad)} file{'s' if len(bad) != 1 else ''} came back different from what was archived "
                           f"(like {bad[0]}); what came is in {tilde(staging)}, and nothing was put in place")
    for e in links:
        p = staging + e["original"][len(item["original"]):]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not os.path.lexists(p):
            os.symlink(e["symlink"], p)
    os.rename(staging, final)
    at = iso()
    drive("append", file="restores.jsonl", lines=[{"mac": item["from_mac"], "item": item["original"], "to": final, "to_mac": MAC,
                                                   "files": len(files), "bytes": item["bytes"], "restored": at, "job": job["id"]}])
    item["state"], item["error"] = "done", None
    app.emit("item", {"entity": item_entity(item["original"], item["from_mac"]), "kind": "archive-item", "name": tilde(item["original"]),
                      "summary": f"Brought {tilde(item['original'])} back from {DRIVE_NAME} to {tilde(final)} on {MAC} (its copy on the drive stays)",
                      "facts": {"mac": item["from_mac"], "original": item["original"], "archive": src_root, "restored": at,
                                "restored_to": final, "restored_mac": MAC, "job": job["id"]}},
             about=[f"archive-job:{job['id']}", DRIVE])


# Finding what's worth moving.

def kind_of(name, is_dir):
    ext = os.path.splitext(name)[1].lower()
    if is_dir:
        return BUNDLES.get(ext)
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in ARCHIVES or name.lower().endswith((".tar.gz", ".tar.zst")):
        return "archive"
    if ext in INSTALLERS:
        return "installer"
    return None


def scan(path, depth, deadline, opts):
    """Sizes a folder and collects what in it is worth archiving, as (bytes, media bytes, newest
    mtime, files, candidates). A quiet folder that's mostly media is one candidate rather than its
    files, unless it holds several such folders (a recordings app's folder of meetings), which are
    listed instead. Nothing directly in the home folder is a candidate as a whole."""
    total = media = files = 0
    newest = 0.0
    inner = []
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, 0, 0.0, 0, []
    for e in entries:
        if time.time() > deadline:
            opts["cut"] = True
            break
        name = e.name
        try:
            if e.is_symlink():
                continue
            is_dir = e.is_dir()
        except OSError:
            continue
        if name.startswith(".") or name in PRUNE:
            continue
        if depth == 0 and name == "Library":
            continue
        p = e.path
        if is_dir and app_managed(os.path.relpath(p, USER_HOME)):
            continue
        if is_dir:
            bundle = kind_of(name, True)
            t, m, n, f, sub = scan(p, depth + 1, deadline, opts)
            quiet = t >= opts["min"] and n < opts["cutoff"]
            if bundle == "app":
                m = 0
                if quiet and path in opts["app_homes"]:
                    inner.append({"path": p, "kind": bundle, "bytes": t, "files": f, "newest": n})
            elif bundle:
                m = t
                if quiet:
                    inner.append({"path": p, "kind": bundle, "bytes": t, "files": f, "newest": n})
                else:
                    inner += sub
            elif depth >= 1 and quiet and m >= 0.8 * t and \
                    sum(1 for c in sub if os.path.dirname(c["path"]) == p and c["kind"] != "video" and c["files"] > 1) < 2:
                inner.append({"path": p, "kind": "media folder", "bytes": t, "files": f, "newest": n})
            else:
                inner += sub
            total, media, files, newest = total + t, media + m, files + f, max(newest, n)
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        kind = kind_of(name, False)
        total, files, newest = total + st.st_size, files + 1, max(newest, st.st_mtime)
        if kind in ("video", "audio") or os.path.splitext(name)[1].lower() in IMAGE:
            media += st.st_size
        if kind and st.st_size >= opts["min"] and st.st_mtime < opts["cutoff"]:
            inner.append({"path": p, "kind": kind, "bytes": st.st_size, "files": 1, "newest": st.st_mtime})
    return total, media, newest, files, inner


def git_note(path):
    """The git repo a candidate is in (or is), and whether git tracks it there."""
    if os.path.isdir(os.path.join(path, ".git")):
        return path, False
    d = os.path.dirname(path)
    while d.startswith(USER_HOME + "/") and d != USER_HOME:
        if os.path.isdir(os.path.join(d, ".git")):
            try:
                tracked = subprocess.run(["git", "-C", d, "ls-files", "--error-unmatch", "--", os.path.relpath(path, d)],
                                         capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                tracked = False
            return d, tracked
        d = os.path.dirname(d)
    return None, False


# Its tools.

@app.tool(read_only=True, schema={"type": "object", "properties": {
    "min_mb": {"type": "number", "description": "Smallest thing worth listing, MB (default 50)."},
    "older_than_days": {"type": "number", "description": "Left alone at least this long (default 14)."},
    "folders_in": {"type": "array", "items": {"type": "string"}, "description": "Folders whose subfolders are each a candidate when quiet that long, like [\"~/Projects\"] (project or job folders)."},
    "limit": {"type": "integer", "description": "Most to list (default 40)."}}})
@failing
def candidates(min_mb: float = 50, older_than_days: float = 14, folders_in: list = None, limit: int = 40):
    """What on this Mac is worth moving to the archive drive, biggest first: videos, screen recordings (Screen Studio
    projects, folders of recordings), audio, old archives and backups (zip, tar), and with folders_in, project folders
    nobody has touched lately. Leaves out ~/Library, hidden folders, app-managed libraries (Photos), and what's in git
    repos' dependency folders; says when a file is tracked in a git repo. Installers (dmg, pkg) are listed to delete,
    not archive. Nothing is moved."""
    opts = {"min": int(min_mb * 1e6), "cutoff": time.time() - older_than_days * 86400, "cut": False,
            "app_homes": {os.path.join(USER_HOME, "Downloads"), os.path.join(USER_HOME, "Desktop")}}
    deadline = time.time() + 90
    _, _, _, _, inner = scan(USER_HOME, 0, deadline, opts)
    for folder in folders_in or []:
        root = expand(folder)
        try:
            subs = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
        except OSError as e:
            raise ValueError(f"can't read {folder}: {e}")
        for e in subs:
            t, _, n, f, _ = scan(e.path, 2, deadline, {**opts, "min": 1 << 62})
            if t >= opts["min"] and n < opts["cutoff"]:
                inner = [c for c in inner if not c["path"].startswith(e.path + "/")]
                inner.append({"path": e.path, "kind": "quiet folder", "bytes": t, "files": f, "newest": n})
    busy = {i["path"] for j in jobs.values() if j["state"] in ("queued", "running", "waiting") for i in j["items"]}
    inner.sort(key=lambda c: -c["bytes"])
    out, totals = [], {}
    for c in inner:
        if c["path"] in busy:
            continue
        entry = {"path": tilde(c["path"]), "kind": c["kind"], "size": gb(c["bytes"]), "bytes": c["bytes"], "files": c["files"],
                 "last_changed": time.strftime("%Y-%m-%d", time.localtime(c["newest"])) if c["newest"] else None,
                 "do": "delete (an installer)" if c["kind"] == "installer" else "archive"}
        if len(out) < limit:
            repo, tracked = git_note(c["path"])
            if repo == c["path"]:
                entry["git"] = "a whole git repo (push it first if its history matters)"
            elif repo:
                entry["git"] = f"{'tracked in' if tracked else 'untracked, in'} {tilde(repo)}"
                if tracked:
                    entry["do"] = "leave (tracked in git: moving it shows as deleted there)"
            out.append(entry)
        totals[c["kind"]] = totals.get(c["kind"], 0) + c["bytes"]
    archivable = sum(c["bytes"] for c in inner if c["kind"] != "installer" and c["path"] not in busy)
    free = shutil.disk_usage(USER_HOME).free
    answer = {"mac": MAC, "free": gb(free), "archivable": gb(archivable), "by_kind": {k: gb(v) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])},
              "candidates": out, "listed": len(out), "found": len(inner)}
    if opts["cut"]:
        answer["note"] = "It stopped looking after 90 seconds; there may be more."
    return answer


@app.tool(destructive=True, schema={"type": "object", "required": ["paths"], "properties": {
    "paths": {"type": "array", "items": {"type": "string"}, "description": "Files and folders on this Mac, under its home folder (~/... or full paths)."},
    "dry_run": {"type": "boolean", "description": "Only say what would move, where to, how big, and how long it'd take; move nothing."},
    "wait_seconds": {"type": "number", "description": "How long to wait for it before answering with the job as it stands (default 60, at most 600); it carries on after."},
    "notify": {"type": "boolean", "description": "Emit ended when it ends, if it ends after this call answered (default true)."},
    "streams": {"type": "integer", "description": "Items copied at once (default 2 over the network, 1 onto the drive here)."},
    "force": {"type": "boolean", "description": "Move things changed in the last ten minutes too."},
    "allow_hidden": {"type": "boolean", "description": "Move hidden files and folders too (names starting with a dot)."}}})
@failing
def move(paths: list, dry_run: bool = False, wait_seconds: float = 60, notify: bool = True, streams: int = 0,
         force: bool = False, allow_hidden: bool = False, ctx=None):
    """Move files and folders off this Mac onto the archive drive, verified: each lands at Archive/<this mac>/<its path
    under home>, every file's size and sha256 are read back off the drive and must match before its original is
    removed, the move is written in the drive's manifest, and only the files it checked are removed (anything that
    appears meanwhile stays). Something archived at the same place already is never written over: a different version
    goes beside it, named for the day. Starts a job that outlasts the call (job waits on it, ended is emitted when it
    ends), picks up again after a restart, and keeps the Mac awake while it runs. Refuses ~/Library, macOS's own
    folders, app libraries, hidden things, links, iCloud-only files and anything changed in the last ten minutes."""
    if not paths:
        raise ValueError("give paths to move")
    items, refused = plan(paths, force, allow_hidden)
    if not items:
        raise ValueError("Nothing to move: " + "; ".join(f"{r['path']}: {r['why']}" for r in refused))
    total = sum(i["bytes"] for i in items)
    here = mounted_here()
    info = {"mac": MAC, "to": f"{DRIVE_NAME}/Archive/{MAC}/", "items": len(items), "bytes": total, "size": gb(total),
            "what": what_of(items), "refused": refused}
    if dry_run:
        free = drive("free").get("free_bytes")
        info["drive_free"] = gb(free) if free is not None else None
        info["fits"] = free is None or free > total + MARGIN
        if not here:
            target = route(fresh=True)
            info["route"] = target
            info["probes"] = route_cache["probes"]
            mb_s = next((p["mb_s"] for p in route_cache["probes"] if p["target"] == target), None)
            if mb_s:
                info["estimate_minutes"] = round(total / (mb_s * 1.6e6) / 60) or 1  # two streams beat one probe by about 60%
        info["plan"] = [{"path": tilde(i["path"]), "to": i["dest"], "size": gb(i["bytes"]), "files": i["files"]} for i in items[:50]]
        return info
    job = new_job("move", items, work=ctx.work if ctx else None, notify_after=notify, streams=streams,
                  notes=[f"Refused {r['path']}: {r['why']}" for r in refused])
    start(job)
    return follow(job, wait_seconds, ctx)


def follow(job, wait_seconds, ctx):
    """Waits on a job for the asker (saying how it goes), then answers how it stands. While someone
    waits on it this way, its end isn't emitted separately: they hear it here."""
    deadline = time.time() + max(0, min(float(wait_seconds or 0), 600))
    said = None
    with lock:
        watching[job["id"]] = watching.get(job["id"], 0) + 1
    try:
        while time.time() < deadline and (job["state"] in ("queued", "running", "waiting") or job["id"] in running):
            line = summary(job)
            if ctx and line != said:
                ctx.progress(line)
                said = line
            if ctx and ctx.cancelled.is_set():
                break
            time.sleep(1)
    finally:
        with lock:
            watching[job["id"]] -= 1
    out = view(job)
    if job["state"] in ("queued", "running", "waiting"):
        out["note"] = ("It carries on by itself" + (", and ended is emitted when it does" if job.get("notify") else "") +
                       f": job with id {job['id']} waits on it.")
    return out


@app.tool(name="job", read_only=True, schema={"type": "object", "properties": {
    "id": {"type": "string", "description": "The job's id (default: the latest)."},
    "wait_seconds": {"type": "number", "description": "Wait this long for it to end (default 0, at most 600)."}}})
@failing
def job_tool(id: str = "", wait_seconds: float = 0, ctx=None):
    """A move or restore job as it stands: each item done, failed (and why) or under way, how much has moved, the rate
    and time left; with wait_seconds, waits for it to end first. Without id, the latest job."""
    with lock:
        found = jobs.get(id) if id else (max(jobs.values(), key=lambda j: j["created"]) if jobs else None)
    if not found:
        raise ValueError(f"no job {id}" if id else "no jobs yet")
    return follow(found, wait_seconds, ctx)


@app.tool(schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}})
@failing
def cancel(id: str):
    """Stop a job: the item being copied stops at once and keeps its originals, items already moved stay moved."""
    with lock:
        j, ctl = jobs.get(id), running.get(id)
    if not j:
        raise ValueError(f"no job {id}")
    if not ctl:
        return {"job": id, "state": j["state"], "note": "it isn't running"}
    ctl["cancel"].set()
    for p in list(ctl["procs"]):
        p.terminate()
    for i in j["items"]:
        if i["state"] == "pending":
            i["state"], i["error"] = "cancelled", "cancelled before it started"
    save(j)
    return {"job": id, "state": "cancelling", "summary": summary(j)}


@app.tool(read_only=True, schema={"type": "object", "properties": {
    "query": {"type": "string", "description": "Words in its original path, like \"tax 2024\" or \"screenstudio 2026-09\"."},
    "mac": {"type": "string", "description": "Only what came from this Mac (its name, as status says it)."},
    "limit": {"type": "integer"}}})
@failing
def where(query: str = "", mac: str = "", limit: int = 25):
    """What's archived on the drive, found by words in where it came from: each item's original path and Mac, where it
    is on the drive, its size and files, when it moved, and whether it's been brought back since. Newest first."""
    found = drive("search", query=query, mac=mac or None, limit=limit)
    for g in found["items"]:
        g["size"] = gb(g["bytes"])
    return found


@app.tool(schema={"type": "object", "required": ["item"], "properties": {
    "item": {"type": "string", "description": "Its original path (as where lists it), or words that find exactly one item."},
    "from_mac": {"type": "string", "description": "The Mac it came from (default this one)."},
    "to": {"type": "string", "description": "A folder on this Mac to put it in instead of where it was (needed for another Mac's item)."},
    "wait_seconds": {"type": "number", "description": "How long to wait before answering (default 60); it carries on after."},
    "notify": {"type": "boolean"}}})
@failing
def restore(item: str, from_mac: str = "", to: str = "", wait_seconds: float = 60, notify: bool = True, ctx=None):
    """Bring an archived item back onto this Mac, verified: it's copied into a hidden folder beside where it goes, every
    file must match the sha256 it was archived with, its links are made again, and only then is it put in place
    (never over something there). Its copy on the drive stays. Starts a job like move."""
    from_mac = from_mac or MAC
    original = expand(item) if item.startswith(("/", "~/")) else None
    if original is None:
        hits = drive("search", query=item, mac=from_mac, limit=5)
        if hits["found"] != 1:
            names = ", ".join(tilde(h["item"]) for h in hits["items"])
            raise ValueError(f"'{item}' finds {hits['found']} archived items{': ' + names if names else ''}; give its original path")
        original = hits["items"][0]["item"]
    entries = drive("entries", item=original, mac=from_mac).get("entries") or []
    if not entries:
        raise ValueError(f"nothing from {original} on {from_mac} is in the manifest")
    if from_mac != MAC and not to:
        raise ValueError(f"it came from {from_mac}: give to, a folder on this Mac to put it in")
    final = os.path.join(expand(to), os.path.basename(original)) if to else original
    if os.path.lexists(final):
        raise ValueError(f"{tilde(final)} is there already: give to, another folder to put it in")
    single = len(entries) == 1 and entries[0].get("original") == original
    archive = entries[0]["archive"] if single else None
    if not single:
        # Where its folder is on the drive: from any file in it.
        e = next(e for e in entries if e.get("archive"))
        archive = e["archive"][: len(e["archive"]) - len(e["original"][len(original):])]
    files = [e for e in entries if e.get("archive")]
    it = {"path": final, "original": original, "from_mac": from_mac, "archive": archive, "dir": not single, "entries": entries,
          "bytes": sum(int(e.get("bytes") or 0) for e in files), "files": len(files), "state": "pending", "attempts": 0,
          "transferred": 0, "error": None, "rel": os.path.relpath(final, USER_HOME) if final.startswith(USER_HOME) else final}
    j = new_job("restore", [it], work=ctx.work if ctx else None, notify_after=notify)
    start(j)
    return follow(j, wait_seconds, ctx)


@app.tool(read_only=True, schema={"type": "object", "properties": {
    "item": {"type": "string", "description": "One item's original path: check all of its files."},
    "query": {"type": "string", "description": "Or words in original paths."},
    "mac": {"type": "string"},
    "sample": {"type": "integer", "description": "Check this many files picked at random (default 200 when nothing narrows it)."},
    "max_gb": {"type": "number", "description": "Read at most this much (default 50)."}}})
@failing
def verify(item: str = "", query: str = "", mac: str = "", sample: int = 0, max_gb: float = 50, ctx=None):
    """Read archived files back off the drive and check each against the size and sha256 it was archived with: an
    item's, words' worth, or a random sample. Says how many match, are missing or have changed, with examples."""
    if not item and not query and not sample:
        sample = 200
    if item.startswith(("/", "~/")):
        item = expand(item)
    return drive("verify", item=item or None, query=query or None, mac=mac or None, sample=sample, max_bytes=int(max_gb * 1e9),
                 timeout=7200, on_progress=(lambda w: ctx.progress(w)) if ctx else None)


# Backups on the drive.

def age_text(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def backups_now():
    """Each configured backup as the drive has it now: its newest file, age, and whether it's stale."""
    if not BACKUPS:
        return []
    r = drive("backups", backups=[{"name": b["name"], "path": b["path"], "pattern": b.get("pattern") or "*"} for b in BACKUPS])
    by = {b["name"]: b for b in BACKUPS}
    out = []
    for f in r["backups"]:
        limit = float(by[f["name"]].get("max_age_hours") or 26) * 3600
        age = r["now"] - f["mtime"] if f["mtime"] is not None else None
        out.append({"name": f["name"], "root": f["root"], "newest": f["newest"], "files": f["files"],
                    "size": gb(f["bytes"]) if f["bytes"] is not None else None,
                    "landed": iso(f["mtime"]) if f["mtime"] is not None else None,
                    "age_hours": round(age / 3600, 1) if age is not None else None, "max_age_hours": limit / 3600,
                    "stale": age is None or age > limit, "copying": f["partials"] > 0, "folder_missing": not f["exists"]})
    return out


def mind_backups():
    """Says when a backup lands (its newest file changes) and, once each time, when one falls behind."""
    found = backups_now()
    st = read_state()
    kept = st.get("backups") or {}
    for b in found:
        was = kept.get(b["name"]) or {}
        if b["newest"] and (b["newest"], b["landed"]) != (was.get("newest"), was.get("landed")):
            app.emit("backup", {"entity": f"archive-backup:{re.sub(r'[^a-z0-9]+', '-', b['name'].lower()).strip('-')}",
                                "summary": f"{b['name']}: {b['newest']} ({b['size']}) on {DRIVE_NAME}", "facts": b}, about=[DRIVE])
        if b["stale"] and not was.get("stale"):
            why = (f"no backup at {b['root']}" if not b["newest"] else
                   f"its newest, {b['newest']}, is {age_text(b['age_hours'] * 3600)} old (expected within {age_text(b['max_age_hours'] * 3600)})")
            app.emit("backup_stale", {"text": f"{b['name']} on {DRIVE_NAME} is behind: {why}.", "severity": "medium", "facts": b},
                     about=[DRIVE])
        kept[b["name"]] = {"newest": b["newest"], "landed": b["landed"], "stale": b["stale"]}
    st["backups"] = kept
    write_state(st)
    return found


# Copies before deleting.

@app.tool(read_only=True, schema={"type": "object", "required": ["paths"], "properties": {
    "paths": {"type": "array", "items": {"type": "string"}, "description": "Files and folders on this Mac (~/... or full paths)."},
    "max_gb": {"type": "number", "description": "Read at most this much of them (default 50)."}}})
@failing
def safe_to_delete(paths: list, max_gb: float = 50, ctx=None):
    """Whether each path has a verified copy on the archive drive, so it can be deleted: every file in it is hashed
    here, found in the drive's manifest by its sha256 (wherever it was archived from), and that copy is read back off
    the drive and must match in size and sha256. Says per path how many files have a copy and which don't. Reads only;
    deletes nothing."""
    if not paths:
        raise ValueError("give paths to check")
    budget, read = int(max_gb * 1e9), 0
    local = []
    for raw in paths:
        path = expand(raw)
        entry = {"path": tilde(path), "files": 0, "bytes": 0, "hashes": {}, "unread": []}
        try:
            tree = walk(path)
        except Exception as e:
            entry["error"] = f"can't read it: {e}"
            local.append(entry)
            continue
        for p, size, _ in tree["files"]:
            entry["files"] += 1
            entry["bytes"] += size
            if p in tree["dataless"]:
                entry["unread"].append(f"{tilde(p)} (only in iCloud)")
            elif read + size > budget:
                entry["unread"].append(f"{tilde(p)} (over max_gb)")
            else:
                entry["hashes"][p] = (size, drivelib.sha256(p, nocache=False))
                read += size
        entry["unread"] += [f"{tilde(p)} (not a file)" for p in tree["special"]]
        local.append(entry)
        if ctx:
            ctx.progress(f"hashed {tilde(path)}")
    shas = sorted({h for e in local for _, h in e["hashes"].values()})
    found = {}
    for k in range(0, len(shas), 2000):
        found.update(drive("find", sha256s=shas[k:k + 2000])["found"])
    archived = sorted({a for v in found.values() for a in v})
    there = {}
    for k in range(0, len(archived), 2000):
        there.update(drive("hash", paths=archived[k:k + 2000], timeout=3600)["hashes"])
    out = []
    for e in local:
        missing = list(e["unread"])
        copied = 0
        for p, (size, sha) in e["hashes"].items():
            if any(there.get(a) == [size, sha] for a in found.get(sha, [])):
                copied += 1
            else:
                missing.append(tilde(p))
        row = {"path": e["path"], "files": e["files"], "size": gb(e["bytes"]), "copied": copied,
               "safe": not e.get("error") and not missing and e["files"] > 0}
        if e.get("error"):
            row["error"] = e["error"]
        if missing:
            row["without_copy"] = len(missing)
            row["examples"] = missing[:10]
        out.append(row)
    return {"drive": DRIVE_NAME, "safe": all(r["safe"] for r in out), "paths": out}


# How it all stands.

def rows_of(out):
    """The widget's lines: the drive, each backup, each job under way."""
    d = out.get("drive")
    problem = out.get("drive_problem")
    if not VOLUME:
        return []
    if problem or not d or not d.get("mounted"):
        drive_row = {"label": DRIVE_NAME, "mark": "stop", "detail": "Unreachable" if problem else "Unplugged", "alert": True, "values": []}
    else:
        drive_row = {"label": DRIVE_NAME, "mark": "", "detail": d.get("plugged_into") or "", "alert": False,
                     "values": [f"{d['free']} free"]}
    rows = [{**drive_row, "progress": None, "endsAt": None}]
    for b in out.get("backups") or []:
        rows.append({"label": b["name"], "mark": "running" if b["copying"] else "warn" if b["stale"] else "",
                     "detail": "Missing" if not b["newest"] else "", "alert": not b["newest"],
                     "values": [age_text(b["age_hours"] * 3600)] if b["age_hours"] is not None else [], "progress": None, "endsAt": None})
    with lock:
        live = [j for j in jobs.values() if j["state"] in ("queued", "running", "waiting")]
    for j in live:
        total, moved, _, _, eta = progress_of(j)
        rows.append({"label": j["what"], "mark": "running", "detail": "", "alert": False, "values": [],
                     "progress": round(100 * moved / total) if total else 0,
                     "endsAt": now_ms() + eta * 1000 if eta is not None else None})
    return rows


@app.tool(read_only=True)
@failing(needs_drive=False)
def status():
    """How archiving stands from this Mac: where the drive is plugged in and how this Mac reaches it (each address's
    speed), its space and what's archived from each Mac, this Mac's free space, each backup it minds on the drive
    (newest file, age, stale or not), the jobs running and lately, and how the tool itself is (calls, failures, last
    error)."""
    out = {"mac": MAC, "free_here": gb(shutil.disk_usage(USER_HOME).free), "drive": None, **state}
    try:
        d = drive("status", addresses=False)
        out["drive"] = {"name": DRIVE_NAME, "volume": VOLUME, "plugged_into": MAC if mounted_here() else d.get("host"),
                        "mounted": d.get("mounted"), "free": gb(d.get("free_bytes")), "size": gb(d.get("size_bytes")),
                        "archived": gb(d.get("archived_bytes")),
                        "by_mac": {m: {"files": v["files"], "size": gb(v["bytes"]), "last_moved": v["last_moved"]} for m, v in (d.get("macs") or {}).items()}}
        if not mounted_here():
            out["route"] = route_cache["target"]
            out["probes"] = route_cache["probes"]
        if d.get("mounted"):
            out["backups"] = backups_now()
    except Exception as e:
        out["drive_problem"] = str(e)
    with lock:
        recent = sorted(jobs.values(), key=lambda j: -j["created"])[:5]
    out["jobs"] = [{"job": j["id"], "op": j["op"], "state": j["state"], "summary": summary(j)} for j in recent]
    out["rows"] = rows_of(out)
    # With no drive set up there's nothing to show: only the empty word, which keeps it off the board.
    out["empty"] = "" if out["rows"] else "Not set up"
    return out


BASIC_CATALOG = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"


app.widget("drive", "Archive", lambda: sbmcp.surface("drive", [
    {"id": "root", "component": "Column", "catalogId": BASIC_CATALOG, "children": ["lines", "empty"]},
    {"id": "lines", "component": "List", "catalogId": BASIC_CATALOG, "children": {"componentId": "line", "path": "/rows"}},
    {"id": "empty", "component": "Text", "catalogId": BASIC_CATALOG, "text": {"@path": "/empty"}, "variant": "caption"},
    {"id": "line", "component": "Row", "label": {"@path": "label"}, "mark": {"@path": "mark"}, "detail": {"@path": "detail"},
     "alert": {"@path": "alert"}, "values": {"@path": "values"}, "progress": {"@path": "progress"}, "endsAt": {"@path": "endsAt"}},
]), tool="status", refresh=["archive.job", "archive.ended", "archive.drive", "archive.backup", "archive.backup_stale"])


# Its senses: the drive, itself.

def state_path():
    return os.path.join(HOME, "state.json")


def read_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(st):
    with open(state_path() + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(state_path() + ".tmp", state_path())


def observe_drive(force=False):
    """Says where the drive is and how it is, when that changes: only the Mac it's plugged into
    says so (and the Mac it was last plugged into, once it's gone)."""
    st = read_state()
    last = st.get("drive") or {}
    if mounted_here():
        d = drivelib.op({"op": "status", "volume": VOLUME, "addresses": True})
        facts = {"mounted": True, "host": MAC, "user": d.get("user"), "addresses": d.get("addresses"), "volume": VOLUME,
                 "free_bytes": d.get("free_bytes"), "size_bytes": d.get("size_bytes"), "archived_bytes": d.get("archived_bytes"),
                 "manifest_lines": d.get("manifest_lines"), "macs": d.get("macs"), "seen": now_ms(),
                 "last_spot_check": st.get("spot_check")}
        changed = force or not last.get("mounted") or last.get("addresses") != facts["addresses"] or \
            abs((last.get("free_bytes") or 0) - (facts["free_bytes"] or 0)) > 1e9 or last.get("manifest_lines") != facts["manifest_lines"] or \
            now_ms() - (last.get("seen") or 0) > 6 * 3600 * 1000 or last.get("last_spot_check") != facts["last_spot_check"]
        if changed:
            app.emit("drive", {"entity": DRIVE, "kind": "archive-drive", "name": DRIVE_NAME,
                               "summary": f"{DRIVE_NAME} is plugged into {MAC}: {gb(facts['free_bytes'])} free of {gb(facts['size_bytes'])}, "
                                          f"{gb(facts['archived_bytes'])} archived", "facts": facts})
            st["drive"] = facts
            write_state(st)
        return facts
    if last.get("mounted") and last.get("host") == MAC:
        facts = {**last, "mounted": False, "seen": now_ms()}
        app.emit("drive", {"entity": DRIVE, "kind": "archive-drive", "name": DRIVE_NAME,
                           "summary": f"{DRIVE_NAME} isn't plugged into {MAC} anymore", "facts": facts})
        st["drive"] = facts
        write_state(st)
    return None


def spot_check():
    """Once a day on the drive's Mac: reads a few archived files back and checks them, so a drive
    going bad is caught while the originals' copies still matter."""
    st = read_state()
    if not mounted_here() or time.time() - (st.get("spot_check") or {}).get("at", 0) / 1000 < SPOT_CHECK_EVERY_S:
        return None
    r = drivelib.op({"op": "verify", "volume": VOLUME, "sample": 40, "max_bytes": 3_000_000_000})
    st["spot_check"] = {"at": now_ms(), "checked": r["checked"], "ok": r["ok"], "missing": r["missing"], "changed": r["changed"]}
    write_state(st)
    if r["missing"] or r["changed"]:
        text = (f"{r['missing'] + r['changed']} of {r['checked']} archived files I read back off {DRIVE_NAME} today don't match what "
                f"was moved there ({r['missing']} missing, {r['changed']} changed), e.g. {', '.join(r['examples'][:2])}.")
        app.emit("surprise", {"text": text, "severity": "high"}, about=[DRIVE])
    return r


def resume():
    """Jobs that were running when it stopped carry on where they were."""
    load_jobs()
    for j in list(jobs.values()):
        if j["state"] in ("queued", "running", "waiting"):
            for i in j["items"]:
                if i["state"] not in ("done", "failed", "cancelled", "pending"):
                    i["state"], i["transferred"] = "pending", 0
            j.setdefault("notes", []).append(f"Picked up again after a restart at {iso()}")
            save(j)
            start(j)


@app.background
def beat():
    resume()
    last_backups = 0.0
    while True:
        try:
            refresh_config()
            observe_drive()
            spot_check()
        except Exception as e:
            state["last_error"] = f"drive: {type(e).__name__}: {e}"
        if BACKUPS and (mounted_here() or HOSTS) and time.time() - last_backups >= BACKUP_EVERY_S:
            last_backups = time.time()
            try:
                mind_backups()
            except Unreachable:
                pass
            except Exception as e:
                state["last_error"] = f"backups: {type(e).__name__}: {e}"
        time.sleep(min(DRIVE_EVERY_S, BACKUP_EVERY_S))



def heartbeat():
    """Says how it is, in vitals/archive. `failing` is what's broken now: the drive plugged in here but its archive
    unreadable."""
    failing = None
    try:
        refresh_config()
    except RuntimeError as e:
        failing = str(e)
    if not VOLUME:
        failing = failing or f"No archive drive configured: put volume in {CONFIG_PATH}"
    elif mounted_here():
        root = drivelib.archive_root(VOLUME)
        try:
            os.listdir(root if os.path.isdir(root) else VOLUME)
        except OSError as e:
            failing = f"Can't read {root}: {e.strerror or e}"
    with lock:
        moving = len(running)
    summary = failing or "Ready" + (f"; {moving} job{'s' if moving != 1 else ''} running" if moving else "")
    app.emit("heartbeat", {"entity": f"sense:{NAME}", "summary": summary,
                           "facts": {"drive_here": mounted_here(), "running": moving, "calls": state["calls"],
                                     "failed": state["failed"], "last_error": state["last_error"], "failing": failing}},
             about=[f"sense:{NAME}"])


@app.background
def beats():
    while True:
        heartbeat()
        time.sleep(HEARTBEAT_S)


if __name__ == "__main__":
    app.run()
