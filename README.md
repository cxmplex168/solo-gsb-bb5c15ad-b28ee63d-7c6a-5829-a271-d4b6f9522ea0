# 交叉实验顺序平衡 API（按周期受控揭盲）

基于 **Python + FastAPI + SQLite** 的交叉（crossover）实验顺序分配服务。
创建实验时提交互异条件、受试者 ID 与随机种子，服务生成 **Williams 平衡拉丁方**序列
并映射给受试者，保证：

- 每个条件在每个位置上出现次数相等；
- 每个**有向相邻条件对**出现次数相等（平衡一阶残留效应）；
- 完整序列组：偶数条件含 `n` 条序列，奇数条件含 `2n` 条序列；
- 受试者数量必须是完整组大小的整数倍，否则返回 `422` 及最近可接受数量；
- 相同输入与相同种子产生完全相同的分配结果（确定性分配）。

**受控揭盲**：创建 / 详情接口**不返回完整条件序列**；每个受试者的条件通过
推进接口**按周期逐个揭盲**，且：

- 仅当 `expected_period` 等于该受试者下一待揭盲周期时才返回当前条件并**原子推进**；
- 相同 `request_id` 重试返回**首次结果**，不重复推进（幂等）；
- 并发或过期周期请求返回 `409`，且不多揭露任何条件；
- 进度查询只返回已揭盲记录与 `next_period`，不暴露后续安排。

## 快速启动（Docker Compose，一条命令）

```bash
docker compose up --build
```

该命令会构建镜像并启动容器：构建时安装依赖，容器启动命令中再次校验依赖安装，
应用启动钩子（lifespan）自动完成 SQLite 建表。**已有数据库（旧版本数据卷）会在
启动时自动补齐缺失的表结构**（`CREATE TABLE IF NOT EXISTS` / 唯一索引），无需手工迁移。

- **访问地址**：http://localhost:8000
- **交互式 API 文档（Swagger UI）**：http://localhost:8000/docs
- **端口**：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 中修改）

## 必需配置

无强制配置，开箱即用。可选环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/app/data/experiments.db`（compose 已设置）；本地运行默认 `./experiments.db` | SQLite 数据库文件路径，目录不存在时自动创建 |

compose 中通过命名卷 `crossover-data` 持久化数据，删除容器数据不丢失。

## API 说明

### 创建实验 `POST /experiments`

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
        "conditions": ["A", "B", "C"],
        "subject_ids": ["s1", "s2", "s3", "s4", "s5", "s6"],
        "seed": 42
      }'
```

- `conditions`：互异条件，至少 2 个；
- `subject_ids`：互异受试者 ID；数量必须是完整组大小（3 个条件时为 6）的整数倍；
- `seed`：随机种子（整数）。相同输入与种子 → 相同分配。

成功返回 `201`，响应**不含条件序列**，只包含实验元信息与受试者列表：

```json
{
  "experiment_id": "d2fa5e54…",
  "conditions": ["A", "B", "C"],
  "seed": 42,
  "group_size": 6,
  "num_subjects": 6,
  "num_periods": 3,
  "created_at": "2026-09-21T04:14:00.293884+00:00",
  "subjects": ["s1", "s2", "s3", "s4", "s5", "s6"]
}
```

受试者数量不成整组时返回 `422`，并给出最近可接受数量：

```json
{
  "detail": {
    "message": "受试者数量 7 无法整分为完整的序列组（每组 6 条序列）",
    "subject_count": 7,
    "group_size": 6,
    "nearest_acceptable": [6, 12]
  }
}
```

### 按周期揭盲 `POST /experiments/{experiment_id}/advance`

```bash
curl -X POST http://localhost:8000/experiments/<experiment_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{
        "subject_id": "s1",
        "expected_period": 1,
        "request_id": "client-generated-unique-id"
      }'
```

- `expected_period`：期望揭盲的周期（1 起），**必须等于该受试者下一待揭盲周期**；
- `request_id`：调用方生成的幂等键（同一实验内唯一）。

成功返回 `200`，只揭露当前周期的一个条件：

```json
{
  "experiment_id": "d2fa5e54…",
  "subject_id": "s1",
  "period": 1,
  "condition": "A",
  "next_period": 2,
  "request_id": "client-generated-unique-id",
  "revealed_at": "2026-09-21T04:14:00.370331+00:00"
}
```

语义：

- **幂等重试**：相同 `request_id`（且 `subject_id`、`expected_period` 与首次一致）
  返回与首次完全相同的响应，不重复推进；
- **409 冲突**：`expected_period` 过期 / 超前、全部周期已揭盲、或 `request_id`
  已被参数不同的请求使用——均返回 `409` 且不揭露任何条件；
- **404**：实验或受试者不存在。

### 受试者进度 `GET /experiments/{experiment_id}/subjects/{subject_id}/progress`

```json
{
  "experiment_id": "d2fa5e54…",
  "subject_id": "s1",
  "num_periods": 3,
  "next_period": 2,
  "completed": false,
  "reveals": [
    {
      "period": 1,
      "condition": "A",
      "request_id": "client-generated-unique-id",
      "revealed_at": "2026-09-21T04:14:00.370331+00:00"
    }
  ]
}
```

只返回**已揭盲记录**与 `next_period`，不暴露后续周期安排。

### 其他查询

- `GET /experiments/{experiment_id}`：实验详情（同样不含条件序列，不存在返回 `404`）；
- `GET /experiments`：列出全部实验摘要；
- `GET /health`：健康检查。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                       # 运行测试（含序列平衡性、幂等与并发校验）
uvicorn app.main:app --reload  # 本地启动，默认 http://127.0.0.1:8000
```

## 实现要点

- **Williams 序列**（`app/williams.py`）：第一行按 `0, n-1, 1, n-2, …` 从两端交替取值，
  其余行循环移位（模 n）；奇数阶再拼接镜像方阵，使每个有向相邻对出现次数相等。
- **确定性分配**（`app/main.py`）：将完整序列组复制 `受试者数 / 组大小` 份构成序列池，
  用 `random.Random(seed)` 打乱后依次映射给受试者，因此相同输入与种子结果一致。
- **受控揭盲**（`app/db.py`）：
  - 完整序列仅存于 `assignments` 表，创建 / 详情接口不返回；
  - 推进时在单条 `BEGIN IMMEDIATE` 事务内完成「校验幂等键 → 校验周期 →
    写入揭盲记录 → 推进 `next_period`」，并发请求被 SQLite 写锁串行化，
    过期周期自然校验失败返回 `409`；
  - `reveals` 表上 `(experiment_id, request_id)` 唯一索引保证相同 `request_id`
    的重试命中首次记录直接重放，不重复推进。
- **存储**：SQLite 四张表 `experiments`、`assignments`、`subject_progress`、`reveals`，
  启动时 `CREATE TABLE IF NOT EXISTS` 建表，已有数据库自动补齐缺失表结构。
