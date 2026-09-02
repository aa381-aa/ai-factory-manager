-- ============================================================
-- 005_system_configs.sql
-- 系统配置、错误码、审核链表迁移
-- 对应技术规格v6.08：§A.0 系统配置和错误码定义
-- 说明：
--   创建系统配置表、错误码定义表、审核链记录表（含7层链式记录）、
--   并插入默认错误码（auth/rule/agent/llm/system五大类共11条）。
-- 依赖：先执行 001 ~ 003 表结构
-- ============================================================

-- 系统配置表
CREATE TABLE system_configs (
    id SERIAL PRIMARY KEY,
    config_key VARCHAR(200) NOT NULL UNIQUE,
    config_value TEXT,
    config_type VARCHAR(50),  -- string/number/boolean/json
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 错误码定义表
CREATE TABLE error_codes (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    message VARCHAR(500) NOT NULL,
    category VARCHAR(50),  -- auth/rule/agent/system/llm
    severity VARCHAR(20),  -- info/warning/error/critical
    solution TEXT
);

-- 审核日志表（§2.1 Step 6 审计归档 + R.2.9 硬编码规则）
-- 规格要求：audit_logs表，只追加不可变，保留≥7年
-- 哈希校验：规格书 §2.1 Step 5 描述为四要素 SHA-256(user_id+entity_id+amount+timestamp)；
-- 实际实现（v6.84 核对）为 audit_engine 链式哈希 SHA-256(prev_hash|chain_id|layer_name|payload)
-- （含 prev_hash 更防篡改，verify_chain_integrity 按链式重算校验）
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    chain_id VARCHAR(64) NOT NULL,  -- 审核链ID（一次操作一条链，7条记录共享同一chain_id）
    layer_name VARCHAR(50) NOT NULL,  -- identity_verification/permission_check/rule_validation/operation_confirm/ai_compliance_check/hash_verification/archive
    layer_order INTEGER NOT NULL,  -- 层序号 1~7
    user_id VARCHAR(100),  -- 操作用户标识
    action VARCHAR(200),  -- 操作动作描述
    input_data JSONB,  -- 该层输入数据
    output_data JSONB,  -- 该层输出数据
    result VARCHAR(20),  -- pass/fail/blocked
    hash_value VARCHAR(64),  -- 当前记录SHA-256哈希值
    prev_hash VARCHAR(64),  -- 上一条记录哈希值
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX idx_system_configs_key ON system_configs(config_key);
CREATE INDEX idx_error_codes_category ON error_codes(category);
CREATE INDEX idx_audit_logs_chain ON audit_logs(chain_id, layer_order);
CREATE INDEX idx_audit_logs_user ON audit_logs(user_id, timestamp);
CREATE INDEX idx_audit_logs_hash ON audit_logs(hash_value);

-- ============================================================
-- 插入默认错误码
-- ============================================================
INSERT INTO error_codes (code, message, category, severity, solution) VALUES
('E001', '用户未登录', 'auth', 'warning', '请先登录'),
('E002', '权限不足', 'auth', 'warning', '联系管理员获取权限'),
('E101', '售价低于成本线', 'rule', 'error', '售价不得低于成本价×1.15'),
('E102', '折扣超出权限', 'rule', 'warning', '需要上级审批'),
('E103', '信用额度不足', 'rule', 'error', '客户信用余额不足'),
('E201', 'Agent未找到', 'agent', 'error', '检查Agent配置'),
('E202', '意图识别失败', 'agent', 'warning', '请重新描述需求'),
('E301', 'LLM调用失败', 'llm', 'error', '检查LLM配置和网络'),
('E302', 'LLM响应超时', 'llm', 'warning', '增加超时时间或重试'),
('E401', '数据库连接失败', 'system', 'critical', '检查数据库配置'),
('E402', '向量库连接失败', 'system', 'critical', '检查Milvus配置');
