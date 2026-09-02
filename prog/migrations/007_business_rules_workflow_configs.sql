-- ============================================================
-- 007_business_rules_workflow_configs.sql
-- 可训练规则配置与审批链表迁移
-- 对应技术规格v6.11：§2.6 R.1 规则配置表结构 + §2.5.5 workflow_configs（v5.13新增，v6.10审批链预置修正）
-- 说明：
--   business_rules表存储所有规则参数（parameter层，有默认值+可训练修改），
--   workflow_configs表存储审批链定义（approval_chain层，可训练优化）。
--   v5.13将R.2.1/R.2.4/R.2.5/R.2.6/R.2.8/R.2.10从hard_logic改为
--   parameter+approval_chain，规则参数需存入business_rules表。
--   rule_configs表（003_rules_schema.sql）保留兼容，后续迁移至business_rules。
--   v6.10修正：代码层不硬编码具体审批步骤；部署时通过本SQL预置基础审批链模板
--   （is_trained=false），后续由L0/L2训练优化。本文件承担init_rules.sql+init_workflows.sql职责。
--   v6.00新增：workflow_configs表含gate_checks JSONB字段（链式Gate校验配置，见§3.11.3.1）。
-- 依赖：先执行 001_init_schema.sql, 003_rules_schema.sql
-- ============================================================

-- 规则配置主表（参数初始值层，v5.13新增）
-- 存储所有可训练规则的参数默认值，管理员可通过SQL或管理界面修改
CREATE TABLE business_rules (
    rule_id         VARCHAR(32) PRIMARY KEY,           -- 规则编号，如 RULE-005
    rule_name       VARCHAR(128) NOT NULL,             -- 规则名称
    rule_type       VARCHAR(32) NOT NULL,              -- hard_logic / parameter / approval_chain
    department      VARCHAR(64),                       -- 归属部门
    config_json     JSONB NOT NULL,                    -- 规则参数（JSON格式存储）
    is_immutable    BOOLEAN DEFAULT FALSE,             -- TRUE=代码级硬编码不可改（仅R.2.9/R.2.11为TRUE）
    modified_by     VARCHAR(64),                       -- 最后修改人
    modified_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description     TEXT
);

-- 审批链配置表（可训练层，v5.13新增）
-- 存储规则变更的审批流程定义，可通过四层学习体系L2训练优化
CREATE TABLE workflow_configs (
    config_id       SERIAL PRIMARY KEY,
    workflow_type   VARCHAR(50) NOT NULL,              -- customer_change/product_change/drawing_change/production_schedule/inv_stage_change/cost_markup_change/version_sm_change/sched_constraint_change/bom_check_change/drawing_field_change
    workflow_name   VARCHAR(128) NOT NULL,             -- 如"信用额度变更审批"
    owner_dept      VARCHAR(30) NOT NULL,              -- 数据Owner部门
    trigger_rule    VARCHAR(32),                       -- 关联的规则编号（如RULE-005）
    approval_chain  JSONB NOT NULL,                    -- [{step:1, role:'sales_director', condition:'always'},...]
    notify_rules    JSONB,                             -- [{event:'approved', target:'production_dept', channel:'event_bus'},...]
    thresholds      JSONB,                             -- {credit_limit_threshold: 500000, approval_timeout_hours: 24}
    gate_checks     JSONB,                             -- 链式Gate校验配置（v6.00新增，见§3.11.3.1）：{required_approvals:{...}, required_fields:[...], required_statuses:[...], timeout_hours:24}
    is_active       BOOLEAN DEFAULT TRUE,
    is_trained      BOOLEAN DEFAULT FALSE,             -- 是否经过训练优化（L2规则迭代层写入时置TRUE）
    version         INTEGER DEFAULT 1,
    updated_by      VARCHAR(32) REFERENCES users(user_id),      -- 最后更新人
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 默认规则参数初始化（v5.13 parameter层默认值）
-- ============================================================

-- R.2.1 成本线拦截：加价率默认1.15（15%最低毛利）
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('RULE-005', '成本线拦截规则', 'parameter', 'finance',
'{"min_markup_rate": 1.15, "logic": "if order_price < product_cost * min_markup_rate: block"}'::jsonb,
FALSE, '加价率可训练修改（需审批），拦截逻辑本身为硬编码不可变');

-- R.2.4 版本管理：状态机默认定义
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('VERSION-SM', '版本管理状态机', 'parameter', 'technical',
'{"transitions": {"draft": ["reviewing"], "reviewing": ["approved", "draft"], "approved": ["effective"], "effective": ["superseded"], "superseded": ["obsolete"], "obsolete": []}}'::jsonb,
FALSE, '状态机定义可训练修改（需审批），仅生效版本可用于生产为硬编码不可变');

-- R.2.5 排产硬约束：工序顺序/设备兼容映射默认值
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('SCHED-HARD', '排产硬约束规则', 'parameter', 'production',
'{"process_order": ["CNC加工→去毛刺→阳极氧化→质检"], "equipment_compatibility": {"CNC-A": ["铝件", "钢件"], "CNC-B": ["铝件"]}, "delivery_priority_rule": "earliest_due_first"}'::jsonb,
FALSE, '约束数据可训练修改（需审批），约束校验必须执行为硬编码不可变');

-- R.2.6 库存五阶段：阶段定义默认值
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('INV-STAGE', '库存阶段流转规则', 'parameter', 'production',
'{"stages": ["raw", "wip_cnc", "wip_anode", "wip_qc", "finished"], "transition_rule": "sequential_only", "rollback_rule": "wip stages can rollback to previous stage with approval"}'::jsonb,
FALSE, '阶段定义可训练修改（需审批），顺序流转规则为硬编码不可变');

-- R.2.8 BOM一致性：校验项列表/容差默认值
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('BOM-CHECK', 'BOM一致性校验规则', 'parameter', 'technical',
'{"checks": ["BOM零件图号必须与图纸明细栏一致", "BOM物料编码必须与products表一致", "BOM数量必须与图纸明细栏数量一致", "工艺路线工序必须覆盖BOM所有零件"], "tolerance": {"quantity_deviation": 0.05}}'::jsonb,
FALSE, '校验项列表/容差可训练修改（需审批），校验必须执行为硬编码不可变');

-- R.2.10 图纸字段：必填字段列表默认值
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description)
VALUES ('DRAWING-FIELD', '图纸解析字段完整性规则', 'parameter', 'technical',
'{"required_fields": ["图号", "名称", "版本", "日期", "设计", "审核", "批准", "材料", "数量", "重量", "比例", "单位", "零件名称", "零件图号", "表面处理", "热处理", "备注"]}'::jsonb,
FALSE, '必填字段列表可训练修改（需审批），字段完整性校验必须执行为硬编码不可变');

-- ============================================================
-- 默认审批链初始化（v5.13 approval_chain层初始模板）
-- ============================================================

INSERT INTO workflow_configs (workflow_type, workflow_name, owner_dept, trigger_rule, approval_chain, is_trained) VALUES
('cost_markup_change', '成本线加价率变更审批', 'finance', 'RULE-005',
 '[{"step":1,"role":"sales_manager","action":"发起"},{"step":2,"role":"finance_manager","action":"财务确认"}]'::jsonb, FALSE),
('version_sm_change', '版本状态机变更审批', 'technical', 'VERSION-SM',
 '[{"step":1,"role":"technical_manager","action":"发起"},{"step":2,"role":"quality_manager","action":"质量确认"},{"step":3,"role":"production_manager","action":"生产确认"}]'::jsonb, FALSE),
('sched_constraint_change', '排产约束变更审批', 'production', 'SCHED-HARD',
 '[{"step":1,"role":"production_manager","action":"发起"},{"step":2,"role":"technical_manager","action":"工艺确认"}]'::jsonb, FALSE),
('inv_stage_change', '库存阶段定义变更审批', 'production', 'INV-STAGE',
 '[{"step":1,"role":"warehouse_manager","action":"发起"},{"step":2,"role":"production_manager","action":"工艺确认"},{"step":3,"role":"finance_manager","action":"财务确认"}]'::jsonb, FALSE),
('bom_check_change', 'BOM校验项变更审批', 'technical', 'BOM-CHECK',
 '[{"step":1,"role":"technical_manager","action":"发起"},{"step":2,"role":"production_manager","action":"生产确认"}]'::jsonb, FALSE),
('drawing_field_change', '图纸必填字段变更审批', 'technical', 'DRAWING-FIELD',
 '[{"step":1,"role":"technical_manager","action":"发起"},{"step":2,"role":"quality_manager","action":"质量确认"}]'::jsonb, FALSE);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX idx_business_rules_type ON business_rules(rule_type);
CREATE INDEX idx_business_rules_immutable ON business_rules(is_immutable);
CREATE INDEX idx_workflow_configs_type ON workflow_configs(workflow_type);
CREATE INDEX idx_workflow_configs_active ON workflow_configs(is_active);
CREATE INDEX idx_workflow_configs_trigger ON workflow_configs(trigger_rule);

-- ============================================================
-- 触发器：自动更新 updated_at / modified_at
-- ============================================================
-- business_rules 表使用 modified_at 字段（与其余表 updated_at 不同），需专用触发器函数
CREATE OR REPLACE FUNCTION update_modified_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.modified_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_business_rules_modified BEFORE UPDATE ON business_rules
    FOR EACH ROW EXECUTE FUNCTION update_modified_at();

CREATE TRIGGER trg_workflow_configs_updated BEFORE UPDATE ON workflow_configs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- 补充规则参数种子数据（discount/qc/credit-warning）
-- ============================================================

-- 折扣权限规则参数
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description) VALUES
('DISCOUNT-RULE', '折扣权限规则', 'parameter', '销售部',
 '{"threshold_direct": 0.95, "threshold_manager": 0.90, "role_discount_max": {"operator": 0.0, "sales": 0.05, "manager": 0.15, "admin": 1.0}}',
 false, '折扣阈值与角色折扣权限上限，可训练调整') ON CONFLICT DO NOTHING;

-- 质检标准规则参数
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description) VALUES
('QC-STANDARD', '质检标准规则', 'parameter', '质检部',
 '{"aql_critical": 1.0, "aql_general": 2.5, "sample_lower_limit": 10, "critical_keywords": ["模组","关键","key","B-305"], "reject_threshold": 0.1, "aql_sample_table": {"51-90": {"sample": 13, "accept": 0, "reject": 1}, "91-150": {"sample": 20, "accept": 1, "reject": 2}, "151-280": {"sample": 32, "accept": 2, "reject": 3}, "281-500": {"sample": 50, "accept": 3, "reject": 4}, "501-1200": {"sample": 80, "accept": 5, "reject": 6}, "1201-3200": {"sample": 125, "accept": 7, "reject": 8}, "3201-10000": {"sample": 200, "accept": 10, "reject": 11}, "10001-35000": {"sample": 315, "accept": 14, "reject": 15}}}',
 false, 'AQL抽样标准、关键产品关键词、报废阈值，可训练调整') ON CONFLICT DO NOTHING;

-- 信用预警规则参数
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description) VALUES
('CREDIT-WARNING', '信用预警规则', 'parameter', '财务部',
 '{"warning_threshold": 0.6, "critical_threshold": 0.8}',
 false, '信用额度使用率预警阈值，可训练调整') ON CONFLICT DO NOTHING;

-- 财务阈值规则参数（催收天数+利润率）
INSERT INTO business_rules (rule_id, rule_name, rule_type, department, config_json, is_immutable, description) VALUES
('FINANCE-THRESHOLD', '财务阈值规则', 'parameter', '财务部',
 '{"collection_urgent_days": 60, "collection_normal_days": 30, "profit_margin_good": 30, "profit_margin_fair": 15}',
 false, '催收紧急度天数阈值与利润率评价阈值，可训练调整') ON CONFLICT DO NOTHING;

-- SCHED-HARD 补充 shift_hours 参数（原种子数据未包含，对应 schedule_rule.py DEFAULT_SHIFT_HOURS）
UPDATE business_rules
SET config_json = config_json || '{"shift_hours": 8}'::jsonb
WHERE rule_id = 'SCHED-HARD' AND NOT (config_json ? 'shift_hours');

-- ============================================================
-- 基础审批链模板（v6.10审批链预置修正版新增）
-- 对应技术规格 §2.5.5 Phase 1 初始化审批流程（is_trained=false，后续由L0/L2训练优化）
-- 覆盖 §2.5.2客户信息维护 / §2.5.3产品与图号维护 / §2.5.4生产排班流程
-- 每条约含 approval_chain（审批链）+ notify_rules（通知规则）+ thresholds（可训练阈值）
-- + gate_checks（链式Gate校验配置，见§3.11.3.1）
-- ============================================================

INSERT INTO workflow_configs (workflow_type, workflow_name, owner_dept, approval_chain, notify_rules, thresholds, gate_checks, is_trained) VALUES
('customer_change', '客户信用额度上调审批', 'sales',
 '[{"step":1,"role":"sales_director","condition":"always"},{"step":2,"role":"finance","condition":"amount>500000"}]'::jsonb,
 '[{"event":"approved","target":"finance","channel":"system"},{"event":"approved","target":"requester","channel":"websocket"}]'::jsonb,
 '{"credit_limit_threshold":500000,"approval_timeout_hours":24}'::jsonb,
 '{"required_approvals":{"1":{"role":"sales_director","required":true},"2":{"role":"finance","required":"amount>500000"}},"required_fields":{"1":["credit_limit","reason"]},"required_statuses":[],"timeout_hours":24}'::jsonb, FALSE),
('product_change', '新产品建档审批', 'tech',
 '[{"step":1,"role":"tech_supervisor","condition":"always"},{"step":2,"role":"production","condition":"feasibility_check"}]'::jsonb,
 '[{"event":"effective","target":"production","channel":"event_bus"},{"event":"effective","target":"warehouse","channel":"event_bus"},{"event":"effective","target":"sales","channel":"event_bus"}]'::jsonb,
 '{"approval_timeout_hours":48}'::jsonb,
 '{"required_approvals":{"1":{"role":"tech_supervisor","required":true},"2":{"role":"production","required":true}},"required_fields":{"1":["product_code","product_name","bom_id"]},"required_statuses":[{"entity_type":"drawing","expected_status":"effective"}],"timeout_hours":48}'::jsonb, FALSE),
('drawing_change', '图纸版本变更审批', 'tech',
 '[{"step":1,"role":"tech_supervisor","condition":"always"},{"step":2,"role":"production","condition":"wip_impact_check"},{"step":3,"role":"warehouse","condition":"bom_diff_check"}]'::jsonb,
 '[{"event":"effective","target":"production","channel":"event_bus"},{"event":"effective","target":"sales","channel":"event_bus"},{"event":"effective","target":"warehouse","channel":"event_bus"}]'::jsonb,
 '{"approval_timeout_hours":48}'::jsonb,
 '{"required_approvals":{"1":{"role":"tech_supervisor","required":true},"2":{"role":"production","required":true},"3":{"role":"warehouse","required":true}},"required_fields":{"1":["change_description","new_version_pdf"]},"required_statuses":[{"entity_type":"drawing","expected_status":"effective"}],"timeout_hours":48}'::jsonb, FALSE),
('production_schedule', '排产方案审批', 'production',
 '[{"step":1,"role":"tech","condition":"feasibility_check"},{"step":2,"role":"warehouse","condition":"material_check"},{"step":3,"role":"qc","condition":"qc_standard_check"},{"step":4,"role":"production_manager","condition":"always"}]'::jsonb,
 '[{"event":"scheduled","target":"sales","channel":"websocket"},{"event":"scheduled","target":"warehouse","channel":"event_bus"}]'::jsonb,
 '{"approval_timeout_hours":12}'::jsonb,
 '{"required_approvals":{"1":{"role":"tech","required":true},"2":{"role":"warehouse","required":true},"3":{"role":"qc","required":true}},"required_fields":{"1":["process_route_version"],"2":["material_check_result"]},"required_statuses":[{"entity_type":"drawing","expected_status":"effective"},{"entity_type":"process_route","expected_status":"effective"}],"timeout_hours":12}'::jsonb, FALSE);
