"""交叉实验顺序平衡 API。

使用 Williams 平衡拉丁方为受试者分配条件顺序，保证：
* 每个条件在每个位置上出现次数相等；
* 每个有向相邻条件对出现次数相等。
"""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from . import db
from .schemas import ExperimentCreate, ExperimentOut, ExperimentSummary
from .williams import group_size, williams_sequences


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器 / 进程启动时建表
    db.init_db()
    yield


app = FastAPI(
    title="Crossover Experiment Order Balancer",
    description="基于 Williams 平衡拉丁方的交叉实验顺序分配服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/experiments",
    status_code=201,
    response_model=ExperimentOut,
    summary="创建实验并生成分配结果",
)
def create_experiment(payload: ExperimentCreate) -> dict:
    conditions = payload.conditions
    subject_ids = payload.subject_ids

    if any(not c.strip() for c in conditions):
        raise HTTPException(
            status_code=422, detail="conditions 不能为空字符串"
        )
    if len(set(conditions)) != len(conditions):
        raise HTTPException(
            status_code=422, detail="conditions 必须互异（存在重复条件）"
        )
    if any(not s.strip() for s in subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 不能为空字符串"
        )
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 必须互异（存在重复受试者）"
        )

    n = len(conditions)
    size = group_size(n)
    count = len(subject_ids)

    if count % size != 0:
        lower = (count // size) * size
        upper = lower + size
        nearest = [lower, upper] if lower > 0 else [upper]
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"受试者数量 {count} 无法整分为完整的序列组"
                    f"（每组 {size} 条序列）"
                ),
                "subject_count": count,
                "group_size": size,
                "nearest_acceptable": nearest,
            },
        )

    # Williams 平衡序列（下标形式），再映射为实际条件
    index_sequences = williams_sequences(n)
    condition_sequences = [
        [conditions[i] for i in seq] for seq in index_sequences
    ]

    # 用种子确定性地打乱“序列池”，再依次分配给受试者。
    # 池中每条序列恰好出现 count // size 次，保证整组完整。
    rng = random.Random(payload.seed)
    pool = list(range(size)) * (count // size)
    rng.shuffle(pool)

    assignments = [
        {
            "subject_id": subject_id,
            "sequence_index": pool[i],
            "sequence": condition_sequences[pool[i]],
        }
        for i, subject_id in enumerate(subject_ids)
    ]

    experiment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_experiment(
        experiment_id, conditions, payload.seed, size, created_at, assignments
    )

    return {
        "experiment_id": experiment_id,
        "conditions": conditions,
        "seed": payload.seed,
        "group_size": size,
        "num_subjects": count,
        "created_at": created_at,
        "assignments": assignments,
    }


@app.get(
    "/experiments",
    response_model=list[ExperimentSummary],
    summary="列出全部实验",
)
def list_experiments() -> list[dict]:
    return db.list_experiments()


@app.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentOut,
    summary="查询实验分配结果",
)
def get_experiment(experiment_id: str) -> dict:
    experiment = db.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment
