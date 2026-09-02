# 贡献指南

感谢你关注「AI工厂管家」社区版！欢迎提 Issue、PR、改进文档与示例。

## 贡献范围

- **欢迎**：bug 修复、文档完善、示例补充、通用能力改进、测试用例。
- **不接受**：商业版能力（见下方「功能边界」）相关的功能 PR——那些能力不在本仓库维护，外部 PR 无法合并。

## 开发环境

```bash
# 1. 克隆与安装
git clone https://github.com/OWNER/ai-factory-manager.git
cd ai-factory-manager
pip install -r requirements.txt

# 2. 配置
cp .env.example .env

# 3. 语法与测试
python -m py_compile prog/runtime/coordinator.py prog/scripts/run_server.py
python -m pytest prog/tests -v   # 全绿再提 PR
```

## 分支与提交

- 默认分支：`main`。
- 命名建议：`fix/xxx`、`feat/xxx`、`docs/xxx`。
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)：`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`。
- 提交前确保 `py_compile` 通过，新增功能附对应测试。

## DCO 签署

本仓库要求贡献者以 **DCO（Developer Certificate of Origin）** 签署提交，即每条提交消息需包含：

```
Signed-off-by: 你的名字 <你的邮箱>
```

若你的 git 未自动追加，可在提交时使用：

```bash
git commit -s -m "your message"
```

签署 DCO 表示你确认该提交的代码权属与您在 DCO 条款下的授权。

## 提交 PR

1. Fork 并创建分支。
2. 改动并补充测试 / 文档。
3. 本地跑通 `py_compile` 与 `pytest`。
4. 发起 PR，描述改动动机与验证结果。
5. 维护者 review 后合并。

## 功能边界（重要）

本仓库为「社区版」，仅包含通用基础能力。请勿提交与以下商业能力相关的代码或数据：
七层审核与审计、跨部门授权、多 Agent 并行 / 多跳、ABAC、SSO/OIDC、多租户、WORM 归档、ISO 导入、行业模板库（BOM / 工艺 / 工单 / 质检 / 成本规则）、专属模型接入、制造术语意图种子、LLM 语义意图兜底、知识自动沉淀（KbSink）。

若您有上述需求，请联系商业版产品渠道。

## 行为准则

维护一个友善、尊重、建设性的社区。请参阅 `CODE_OF_CONDUCT.md`（管理员会在仓库根目录提供）。对骚扰、冒犯与人身攻击零容忍。

## 疑问

- 设计问题：先开 Issue 描述场景，附复现步骤。
- 紧急安全问题：请直接联系仓库维护者（请在 Issue 中标注 `security`，勿公开敏感细节）。