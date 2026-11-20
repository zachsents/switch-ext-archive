# spur-archive

Moves files off a Mac onto an archive drive, verified file by file with sha256, brings them back the same way, says whether paths have a verified copy there before they're deleted, and minds the backups kept on that drive.

A [Switch](https://switch.ax) spur: a Python MCP server (`sbmcp.py`) that Switch runs through [spur-kit](https://github.com/zachsents/spur-kit)'s shim. `drive.py` is the drive's side: it runs in-process on the Mac the drive is plugged into, and is sent over SSH from any other.

Tools: `candidates`, `move` (removes originals once verified), `job`, `cancel`, `where`, `restore`, `verify`, `safe_to_delete`, `status`. Events: `job`, `ended`, `item`, `drive`, `surprise`, `backup`, `backup_stale`. Widget: `drive` (the drive, each backup's age, moves under way).

## Install

Ask Switch's agent to install `github:zachsents/spur-archive`, or add it in the desktop's Spurs view (⌘K, Spurs). It needs `macos`.

## Setup

Put a `config.json` in the spur's `home`:

```json
{
  "volume": "/Volumes/Archive",
  "hosts": ["user@archive-mac.example.com"],
  "backups": [
    { "name": "Nightly", "path": "Backups/nightly", "pattern": "*.db", "max_age_hours": 26 }
  ]
}
```

- `volume`: where the drive mounts (or a folder that holds `Archive/`). `drive_name` overrides the name it shows.
- `hosts`: `user@address` of the Mac the drive is plugged into, for when it isn't plugged in here. That Mac needs SSH keys from this one (Remote Login on) and rsync.
- `backups`: each a folder on the drive (or absolute) and a glob its files match (`**` for any depth), with how old the newest may be before `backup_stale` is emitted.
- Optional: `mac` (this Mac's name under `Archive/`, default its host name), `user_home`, `ssh`, `rsync`, `remote_python`, `margin_gb`, `wait_minutes`, `check_minutes`, `backup_check_minutes`.

On the drive: `Archive/<mac>/<path under home>` holds what was moved, `Archive/manifest.jsonl` a line per file (original, archive path, bytes, sha256), `Archive/restores.jsonl` a line per item brought back. Nothing on the drive is ever deleted or written over.
