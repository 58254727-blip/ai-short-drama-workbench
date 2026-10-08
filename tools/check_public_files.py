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


def listed_files():
    result = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "ls-files", "--cached", "-z"],
                            cwd=ROOT, capture_output=True, check=True)
    return {name.decode("utf-8", "strict") for name in result.stdout.split(b"\0") if name}


def check_path(name):
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
    target = ROOT / normalized
    if target.is_symlink() or not target.is_file():
        return "missing file or symlink"
    if suffix == ".png":
        expected = APPROVED_IMAGES.get(normalized)
        if not expected or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            return "PNG lacks exact approved provenance/hash"
        return None
    if suffix not in TEXT_SUFFIXES and filename != ".gitignore":
        return "file type not approved"
    if target.stat().st_size > 4 * 1024 * 1024:
        return "text file too large to inspect"
    try:
        content = target.read_text(encoding="utf-8", errors="strict")
    except UnicodeError:
        return "not UTF-8 text"
    if "\0" in content:
        return "binary content in text file"
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        return "private path or credential material"
    return None


def main():
    try:
        names = listed_files() | set(sys.argv[1:])
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        print(f"Unable to read Git index: {type(error).__name__}", file=sys.stderr)
        return 2
    failures = [(name, reason) for name in sorted(names) if (reason := check_path(name))]
    for name, reason in failures:
        print(f"REJECT {name}: {reason}", file=sys.stderr)
    if failures:
        print(f"Public file check failed: {len(failures)} of {len(names)} files", file=sys.stderr)
        return 1
    print(f"Public file check passed: {len(names)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
