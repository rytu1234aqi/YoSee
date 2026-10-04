"""Alembic migration environment.

The database URL comes from ``DATABASE_URL`` or the SQLite default. Model
modules are imported so autogenerate can see every table.
"""

from logging.config import fileConfig

from alembic import context
from server.database.base import Base
from server.database.session import create_db_engine, get_database_url
from server.models import ContentItem, FetchRun, Source
from sqlalchemy import pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Imported for metadata registration. Autogenerate compares these tables.
_REGISTERED_MODELS = (Source, ContentItem, FetchRun)


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_db_engine(get_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
