"""Shared file-path validation — blocks path traversal and system directories.

Used by every tool that reads or writes a file at an AI-supplied path
(project files, exported audio, inserted media, rendered-mix analysis,
sample-folder scans). Previously duplicated verbatim between item_tools.py
and project_tools.py, and missing entirely from analysis_tools.py/
loops_tools.py — consolidated here so every path-touching tool gets the
same protection instead of some having it and some not.
"""
import os
import sys
import ctypes

from reaper_mcp_shared.error_codes import ReaperMCPError, ErrorCode

# Directories that should never be accessed
_BLOCKED_DIRS_WIN = [
    os.environ.get("SYSTEMROOT", r"C:\Windows"),
    os.environ.get("SYSTEMDRIVE", "C:") + os.sep + "Program Files",
    os.environ.get("SYSTEMDRIVE", "C:") + os.sep + "Program Files (x86)",
]
_BLOCKED_DIRS_NIX = [
    "/etc", "/bin", "/sbin", "/usr", "/boot", "/proc", "/sys", "/dev",
    "/System", "/Library",
]


def _is_blocked(resolved: str, blocked: str) -> bool:
    # blocked must be resolved through realpath the same way `resolved`
    # already was - on macOS several of these top-level dirs are actually
    # symlinks (/etc -> /private/etc, same pattern as /tmp and /var), so
    # comparing a resolved input path against the literal unresolved
    # "/etc" never matches there. realpath() on a path that doesn't exist
    # on this OS (e.g. /proc on macOS) just returns it unchanged, so this
    # is safe to call unconditionally.
    blocked_resolved = os.path.realpath(blocked)
    return resolved == blocked_resolved or resolved.startswith(blocked_resolved + os.sep)


# Hardcoded, not user-configurable (by design - this is a security floor,
# not a preference). Directory names that are always skipped during a
# recursive walk, regardless of where they appear in an otherwise-allowed
# tree, and file basenames that are always excluded from any scan result.
_EXCLUDED_DIR_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".git", ".docker",
    ".credentials", ".secrets",
}
_EXCLUDED_FILE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".npmrc", ".netrc", ".pgpass", "id_rsa", "id_ed25519", "id_ecdsa",
    "known_hosts", "authorized_keys", "credentials.json", "secrets.json",
}


def is_excluded_dir_name(name: str) -> bool:
    """True if a directory name is always excluded from recursive walks -
    known secrets-adjacent locations (.ssh, .git, cloud CLI credential
    dirs, ...), checked by exact basename, case-insensitive."""
    return name.lower() in _EXCLUDED_DIR_NAMES


def is_excluded_file_name(name: str) -> bool:
    """True if a file's basename is always excluded from scan results -
    common credential/secret file names, checked case-insensitively.
    Matches the bare name and any leading-dot variant (e.g. "env" alone
    isn't excluded, but ".env" and ".ENV" are)."""
    return name.lower() in _EXCLUDED_FILE_NAMES


def prune_unsafe_subdirs(dirnames: list[str], current_dir: str, root_realpath: str) -> None:
    """Mutate dirnames in place (for os.walk's topdown pruning) to drop any
    subdirectory that is either an always-excluded name or whose REAL path
    - after resolving symlinks/junctions - escapes root_realpath.

    Why this exists: safe_path only validates the top-level path a caller
    supplies, once. A symlink or NTFS junction placed anywhere inside an
    otherwise-legitimate, already-validated folder can silently redirect a
    recursive walk to a completely different location on disk - verified
    live (see the loops_tools hardening pass this landed with): a junction
    inside an allowed folder made both an os.walk and a pathlib rglob walk
    return files from a totally separate, never-validated directory,
    completely bypassing safe_path's system-directory blocklist, since
    that blocklist is never re-checked against anything the walk actually
    descends into. Every directory a recursive walk is about to enter
    needs this same check, not just the one the caller originally asked
    for.
    """
    safe = []
    for name in dirnames:
        if is_excluded_dir_name(name):
            continue
        full = os.path.join(current_dir, name)
        try:
            real = os.path.realpath(full)
        except OSError:
            continue
        if real != root_realpath and not real.startswith(root_realpath + os.sep):
            continue
        safe.append(name)
    dirnames[:] = safe


def safe_path(path: str) -> str:
    """Validate and normalize a file path. Blocks traversal and system directories."""
    if not path:
        raise ReaperMCPError(ErrorCode.INVALID_PATH, "Path cannot be empty")
    # Must check isabs on the input itself, before realpath: realpath
    # resolves a relative path against the server process's cwd, which
    # makes it absolute - checking isabs on its result can never fire,
    # silently letting a relative path through resolved against whatever
    # directory the server happened to be launched from, instead of
    # rejecting it. Confirmed live: safe_path("some/relative/path") was
    # accepted and resolved under the server's cwd with no error, despite
    # the very next line's now-unreachable check claiming otherwise.
    if not os.path.isabs(path):
        raise ReaperMCPError(ErrorCode.INVALID_PATH, "Path must be absolute")
    path = os.path.normpath(path)
    # A literal ".." check on the resolved path is not what actually
    # blocks traversal - normpath/realpath already collapse "a/../b" to
    # "b" before this point, so it can never be true. Traversal into a
    # blocked directory is instead caught correctly below, because the
    # blocklist check runs against this fully-resolved canonical path
    # (verified live: "C:\Users\x\..\..\Windows\System32" resolves to
    # "C:\WINDOWS" here and is blocked by that check, not this one).
    resolved = os.path.realpath(path)
    # Block system directories
    if sys.platform == "win32":
        resolved_lower = resolved.lower()
        # realpath can leave 8.3 aliases unresolved in constrained Windows hosts.
        # Expand the existing ancestor with Win32, then restore a missing suffix.
        ancestor, suffix = resolved, []
        while not os.path.exists(ancestor):
            parent, leaf = os.path.split(ancestor)
            if parent == ancestor or not leaf:
                break
            suffix.insert(0, leaf)
            ancestor = parent
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetLongPathNameW(ancestor, buffer, len(buffer)):
            resolved_lower = os.path.join(buffer.value, *suffix).lower()
        for blocked in _BLOCKED_DIRS_WIN:
            if resolved_lower.startswith(blocked.lower()):
                raise ReaperMCPError(ErrorCode.INVALID_PATH, f"Access to system directory not allowed: {blocked}")
    else:
        for blocked in _BLOCKED_DIRS_NIX:
            if _is_blocked(resolved, blocked):
                raise ReaperMCPError(ErrorCode.INVALID_PATH, f"Access to system directory not allowed: {blocked}")
    return resolved
