import argparse
import hashlib
import sys
from pathlib import Path


def file_digest(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.blake2b(digest_size=16)
    with file_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path, recursive: bool):
    if recursive:
        return (p for p in root.rglob("*") if p.is_file())
    return (p for p in root.iterdir() if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete all files that are part of duplicate-content groups."
    )
    parser.add_argument("target_dir", help="Directory to scan for duplicate files")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Scan subdirectories recursively",
    )
    args = parser.parse_args()

    target_dir = Path(args.target_dir).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"Directory not found: {target_dir}", file=sys.stderr)
        return 1

    files = sorted(iter_files(target_dir, args.recursive), key=lambda p: str(p).lower())
    if not files:
        print("No files found.")
        return 0

    files_by_size = {}
    for file_path in files:
        try:
            file_size = file_path.stat().st_size
        except OSError as exc:
            print(f"Failed to stat {file_path}: {exc}", file=sys.stderr)
            return 1
        files_by_size.setdefault(file_size, []).append(file_path)

    files_by_hash = {}

    for group in files_by_size.values():
        if len(group) == 1:
            continue
        for file_path in group:
            try:
                fingerprint = file_digest(file_path)
            except OSError as exc:
                print(f"Failed to read {file_path}: {exc}", file=sys.stderr)
                return 1
            files_by_hash.setdefault(fingerprint, []).append(file_path)

    deleted_count = 0
    for group in files_by_hash.values():
        if len(group) < 2:
            continue
        for file_path in group:
            try:
                file_path.unlink()
                deleted_count += 1
                print(f"Deleted duplicate: {file_path}")
            except OSError as exc:
                print(f"Failed to delete {file_path}: {exc}", file=sys.stderr)
                return 1

    remaining_count = len(files) - deleted_count
    print(
        f"Completed. Deleted {deleted_count} file(s) from duplicate groups, "
        f"left {remaining_count} non-duplicate file(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
