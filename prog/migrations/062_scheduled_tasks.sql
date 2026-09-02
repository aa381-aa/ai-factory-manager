-- 062_scheduled_tasks.sql
-- v6.83：通用能力第1档——轻量调度器 scheduled_tasks 表 + 3 个内置日报模板
--
-- 设计说明：
--   - scheduled_tasks 由 runtime/scheduler.py 读取：enabled 停用/启用任务、
--     schedule_expr 调整每日执行时间（HH:MM）、last_run_* 记录最近一次执行。
--   - 幂等：CREATE TABLE IF NOT EXISTS + INSERT ON CONFLICT DO NOTHING，
--     重复执行不报错、不覆盖已有配置。
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id           VARCHAR(64) PRIMARY KEY,
    task_name         VARCHAR(200) NOT NULL,
    schedule_expr     VARCHAR(20) NOT NULL DEFAULT '08:30',
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_date     DATE,
    last_run_status   VARCHAR(20),
    last_run_message  TEXT,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW()
);

-- 3 个内置日报模板（业务处理器见 prog/scripts/report_tasks.py）
INSERT INTO scheduled_tasks (task_id, task_name, schedule_expr, enabled) VALUES
    ('inventory_daily', '库存日报', '08:30', TRUE),
    ('order_daily',     '订单日报', '09:00', TRUE),
    ('quality_daily',   '质量日报', '09:30', TRUE)
ON CONFLICT (task_id) DO NOTHING;
