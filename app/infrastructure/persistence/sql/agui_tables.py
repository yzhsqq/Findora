"""AG-UI 运行日志表（会话 / 运行 / 事件）。

原实现把 DDL 写在 AGUIJournal.initialize() 的 executescript 里，绑定 SQLite 语法
（BEGIN IMMEDIATE + TEXT + REAL）。改为声明式模型后，建表交给 SQLAlchemy，
升到 MySQL 时列类型自动按方言映射。

时间戳沿用 REAL（Unix 秒）而非 DATETIME：现有数据与租约比较（`lease_until > now`）
都按浮点秒计算，保持口径不变。
"""
from __future__ import annotations

from sqlalchemy import Double, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.persistence.sql.tables import Base, _LongText

# 说明：*_json 列在 SQLite 下是 TEXT（无长度上限），MySQL 下 TEXT 只有 64KB。
# 实测 agui_events.event_json 最大 31KB 且随会话增长，统一走 _LongText（MySQL LONGTEXT）。
#
# 时间戳用 Double 而非 Float：SQLAlchemy 的 Float 在 MySQL 上建成 4 字节 FLOAT，
# 而 Unix 秒已到 1.79e9，float32 在该量级只剩约 128 秒分辨率——lease_until 的
# 租约过期判断会偏差到分钟级。Double 在 MySQL 上是 8 字节，SQLite 侧仍是 REAL。


class AGUISessionRow(Base):
    __tablename__ = "agui_sessions"

    # 原表 session_id 是 TEXT PRIMARY KEY（SQLite 无长度限制），这里给足 255：
    # threadId 由前端生成，长度不受本仓控制，收窄会造成插入失败。
    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(512))
    messages_json: Mapped[str] = mapped_column(_LongText)
    state_json: Mapped[str] = mapped_column(_LongText)
    last_run_id: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[float] = mapped_column(Double)

    __table_args__ = (Index("agui_buyer_sessions", "buyer_id", "updated_at"),)


class AGUIRunRow(Base):
    __tablename__ = "agui_runs"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(255), index=True)
    buyer_id: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(128))
    input_json: Mapped[str] = mapped_column(_LongText)
    projection_json: Mapped[str] = mapped_column(_LongText)
    status: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(255))
    lease_until: Mapped[float] = mapped_column(Double)
    # server_default 而非 default：journal 用裸 SQL INSERT，不经过 ORM，
    # Python 层默认值不会出现在语句里；原 DDL 就是 DEFAULT 0。
    last_seq: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    stop_requested: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)

    __table_args__ = (Index("agui_run_session", "session_id", "status"),)


class AGUIEventRow(Base):
    __tablename__ = "agui_events"

    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_json: Mapped[str] = mapped_column(_LongText)
