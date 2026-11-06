"""contracts/ must match the code that generates it (copies live in cams-detect)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_contract_files_are_current():
    spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "gen_camconfig_contracts.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for rel, text in gen.render().items():
        current = (ROOT / rel).read_text(encoding="utf-8")
        assert current == text, f"{rel} is stale: run scripts/gen_camconfig_contracts.py"


def test_schema_sql_builds_cams_config(store):
    """The .sql copy really creates the schema cams-record migrates to (cams-detect's tests rely on it)."""
    from cams_record.camconfig.schema import TABLES

    def tables(cur):
        cur.execute("SHOW TABLES")
        return {r[0] for r in cur.fetchall()}

    migrated = store.db.run(tables)

    def rebuild(cur):
        for t in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        sql = (ROOT / "contracts" / "camconfig_schema.sql").read_text()
        body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
        for stmt in body.split(";\n"):
            if stmt.strip():
                cur.execute(stmt)
        return tables(cur)

    assert store.db.run(rebuild) == migrated - {"schema_version"}
