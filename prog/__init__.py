"""
prog — AI工厂管家社区版代码包
=====================================
对应技术规格：ai-factory-tech-spec.md v6.21

本包为正式版代码根包，包含以下子模块：
    - config:  配置加载层（三层变量加载 §A.0）
    - core:    六大统一接口层（§1.8）
    - models:  数据模型与DAL（§1.7）
    - rules:   规则引擎（§2.1-§2.8）
    - agents:  Agent模块（§3.1-§3.8）
    - llm:     LLM引擎（5道安全门控）
    - api:     API路由层（Flask Blueprint）
    - mcp:     MCP工具中心（§1.3）
    - utils:   共享工具
    - scripts: 工具脚本
    - tests:   测试套件

开发原则：demo代码（server.py/llm_engine.py/data_manager.py）保持不动，
所有新代码均位于本prog目录内。

框架复用：Agent 运行时通用能力（BaseAgent/Coordinator/规则引擎/审核链/权限/
安全/模块开关/流程引擎/意图识别/会话/日志/缓存/事件总线/文件存储/SSE/trace）
由AI工厂管家框架运行时（本仓库 prog/runtime）提供，本包不再重复实现；
业务侧保留具体业务 Agent、业务规则、业务配置与部署逻辑。
"""
