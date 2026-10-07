"""Project-only portable ZIP with checked binaries and transactional relink."""

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .assets import binary_available
from .domain import DomainError, require
from .store import Store


TABLES = ("projects", "episodes", "scenes", "assets", "shots")
MAX_METADATA = 16 * 1024 * 1024
MAX_BINARY = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024


def _hash_stream(binary):
    digest = hashlib.sha256()
    size = 0
    while chunk := binary.read(1024 * 1024):
        size += len(chunk)
        require(size <= MAX_BINARY, "archive_too_large", 400, "归档素材过大")
        digest.update(chunk)
    return digest.hexdigest(), size


def _hash_archive(path):
    digest = hashlib.sha256()
    with path.open("rb") as complete:
        while chunk := complete.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_project(store: Store, project_id: str, destination: Path) -> dict:
    bundle = store.export_project(project_id)
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(), "archive_conflict", 409, "归档已存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    binaries = {}
    for asset in bundle["assets"]:
        key = asset["sha256"]
        require(binary_available(store.data_root, key), "asset_binary_missing", 409, "归档素材缺失或哈希不符")
        binaries[key] = store.data_root / "assets" / key
    metadata = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    require(len(metadata) <= MAX_METADATA, "archive_too_large", 400, "归档元数据过大")
    require(len(binaries) + 1 <= MAX_MEMBERS and len(metadata) + sum(path.stat().st_size for path in binaries.values()) <= MAX_TOTAL_UNCOMPRESSED_BYTES, "archive_too_large", 400, "归档总量超限")
    fd, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".zip", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.writestr("metadata.json", metadata)
            for sha, path in binaries.items():
                archive.write(path, f"assets/{sha}")
        with zipfile.ZipFile(temporary) as archive:
            names = _inspect(archive)
            require(names == {"metadata.json"} | {f"assets/{sha}" for sha in binaries}, "invalid_archive", 400, "归档成员不匹配")
            with archive.open("metadata.json") as stored_metadata:
                require(stored_metadata.read(MAX_METADATA + 1) == metadata, "invalid_archive", 400, "归档元数据变化")
            _verify_binaries(archive, bundle["assets"])
        require(not destination.exists() and not destination.is_symlink(), "archive_conflict", 409, "归档已存在")
        os.link(temporary, destination)
    except FileExistsError:
        raise DomainError("archive_conflict", 409, "归档已存在") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"path": str(destination), "project_id": project_id, "asset_count": len(bundle["assets"]), "binary_count": len(binaries), "sha256": _hash_archive(destination)}


def _inspect(archive):
    names = set()
    total = 0
    for info in archive.infolist():
        require(len(names) < MAX_MEMBERS, "archive_too_large", 400, "归档成员数超限")
        path = PurePosixPath(info.filename)
        require(not info.is_dir() and not info.filename.startswith("/") and "\\" not in info.filename and path.parts and all(part not in (".", "..") for part in path.parts), "invalid_archive", 400, "归档路径不安全")
        require(info.filename not in names, "invalid_archive", 400, "归档成员重复")
        names.add(info.filename)
        mode = info.external_attr >> 16
        require((mode & 0o170000) in (0, 0o100000), "invalid_archive", 400, "归档不可包含链接")
        require(info.file_size <= MAX_BINARY and info.compress_size <= MAX_BINARY, "archive_too_large", 400, "归档成员过大")
        total += info.file_size
        require(total <= MAX_TOTAL_UNCOMPRESSED_BYTES, "archive_too_large", 400, "归档解压总量超限")
        require(info.file_size <= max(info.compress_size * 1000, MAX_METADATA), "archive_bomb", 400, "归档压缩比异常")
    require("metadata.json" in names and archive.getinfo("metadata.json").file_size <= MAX_METADATA, "invalid_archive", 400, "归档缺少元数据")
    return names


def _verify_binaries(archive, assets):
    total = archive.getinfo("metadata.json").file_size
    checked = {}
    for asset in assets:
        if asset["sha256"] not in checked:
            with archive.open(f"assets/{asset['sha256']}") as binary:
                actual_sha, size = _hash_stream(binary)
            total += size
            require(total <= MAX_TOTAL_UNCOMPRESSED_BYTES, "archive_too_large", 400, "归档解压总量超限")
            checked[asset["sha256"]] = (actual_sha, size)
        actual_sha, size = checked[asset["sha256"]]
        require(actual_sha == asset["sha256"] and size == asset["size_bytes"], "archive_hash_mismatch", 400, "归档素材哈希或大小错误")


def restore_archive(store: Store, source: Path) -> dict:
    source = Path(source)
    require(source.is_file() and not source.is_symlink(), "invalid_archive", 400, "归档不可用")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DomainError("invalid_archive", 400, "归档格式错误") from exc
    with archive:
        names = _inspect(archive)
        try:
            bundle = json.loads(archive.read("metadata.json").decode("utf-8"))
        except (UnicodeError, ValueError, RuntimeError) as exc:
            raise DomainError("invalid_archive", 400, "归档元数据错误") from exc
        assets = bundle.get("assets") if isinstance(bundle, dict) else None
        require(isinstance(assets, list), "invalid_archive", 400, "归档资产清单错误")
        expected = {f"assets/{a['sha256']}" for a in assets if isinstance(a, dict) and isinstance(a.get("sha256"), str)}
        require(names == expected | {"metadata.json"}, "invalid_archive", 400, "归档成员与资产清单不匹配")
        # Validate the complete metadata graph without touching the destination DB.
        with tempfile.TemporaryDirectory(prefix=".archive-validate-") as trial:
            verified_store = Store(Path(trial) / "verify.sqlite", Path(trial))
            verified_store.restore_project(bundle)
            with verified_store.connection() as source_conn:
                validated_rows = {table: [dict(row) for row in source_conn.execute(f"SELECT * FROM {table}")] for table in TABLES}
        _verify_binaries(archive, assets)
        # All IDs and existing content-addressed blobs are checked before any write.
        with store.connection() as conn:
            for table, ids in (("projects", [bundle["project"]["id"]]), ("episodes", [x["id"] for x in bundle["episodes"]]), ("scenes", [x["id"] for x in bundle["scenes"]]), ("assets", [x["id"] for x in assets]), ("shots", [x["id"] for x in bundle["shots"]])):
                for identifier in ids:
                    require(conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (identifier,)).fetchone() is None, "restore_conflict", 409, "恢复 ID 已存在")
        asset_dir = store.data_root / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        require(not asset_dir.is_symlink() and not asset_dir.is_junction() and asset_dir.resolve() == store.data_root.resolve() / "assets", "invalid_target", 400, "素材目录不安全")
        created = []
        try:
            copied_total = archive.getinfo("metadata.json").file_size
            for asset in assets:
                sha = asset["sha256"]
                destination = asset_dir / sha
                if destination.exists() or destination.is_symlink():
                    require(binary_available(store.data_root, sha), "restore_conflict", 409, "已有素材文件与备份不符")
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=asset_dir)
                try:
                    with os.fdopen(fd, "wb") as output, archive.open(f"assets/{sha}") as binary:
                        while chunk := binary.read(1024 * 1024):
                            copied_total += len(chunk)
                            require(copied_total <= MAX_TOTAL_UNCOMPRESSED_BYTES, "archive_too_large", 400, "恢复素材总量超限")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    with open(temporary, "rb") as check:
                        digest, size = _hash_stream(check)
                    require(digest == sha and size == asset["size_bytes"], "archive_hash_mismatch", 400, "复制素材校验失败")
                    os.link(temporary, destination)
                    created.append(destination)
                except FileExistsError:
                    require(binary_available(store.data_root, sha), "restore_conflict", 409, "素材文件冲突")
                finally:
                    Path(temporary).unlink(missing_ok=True)
            # Reuse the validated rows and attach immutable hashes within one transaction.
            with store.transaction() as conn:
                for table in TABLES:
                    for row in validated_rows[table]:
                        record = row.copy()
                        if table == "assets":
                            record["storage_key"] = record["sha256"]
                        columns = list(record)
                        placeholders = ",".join("?" for _ in columns)
                        conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", list(record.values()))
        except (sqlite3.IntegrityError, OSError, DomainError) as exc:
            for path in created:
                path.unlink(missing_ok=True)
            if isinstance(exc, sqlite3.IntegrityError):
                raise DomainError("restore_conflict", 409, "恢复记录冲突") from exc
            raise
    return {"project_id": bundle["project"]["id"], "asset_count": len(assets), "binary_restored": True}
