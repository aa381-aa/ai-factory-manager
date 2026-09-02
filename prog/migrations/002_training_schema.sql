-- ============================================================
-- 002_training_schema.sql
-- 训练数据表结构迁移
-- 对应技术规格：§1.4.2 训练数据存储（PostgreSQL JSONB，不用MongoDB）
-- 说明：
--   创建AI工厂管家的训练数据存储结构，使用PostgreSQL JSONB字段
--   存储多Agent训练样本与微调数据集元数据。
--   替代demo中无训练数据持久化的缺陷。
-- 依赖：先执行 001_init_schema.sql
-- ============================================================

-- 训练数据表（PostgreSQL JSONB存储，不用MongoDB）
CREATE TABLE training_data (
    id SERIAL PRIMARY KEY,
    agent_type VARCHAR(50) NOT NULL,  -- sales/production/warehouse/technical/finance
    intent VARCHAR(100),
    user_input TEXT NOT NULL,
    ai_output TEXT,
    user_correction TEXT,
    final_output TEXT,
    metadata JSONB,
    approved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 模型微调数据集表
CREATE TABLE fine_tune_datasets (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    agent_type VARCHAR(50),
    version VARCHAR(50),
    data_count INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'draft',  -- draft/ready/training/completed
    file_path VARCHAR(500),  -- MinIO/TOS路径
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX idx_training_data_agent ON training_data(agent_type);
CREATE INDEX idx_training_data_intent ON training_data(intent);
CREATE INDEX idx_training_data_approved ON training_data(approved);
CREATE INDEX idx_fine_tune_datasets_agent ON fine_tune_datasets(agent_type);
CREATE INDEX idx_fine_tune_datasets_status ON fine_tune_datasets(status);

-- ============================================================
-- 视图：按Agent类型统计训练样本数
-- ============================================================
CREATE OR REPLACE VIEW v_training_stats AS
SELECT
    agent_type,
    COUNT(*) AS total_samples,
    COUNT(*) FILTER (WHERE approved) AS approved_samples,
    COUNT(*) FILTER (WHERE user_correction IS NOT NULL) AS corrected_samples,
    COUNT(DISTINCT intent) AS intent_count
FROM training_data
GROUP BY agent_type;
