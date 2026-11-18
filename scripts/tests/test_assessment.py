"""Release integrity and scope boundaries; no real model calls."""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.pack import MANIFEST, build_archive, digest, source_info, verify_archive


class ReleaseTests(unittest.TestCase):
    def test_extracted_source_does_not_inherit_parent_git_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("scripts.pack.subprocess.check_output") as git:
                self.assertIsNone(source_info(root)["commit"])
                git.assert_not_called()

    def test_repeatable_source_archive_excludes_private_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("anvil.py", "docs/guide.md", "deploy/requirements-code.lock",
                         "service/.env", "service/key.pem", "vendor/weight.pt",
                         ".runtime/service-token", "core/compatibility/node_profiles.json",
                         ".git/config", "personal-notes.txt"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
            first, second = root / "dist/one.zip", root / "dist/two.zip"
            build_archive(root, first)
            os.utime(root / "anvil.py", (1234567, 1234567))
            build_archive(root, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            manifest = verify_archive(first)
            self.assertEqual(set(manifest["files"]), {
                "anvil.py", "docs/guide.md", "deploy/requirements-code.lock"})

    def test_tampering_extra_and_missing_members_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "anvil.py").write_text("original", encoding="utf-8")
            archive = root / "dist/release.zip"
            build_archive(root, archive)
            with zipfile.ZipFile(archive) as original:
                members = {name: original.read(name) for name in original.namelist()}
            for altered in (dict(members, **{"anvil.py": b"modified"}),
                            dict(members, **{"extra.txt": b"extra"}),
                            {MANIFEST: members[MANIFEST]}):
                with zipfile.ZipFile(archive, "w") as target:
                    for name, data in altered.items():
                        target.writestr(name, data)
                with self.assertRaises(ValueError):
                    verify_archive(archive)

    def test_path_traversal_fails_even_with_valid_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad.zip"
            manifest = {"files": {"../anvil.py": {"sha256": digest(b"x"), "bytes": 1}}}
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr(MANIFEST, json.dumps(manifest))
                target.writestr("../anvil.py", b"x")
            with self.assertRaises(ValueError):
                verify_archive(archive)


class ReadinessTests(unittest.TestCase):
    def test_code_scope_does_not_require_optional_models(self):
        from anvil import diagnostics
        from integration.config import settings
        available = {"models": [{"name": settings()["models"]["code"], "digest": "test-digest"}]}
        with patch("gateway.app.get_ollama", return_value=(200, available)), \
                patch("importlib.util.find_spec", return_value=object()):
            code = diagnostics("code")
            self.assertTrue(code["ok"])
            self.assertFalse(code["inference_tested"])
            self.assertEqual(code["models"]["code"]["digest"], "test-digest")
            self.assertFalse(diagnostics("memory")["ok"])
            self.assertFalse(diagnostics("full")["ok"])

    def test_missing_required_dependency_and_offline_ollama_fail(self):
        from anvil import diagnostics
        from integration.config import settings
        available = {"models": [{"name": settings()["models"]["code"]}]}
        with patch("gateway.app.get_ollama", return_value=(200, available)), \
                patch("importlib.util.find_spec", side_effect=lambda name: None if name == "requests" else object()):
            self.assertIn("dependency:requests", diagnostics("code")["missing"])
        with patch("gateway.app.get_ollama", return_value=(503, {})):
            self.assertFalse(diagnostics("code")["ok"])


if __name__ == "__main__":
    unittest.main()
