import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]


def test_identity_migration_upgrade_downgrade_and_metadata(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "6cf2b87d401a")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO local_users (id) VALUES (1), (2)"))
    alembic("upgrade", "head")
    with engine.connect() as connection:
        keys = list(connection.scalars(text("SELECT session_key FROM local_users")))
        assert len(set(keys)) == 2 and all(len(key) == 32 for key in keys)
    schema = inspect(engine)
    assert {"events", "local_users", "external_identities"} <= set(
        schema.get_table_names()
    )
    assert any(
        item["column_names"] == ["issuer", "subject"]
        for item in schema.get_unique_constraints("external_identities")
    )
    assert (
        schema.get_foreign_keys("external_identities")[0]["referred_table"]
        == "local_users"
    )
    alembic("check")
    alembic("downgrade", "f1de09a2a1d9")
    assert "local_users" not in inspect(engine).get_table_names()
    assert "events" in inspect(engine).get_table_names()
    alembic("upgrade", "head")
    engine.dispose()
