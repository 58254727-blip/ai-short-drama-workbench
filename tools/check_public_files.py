"""Fail closed on unsafe tracked/staged files and explicitly supplied candidates.

Only Git's index and named candidates are inspected; ignored runtime trees are
never traversed. This is a release gate, not a guarantee against every secret.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path.cwd().resolve()
APPROVED_IMAGES = {
    "docs/design/workbench-concept-v1.png": "4045099c9f02dd1afc6564b048dce404e56eeb4aad846752cc1fada40970e131",
    "docs/design/queue-concept-v1.png": "4c8390de7affc59efdf663beb8c05f251cdc24ee026fa832f919ee07ee885dc1",
    "web/assets/rain-alley-demo.png": "e0a85449e2fe68a94da4a693b2fca8c3d6dd120ba671b65f2c069cfc01baf4a2",
}
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".svg", ".md", ".yml", ".yaml", ".json", ".txt"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".mp4", ".mov", ".webm", ".wav", ".mp3", ".m4a", ".flac", ".safetensors", ".pt", ".pth", ".ckpt", ".onnx", ".bin", ".zip", ".pem", ".key", ".log", ".pdf"}
FORBIDDEN_PARTS = {".runtime", ".workbench-data", ".superpowers", ".design", "__pycache__", "node_modules", ".venv", "models", "weights", "private", "secrets"}
SECRET_PATTERNS = (
    re.compile(r"(?m)^\s*-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|xoxb-|sk-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bC:[\\/]Users[\\/][^\\/\s'\"`]+[\\/]"),
)


def git_command(*args):
    return subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", *args],
                          cwd=ROOT, capture_output=True, check=True).stdout


def listed_files():
    result = git_command("ls-files", "--stage", "-z")
    entries = {}
    for record in result.split(b"\0"):
        if not record:
            continue
        header, separator, name = record.partition(b"\t")
        if not separator:
            raise ValueError("invalid index record")
        mode, oid, stage = header.decode("ascii").split(" ")
        filename = name.decode("utf-8", "strict")
        if filename in entries or stage != "0":
            raise ValueError("unmerged or duplicate index path")
        entries[filename] = (mode, oid)
    return entries


def index_bytes(oid):
    size = int(git_command("cat-file", "-s", oid))
    if size > 8 * 1024 * 1024:
        raise ValueError("index blob too large to inspect")
    return git_command("cat-file", "blob", oid)


def check_path(name, indexed=None):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized.startswith("/") or ":" in normalized:
        return "path leaves repository"
    lower = [part.lower() for part in path.parts]
    if any(part in FORBIDDEN_PARTS for part in lower):
        return "private or runtime directory"
    filename = lower[-1]
    if filename.startswith(".env") or "config.local" in filename or "operator-config" in filename or "credential" in filename:
        return "environment or private configuration"
    suffix = path.suffix.lower()
    if suffix in FORBIDDEN_SUFFIXES or filename.endswith((".db-wal", ".db-shm")) or filename.startswith(".sqlite"):
        return "database, media, model, key, log, or archive"
    if indexed is not None:
        mode, oid = indexed
        if mode not in {"100644", "100755"}:
            return "index entry is not a regular file"
        try:
            data = index_bytes(oid)
        except (ValueError, subprocess.CalledProcessError):
            return "index blob unreadable or too large"
    else:
        target = ROOT / normalized
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(ROOT):
            return "missing file, symlink, or outside repository"
        if target.stat().st_size > 8 * 1024 * 1024:
            return "candidate too large to inspect"
        try:
            data = target.read_bytes()
        except OSError:
            return "candidate unreadable"
    if suffix == ".png":
        expected = APPROVED_IMAGES.get(normalized)
        if not expected or hashlib.sha256(data).hexdigest() != expected:
            return "PNG lacks exact approved provenance/hash"
        return None
    if suffix not in TEXT_SUFFIXES and filename != ".gitignore":
        return "file type not approved"
    if len(data) > 4 * 1024 * 1024:
        return "text file too large to inspect"
    try:
        content = data.decode("utf-8", errors="strict")
    except UnicodeError:
        return "not UTF-8 text"
    if "\0" in content:
        return "binary content in text file"
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        return "private path or credential material"
    return None


def main():
    try:
        indexed = listed_files()
        names = set(indexed) | set(sys.argv[1:])
    except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError) as error:
        print(f"Unable to read Git index: {type(error).__name__}", file=sys.stderr)
        return 2
    failures = [(name, reason) for name in sorted(names) if (reason := check_path(name, indexed.get(name)))]
    for name, reason in failures:
        print(f"REJECT {name}: {reason}", file=sys.stderr)
    if failures:
        print(f"Public file check failed: {len(failures)} of {len(names)} files", file=sys.stderr)
        return 1
    print(f"Public file check passed: {len(names)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
