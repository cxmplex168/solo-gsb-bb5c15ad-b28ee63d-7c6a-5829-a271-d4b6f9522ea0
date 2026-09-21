"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id           TEXT PRIMARY KEY,
    conditions   TEXT NOT NULL,          -- JSON 数组
    seed         INTEGER NOT NULL,
    group_size   INTEGER NOT NULL,       -- 一组完整序列的条数
    num_subjects INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    experiment_id  TEXT NOT NULL REFERENCES experiments(id),
    subject_id     TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,     -- 命中的 Williams 序列下标
    sequence       TEXT NOT NULL,        -- JSON 数组，映射后的条件序列
    PRIMARY KEY (experiment_id, subject_id)
);
"""


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "experiments.db")


def get_connection() -> sqlite3.Connection:
    path = Path(_database_path())
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等），在应用启动时调用。"""
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)


def insert_experiment(
    experiment_id: str,
    conditions: list[str],
    seed: int,
    group_size: int,
    created_at: str,
    assignments: list[dict[str, Any]],
) -> None:
    with closing(get_connection()) as conn:
        with conn:
            conn.execute(
                "INSERT INTO experiments "
                "(id, conditions, seed, group_size, num_subjects, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    json.dumps(conditions, ensure_ascii=False),
                    seed,
                    group_size,
                    len(assignments),
                    created_at,
                ),
            )
            conn.executemany(
                "INSERT INTO assignments "
                "(experiment_id, subject_id, sequence_index, sequence) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        experiment_id,
                        a["subject_id"],
                        a["sequence_index"],
                        json.dumps(a["sequence"], ensure_ascii=False),
                    )
                    for a in assignments
                ],
            )


def get_experiment(experiment_id: str) -> dict[str, Any] | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        assignment_rows = conn.execute(
            "SELECT subject_id, sequence_index, sequence FROM assignments "
            "WHERE experiment_id = ? ORDER BY rowid",
            (experiment_id,),
        ).fetchall()
    return _experiment_dict(row, assignment_rows)


def list_experiments() -> list[dict[str, Any]]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at, id"
        ).fetchall()
    return [_experiment_dict(row, None) for row in rows]


def _experiment_dict(
    row: sqlite3.Row, assignment_rows: list[sqlite3.Row] | None
) -> dict[str, Any]:
    experiment: dict[str, Any] = {
        "experiment_id": row["id"],
        "conditions": json.loads(row["conditions"]),
        "seed": row["seed"],
        "group_size": row["group_size"],
        "num_subjects": row["num_subjects"],
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        experiment["assignments"] = [
            {
                "subject_id": a["subject_id"],
                "sequence_index": a["sequence_index"],
                "sequence": json.loads(a["sequence"]),
            }
            for a in assignment_rows
        ]
    return experiment
