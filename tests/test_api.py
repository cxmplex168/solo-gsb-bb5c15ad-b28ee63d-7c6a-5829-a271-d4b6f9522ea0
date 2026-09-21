"""API、受控揭盲流程与 Williams 序列的自动化测试。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.williams import group_size, williams_sequences


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def assert_balanced(sequences: list[list[str]], n: int) -> None:
    """校验位置平衡与有向相邻对平衡。"""
    size = group_size(n)
    assert len(sequences) % size == 0
    k = len(sequences) // size  # 完整组数
    per_position = k * (1 if n % 2 == 0 else 2)

    # 每个条件在每个位置出现次数相等
    for pos in range(n):
        counts = Counter(seq[pos] for seq in sequences)
        assert len(counts) == n
        assert all(v == per_position for v in counts.values())

    # 每个有向相邻条件对出现次数相等
    pairs: Counter = Counter()
    for seq in sequences:
        for a, b in zip(seq, seq[1:]):
            pairs[(a, b)] += 1
    assert len(pairs) == n * (n - 1)
    assert len(set(pairs.values())) == 1


# ---------- 测试辅助 ----------

def create_experiment(
    client: TestClient,
    conditions: list[str],
    subject_ids: list[str],
    seed: int = 42,
) -> dict:
    resp = client.post(
        "/experiments",
        json={
            "conditions": conditions,
            "subject_ids": subject_ids,
            "seed": seed,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def advance(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    expected_period: int,
    request_id: str,
):
    return client.post(
        f"/experiments/{experiment_id}/advance",
        json={
            "subject_id": subject_id,
            "expected_period": expected_period,
            "request_id": request_id,
        },
    )


def reveal_all(
    client: TestClient, experiment_id: str, subject_id: str, num_periods: int
) -> list[str]:
    """依次揭盲某受试者的全部周期，返回条件序列。"""
    conditions = []
    for period in range(1, num_periods + 1):
        resp = advance(
            client, experiment_id, subject_id, period,
            f"reveal-{subject_id}-{period}",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == period
        assert body["next_period"] == period + 1
        conditions.append(body["condition"])
    return conditions


def get_progress(
    client: TestClient, experiment_id: str, subject_id: str
) -> dict:
    resp = client.get(
        f"/experiments/{experiment_id}/subjects/{subject_id}/progress"
    )
    assert resp.status_code == 200
    return resp.json()


# ---------- Williams 序列单元测试 ----------

@pytest.mark.parametrize("n", range(2, 9))
def test_williams_sequences_balanced(n: int) -> None:
    sequences = [[str(i) for i in seq] for seq in williams_sequences(n)]
    assert len(sequences) == group_size(n)
    assert_balanced(sequences, n)


# ---------- 创建 / 查询（盲态） ----------

def test_create_even_conditions(client: TestClient) -> None:
    body = create_experiment(
        client, ["A", "B", "C", "D"], [f"s{i}" for i in range(8)]
    )
    assert body["group_size"] == 4
    assert body["num_subjects"] == 8
    assert body["num_periods"] == 4
    assert body["subjects"] == [f"s{i}" for i in range(8)]

    # 逐期揭盲后，整体仍满足 Williams 平衡
    sequences = [
        reveal_all(client, body["experiment_id"], f"s{i}", 4)
        for i in range(8)
    ]
    assert_balanced(sequences, 4)


def test_create_odd_conditions(client: TestClient) -> None:
    body = create_experiment(
        client, ["A", "B", "C"], [f"s{i}" for i in range(6)], seed=7
    )
    assert body["group_size"] == 6
    sequences = [
        reveal_all(client, body["experiment_id"], f"s{i}", 3)
        for i in range(6)
    ]
    assert_balanced(sequences, 3)


def test_create_and_detail_do_not_expose_sequences(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    for body in (
        created,
        client.get(f"/experiments/{created['experiment_id']}").json(),
    ):
        # 不得返回完整条件序列相关的任何字段
        assert "assignments" not in body
        assert "sequence" not in body
        assert "sequence_index" not in body
        assert all(isinstance(s, str) for s in body["subjects"])


def test_same_input_and_seed_gives_same_assignment(client: TestClient) -> None:
    payload = {
        "conditions": ["A", "B", "C"],
        "subject_ids": [f"s{i}" for i in range(12)],
        "seed": 123,
    }
    first = client.post("/experiments", json=payload).json()
    second = client.post("/experiments", json=payload).json()
    # 分配结果不再直接可见，通过逐期揭盲比较两次分配是否一致
    for i in range(12):
        seq_first = reveal_all(client, first["experiment_id"], f"s{i}", 3)
        seq_second = reveal_all(client, second["experiment_id"], f"s{i}", 3)
        assert seq_first == seq_second


def test_subject_count_not_multiple_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": [f"s{i}" for i in range(7)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["group_size"] == 6
    assert detail["subject_count"] == 7
    assert detail["nearest_acceptable"] == [6, 12]


def test_subject_count_below_one_group_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C", "D"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["nearest_acceptable"] == [4]


def test_duplicate_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "A", "B"],
            "subject_ids": [f"s{i}" for i in range(6)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_duplicate_subjects_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": ["s1", "s1", "s2", "s3", "s4", "s5"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_too_few_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={"conditions": ["A"], "subject_ids": ["s1"], "seed": 1},
    )
    assert resp.status_code == 422


def test_get_experiment(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    resp = client.get(f"/experiments/{experiment_id}")
    assert resp.status_code == 200
    assert resp.json() == created

    listing = client.get("/experiments").json()
    summaries = [e for e in listing if e["experiment_id"] == experiment_id]
    assert len(summaries) == 1
    assert summaries[0]["num_periods"] == 2
    assert "subjects" not in summaries[0]


def test_get_unknown_experiment_returns_404(client: TestClient) -> None:
    assert client.get("/experiments/nope").status_code == 404


# ---------- 按周期揭盲 ----------

def test_advance_reveals_one_period_at_a_time(client: TestClient) -> None:
    created = create_experiment(
        client, ["A", "B", "C"], [f"s{i}" for i in range(6)], seed=3
    )
    experiment_id = created["experiment_id"]

    resp = advance(client, experiment_id, "s1", 1, "req-1")
    assert resp.status_code == 200
    first = resp.json()
    assert first["experiment_id"] == experiment_id
    assert first["subject_id"] == "s1"
    assert first["period"] == 1
    assert first["condition"] in {"A", "B", "C"}
    assert first["next_period"] == 2
    assert first["request_id"] == "req-1"

    resp = advance(client, experiment_id, "s1", 2, "req-2")
    assert resp.status_code == 200
    assert resp.json()["next_period"] == 3

    progress = get_progress(client, experiment_id, "s1")
    assert progress["next_period"] == 3
    assert progress["num_periods"] == 3
    assert progress["completed"] is False
    assert [r["period"] for r in progress["reveals"]] == [1, 2]
    assert progress["reveals"][0]["condition"] == first["condition"]

    # 各受试者进度互不影响
    assert get_progress(client, experiment_id, "s2")["next_period"] == 1


def test_stale_or_future_period_returns_409(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    # 超前：期望周期不是下一周期
    resp = advance(client, experiment_id, "s1", 2, "req-early")
    assert resp.status_code == 409
    assert resp.json()["detail"]["next_period"] == 1

    assert advance(client, experiment_id, "s1", 1, "req-1").status_code == 200

    # 过期：周期 1 已揭盲
    resp = advance(client, experiment_id, "s1", 1, "req-stale")
    assert resp.status_code == 409
    assert resp.json()["detail"]["next_period"] == 2

    # 409 不多揭露任何条件：进度与已揭盲记录不变
    progress = get_progress(client, experiment_id, "s1")
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_same_request_id_replays_first_result(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    first = advance(client, experiment_id, "s1", 1, "req-1").json()
    for _ in range(3):
        replay = advance(client, experiment_id, "s1", 1, "req-1")
        assert replay.status_code == 200
        assert replay.json() == first  # 与首次结果完全一致

    # 不重复推进
    progress = get_progress(client, experiment_id, "s1")
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_request_id_reused_with_different_params_conflicts(
    client: TestClient,
) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    assert advance(client, experiment_id, "s1", 1, "req-1").status_code == 200
    # 相同 request_id 但受试者 / 周期不同 → 409
    assert advance(client, experiment_id, "s2", 1, "req-1").status_code == 409
    assert advance(client, experiment_id, "s1", 2, "req-1").status_code == 409

    # 冲突请求不影响任何受试者进度
    assert get_progress(client, experiment_id, "s1")["next_period"] == 2
    assert get_progress(client, experiment_id, "s2")["next_period"] == 1


def test_advance_beyond_last_period_returns_409(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    reveal_all(client, experiment_id, "s1", 2)
    resp = advance(client, experiment_id, "s1", 3, "req-extra")
    assert resp.status_code == 409

    progress = get_progress(client, experiment_id, "s1")
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert len(progress["reveals"]) == 2


def test_advance_unknown_experiment_or_subject(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    assert advance(client, "nope", "s1", 1, "req-1").status_code == 404
    assert advance(client, experiment_id, "ghost", 1, "req-1").status_code == 404
    assert (
        client.get(f"/experiments/{experiment_id}/subjects/ghost/progress")
        .status_code == 404
    )
    assert client.get("/experiments/nope/subjects/s1/progress").status_code == 404


def test_progress_only_shows_revealed(client: TestClient) -> None:
    created = create_experiment(
        client, ["A", "B", "C"], [f"s{i}" for i in range(6)], seed=9
    )
    experiment_id = created["experiment_id"]

    advance(client, experiment_id, "s1", 1, "req-1")
    progress = get_progress(client, experiment_id, "s1")

    # 只返回已揭盲记录与 next_period，不暴露后续安排
    assert set(progress) == {
        "experiment_id", "subject_id", "num_periods",
        "next_period", "completed", "reveals",
    }
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1
    assert all(
        set(r) == {"period", "condition", "request_id", "revealed_at"}
        for r in progress["reveals"]
    )


def test_concurrent_advance_same_period_only_one_wins(
    client: TestClient,
) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    def do_advance(request_id: str):
        return advance(client, experiment_id, "s1", 1, request_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(do_advance, [f"req-{i}" for i in range(4)]))

    # 恰好一个成功，其余 409；只揭露一个条件
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]
    progress = get_progress(client, experiment_id, "s1")
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_concurrent_same_request_id_replays(client: TestClient) -> None:
    created = create_experiment(client, ["A", "B"], ["s1", "s2"], seed=5)
    experiment_id = created["experiment_id"]

    def do_advance(request_id: str):
        return advance(client, experiment_id, "s1", 1, request_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(do_advance, ["req-same"] * 4))

    # 并发重试同一 request_id：全部返回首次结果，且只推进一次
    assert all(r.status_code == 200 for r in responses)
    bodies = {json.dumps(r.json(), sort_keys=True) for r in responses}
    assert len(bodies) == 1
    progress = get_progress(client, experiment_id, "s1")
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


# ---------- 已有数据库启动迁移 ----------

def test_existing_database_is_migrated_on_startup(
    tmp_path, monkeypatch
) -> None:
    """旧版本（无揭盲表）的数据库，启动后自动补齐表结构并可正常揭盲。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE experiments (
            id           TEXT PRIMARY KEY,
            conditions   TEXT NOT NULL,
            seed         INTEGER NOT NULL,
            group_size   INTEGER NOT NULL,
            num_subjects INTEGER NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE assignments (
            experiment_id  TEXT NOT NULL,
            subject_id     TEXT NOT NULL,
            sequence_index INTEGER NOT NULL,
            sequence       TEXT NOT NULL,
            PRIMARY KEY (experiment_id, subject_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO experiments VALUES "
        "('exp1', '[\"A\", \"B\"]', 5, 2, 2, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO assignments VALUES ('exp1', 's1', 0, '[\"A\", \"B\"]')"
    )
    conn.execute(
        "INSERT INTO assignments VALUES ('exp1', 's2', 1, '[\"B\", \"A\"]')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    with TestClient(app) as test_client:
        # 旧实验详情可查（不含序列），且能按周期揭盲
        detail = test_client.get("/experiments/exp1")
        assert detail.status_code == 200
        assert detail.json()["subjects"] == ["s1", "s2"]
        assert "sequence" not in detail.json()

        resp = advance(test_client, "exp1", "s1", 1, "req-1")
        assert resp.status_code == 200
        assert resp.json()["condition"] == "A"

        progress = get_progress(test_client, "exp1", "s1")
        assert progress["next_period"] == 2
        assert [r["condition"] for r in progress["reveals"]] == ["A"]
