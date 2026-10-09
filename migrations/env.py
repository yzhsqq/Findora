"""Alembic 迁移环境（异步模板）。

与 app 共用同一套 Base.metadata 与库解析：目标库由 DATABASE_URL / MYSQL_URL 决定，
未配时落到 SQLite（data/findora.db 或 data/globex.db）。可用 ``-x db_url=...`` 覆盖，
用于对临时空库生成基线、或对 MySQL 库 stamp。

依赖 asyncmy / aiosqlite（pyproject 已声明），不引入同步驱动。
"""
from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

# `alembic` 是控制台脚本，CWD 未必在 sys.path；把项目根加进去以便 import app 包。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 仅 import 即把全部声明式模型注册进 Base.metadata。
from app.infrastructure.persistence.sql import (  # noqa: F401
    agui_tables,
    session_store,
    tables,
    trade_tables,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = tables.Base.metadata

# agui_* 是独立日志库（SQLite 下为 ag_ui_runs.db，MySQL 下才与主库同库）的表，
# 由 AGUIJournal 自行建表与管理，主库 Alembic 不接管，避免把两个库的 schema 缠在一起。
_JOURNAL_TABLES = {"agui_sessions", "agui_runs", "agui_events"}


def _include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "table" and name in _JOURNAL_TABLES:
        return False
    return True


def _resolve_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    from app.infrastructure.settings import PROJECT_ROOT, _database_url

    data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
    return _database_url(data_dir)


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=False,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
