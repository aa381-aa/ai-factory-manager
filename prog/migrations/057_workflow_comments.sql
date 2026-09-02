-- ============================================================
-- 057_workflow_comments.sql
-- 审批留言表（多人协作场景①：申请人↔审批人同上下文往返）
-- 对应方案：多人协作能力落地方案（场景① 审批留言）
-- 关联：
--   instance_id → workflow_instances.instance_id（FK 关联审批实例）
--   author_id   → users.user_id（FK 关联留言人）
-- 说明：
--   - 留言为过程性/时效数据，不进入知识库（RAG），仅作审批往返记录；
--   - workflow_query（查询流程单据）经 workflow_enforcer 表白名单读取。
-- 依赖：先执行 008_v616_schema_upgrade.sql（workflow_instances 表）
-- ============================================================

CREATE TABLE IF NOT EXISTS workflow_comments (
    comment_id  SERIAL PRIMARY KEY,
    instance_id INTEGER NOT NULL REFERENCES workflow_instances(instance_id),
    step        INTEGER DEFAULT 1,                      -- 留言所属审批步骤
    author_id   VARCHAR(32) REFERENCES users(user_id),  -- 留言人工号
    content     TEXT NOT NULL,                          -- 留言内容
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wf_comments_instance
    ON workflow_comments(instance_id, step);
