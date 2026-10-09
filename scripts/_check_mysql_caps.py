"""临时探针：列出 MySQL 库、定位 M09/M10 迁入的快照表及其行数。"""

import asyncio
import os

import asyncmy


async def main() -> None:
    # 凭据只从环境变量读（铁律：密码不进仓库），缺省空密码需自行导出 MYSQL_PASSWORD。
    conn = await asyncmy.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
    )
    cur = await conn.cursor()

    await cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in await cur.fetchall()]
    print("DATABASES:", dbs)

    for db in dbs:
        if db in ("information_schema", "mysql", "performance_schema", "sys"):
            continue
        await cur.execute(
            "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
            (db,),
        )
        rows = await cur.fetchall()
        if rows:
            print(f"--- {db} ---")
            for name, n in rows:
                print(f"    {name}: ~{n} rows")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
