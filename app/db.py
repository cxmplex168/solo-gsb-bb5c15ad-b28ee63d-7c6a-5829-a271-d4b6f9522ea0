"""SQLite 存储层。

数据库路径通过环境变量 ``DATABASE_PATH`` 配置，默认为当前目录下的
``experiments.db``。表结构在应用启动时通过 :func:`init_db` 创建（幂等），
已有数据库会在启动时自动补齐缺失的表与索引。

受控揭盲相关表：

* ``subject_progress``：每个受试者下一个待揭盲的周期（1 起）；
* ``reveals``：已揭盲记录，``(experiment_id, request_id)`` 唯一索引
  保证相同 ``request_id`` 的重试返回首次结果且不重复推进。
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
    sequence       TEXT NOT NULL,        -- JSON 数组，完整条件序列（不对外暴露）
    PRIMARY KEY (experiment_id, subject_id)
);

-- 揭盲进度：每个受试者下一个待揭盲的周期（1 起）
CREATE TABLE IF NOT EXISTS subject_progress (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    next_period   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (experiment_id, subject_id)
);

-- 已揭盲记录；request_id 唯一索引用于幂等重放
CREATE TABLE IF NOT EXISTS reveals (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    subject_id    TEXT NOT NULL,
    period        INTEGER NOT NULL,
    condition     TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    revealed_at   TEXT NOT NULL,
    PRIMARY KEY (experiment_id, subject_id, period)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reveals_request_id
    ON reveals (experiment_id, request_id);
"""


def _database_path() -> str:
    return os.environ.get("DATABASE_PATH", "experiments.db")


def get_connection() -> sqlite3.Connection:
    path = Path(_database_path())
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    # timeout：并发写事务冲突时等待锁释放，而不是立即报 database is locked
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等），在应用启动时调用；已有数据库自动补齐缺失表结构。"""
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
    """查询实验详情（不含任何条件序列，序列只在揭盲时逐期返回）。"""
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            return None
        assignment_rows = conn.execute(
            "SELECT subject_id FROM assignments "
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


def advance_subject(
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    revealed_at: str,
) -> tuple[str, dict[str, Any]]:
    """按周期揭盲并原子推进，返回 ``(结果类型, 负载)``。

    结果类型：

    * ``"revealed"``：成功揭盲，负载为响应体；
    * ``"replayed"``：``request_id`` 幂等重放，负载为首次结果；
    * ``"not_found"``：实验或受试者不存在，负载含 ``message``；
    * ``"conflict"``：周期不符 / 已完成 / ``request_id`` 冲突，
      负载为 409 的 detail，且不会揭露任何条件。
    """
    with closing(get_connection()) as conn:
        conn.isolation_level = None  # 手动控制事务边界
        # BEGIN IMMEDIATE 立即获取写锁，把并发推进串行化：
        # 后到的事务在锁释放后重新读取进度，过期周期自然校验失败
        conn.execute("BEGIN IMMEDIATE")
        try:
            outcome, payload = _advance_in_transaction(
                conn,
                experiment_id,
                subject_id,
                expected_period,
                request_id,
                revealed_at,
            )
            conn.execute("COMMIT")
            return outcome, payload
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _advance_in_transaction(
    conn: sqlite3.Connection,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
    revealed_at: str,
) -> tuple[str, dict[str, Any]]:
    # 1) 幂等重放：request_id 已记录则返回首次结果，不再推进
    replay = conn.execute(
        "SELECT subject_id, period, condition, revealed_at FROM reveals "
        "WHERE experiment_id = ? AND request_id = ?",
        (experiment_id, request_id),
    ).fetchone()
    if replay is not None:
        if replay["subject_id"] == subject_id and replay["period"] == expected_period:
            return "replayed", {
                "experiment_id": experiment_id,
                "subject_id": subject_id,
                "period": replay["period"],
                "condition": replay["condition"],
                "next_period": replay["period"] + 1,
                "request_id": request_id,
                "revealed_at": replay["revealed_at"],
            }
        return "conflict", {
            "message": "request_id 已被其他揭盲请求使用",
            "request_id": request_id,
        }

    # 2) 实验与受试者存在性
    experiment = conn.execute(
        "SELECT id FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        return "not_found", {"message": "experiment not found"}
    assignment = conn.execute(
        "SELECT sequence FROM assignments "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()
    if assignment is None:
        return "not_found", {"message": "subject not found"}

    sequence = json.loads(assignment["sequence"])
    num_periods = len(sequence)

    # 3) 当前进度（老实验缺省从第 1 周期开始）
    conn.execute(
        "INSERT OR IGNORE INTO subject_progress "
        "(experiment_id, subject_id, next_period) VALUES (?, ?, 1)",
        (experiment_id, subject_id),
    )
    next_period = conn.execute(
        "SELECT next_period FROM subject_progress "
        "WHERE experiment_id = ? AND subject_id = ?",
        (experiment_id, subject_id),
    ).fetchone()["next_period"]

    # 4) 校验周期：过期 / 超前 / 已完成一律冲突，不揭露任何条件
    if next_period > num_periods:
        return "conflict", {
            "message": "该受试者全部周期均已揭盲",
            "next_period": next_period,
        }
    if expected_period != next_period:
        return "conflict", {
            "message": "expected_period 与下一待揭盲周期不一致",
            "expected_period": expected_period,
            "next_period": next_period,
        }

    # 5) 揭盲当前周期并原子推进
    condition = sequence[next_period - 1]
    conn.execute(
        "INSERT INTO reveals "
        "(experiment_id, subject_id, period, condition, request_id, revealed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (experiment_id, subject_id, next_period, condition, request_id, revealed_at),
    )
    conn.execute(
        "UPDATE subject_progress SET next_period = ? "
        "WHERE experiment_id = ? AND subject_id = ?",
        (next_period + 1, experiment_id, subject_id),
    )
    return "revealed", {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "period": next_period,
        "condition": condition,
        "next_period": next_period + 1,
        "request_id": request_id,
        "revealed_at": revealed_at,
    }


def get_subject_progress(
    experiment_id: str, subject_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """查询受试者揭盲进度，返回 ``(进度, 错误)``。

    只包含已揭盲记录与 ``next_period``，不暴露后续安排；
    错误为 ``"experiment"`` / ``"subject"`` / ``None``。
    """
    with closing(get_connection()) as conn:
        experiment = conn.execute(
            "SELECT conditions FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment is None:
            return None, "experiment"
        assignment = conn.execute(
            "SELECT 1 FROM assignments "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        if assignment is None:
            return None, "subject"
        progress = conn.execute(
            "SELECT next_period FROM subject_progress "
            "WHERE experiment_id = ? AND subject_id = ?",
            (experiment_id, subject_id),
        ).fetchone()
        reveal_rows = conn.execute(
            "SELECT period, condition, request_id, revealed_at FROM reveals "
            "WHERE experiment_id = ? AND subject_id = ? ORDER BY period",
            (experiment_id, subject_id),
        ).fetchall()

    num_periods = len(json.loads(experiment["conditions"]))
    next_period = progress["next_period"] if progress is not None else 1
    return {
        "experiment_id": experiment_id,
        "subject_id": subject_id,
        "num_periods": num_periods,
        "next_period": next_period,
        "completed": next_period > num_periods,
        "reveals": [dict(row) for row in reveal_rows],
    }, None


def _experiment_dict(
    row: sqlite3.Row, assignment_rows: list[sqlite3.Row] | None
) -> dict[str, Any]:
    conditions = json.loads(row["conditions"])
    experiment: dict[str, Any] = {
        "experiment_id": row["id"],
        "conditions": conditions,
        "seed": row["seed"],
        "group_size": row["group_size"],
        "num_subjects": row["num_subjects"],
        "num_periods": len(conditions),
        "created_at": row["created_at"],
    }
    if assignment_rows is not None:
        # 只返回受试者 ID，完整条件序列不对外暴露
        experiment["subjects"] = [a["subject_id"] for a in assignment_rows]
    return experiment
