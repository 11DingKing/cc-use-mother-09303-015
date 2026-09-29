# 产教需求版本库

本项目维护产教需求版本库的领域约定、角色边界与样例数据，并提供从零实现的完整后端服务：把企业岗位能力需求连同**证据来源、适用地区、有效期、发布方**保存为不可变版本，关联课程、实训项目与技能标准，经企业与院校双方共同确认后发布；撤回与更正只产生替代链，既有培养方案始终引用原版本，并可对任一历史版本做影响分析。

## 领域规则（不变量）

1. **需求版本**：版本一经发布即冻结，修订只能新建版本；同一需求线（`demand_code`）的 `revision` 单调递增。
2. **双方确认**：企业、院校各自独立确认（不可重复、不可代签），双方齐备后才允许发布。
3. **状态机**：采集 → 核验 → 待确认 → 发布；发布后可进入 已撤回 / 已更正。
4. **替代链**：撤回/更正不删除、不改写旧版本；后继经 `supersedes_id` 线性串联，禁止链分叉。
5. **唯一生效版本**：任一需求线至多一个"发布"版本；后继发布时前驱在同一事务内自动转为"已更正"。
6. **失效后恢复**：仅"已撤回"可恢复；"已更正"版本因存在生效后继不可恢复，避免双生效。
7. **关联无环**：需求→对象、对象→对象两类有向边，新增前统一做循环检测。
8. **影响分析**：沿反向依赖图列出直接引用对象与全部下游对象（去重、稳定排序）；版本失效后历史引用仍可完整追溯。
9. **并发安全**：写事务 `BEGIN IMMEDIATE` 串行化 + 版本号乐观锁 + 数据库层"唯一生效"部分唯一索引，三重守卫并发发布。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/demand_registry/`：后端实现
  - `models.py`：领域模型、状态机与迁移表
  - `errors.py`：领域错误类型
  - `ports.py`：仓储端口（领域不依赖存储细节）
  - `service.py`：应用服务（全部用例与一致性守卫）
  - `repositories.py`：内存仓储与 SQLite 仓储（零第三方依赖）
  - `serializers.py`：JSON 序列化
  - `api.py`：标准库 HTTP 接口（`/api/v1`）
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、服务不变量（内存/SQLite 双跑）、并发、HTTP 端到端测试。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部回归（28 个用例）
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 启动服务

```bash
PYTHONPATH=src python3 -m demand_registry.api --db registry.db --port 8080
# --db 缺省时为内存模式（适合演示/测试）
```

主要接口（前缀 `/api/v1`）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /demands` | 采集需求，生成需求线第 1 版（采集态） |
| `GET /versions?demand_code=...` | 按需求线列出全部历史版本 |
| `PATCH /versions/{id}` | 修改采集态草稿（核验后冻结，返回 409） |
| `POST /versions/{id}/verify` | 采集 → 核验 |
| `POST /versions/{id}/request-confirmation` | 核验 → 待确认 |
| `POST /versions/{id}/confirmations` | 一方确认：`{"party":"企业"|"院校","confirmer":"..."}` |
| `POST /versions/{id}/publish` | 双方齐备后发布（可带 `expected_version` 乐观锁） |
| `POST /versions/{id}/withdraw` | 撤回（`reason` 必填），历史保留 |
| `POST /versions/{id}/correct` | 更正：创建替代后继草稿，旧版不变 |
| `POST /versions/{id}/restore` | 已撤回版本恢复；已更正版本返回 409 |
| `GET /versions/{id}/chain` | 替代链：祖先链、后继、最新版本 |
| `GET /versions/{id}/impact` | 影响分析：直接引用 + 下游调整范围 |
| `POST /targets` / `GET /targets` | 登记/列出 课程、实训项目、技能标准 |
| `POST /versions/{id}/links` | 需求版本关联对象（仅生效版本；类型须匹配） |
| `POST /targets/{id}/edges` | 对象间有向依赖，成环返回 422 |

错误统一为 `{"error": 类型, "message": 中文说明}`：校验错误 400、未找到 404、状态/并发冲突 409、成环 422。
