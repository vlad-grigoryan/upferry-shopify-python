from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import Base, Shop


def test_initial_migration_and_repeated_upgrade_preserve_installations(database, monkeypatch):
    Base.metadata.drop_all(database[0])
    monkeypatch.setenv("DATABASE_URL", database[2])
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")
    with database[1].begin() as session:
        session.add(Shop(name="survives-upgrade"))
    command.upgrade(config, "head")
    with database[1]() as session:
        assert session.get(Shop, "survives-upgrade") is not None
    assert set(inspect(database[0]).get_table_names()) == {"alembic_version", "shops", "jobs", "webhook_receipts"}
