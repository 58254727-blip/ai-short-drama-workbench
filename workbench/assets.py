"""Controlled staging import and content-addressed binary storage."""

import hashlib
import os
import tempfile
from pathlib import Path

from .domain import DomainError, require


KINDS = frozenset({"character", "scene", "prop", "video", "audio", "image", "document"})


def _matches_hash(path: Path, expected: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as binary:
            while chunk := binary.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def import_binary(data_root: Path, source: Path) -> tuple[str, str, int]:
    staging = data_root / "staging"
    source = Path(source)
    require(source.is_file() and not source.is_symlink(), "invalid_source", 400, "素材源文件不存在或不可用")
    try:
        root = data_root.resolve(strict=True)
        require(not staging.is_symlink() and not staging.is_junction(), "invalid_source", 400, "staging 目录不可为链接")
        staging_root = staging.resolve(strict=True)
        require(staging_root == root / "staging", "invalid_source", 400, "staging 目录不在数据根内")
        resolved = source.resolve(strict=True)
        resolved.relative_to(staging_root)
    except (OSError, ValueError):
        raise DomainError("invalid_source", 400, "素材必须位于数据目录 staging 内") from None
    require(resolved.is_file(), "invalid_source", 400, "素材源文件不可用")
    target_dir = data_root / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    require(not target_dir.is_symlink() and not target_dir.is_junction() and target_dir.resolve() == root / "assets", "invalid_source", 400, "assets 目录不在数据根内")
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
        if not destination.exists() and not destination.is_symlink():
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
        require(destination.resolve() == target_dir.resolve() / sha and _matches_hash(destination, sha), "invalid_asset_binary", 409, "已有素材文件与哈希不符或路径不安全")
        return sha, sha, size
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def binary_available(data_root: Path, storage_key: str) -> bool:
    if not isinstance(storage_key, str) or len(storage_key) != 64 or any(c not in "0123456789abcdef" for c in storage_key):
        return False
    target_dir = data_root / "assets"
    destination = target_dir / storage_key
    try:
        return bool(
            not target_dir.is_symlink()
            and not target_dir.is_junction()
            and target_dir.resolve() == data_root.resolve() / "assets"
            and destination.resolve() == target_dir.resolve() / storage_key
            and _matches_hash(destination, storage_key)
        )
    except OSError:
        return False
