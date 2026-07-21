"""Guard: every model table must be created by exactly one migration.

Migrations pin explicit table lists (metadata carries later-phase tables the
earlier revisions must not create). This test fails the moment someone adds a
model without adding it to a migration — at test time instead of as an
UndefinedTable error on a fresh production install.
"""

from __future__ import annotations

from app.db import models  # noqa: F401  (register all tables)
from app.db.base import Base


def _table_names(tables) -> set[str]:
    return {table.name for table in tables}


def test_migration_table_lists_cover_metadata() -> None:
    import importlib.util
    from pathlib import Path

    versions = Path(__file__).parent.parent / "alembic" / "versions"

    def load(name: str):
        path = next(versions.glob(f"{name}_*.py"))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    m0001 = load("0001")
    m0002 = load("0002")

    covered = _table_names(m0001._phase1_tables()) | _table_names(m0002._phase2_tables())
    declared = set(Base.metadata.tables.keys())
    missing = declared - covered
    assert not missing, (
        f"tables missing from every migration's pinned list: {sorted(missing)} — "
        "add them to the owning revision"
    )
    duplicated = _table_names(m0001._phase1_tables()) & _table_names(m0002._phase2_tables())
    assert not duplicated, f"tables created by two migrations: {sorted(duplicated)}"
