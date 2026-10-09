# -*- coding: utf-8 -*-
"""快照层（CJ/Amazon/eBay 商品快照）的 MySQL schema 定义。

与热路径 :mod:`tables` 完全解耦：热路径的 ``Base`` 由 Alembic 管理（findora 业务库），
而快照层走一次性全量迁移脚本（``scripts/migrate_snapshot_to_mysql.py``），不进 Alembic、
也不复用 ``tables.Base``——两套 metadata 彼此独立，避免热路径表被快照迁移误建、
或快照表被 Alembic 意外接管。

``_LongText`` 在此文件内**重定义**（不从 ``tables`` import），原因同热路径：
MySQL 的 ``TEXT`` 上限 65535 字节，CJ 快照的 ``detail_json`` 实测存在 >64KB 的行，
不升 ``LONGTEXT`` 会在写入时截断或报错；SQLite 的 ``TEXT`` 本就无长度上限，无需变体。
"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 大文本：MySQL 用 LONGTEXT（4 字节 emoji 等 utf8mb4 内容由表级 __table_args__ 保证），
# SQLite 的 TEXT 无长度上限、无需变体。与 tables._LongText 是各自独立的对象。
_LongText = Text().with_variant(mysql.LONGTEXT, "mysql")


class SnapshotBase(DeclarativeBase):
    """快照层的独立 Base，与热路径 ``tables.Base`` 零耦合（不共享 metadata）。"""


class CJProductRow(SnapshotBase):
    """CJ 商品快照表，列与 ``scripts/sync_cj_catalog.py`` 的 DDL 及其后的
    ``cj_enrichment`` ALTER 扩展（source_url* / enrichment_attempted_at）一一对应。

    三张 JSON 列在 MySQL 上统一 utf8mb4 / utf8mb4_unicode_ci（表级 charset），
    承载中文标题与 4 字节 emoji，且 LONGTEXT 允许 >64KB 的 detail_json 完整保留。
    """

    __tablename__ = "products"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    pid: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_category: Mapped[str] = mapped_column(String(128))
    second_category: Mapped[str] = mapped_column(String(128))
    third_category: Mapped[str] = mapped_column(String(128))
    list_json: Mapped[str] = mapped_column(_LongText)
    list_fetched_at: Mapped[str] = mapped_column(String(40))
    detail_json: Mapped[str | None] = mapped_column(_LongText, nullable=True)
    detail_fetched_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    inventory_json: Mapped[str | None] = mapped_column(_LongText, nullable=True)
    inventory_fetched_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    inventory_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 以下 7 列为 cj_enrichment 采集阶段对 products 的 ALTER 扩展（源 URL 与证据），
    # 与 sync_cj_catalog.py 原始 DDL 之后的实际 schema 对齐；实测均 < 700 字节，无 64KB 风险。
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url_checked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_url_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url_page_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url_page_checked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    enrichment_attempted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class AmazonProductRow(SnapshotBase):
    """Amazon 商品快照表，列与 :mod:`snapshot_import` 的建表 DDL 一一对应。

    与 CJ 不同，Amazon/eBay 每张卡是**整段 JSON**（``card_json`` + ``raw_json``），不是多列。
    字节口径实测（``scripts/scan_snapshot_json_bytes.py``，2320 行）：

    - ``card_json`` 最大 12,061 B：由 ``normalize()`` 截断（title 500 / description 1400 /
      highlights 240×6 / brand 100 / spec 180~200），有界，无 64KB 风险；
    - ``raw_json`` 最大 221,602 B，**12 行 >64KB**：存的是未截断的原始采集记录，
      MySQL ``TEXT`` 存不下。故 ``raw_json`` 定为 LONGTEXT；``card_json`` 同表统一 LONGTEXT，
      避免日后放宽截断后静默溢出。
    """

    __tablename__ = "amazon_products"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    product_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    card_json: Mapped[str] = mapped_column(_LongText)
    raw_json: Mapped[str] = mapped_column(_LongText)


class EbayProductRow(SnapshotBase):
    """eBay 商品快照表，列与 Amazon 同构（见 :class:`AmazonProductRow`）。

    字节口径实测（924 行）：``card_json`` 最大 11,804 B；``raw_json`` 最大 386,947 B，
    **57 行 >64KB**（占比 6.2%，远高于 Amazon 的 0.5%），是本轮最需要用 LONGTEXT 的一列。
    """

    __tablename__ = "ebay_products"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    product_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    card_json: Mapped[str] = mapped_column(_LongText)
    raw_json: Mapped[str] = mapped_column(_LongText)
