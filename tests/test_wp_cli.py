import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from runtime import wp_cli
from lab import Lab

class WpCliTest(TestCase):
    def test_missing_corrupt_and_valid(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config/wp-cli.json").write_text(json.dumps({
                "version": "2.12.0", "sha512": hashlib.sha512(b"fixture").hexdigest()
            }))
            with patch("runtime.ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "setup"):
                    wp_cli()
                asset = root / ".tools/wp-cli/wp-cli-2.12.0.phar"
                asset.parent.mkdir(parents=True)
                asset.write_bytes(b"corrupt")
                with self.assertRaisesRegex(RuntimeError, "setup"):
                    wp_cli()
                asset.write_bytes(b"fixture")
                self.assertEqual(wp_cli(), asset)

    def test_preflight_precedes_cloud(self):
        with patch("lab.wp_cli", side_effect=RuntimeError("missing")), patch("lab.Cloud") as cloud:
            with self.assertRaisesRegex(RuntimeError, "missing"):
                Lab.__new__(Lab).deploy()
            cloud.assert_not_called()

    def test_bundle_contains_verified_binary(self):
        import tarfile
        from remote import Remote
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("ansible", "portal", "scripts", "config"):
                (root / directory).mkdir()
            (root / "scripts/bootstrap.sh").write_text("")
            (root / "config/stack.yml").write_text("")
            local = root / ".local"
            local.mkdir()
            (local / "ansible").write_text("test-key")
            binary = root / "verified.phar"
            binary.write_bytes(b"fixture")
            remote = Remote.__new__(Remote)
            remote.local = local
            bundle = root / "bundle.tar.gz"
            with patch("remote.ROOT", root), patch("remote.wp_cli", return_value=binary) as check, patch.object(Remote, "settings", return_value={}):
                remote.bundle(bundle)
            check.assert_called_once()
            with tarfile.open(bundle) as archive:
                self.assertEqual(archive.extractfile("wp-cli.phar").read(), b"fixture")
