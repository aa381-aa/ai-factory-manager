# API 参考（AI工厂管家 · 社区版）

本文档仅列出社区版开放的 REST API。商业版 API（审计、ISO、L4 微调、SSO、多租户、跨部门授权等）不在开源范围。

## 基础约定

- 基础路径：`/api`
- 认证：`Authorization: Bearer <JWT>`（`POST /api/auth/login` 获取）
- 响应格式：`{"code": 0, "message": "ok", "data": {...}}`

## 认证（/api/auth）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/login` | 登录，返回 JWT |
| POST | `/auth/refresh` | 刷新令牌 |
| GET | `/auth/me` | 当前用户信息 |

## 对话（/api/chat）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 对话路由（意图识别 → Agent 分发） |
| POST | `/chat/stream` | 流式对话（SSE） |
| GET | `/chat/history` | 会话历史（分页游标） |

请求体：
```json
{
  "message": "查一下 A-202 的库存",
  "session_id": "xxx",
  "user_context": {"user_id": "u001", "role": "manager"}
}
```

## 流程训练与审批（/api/training）

### L1 会话学习（对话采集 → 训练样本）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/training/l1/sessions` | 查询会话学习记录 |
| POST | `/training/l1/sessions` | 创建会话学习记录 |
| POST | `/training/l1/sessions/<id>/approve` | 审批通过/驳回 |

### L2 规则配置（规则参数可训练）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/training/l2/rules` | 规则列表 |
| GET | `/training/l2/rules/<rule_id>/config` | 规则配置 |
| PUT | `/training/l2/rules/<rule_id>/config` | 提交配置变更 |
| GET | `/training/l2/proposals` | 变更提案列表 |
| POST | `/training/l2/proposals/<id>/approve` | 提案审批 |

### 审批链（可训练）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/training/approval-chain` | 审批链列表 |
| POST | `/training/approval-chain` | 提交审批链变更 |
| POST | `/training/approval-chain/<id>/approve` | 审批链变更审批 |

### L3 知识文档发布

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/training/l3/knowledge` | 知识发布列表 |
| POST | `/training/l3/knowledge` | 提交知识文档发布 |
| POST | `/training/l3/knowledge/<doc_id>/approve` | 发布审批 |
| DELETE | `/training/l3/knowledge/<doc_id>` | 删除 |

### 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/training/stats` | 训练统计 |

## 知识库（/api/knowledge）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/knowledge/documents` | 文档列表 |
| GET | `/knowledge/documents/<id>` | 文档详情 |
| POST | `/knowledge/documents` | 上传文档（向量化入库） |
| POST | `/knowledge/search` | 语义检索（RAG） |
| POST | `/knowledge/feedback` | 检索反馈 |
| GET | `/knowledge/gaps` | 知识缺口 |
| PATCH | `/knowledge/documents/<id>/status` | 文档状态 |
| GET | `/knowledge/recommend-config` | 推荐配置 |
| PUT | `/knowledge/recommend-config` | 更新推荐配置 |

## 数据（/api/data）

通用业务数据 CRUD（表名白名单内），用于基础业务查询。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/data/<table>` | 列表 / 创建 |
| GET/PUT/DELETE | `/data/<table>/<id>` | 详情 / 更新 / 删除 |

## LLM（/api/llm）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/llm/status` | LLM 健康状态 |
| GET | `/llm/providers` | 提供者配置 |

## 系统（/api/system）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/system/config` | 系统配置 |
| GET | `/system/health` | 健康检查 |

## MCP（/api/mcp）

JSON-RPC 2.0 接口，支持工具：
- `parse_file` — 解析 PDF/Word/Excel/图片（文本提取）
- `recognize_intent` — 意图识别
- `query_rules` — 规则查询（商业版可用，社区版返回不可用提示）

## 错误码

| code | 含义 |
|---|---|
| 0 | 成功 |
| 400 | 参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 不存在 |
| 500 | 服务器错误 |
