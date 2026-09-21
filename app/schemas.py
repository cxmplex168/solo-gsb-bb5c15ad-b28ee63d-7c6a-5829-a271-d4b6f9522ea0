"""请求 / 响应模型。

受控揭盲约定：创建与详情接口只返回受试者 ID 列表，不返回任何
条件序列；条件按周期通过推进接口逐个揭盲。
"""

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


class ExperimentOut(BaseModel):
    """创建 / 详情响应：不含任何条件序列（受控揭盲）。"""

    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    created_at: str
    subjects: list[str]


class ExperimentSummary(BaseModel):
    experiment_id: str
    conditions: list[str]
    seed: int
    group_size: int
    num_subjects: int
    num_periods: int
    created_at: str


class AdvanceRequest(BaseModel):
    """按周期揭盲的请求体。"""

    subject_id: str = Field(..., min_length=1, description="受试者 ID")
    expected_period: int = Field(
        ..., ge=1, description="期望揭盲的周期，必须等于该受试者下一待揭盲周期"
    )
    request_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="幂等键：相同 request_id 重试返回首次结果，不重复推进",
    )


class AdvanceOut(BaseModel):
    """揭盲成功（或幂等重放）的响应。"""

    experiment_id: str
    subject_id: str
    period: int
    condition: str
    next_period: int
    request_id: str
    revealed_at: str


class RevealOut(BaseModel):
    """一条已揭盲记录。"""

    period: int
    condition: str
    request_id: str
    revealed_at: str


class ProgressOut(BaseModel):
    """受试者揭盲进度：只含已揭盲记录，不暴露后续安排。"""

    experiment_id: str
    subject_id: str
    num_periods: int
    next_period: int
    completed: bool
    reveals: list[RevealOut]
