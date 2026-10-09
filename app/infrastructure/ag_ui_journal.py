# -*- coding: utf-8 -*-
"""AG-UI 持久事件日志：事务内追加序号、更新投影并绑定买家归属。

实现走 SQLAlchemy 引擎而非裸 aiosqlite，因此同一份代码可跑 SQLite 与 MySQL。
两处方言差异集中处理：
  - 进写事务：SQLite 需显式 BEGIN IMMEDIATE，MySQL 靠 InnoDB 行锁；
  - 会话 upsert：SQLite 用 ON CONFLICT，MySQL 用 ON DUPLICATE KEY UPDATE。

时间戳一律用 Unix 秒浮点（与租约比较 `lease_until > now` 口径一致），不随时间列迁移。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.infrastructure.persistence.sql.agui_tables import (
    AGUIEventRow,
    AGUIRunRow,
    AGUISessionRow,
)
from app.infrastructure.persistence.sql.tables import Base


class JournalConflict(ValueError):
    pass


def _is_lock_contention(err: BaseException) -> bool:
    """是否是写锁竞争（SQLite "database is locked" / "busy"，MySQL 锁等待超时）。"""
    message = str(err).lower()
    return "locked" in message or "busy" in message


def _is_retryable_init_error(err: SQLAlchemyError) -> bool:
    """判断建表失败是否属于并发初始化的瞬时冲突。

    两类可以重试：
      - 写锁竞争（见 _is_lock_contention）；
      - 同名表已存在，即两个实例都通过了 checkfirst 才同时 CREATE，输的一方报
        "already exists"。此时库结构已由对方建好，重试时 checkfirst 会直接跳过。
    除此之外（例如文件根本不是数据库，报 "not a database"）应当立即上抛，
    否则会把真正的错误拖成超时。
    """
    return _is_lock_contention(err) or "already exists" in str(err).lower()


class JournalForbidden(PermissionError):
    pass


class JournalNotFound(LookupError):
    pass


class JournalLeaseLost(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AGUIJournal:
    """运行日志。

    构造参数既接受 AsyncEngine（生产装配按 DATABASE_URL 注入），也接受文件路径
    （等价于指向该文件的 SQLite 引擎，供既有测试与脚本沿用旧签名）。
    """

    def __init__(self, engine_or_path: AsyncEngine | str | Path):
        if isinstance(engine_or_path, (str, Path)):
            # 延迟导入避免与 repositories 形成循环依赖。
            from sqlalchemy.pool import NullPool

            from app.infrastructure.persistence.sql.repositories import create_engine

            # NullPool：日志是最高频的写入方，且每次操作本就是一次短事务。
            # 池化会让同一 SQLite 文件上长期并存多条连接，多实例并发时写锁竞争
            # 明显变长（实测会出现 10 秒级等待）；改回"每次操作新建并关闭"后
            # 与原实现一致，锁的持有窗口只覆盖单次事务。
            engine = create_engine(
                f"sqlite+aiosqlite:///{engine_or_path}", poolclass=NullPool
            )
        else:
            engine = engine_or_path
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    async def initialize(self):
        """幂等建表。

        多个实例（含多进程）可能同时打开同一个新库，DDL 本身也要争写锁，因此失败要
        按锁竞争重试；非锁错误（如文件根本不是数据库）必须立刻抛出，不能拖成超时。
        实例内的 asyncio.Lock 只防同实例重入，跨实例串行化靠数据库写锁 + 此处重试。
        """
        async with self._init_lock:
            if self._initialized:
                return
            # 初始化短等待；总体期限避免 busy_timeout × 重试次数形成长时间挂起。
            deadline = time.monotonic() + 8.0
            for attempt in range(20):
                try:
                    async with self._engine.begin() as conn:
                        await conn.run_sync(
                            lambda sync: Base.metadata.create_all(sync, tables=[
                                AGUISessionRow.__table__,
                                AGUIRunRow.__table__,
                                AGUIEventRow.__table__,
                            ])
                        )
                    self._initialized = True
                    return
                except SQLAlchemyError as err:
                    if not _is_retryable_init_error(err) or attempt == 19 or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @asynccontextmanager
    async def _db(self, write=False):
        await self.initialize()
        async with self._sessions() as db:
            try:
                if self.dialect == "sqlite":
                    # 事件追加是全库竞争最激烈的写入路径，等待窗口给足 10 秒：
                    # 引擎连接监听里设的 5 秒在多方同时抢写锁时不够（会偶发
                    # database is locked）。PRAGMA 在进事务前生效，每次取连接重设。
                    await db.execute(text("PRAGMA busy_timeout=10000"))
                    if write:
                        await self._begin_sqlite_write(db)
                yield db
                if write:
                    await db.commit()
            except BaseException:
                if write:
                    await db.rollback()
                raise

    async def _begin_sqlite_write(self, db) -> None:
        """SQLite 单写者：提前取写锁，否则并发提交时才升级锁会报 database is locked。

        仅靠 busy_timeout 不够：写锁与提交交错时 SQLite 可能直接返回 SQLITE_BUSY
        而不进入等待。重试只包住取锁这一步——此刻用户代码尚未执行，重试不会造成重复写入。
        """
        deadline = time.monotonic() + 10.0
        while True:
            try:
                await db.execute(text("BEGIN IMMEDIATE"))
                return
            except SQLAlchemyError as err:
                if not _is_lock_contention(err) or time.monotonic() >= deadline:
                    raise
                # 取锁失败可能残留事务标记，先回滚再重试，避免 next BEGIN 报
                # "cannot start a transaction within a transaction"。
                await db.rollback()
                await asyncio.sleep(0.05)

    @staticmethod
    async def _one(db, sql, params=None):
        result = await db.execute(text(sql), params or {})
        return result.mappings().one_or_none()

    @staticmethod
    async def _all(db, sql, params=None):
        result = await db.execute(text(sql), params or {})
        return result.mappings().all()

    def _upsert_session_sql(self) -> str:
        if self.dialect == "mysql":
            # MySQL 8.0.20 起 VALUES(col) 已废弃并在每次执行时发警告，改用行别名
            # （需 8.0.19+；本机为 8.0.45）。语义等价：引用的是本次待插入的值。
            return (
                "INSERT INTO agui_sessions (session_id,buyer_id,title,messages_json,state_json,last_run_id,updated_at) "
                "VALUES (:session_id,:buyer_id,:title,:messages_json,:state_json,:last_run_id,:updated_at) AS new "
                "ON DUPLICATE KEY UPDATE messages_json=new.messages_json,state_json=new.state_json,"
                "last_run_id=new.last_run_id,updated_at=new.updated_at"
            )
        return (
            "INSERT INTO agui_sessions (session_id,buyer_id,title,messages_json,state_json,last_run_id,updated_at) "
            "VALUES (:session_id,:buyer_id,:title,:messages_json,:state_json,:last_run_id,:updated_at) "
            "ON CONFLICT(session_id) DO UPDATE SET messages_json=excluded.messages_json,"
            "state_json=excluded.state_json,last_run_id=excluded.last_run_id,updated_at=excluded.updated_at"
        )

    @staticmethod
    def _owned(row, buyer_id, session_id=None):
        if row is None:
            raise JournalNotFound("运行或会话不存在")
        if row["buyer_id"] != buyer_id or (session_id is not None and row["session_id"] != session_id):
            raise JournalForbidden("无权访问该买家的会话或运行")
        return row

    @staticmethod
    def _run(row):
        projection = json.loads(row["projection_json"])
        return {"runId": row["run_id"], "threadId": row["session_id"], "status": row["status"],
                "cursor": row["last_seq"], "stopRequested": bool(row["stop_requested"]),
                "input": json.loads(row["input_json"]), "messages": projection["messages"],
                "state": projection["state"], "updatedAt": int(row["updated_at"] * 1000)}

    async def latest_destination(self, session_id, buyer_id):
        """仅从本会话已保存的商品报价恢复目的地，不迁移旧 Agent/Skill 正文。"""
        async with self._db() as db:
            session = await self._one(db, "SELECT * FROM agui_sessions WHERE session_id=:sid", {"sid": session_id})
            self._owned(session, buyer_id)
            rows = await self._all(
                db,
                "SELECT projection_json FROM agui_runs WHERE session_id=:sid AND buyer_id=:bid "
                "ORDER BY created_at DESC LIMIT 30",
                {"sid": session_id, "bid": buyer_id},
            )
            for row in rows:
                products = json.loads(row["projection_json"]).get("state", {}).get("products", [])
                destinations = {(p.get("landed_price") or {}).get("ship_to") for p in products}
                destinations.discard(None)
                if len(destinations) == 1:
                    country = destinations.pop()
                    if isinstance(country, str) and len(country) == 2 and country.isascii() and country.isalpha():
                        return country.upper()
        return None

    async def reserve(self, body: dict, buyer_id: str, owner: str, lease_seconds=30) -> tuple[dict, bool]:
        now, run_id, session_id = time.time(), body["runId"], body["threadId"]
        user = body["messages"][-1]
        props = body.get("forwardedProps") or {}
        identity = {"threadId": session_id, "buyerId": buyer_id, "message": user,
                    "locale": props.get("locale", "zh-CN"), "currency": props.get("currency", "CNY")}
        # 未选择时保持旧运行指纹；有明确选择时版本/hash均属于本次请求身份。
        if "selectedSkill" in props:
            identity["selectedSkill"] = props["selectedSkill"]
        if body.get("resume"):
            identity["resume"] = body["resume"]
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._db(True) as db:
            await self._recover(db, now)
            previous = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:rid", {"rid": run_id})
            if previous:
                self._owned(previous, buyer_id, session_id)
                if previous["fingerprint"] != fingerprint:
                    raise JournalConflict("相同 runId 已用于不同请求")
                return self._run(previous), False
            session = await self._one(db, "SELECT * FROM agui_sessions WHERE session_id=:sid", {"sid": session_id})
            if session:
                self._owned(session, buyer_id)
            running = await self._one(
                db,
                "SELECT run_id FROM agui_runs WHERE session_id=:sid AND status='running'",
                {"sid": session_id},
            )
            if running:
                raise JournalConflict("该会话仍有执行中的运行，请先恢复或明确停止它")
            previous_state = json.loads(session["state_json"]) if session else {}
            if body.get("resume"):
                pending = {p['id'] for p in previous_state.get('toolApprovals', [])}
                requested = [p['interruptId'] for p in body['resume']]
                if not requested or len(set(requested)) != len(requested) or not set(requested) <= pending:
                    raise JournalConflict("确认已处理或与当前待执行操作不一致，请刷新会话")
            trusted_state = {k: previous_state[k] for k in ('products', 'decisionReport', 'searchCompleted', 'skillUsages') if k in previous_state} if body.get('resume') else {}
            messages = json.loads(session["messages_json"]) if session else []
            # 客户端历史/state 不是事实来源；只接受本轮 user，旧历史由日志恢复。
            messages = [*messages[-99:], {"id": user["id"], "role": "user", "content": user["content"]}]
            body = {**body, "messages": messages, "state": trusted_state}
            projection = {"messages": messages, "state": {}, "openMessages": [], "openTools": []}
            await db.execute(
                text("INSERT INTO agui_runs (run_id,session_id,buyer_id,fingerprint,input_json,projection_json,"
                     "status,owner,lease_until,created_at,updated_at) "
                     "VALUES (:run_id,:session_id,:buyer_id,:fingerprint,:input_json,:projection_json,"
                     "'running',:owner,:lease_until,:created_at,:updated_at)"),
                {"run_id": run_id, "session_id": session_id, "buyer_id": buyer_id, "fingerprint": fingerprint,
                 "input_json": _json(body), "projection_json": _json(projection),
                 "owner": owner, "lease_until": now + lease_seconds, "created_at": now, "updated_at": now},
            )
            title = session["title"] if session else str(user["content"])[:48]
            await db.execute(text(self._upsert_session_sql()), {
                "session_id": session_id, "buyer_id": buyer_id, "title": title,
                "messages_json": _json(messages), "state_json": "{}", "last_run_id": run_id, "updated_at": now,
            })
            return self._run(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:rid", {"rid": run_id})), True

    @staticmethod
    def _project(projection, event):
        kind = event["type"]
        if kind == "MESSAGES_SNAPSHOT":
            projection["messages"] = [m for m in event["messages"] if m.get("role") in {"user", "assistant"}]
        elif kind == "STATE_SNAPSHOT":
            projection["state"] = event["snapshot"]
        elif kind == "TEXT_MESSAGE_START":
            message_id = event["messageId"]
            if not any(m["id"] == message_id for m in projection["messages"]):
                projection["messages"].append({"id": message_id, "role": "assistant", "content": ""})
            if message_id not in projection["openMessages"]:
                projection["openMessages"].append(message_id)
        elif kind == "TEXT_MESSAGE_CONTENT":
            message = next((m for m in projection["messages"] if m["id"] == event["messageId"]), None)
            if message is None:
                raise JournalConflict("消息增量必须在对应 START 之后")
            message["content"] += event["delta"]
        elif kind == "TEXT_MESSAGE_END":
            projection["openMessages"] = [i for i in projection["openMessages"] if i != event["messageId"]]
        elif kind == "TOOL_CALL_START":
            projection["openTools"].append(event["toolCallId"])
        elif kind == "TOOL_CALL_END":
            projection["openTools"] = [i for i in projection["openTools"] if i != event["toolCallId"]]

    async def _append(self, db, row, events):
        projection, seq, status = json.loads(row["projection_json"]), row["last_seq"], row["status"]
        for event in events:
            self._project(projection, event)
            seq += 1
            await db.execute(
                text("INSERT INTO agui_events (run_id,seq,event_json) VALUES (:run_id,:seq,:event_json)"),
                {"run_id": row["run_id"], "seq": seq, "event_json": _json(event)},
            )
            if event["type"] == "RUN_FINISHED":
                status = "completed"
            elif event["type"] == "RUN_ERROR":
                status = "stopped" if event.get("code") == "CANCELLED" else "interrupted" if event.get("code") == "SERVER_RESTART" else "error"
        now = time.time()
        await db.execute(
            text("UPDATE agui_runs SET projection_json=:p,last_seq=:s,status=:st,updated_at=:u WHERE run_id=:r"),
            {"p": _json(projection), "s": seq, "st": status, "u": now, "r": row["run_id"]},
        )
        await db.execute(
            text("UPDATE agui_sessions SET messages_json=:m,state_json=:s,updated_at=:u "
                 "WHERE session_id=:sid AND last_run_id=:r"),
            {"m": _json(projection["messages"][-100:]), "s": _json(projection["state"]),
             "u": now, "sid": row["session_id"], "r": row["run_id"]},
        )

    async def append(self, run_id, owner, events):
        async with self._db(True) as db:
            row = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": run_id})
            if not row or row["owner"] != owner or row["status"] != "running" or row["lease_until"] <= time.time():
                raise JournalLeaseLost("运行日志执行租约已失效")
            await self._append(db, row, events)

    async def renew(self, run_id, owner, lease_seconds):
        async with self._db(True) as db:
            result = await db.execute(
                text("UPDATE agui_runs SET lease_until=:new WHERE run_id=:r AND owner=:o "
                     "AND status='running' AND stop_requested=0 AND lease_until>:now"),
                {"new": time.time() + lease_seconds, "r": run_id, "o": owner, "now": time.time()},
            )
            return result.rowcount == 1

    async def _recover(self, db, now):
        expired = await self._all(
            db, "SELECT * FROM agui_runs WHERE status='running' AND lease_until<=:now", {"now": now},
        )
        for row in expired:
            projection = json.loads(row["projection_json"])
            stopped = bool(row["stop_requested"])
            events = [{"type": "TEXT_MESSAGE_END", "messageId": i} for i in projection["openMessages"]]
            if row["last_seq"] == 0:
                events.insert(0, {"type": "RUN_STARTED", "threadId": row["session_id"], "runId": row["run_id"]})
            events += [{"type": "TOOL_CALL_END", "toolCallId": i} for i in projection["openTools"]]
            events += [{"type": "STATE_SNAPSHOT", "snapshot": {**projection["state"], "status": "cancelled" if stopped else "error"}},
                       {"type": "RUN_ERROR", "message": "本轮已明确停止" if stopped else "服务重启或执行租约失效，本轮未完成；已恢复保存的内容，可重新提交需求。", "code": "CANCELLED" if stopped else "SERVER_RESTART"}]
            await self._append(db, row, events)

    async def recover_expired(self):
        async with self._db(True) as db:
            await self._recover(db, time.time())

    async def run(self, run_id, buyer_id, session_id=None):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            return self._run(self._owned(
                await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": run_id}), buyer_id, session_id,
            ))

    async def events(self, run_id, buyer_id, after=0, limit=200):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            row = self._owned(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": run_id}), buyer_id)
            if after < 0 or after > row["last_seq"]:
                raise JournalConflict("事件游标超出该运行范围")
            rows = await self._all(
                db,
                "SELECT seq,event_json FROM agui_events WHERE run_id=:r AND seq>:after ORDER BY seq LIMIT :lim",
                {"r": run_id, "after": after, "lim": limit},
            )
            events = [{"seq": item["seq"], "event": json.loads(item["event_json"])} for item in rows]
            return events, row["status"], row["last_seq"]

    async def request_stop(self, run_id, buyer_id):
        async with self._db(True) as db:
            row = self._owned(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": run_id}), buyer_id)
            if row["status"] == "running":
                await db.execute(text("UPDATE agui_runs SET stop_requested=1 WHERE run_id=:r"), {"r": run_id})
        return await self.run(run_id, buyer_id)

    async def end_owned(self, run_id, owner, *, stopped=False):
        """生产协程在首次调度前就被取消时也要收口；终态不覆盖，其他 owner 不可写。"""
        async with self._db(True) as db:
            row = await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": run_id})
            if not row or row["owner"] != owner or row["status"] != "running":
                return
            projection = json.loads(row["projection_json"])
            events = [{"type": "TEXT_MESSAGE_END", "messageId": i} for i in projection["openMessages"]]
            if row["last_seq"] == 0:
                events.insert(0, {"type": "RUN_STARTED", "threadId": row["session_id"], "runId": row["run_id"]})
            events += [{"type": "TOOL_CALL_END", "toolCallId": i} for i in projection["openTools"]]
            events += [{"type": "STATE_SNAPSHOT", "snapshot": {**projection["state"], "status": "cancelled" if stopped else "error"}},
                       {"type": "RUN_ERROR", "message": "本轮已明确停止" if stopped else "服务已关闭，本轮已保存为中断，可恢复查看已有内容。", "code": "CANCELLED" if stopped else "SERVER_RESTART"}]
            await self._append(db, row, events)

    async def sessions(self, buyer_id):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            rows = await self._all(
                db,
                "SELECT session_id,title,updated_at,last_run_id FROM agui_sessions "
                "WHERE buyer_id=:bid ORDER BY updated_at DESC LIMIT 100",
                {"bid": buyer_id},
            )
            return [{"id": row["session_id"], "title": row["title"], "updatedAt": int(row["updated_at"] * 1000),
                     "runId": row["last_run_id"], "source": "server"} for row in rows]

    async def session(self, session_id, buyer_id):
        async with self._db(True) as db:
            await self._recover(db, time.time())
            row = self._owned(
                await self._one(db, "SELECT * FROM agui_sessions WHERE session_id=:sid", {"sid": session_id}), buyer_id,
            )
            run = self._run(await self._one(db, "SELECT * FROM agui_runs WHERE run_id=:r", {"r": row["last_run_id"]}))
            return {"id": session_id, "title": row["title"], "messages": json.loads(row["messages_json"]),
                    "state": json.loads(row["state_json"]), "run": run, "updatedAt": int(row["updated_at"] * 1000)}
