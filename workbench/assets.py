"""Controlled staging import and content-addressed binary storage."""

import hashlib
import os
import tempfile
from pathlib import Path

from .domain import DomainError, require


KINDS = frozenset({"character", "scene", "prop", "video", "audio", "image", "document"})


def import_binary(data_root: Path, source: Path) -> tuple[str, str, int]:
    staging = data_root / "staging"
    source = Path(source)
    require(source.is_file() and not source.is_symlink(), "invalid_source", 400, "素材源文件不存在或不可用")
    try:
        root = data_root.resolve(strict=True)
        require(not staging.is_symlink(), "invalid_source", 400, "staging 目录不可为链接")
        staging_root = staging.resolve(strict=True)
        require(staging_root == root / "staging", "invalid_source", 400, "staging 目录不在数据根内")
        resolved = source.resolve(strict=True)
        resolved.relative_to(staging_root)
    except (OSError, ValueError):
        raise DomainError("invalid_source", 400, "素材必须位于数据目录 staging 内") from None
    require(resolved.is_file(), "invalid_source", 400, "素材源文件不可用")
    target_dir = data_root / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    require(not target_dir.is_symlink() and target_dir.resolve() == root / "assets", "invalid_source", 400, "assets 目录不在数据根内")
    fd, temporary = tempfile.mkstemp(prefix=".import-", dir=target_dir)
    digest = hashlib.sha256()
    size = 0
    try:
        with open(resolved, "rb") as input_file, os.fdopen(fd, "wb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                output_file.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        sha = digest.hexdigest()
        destination = target_dir / sha
        if not destination.exists():
            os.replace(temporary, destination)
        return sha, sha, size
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def binary_available(data_root: Path, storage_key: str) -> bool:
    return bool(storage_key and len(storage_key) == 64 and (data_root / "assets" / storage_key).is_file())
