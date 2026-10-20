"""Prepare private local database configuration; never installs or starts services."""

import argparse
from pathlib import Path
import secrets


def prepare(root):
    root = root.resolve()
    files = [root / name for name in ("server.env", "client.env", "init.sql")]
    if any(path.exists() for path in files):
        if not all(path.is_file() for path in files):
            raise ValueError("partial database configuration; refusing to overwrite")
        print("Existing private database configuration retained.")
        return
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    password = secrets.token_hex(24)
    values = [
        "MYSQL_ROOT_PASSWORD="
        + secrets.token_hex(24)
        + "\nMYSQL_DATABASE=repair_workbench\nMYSQL_USER=repair\nMYSQL_PASSWORD="
        + password
        + "\n",
        "REPAIR_DB_HOST=127.0.0.1\nREPAIR_DB_PORT=33079\nREPAIR_DB_USER=repair\nREPAIR_DB_PASSWORD="
        + password
        + "\nREPAIR_DB_NAME=repair_workbench\n",
        "CREATE DATABASE IF NOT EXISTS repair_workbench_test;\nGRANT ALL PRIVILEGES ON repair_workbench_test.* TO 'repair'@'%';\n",
    ]
    for path, content in zip(files, values):
        with path.open(
            "x", opener=lambda p, flags: __import__("os").open(p, flags, 0o600)
        ) as stream:
            stream.write(content)
    print("Private local database configuration prepared. No credentials printed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runtime/mysql-backend"))
    prepare(parser.parse_args().output)
