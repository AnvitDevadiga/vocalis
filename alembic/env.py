from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv('VOCALIS_DATABASE_URL')
if not database_url:
    raise RuntimeError('VOCALIS_DATABASE_URL is required to run PostgreSQL migrations')
config.set_main_option('sqlalchemy.url', database_url.replace('%','%%'))


def run_migrations_offline():
    context.configure(url=database_url, literal_binds=True, dialect_opts={'paramstyle':'named'})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section,{}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            connection.exec_driver_sql('SELECT pg_advisory_xact_lock(7211640465329942241)')
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
