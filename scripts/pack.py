"""Build a deterministic source ZIP and verify every member against its manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"core", "context", "gateway", "integration", "service", "webui",
               "extension", "vendor", "scripts", "docs", "deploy", ".github"}
SOURCE_FILES = {"anvil.py", "anvil.toml", "start.ps1", "README.md", "README.zh.md",
                "CODEX-HANDOFF.md", "pyproject.toml", ".gitignore", ".gitattributes",
                "requirements.txt", "requirements-dev.txt", "requirements-code.txt",
                "azure-pipelines.yml", "LICENSE", "LICENSE.md"}
SKIP_DIRS = {"__pycache__", ".runtime", ".sm-work", ".sm-runs", ".git", "node_modules",
             ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".openclaw"}
SKIP_EXT = {".pyc", ".pyo", ".pt", ".pth", ".gguf", ".safetensors", ".onnx",
            ".pem", ".key", ".pfx", ".p12", ".db", ".sqlite", ".sqlite3",
            ".bak", ".tmp", ".orig", ".zip"}
MANIFEST = "MANIFEST.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed(rel: str) -> bool:
    p = Path(rel)
    return (bool(p.parts) and (p.parts[0] in SOURCE_DIRS or rel in SOURCE_FILES)
            and not any(part in SKIP_DIRS for part in p.parts)
            and p.suffix.lower() not in SKIP_EXT
            and p.name != ".env" and not p.name.startswith(".env.")
            and p.name not in {"node_profiles.json", "service-token", "credentials.json"})


def iter_files(root: str | Path):
    root = Path(root).resolve()
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not Path(folder, d).is_symlink())
        for name in sorted(names):
            path = Path(folder, name)
            rel = path.relative_to(root).as_posix()
            if allowed(rel):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError("Source symlinks cannot be released: " + rel)
                yield path, rel


def source_info(root: Path) -> dict:
    # An extracted ZIP may live inside another checkout; never attribute that parent's HEAD.
    if not (root / ".git").exists():
        manifest = root / MANIFEST
        return {"commit": None, "working_tree_dirty": None,
                "archive_manifest_sha256": digest(manifest.read_bytes()) if manifest.is_file() else None}
    def git(*args):
        try:
            return subprocess.check_output(["git", "-C", str(root), *args],
                                           stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    return {"commit": git("rev-parse", "HEAD"),
            "working_tree_dirty": bool(git("status", "--porcelain"))}


def verify_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise ValueError("Duplicate members or corrupt archive")
        manifest = json.loads(archive.read(MANIFEST))
        expected = manifest["files"]
        if set(names) != set(expected) | {MANIFEST}:
            raise ValueError("Archive has missing or unexpected members")
        for name, info in expected.items():
            if (name.startswith("/") or "\\" in name or ":" in name
                    or ".." in name.split("/") or not allowed(name)):
                raise ValueError("Unsafe archive member: " + name)
            data = archive.read(name)
            if digest(data) != info["sha256"] or len(data) != info["bytes"]:
                raise ValueError("Checksum mismatch: " + name)
        return manifest


def build_archive(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.suffix != ".zip":
        raise ValueError("Release output must have a .zip extension")
    if output.is_relative_to(root) and output.relative_to(root).parts[0] in SOURCE_DIRS:
        raise ValueError("Release output must not replace source")
    files = {rel: path.read_bytes() for path, rel in iter_files(root)}
    manifest = {"schema": 1, "source": source_info(root), "files": {
        rel: {"sha256": digest(data), "bytes": len(data)} for rel, data in sorted(files.items())}}
    files[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output.parent, suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for rel, data in sorted(files.items()):
                entry = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        verify_archive(Path(temporary))
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    checksum = digest(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(
        checksum + "  " + output.name + "\n", encoding="utf-8")
    return {"ok": True, "files": len(files) - 1, "sha256": checksum, "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "Anvil-assessment.zip")
    parser.add_argument("--verify", type=Path, help="Verify an existing ZIP without rebuilding")
    parser.add_argument("--strict", action="store_true", help="Compatibility flag; all builds are strict")
    args = parser.parse_args()
    if args.verify:
        manifest = verify_archive(args.verify)
        print(json.dumps({"ok": True, "files": len(manifest["files"])}))
    else:
        print(json.dumps(build_archive(ROOT, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
