"""共享的 MySQL 只读连接适配：把 pymysql 包装成 sqlite3.Connection 的最小子集。

快照目录（CJ/Amazon/eBay）切换数据源用。SQLite 保持默认，MySQL 由 ``mysql_dsn``
启用；DictCursor 返回 dict，与 ``sqlite3.Row`` 一样支持 ``row["col"]`` 取值，
下游卡片组装逻辑无需改动。
"""

from __future__ import annotations

import pymysql
from sqlalchemy.engine import make_url


class ReadonlyMySQLConnection:
    """pymysql 连接的最小封装：只暴露 ``execute`` / ``close``，调用点不用写 cursor 分支。"""

    def __init__(self, dsn: str) -> None:
        url = make_url(dsn)
        self._conn = pymysql.connect(
            host=url.host or "127.0.0.1",
            port=url.port or 3306,
            user=url.username or "root",
            password=url.password or "",
            database=url.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def execute(self, sql: str, parameters=None):
        cursor = self._conn.cursor()
        cursor.execute(sql, parameters)
        return cursor

    def close(self) -> None:
        self._conn.close()


def scalar(cursor) -> int:
    """读 ``count(*)`` 这类单值首列；兼容 dict（DictCursor）与序列（sqlite3.Row）。"""
    row = cursor.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])
