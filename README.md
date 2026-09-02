# AI工厂管家（AI Factory Manager）· 规则驱动的业务智能体 · 社区版

规则驱动的业务智能体——基础流程训练、审批与知识库能力的开源社区版。

「AI工厂管家」以业务规则为纲，约束并驱动智能体行为，让智能体像工厂里各司其职的岗位一样照章办事。

> **许可**：AGPL-3.0。社区版仅包含基础通用能力；七层审核、跨部门授权、多跳路由、行业 know-how 规则库、SSO、多租户管理等**商业能力不在本仓库**（见「功能边界」）。

## 定位

本仓库是「AI工厂管家」智能体平台的**开源社区版**，面向：
- 了解规则驱动 Agent 架构的技术评估者；
- 小微企业基础流程数字化（流程训练 + 审批 + 知识库）；
- 希望参与开源贡献、学习框架实现的开发者。

商业版（标准版/旗舰版）提供完整合规与行业能力，见产品官网。

## 功能范围

### 开源（本仓库）
| 能力 | 说明 |
|---|---|
| 基础意图识别 | 通用兜底规则（订单/库存/财务/人事/知识/流程/寒暄），不含制造术语 know-how |
| 规则引擎 | 规则注册/执行内核、参数加载、配置管理 |
| 单 Agent 编排 | Agent 生命周期、线性流程、pending 延续、上下文隔离 |
| 基础审批链 | 单级/多级审批、通过/驳回、审批链可训练（workflow_configs 驱动） |
| 流程训练 | 对话采集 → 训练样本 → 审批 → 入库（含知识文档发布审批） |
| 基础知识库 | 文档上传、向量化、RAG 检索、知识库外回答录入 |
| 基础 RBAC | 角色权限校验、JWT 登录 |
| 会话/记忆 | 会话管理、对话记忆（滑动窗口/摘要）、多轮对话 |
| 数据采集 | 可选上报训练样本/对话记录至工厂公共库（见「公共数据库」） |

### 商业版（不在本仓库）
七层审核 + fail-closed 审计、跨部门临时授权 + 部门双校验、多 Agent 并行/多跳、ABAC、SSO/OIDC、多租户管理、WORM 审计归档、ISO 导入、行业模板库（BOM/工艺/工单/质检/成本规则）、专属模型接入、制造术语意图种子（INT-01~30）、LLM 语义意图兜底、知识自动沉淀（KbSink）。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境
cp .env.example .env
# 编辑 .env：DB_HOST/DB_USER/DB_PASSWORD、LLM_API_KEY

# 3. 初始化数据库（PostgreSQL）
python -m prog.scripts.init_db --apply-migrations

# 4. 启动服务
python prog/scripts/run_server.py --host 0.0.0.0 --port 5000
```

启动后访问：
- `POST /api/chat` — 对话路由（意图识别 → Agent 分发）
- `POST /api/chat/stream` — 流式对话
- `POST /api/training/*` — 流程训练 / 审批
- `POST /api/knowledge/*` — 知识库文档管理与检索

## 公共数据库（数据回传）

社区版可选将**训练样本与对话记录**增量上报至工厂托管的公共 PostgreSQL，用于优化通用意图识别与规则引擎。默认关闭。

```bash
# .env
COMMUNITY_DB_ENABLED=false        # 设为 true 启用
COMMUNITY_TENANT_ID=<工厂分配>     # 租户标识（数据隔离）
COMMUNITY_UPLOAD_INTERVAL=5       # 上报间隔（分钟）
RDS_HOST=<公共库地址>              # 复用 RDS_* 配置
RDS_USER=<公共库账号>
RDS_PASSWORD=<公共库密码>
RDS_DATABASE=ai_factory_community
```

说明：
- 上报数据经脱敏（手机号/身份证/密钥等），仅含业务字段；
- 公共库按 `tenant_id` 隔离，各实例数据互不可见；
- 关闭采集不影响本地任何功能；
- 合规与审计数据（WORM/审计日志）**永不**上报。

## 目录结构

```
opensource/
├── prog/
│   ├── runtime/          # 框架运行时（基础审批链/规则引擎/意图识别/协调器/会话/流式）
│   ├── agents/           # 示例 Agent（知识助手 / 仓储）
│   ├── api/              # REST API（chat/training/knowledge/auth/llm/system/data）
│   ├── core/             # 基础设施（DB/向量库/LLM 提供者/事件总线）
│   ├── llm/              # LLM 引擎/知识库/提示词构建
│   ├── models/           # ORM 模型（训练数据/库存）
│   ├── utils/            # 通用工具（响应/鉴权/加密/NL 解析）
│   ├── config/           # 配置加载与校验
│   ├── mcp/              # MCP 服务（文件解析/意图识别）
│   ├── scripts/          # 启动/初始化/部署检查
│   └── migrations/       # 基础表结构 SQL
├── community/            # 公共数据库采集（db_connector/data_uploader/tenant_bootstrap）
└── examples/             # 示例
```

## 开发

```bash
# 语法检查
python -m py_compile prog/runtime/coordinator.py

# 单元测试（框架层）
python -m pytest prog/tests -v
```

## 贡献

- 行为准则 + DCO（Developer Certificate of Origin）；
- 企业功能（商业版能力）不接受外部 PR；
- 提交前请确保 `py_compile` 通过。

## 免责声明

本软件按「AS IS」提供，无任何明示或暗示担保。生产环境部署请自行评估数据安全与合规要求。
