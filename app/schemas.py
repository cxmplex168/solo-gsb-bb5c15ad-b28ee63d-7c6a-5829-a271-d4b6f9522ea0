"""请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExperimentCreate(BaseModel):
    """创建交叉实验的请求体。"""

    conditions: list[str] = Field(
        ..., min_length=2, description="互异的实验条件，至少 2 个"
    )
    subject_ids: list[str] = Field(
        ..., min_length=1, description="互异的受试者 ID"
    )
    seed: int = Field(..., description="随机种子，相同输入与种子产生相同分配")


class AssignmentOut(BaseModel):
    subject_id: str
    sequence_index: int
    sequence: list[str]


class ExperimentOut(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    created_at: str
    assignments: list[AssignmentOut]


class ExperimentSummary(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    created_at: str
