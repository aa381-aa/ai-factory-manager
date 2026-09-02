-- ============================================================
-- 038_session_memory_tables.sql
-- 会话持久化五表（规格 §1.1.3.5 Memory 持久化表结构 + §1.1.3.6 Memory 读写 API）
-- 对应：v6.38 登记 P0 差距——conversation_sessions/project_memory/user_profile
--       未落地，SessionManager（Redis 30min TTL）仅 debug 降级路径；
--       L441 业务侧保留 PG 长期归档表 conversation_sessions/conversation_messages/
--       conversation_corrections（Redis 过期后从 PG 归档恢复摘要，L1 学习源）。
-- 说明：
--   1. conversation_sessions / project_memory / user_profile DDL 与规格 §1.1.3.5 一致；
--   2. conversation_messages / conversation_corrections 规格仅列名（L441/L3493），
--      DDL 按合理结构设计（字段与 training_data 对齐便于 L1 学习复用）；
--   3. 均不加 FK 依赖（规格 DDL 未含，避免建表顺序耦合）。
-- ============================================================

-- 会话元数据（Redis 过期后归档恢复摘要）
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id     VARCHAR(64) PRIMARY KEY,
    user_id        VARCHAR(32) NOT NULL,
    channel        VARCHAR(16) NOT NULL,          -- erp / llm
    started_at     TIMESTAMP DEFAULT NOW(),
    last_active_at TIMESTAMP DEFAULT NOW(),
    status         VARCHAR(16) DEFAULT 'active',
    context_summary TEXT                           -- 压缩后的会话摘要
);
CREATE INDEX IF NOT EXISTS idx_conv_sessions_user ON conversation_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_sessions_active ON conversation_sessions(status, last_active_at);

-- 完整对话消息长期保存（L441 业务侧保留，Redis 过期后从 PG 归档恢复）
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id   BIGSERIAL PRIMARY KEY,
    session_id   VARCHAR(64) NOT NULL REFERENCES conversation_sessions(session_id),
    role         VARCHAR(16) NOT NULL,            -- user / assistant / system
    content      TEXT NOT NULL,
    intent       VARCHAR(30),                     -- 该轮意图（assistant 侧）
    agent        VARCHAR(30),                     -- 处理 Agent（assistant 侧）
    created_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_messages_session ON conversation_messages(session_id, created_at);

-- 用户纠正记录（L1 学习源，L3493：写入 PG training.conversation_corrections）
CREATE TABLE IF NOT EXISTS conversation_corrections (
    correction_id BIGSERIAL PRIMARY KEY,
    session_id    VARCHAR(64),
    user_input    TEXT NOT NULL,
    recognized    VARCHAR(30),                    -- 系统识别意图
    corrected     VARCHAR(30) NOT NULL,           -- 用户纠正意图
    agent_type    VARCHAR(30) DEFAULT 'intent_recognizer',
    approved      BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_corrections_approved ON conversation_corrections(approved, agent_type);

-- Project Memory（项目级记忆：rule / config / knowledge / training）
CREATE TABLE IF NOT EXISTS project_memory (
    memory_id   BIGSERIAL PRIMARY KEY,
    memory_type VARCHAR(32) NOT NULL,             -- rule / config / knowledge / training
    memory_key  VARCHAR(128) NOT NULL,
    memory_value JSONB NOT NULL,
    version     INTEGER DEFAULT 1,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE(memory_type, memory_key)
);

-- User Profile（用户画像：偏好/常用客户/常用产品/L3 个性化参数）
CREATE TABLE IF NOT EXISTS user_profile (
    user_id           VARCHAR(32) PRIMARY KEY,
    role              VARCHAR(32) NOT NULL,
    department        VARCHAR(64),
    preferences       JSONB DEFAULT '{}',
    frequent_customers TEXT[],                     -- 常用客户列表
    frequent_products TEXT[],                      -- 常用产品列表
    model_params      JSONB DEFAULT '{}',          -- L3个性化参数
    updated_at        TIMESTAMP DEFAULT NOW()
);
