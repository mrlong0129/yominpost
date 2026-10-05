"""极简 SQLite 持久化:保存流水线运行结果与品牌 DNA。

只存 JSON blob,够用且零迁移负担。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import DB_PATH
from .schemas import BrandDNA, PipelineResult


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    with _conn(db_path) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS runs(
                 run_id TEXT PRIMARY KEY,
                 created_at TEXT,
                 provider TEXT,
                 brand_name TEXT,
                 num_packages INTEGER,
                 payload TEXT)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS brands(
                 brand_name TEXT PRIMARY KEY,
                 payload TEXT)"""
        )


def save_run(result: PipelineResult, db_path: Path | str | None = None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)",
            (
                result.run_id,
                result.created_at,
                result.provider,
                result.brand.brand_name,
                len(result.packages),
                result.model_dump_json(),
            ),
        )


def list_runs(db_path: Path | str | None = None) -> list[dict]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT run_id, created_at, provider, brand_name, num_packages "
            "FROM runs ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: str, db_path: Path | str | None = None) -> PipelineResult | None:
    init_db(db_path)
    with _conn(db_path) as c:
        row = c.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    return PipelineResult.model_validate(json.loads(row["payload"]))


def save_brand(brand: BrandDNA, db_path: Path | str | None = None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO brands VALUES(?,?)",
            (brand.brand_name, brand.model_dump_json()),
        )


def get_brand(brand_name: str, db_path: Path | str | None = None) -> BrandDNA | None:
    init_db(db_path)
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT payload FROM brands WHERE brand_name=?", (brand_name,)
        ).fetchone()
    if not row:
        return None
    return BrandDNA.model_validate(json.loads(row["payload"]))
