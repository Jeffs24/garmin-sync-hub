"""What the "Test" button of Settings → Backup says about an rclone destination.

Google Drive allows several folders with the same name side by side, and rclone only sees
what its access scope allows: with "drive.file" it sees only the files it created, so it makes
a new "Backups" folder next to yours. These checks show that before the first copy.
"""
from __future__ import annotations

import re
from typing import Callable

# rclone lsd line: "          -1 2026-11-22 10:00:00        -1 Folder name"
_LSD_LINE = re.compile(r"^\s*-?\d+\s+\S+\s+\S+\s+-?\d+\s+(.+?)\s*$")
LIMITED_SCOPES = ("drive.file", "drive.appfolder")

Check = tuple[str, str]  # (level: "ok" | "warn" | "fail" | "info", message)


def parse_lsd(output: str) -> list[str]:
    return [m.group(1) for line in output.splitlines() if (m := _LSD_LINE.match(line))]


def parse_config(output: str) -> dict[str, str]:
    """`rclone config show <remote>` -> {key: value} (the token is skipped)."""
    values = {}
    for line in output.splitlines():
        if "=" in line and not line.lstrip().startswith(("[", "#", ";")):
            key, _, value = line.partition("=")
            if key.strip() != "token":
                values[key.strip()] = value.strip()
    return values


def check_destination(remote: str, run: Callable[[list[str]], tuple[bool, str]]) -> list[Check]:
    """Checks for "name:path"; `run(args)` runs rclone and returns (ok, full output)."""
    name, _, path = remote.partition(":")
    checks: list[Check] = []
    ok, out = run(["config", "show", name])
    config = parse_config(out) if ok else {}
    if not ok or not config:
        return [("fail", f"No remote named “{name}” in rclone.conf. Create it with rclone config.")]
    kind = config.get("type", "?")
    scope = config.get("scope", "")
    if kind == "drive" and any(s in scope for s in LIMITED_SCOPES):
        checks.append(("warn", f"Access “{scope}”: rclone only sees the folders it created, so it makes "
                               "a new folder even if yours has the same name. To use an existing folder, "
                               "set the scope to “drive” (full access) and reconnect."))
    if config.get("team_drive"):
        checks.append(("info", "This remote points to a shared drive."))
    if config.get("root_folder_id"):
        checks.append(("info", f"This remote starts in the folder with id {config['root_folder_id']}, "
                               "not at the top of My Drive."))

    prefix = f"{name}:"
    segments = [s for s in path.strip("/").split("/") if s]
    ok, out = run(["lsd", prefix])
    if not ok:
        return checks + [("fail", f"rclone cannot read {prefix}: {_last_line(out)}")]
    folders = parse_lsd(out)
    shown = ", ".join(folders[:15]) + (f" and {len(folders) - 15} more" if len(folders) > 15 else "")
    checks.insert(0, ("ok", f"{name} answers. Folders at {prefix}: {shown or 'none'}."))
    for i, segment in enumerate(segments):
        count = folders.count(segment)
        where = prefix + "/".join(segments[:i])
        if count == 0:
            checks.append(("info", f"“{segment}” does not exist in {where} yet: it is created at the first copy."))
            break
        if count > 1:
            checks.append(("warn", f"{count} folders are named “{segment}” in {where}. rclone uses one of "
                                   "them: keep only one in Google Drive (merge them, then delete the extra)."))
            break
        if i + 1 < len(segments):
            ok, out = run(["lsd", prefix + "/".join(segments[: i + 1])])
            if not ok:
                checks.append(("fail", f"rclone cannot read {prefix + '/'.join(segments[:i + 1])}: {_last_line(out)}"))
                break
            folders = parse_lsd(out)
    else:
        if segments:
            checks.append(("ok", f"Backups go into the existing folder {remote}."))
    return checks


def _last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no answer"
