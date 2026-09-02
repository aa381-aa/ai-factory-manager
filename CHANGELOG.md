# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-10

社区版首个正式发布。

### Added

- **规则引擎**：规则注册 / 执行内核（`-_SafeExprEvaluator` 沙箱）、参数加载、配置管理。
- **基础意图识别**：通用兜底规则（订单 / 库存 / 财务 / 人事 / 知识 / 流程 / 寒暄），无需外部模型即可工作。
- **单 Agent 编排**：Agent 生命周期、线性流程、pending 延续、上下文隔离。
- **基础审批链**：单级 / 多级审批、通过 / 驳回、审批链可训练（`workflow_configs` 驱动）。
- **流程训练**：对话采集 → 训练样本 → 审批 → 入库，含知识文档发布审批。
- **基础知识库**：文档上传、向量化、RAG 检索、知识库外回答录入（未部署 Milvus 自动降级 DB 检索）。
- **基础 RBAC**：角色权限校验、JWT 登录（ABCD）。
- **会话 / 记忆**：会话管理、对话记忆（滑动窗口 / 摘要）、多轮对话。
- **数据采集**：可选匿名上报训练样本 / 对话记录至工厂公共库（默认关闭，`COMMUNITY_DB_ENABLED=false`）。
- **MCP 文件解析**：PDF / DOCX / XLSX / 图片 OCR 文本提取。

### Removed / 不在开源范围

以下能力为商业版，本仓库不包含（避免误导与法律风险）：七层审核与 fail-closed 审计、跨部门临时授权 + 部门双校验、多 Agent 并行 / 多跳路由、ABAC、SSO/OIDC 单点登录、多租户管理、WORM 审计归档、ISO 导入、行业模板库（BOM / 工艺 / 工单 / 质检 / 成本规则）、专属模型接入、制造术语意图种子（INT-01~30）、LLM 语义意图兜底、知识自动沉淀（KbSink）。

### Docs

- 新增 README（快速开始 / 功能边界 / 目录结构 / 贡献）。
- 新增 API 参考（全部公开端点）。
- 新增 CONTRIBUTING 与行为准则。

### Security

- JWT 会话签名与校验（HS256，默认密钥需在生产环境替换为 ≥32 字节强密钥）。
- 知识库上传文件类型 / 大小 / 内容审计。
- 数据回传默认关闭，回传数据经脱敏，合规审计数据永不回传。

[Unreleased]: https://github.com/OWNER/ai-factory-manager/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/ai-factory-manager/releases/tag/v1.0.0