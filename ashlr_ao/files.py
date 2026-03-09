"""
Ashlr AO — File Browser & Editor

REST API for browsing project files, reading/writing content,
and serving as a lightweight file manager for the IDE experience.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    pass

log = logging.getLogger("ashlr")

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

_IGNORE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".next", ".nuxt", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
})

_IGNORE_EXTENSIONS = frozenset({".pyc", ".pyo", ".o", ".so", ".dylib"})

_IGNORE_FILES = frozenset({".DS_Store", "Thumbs.db"})

_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".html": "html", ".css": "css", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".rs": "rust", ".go": "go", ".toml": "toml", ".sql": "sql",
    ".rb": "ruby", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".swift": "swift",
}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _validate_file_path(path: str) -> tuple[bool, str]:
    """Validate that a file path is under the home directory or /tmp.

    Returns (is_valid, resolved_path_or_error).  Uses realpath to prevent
    symlink traversal attacks.
    """
    if not path or not isinstance(path, str):
        return False, "Path is required"
    resolved = os.path.realpath(os.path.expanduser(path))
    home = str(Path.home())
    allowed_prefixes = [home, "/tmp", "/private/tmp"]
    if not any(resolved == p or resolved.startswith(p + os.sep) for p in allowed_prefixes):
        return False, "Path must be under home directory or /tmp"
    return True, resolved


def _should_ignore(name: str, is_dir: bool) -> bool:
    """Return True if the entry should be hidden from file listings."""
    if is_dir:
        if name in _IGNORE_DIRS:
            return True
        if name.endswith(".egg-info"):
            return True
        return False
    # File checks
    if name in _IGNORE_FILES:
        return True
    _, ext = os.path.splitext(name)
    return ext in _IGNORE_EXTENSIONS


def _detect_language(filename: str) -> str:
    """Map a filename extension to a highlight.js language identifier."""
    _, ext = os.path.splitext(filename)
    return _LANGUAGE_MAP.get(ext.lower(), "plaintext")


async def _get_git_file_status(dir_path: str) -> dict[str, str]:
    """Run `git status --porcelain` and return {relative_path: status_code}."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", dir_path, "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            return {}
        result: dict[str, str] = {}
        for line in stdout.decode(errors="replace").splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2].strip()
            rel_path = line[3:].strip()
            # Handle renames: "R  old -> new"
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ", 1)[1]
            result[rel_path] = status_code
        return result
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        log.debug(f"Failed to get git status for {dir_path}: {e}")
        return {}


async def _get_git_branch(dir_path: str) -> str:
    """Return current git branch name, or empty string."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", dir_path, "symbolic-ref", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0 and stdout:
            return stdout.decode().strip()
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        log.debug(f"Failed to get git branch for {dir_path}: {e}")
    return ""


# ─────────────────────────────────────────────
# API Handlers
# ─────────────────────────────────────────────

async def file_tree(request: web.Request) -> web.Response:
    """List directory contents with metadata, git status, and ignore filtering.

    GET /api/files/tree?path=...&depth=1
    """
    path = request.query.get("path", "")
    valid, resolved = _validate_file_path(path)
    if not valid:
        return web.json_response({"error": resolved}, status=400)

    try:
        depth = min(int(request.query.get("depth", "1")), 5)
    except (ValueError, TypeError):
        depth = 1

    if not await asyncio.to_thread(os.path.isdir, resolved):
        return web.json_response({"error": "Not a directory"}, status=400)

    # Gather git info in parallel with directory listing
    git_status_task = asyncio.ensure_future(_get_git_file_status(resolved))
    git_branch_task = asyncio.ensure_future(_get_git_branch(resolved))

    git_status = await git_status_task
    entries = await _list_dir(resolved, depth, git_status, "")
    git_branch = await git_branch_task

    # Compute status counts
    status_counts: dict[str, int] = {}
    status_labels = {"M": "modified", "A": "added", "D": "deleted", "??": "untracked", "R": "renamed"}
    for code in git_status.values():
        label = status_labels.get(code, "other")
        status_counts[label] = status_counts.get(label, 0) + 1

    return web.json_response({
        "path": resolved,
        "entries": entries,
        "git_branch": git_branch,
        "git_status_counts": status_counts,
    })


async def _list_dir(
    abs_path: str, depth: int, git_status: dict[str, str], rel_prefix: str,
) -> list[dict]:
    """Recursively list directory entries up to *depth* levels."""
    try:
        raw_entries = await asyncio.to_thread(os.listdir, abs_path)
    except PermissionError:
        return []

    # Stat all entries once and cache results
    home = str(Path.home())
    allowed_prefixes = [home, "/tmp", os.path.realpath("/tmp")]
    entry_info: list[tuple[str, os.stat_result, bool]] = []  # (name, stat, is_dir)
    for name in raw_entries:
        full = os.path.join(abs_path, name)
        try:
            st = await asyncio.to_thread(os.stat, full)
        except (OSError, PermissionError):
            continue
        is_dir = stat_module.S_ISDIR(st.st_mode)
        if _should_ignore(name, is_dir):
            continue
        # Skip symlinks that escape allowed directories
        if await asyncio.to_thread(os.path.islink, full):
            real = os.path.realpath(full)
            if not any(real == p or real.startswith(p + os.sep) for p in allowed_prefixes):
                continue
        entry_info.append((name, st, is_dir))

    # Sort: dirs first, then alphabetical (case-insensitive)
    entry_info.sort(key=lambda t: (not t[2], t[0].lower()))

    items: list[dict] = []
    for name, st, is_dir in entry_info:
        full = os.path.join(abs_path, name)
        rel_path = os.path.join(rel_prefix, name) if rel_prefix else name

        if is_dir:
            entry: dict = {"name": name, "type": "dir"}
            # Count non-ignored children
            try:
                children = await asyncio.to_thread(os.listdir, full)
                entry["children_count"] = await asyncio.to_thread(
                    _count_visible_children, full, children
                )
            except (OSError, PermissionError):
                entry["children_count"] = 0
            if depth > 1:
                entry["children"] = await _list_dir(full, depth - 1, git_status, rel_path)
            items.append(entry)
        else:
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            entry = {
                "name": name,
                "type": "file",
                "size": st.st_size,
                "modified": mtime,
                "language": _detect_language(name),
            }
            gs = git_status.get(rel_path)
            if gs:
                entry["git_status"] = gs
            items.append(entry)

    return items


def _count_visible_children(dir_path: str, children: list[str]) -> int:
    """Count non-ignored children (runs in thread)."""
    count = 0
    for c in children:
        full = os.path.join(dir_path, c)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        if not _should_ignore(c, is_dir):
            count += 1
    return count


async def file_read(request: web.Request) -> web.Response:
    """Read file content with encoding detection and language mapping.

    GET /api/files/read?path=...
    """
    path = request.query.get("path", "")
    valid, resolved = _validate_file_path(path)
    if not valid:
        return web.json_response({"error": resolved}, status=400)

    if not await asyncio.to_thread(os.path.isfile, resolved):
        return web.json_response({"error": "File not found"}, status=404)

    size = (await asyncio.to_thread(os.stat, resolved)).st_size
    if size > MAX_FILE_SIZE:
        return web.json_response(
            {"error": f"File too large ({size} bytes, max {MAX_FILE_SIZE})"}, status=400,
        )

    raw = await asyncio.to_thread(_read_bytes, resolved)
    encoding = "utf-8"
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")
        encoding = "latin-1"

    filename = os.path.basename(resolved)
    return web.json_response({
        "path": resolved,
        "content": content,
        "language": _detect_language(filename),
        "size": len(raw),
        "encoding": encoding,
        "line_count": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
    })


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def file_write(request: web.Request) -> web.Response:
    """Write content to a file, creating parent directories if needed.

    PUT /api/files/write  {"path": "...", "content": "..."}
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    path = data.get("path", "")
    content = data.get("content")
    if content is None or not isinstance(content, str):
        return web.json_response({"error": "content is required (string)"}, status=400)

    if len(content.encode("utf-8")) > MAX_FILE_SIZE:
        return web.json_response(
            {"error": f"Content too large (max {MAX_FILE_SIZE} bytes)"}, status=400,
        )

    valid, resolved = _validate_file_path(path)
    if not valid:
        return web.json_response({"error": resolved}, status=400)

    try:
        await asyncio.to_thread(_write_file, resolved, content)
    except PermissionError:
        return web.json_response({"error": "Permission denied"}, status=403)
    except OSError as e:
        return web.json_response({"error": f"Write failed: {e}"}, status=500)
    size = len(content.encode("utf-8"))

    log.info(f"File written: {resolved} ({size} bytes)")
    return web.json_response({"path": resolved, "size": size, "written": True})


def _write_file(path: str, content: str) -> None:
    import tempfile
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    # Check existing file permissions before overwriting
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise PermissionError(f"Permission denied: {path}")
    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def file_create(request: web.Request) -> web.Response:
    """Create a new empty file or directory.

    POST /api/files/create  {"path": "...", "type": "file"|"dir"}
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    path = data.get("path", "")
    entry_type = data.get("type", "file")
    if entry_type not in ("file", "dir"):
        return web.json_response({"error": "type must be 'file' or 'dir'"}, status=400)

    valid, resolved = _validate_file_path(path)
    if not valid:
        return web.json_response({"error": resolved}, status=400)

    if await asyncio.to_thread(os.path.exists, resolved):
        return web.json_response({"error": "Path already exists"}, status=409)

    try:
        if entry_type == "dir":
            await asyncio.to_thread(os.makedirs, resolved, exist_ok=False)
        else:
            await asyncio.to_thread(os.makedirs, os.path.dirname(resolved), exist_ok=True)
            await asyncio.to_thread(_touch_file, resolved)
    except FileExistsError:
        return web.json_response({"error": "Path already exists"}, status=409)
    except OSError as e:
        return web.json_response({"error": f"Failed to create: {e}"}, status=500)

    log.info(f"Created {entry_type}: {resolved}")
    return web.json_response({"path": resolved, "type": entry_type, "created": True}, status=201)


def _touch_file(path: str) -> None:
    Path(path).touch()


async def file_delete(request: web.Request) -> web.Response:
    """Delete a file or empty directory.

    DELETE /api/files/delete?path=...
    """
    path = request.query.get("path", "")
    valid, resolved = _validate_file_path(path)
    if not valid:
        return web.json_response({"error": resolved}, status=400)

    if not await asyncio.to_thread(os.path.exists, resolved):
        return web.json_response({"error": "Path not found"}, status=404)

    if await asyncio.to_thread(os.path.isdir, resolved):
        try:
            await asyncio.to_thread(os.rmdir, resolved)
        except OSError:
            return web.json_response(
                {"error": "Directory is not empty — refusing to delete"}, status=400,
            )
    else:
        await asyncio.to_thread(os.remove, resolved)

    log.info(f"Deleted: {resolved}")
    return web.json_response({"path": resolved, "deleted": True})


async def file_rename(request: web.Request) -> web.Response:
    """Rename or move a file/directory.

    POST /api/files/rename  {"old_path": "...", "new_path": "..."}
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    old_path = data.get("old_path", "")
    new_path = data.get("new_path", "")

    valid_old, resolved_old = _validate_file_path(old_path)
    if not valid_old:
        return web.json_response({"error": f"old_path: {resolved_old}"}, status=400)

    valid_new, resolved_new = _validate_file_path(new_path)
    if not valid_new:
        return web.json_response({"error": f"new_path: {resolved_new}"}, status=400)

    if not await asyncio.to_thread(os.path.exists, resolved_old):
        return web.json_response({"error": "Source path not found"}, status=404)

    if await asyncio.to_thread(os.path.exists, resolved_new):
        return web.json_response({"error": "Destination already exists"}, status=409)

    # Ensure parent of destination exists
    try:
        await asyncio.to_thread(os.makedirs, os.path.dirname(resolved_new), exist_ok=True)
        await asyncio.to_thread(os.rename, resolved_old, resolved_new)
    except FileNotFoundError:
        return web.json_response({"error": "Source path not found"}, status=404)
    except FileExistsError:
        return web.json_response({"error": "Destination already exists"}, status=409)
    except OSError as e:
        return web.json_response({"error": f"Rename failed: {e}"}, status=500)

    log.info(f"Renamed: {resolved_old} -> {resolved_new}")
    return web.json_response({
        "old_path": resolved_old,
        "new_path": resolved_new,
        "renamed": True,
    })
