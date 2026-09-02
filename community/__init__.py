"""
社区版公共数据库采集模块（数据回传）
====================================
用途：
    开源社区版默认连接工厂托管的公共 PostgreSQL 数据库（tenant_id 隔离），
    采集训练样本 / 对话记录，供工厂持续优化通用意图识别与规则引擎。

设计：
    - db_connector    : 公共库连接（复用 prog.core.database volcano 模式配置）
    - data_uploader   : 增量上报训练数据/对话记录（幂等，checkpoint 游标）
    - tenant_bootstrap: 租户注册与初始化（获取 tenant_id / 上报密钥）

数据范围（仅匿名化业务数据，不含合规审计数据）：
    - training_data          训练样本（用户审批通过后）
    - conversation_messages  对话记录（脱敏后）
    - knowledge_documents    知识库外回答（source=对话录入）

隐私说明：
    - 上报前经 prog.llm.desensitizer 脱敏（手机号/身份证/密钥等）
    - 可通过环境变量 COMMUNITY_DB_ENABLED=false 完全关闭上报
"""
