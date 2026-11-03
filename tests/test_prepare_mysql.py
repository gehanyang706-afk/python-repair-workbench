"""Private database config creation and non-overwrite; no database or network."""
import contextlib
import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_mysql import prepare


class PrepareMySQLTests(unittest.TestCase):
    def test_creates_private_files_and_retains_exact_configuration_on_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mysql"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                prepare(root)
            files = [root / name for name in ("server.env", "client.env", "init.sql")]
            first = {path.name: path.read_bytes() for path in files}
            for path in files:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            server = dict(line.split("=", 1) for line in first["server.env"].decode().splitlines())
            client = dict(line.split("=", 1) for line in first["client.env"].decode().splitlines())
            self.assertEqual(server["MYSQL_PASSWORD"], client["REPAIR_DB_PASSWORD"])
            self.assertNotIn(server["MYSQL_PASSWORD"], output.getvalue())
            self.assertNotIn(server["MYSQL_ROOT_PASSWORD"], output.getvalue())
            with contextlib.redirect_stdout(output):
                prepare(root)
            self.assertEqual(first, {path.name: path.read_bytes() for path in files})

    def test_partial_configuration_is_rejected_without_overwriting_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "client.env").write_text("existing private config")
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                prepare(root)
            self.assertEqual((root / "client.env").read_text(), "existing private config")
            self.assertFalse((root / "server.env").exists())


if __name__ == "__main__":
    unittest.main()
