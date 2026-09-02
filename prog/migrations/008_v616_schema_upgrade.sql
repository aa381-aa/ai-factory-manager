-- ============================================================
-- 008_v616_schema_upgrade.sql
-- v6.16 规格补全迁移：补全规格说明书v6.16要求但代码中缺失的所有表
-- 对应技术规格v6.16：
--   §2.6   加密保护字段（business_rules 扩展）
--   §2.8.1 SOD冲突规则
--   §2.8.11 WORM模式 + training保护表
--   §2.5.5 v6.15 流程实例表 + workflow_configs 扩展字段
--   §3.9   HR四表（报工/计件单价/工资台账/考勤）
--   §3.10.7 AI测试工具表
--   §2.1   审计日志表（audit_logs 已在005创建，此处补充缺失索引）
--   §A.5   其他业务表（供应商/采购/应付/客诉/部门/知识库/Saga/Event Schema等）
-- 依赖：先执行 001 ~ 007
-- 注意：
--   - audit_logs 表已在 005_system_configs.sql 中创建，本文件不重复创建，仅补充 layer_name 索引
--   - error_codes 表已在 005_system_configs.sql 中创建，本文件不重复创建
--   - update_updated_at() 函数已在 001 中定义，本文件直接复用
-- ============================================================


-- ============================================================
-- §2.6 加密保护字段：ALTER TABLE business_rules ADD COLUMN
-- 为 business_rules 表添加加密与版本控制字段
-- ============================================================
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS rule_hash VARCHAR(64);
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS version INTEGER DEFAULT 1;
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0;
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS operation_type VARCHAR(32);
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS encryption_key_id VARCHAR(64);
ALTER TABLE business_rules ADD COLUMN IF NOT EXISTS encrypted_fields JSONB DEFAULT '[]';


-- ============================================================
-- §2.8.11 WORM模式：business_rules_audit 表
-- WORM审计表，记录规则变更历史，通过触发器禁止UPDATE/DELETE
-- ============================================================
CREATE TABLE business_rules_audit (
    audit_id        SERIAL PRIMARY KEY,
    rule_id         VARCHAR(32) NOT NULL,
    old_config      JSONB,
    new_config      JSONB,
    old_hash        VARCHAR(64),
    new_hash        VARCHAR(64),
    changed_by      VARCHAR(32),
    approval_id     VARCHAR(50),
    changed_at      TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}'
);

-- WORM触发器：阻止UPDATE和DELETE操作
CREATE OR REPLACE FUNCTION prevent_worm_modify()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'WORM mode: UPDATE and DELETE are not allowed on %', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_business_rules_audit_no_update
    BEFORE UPDATE ON business_rules_audit
    FOR EACH ROW EXECUTE FUNCTION prevent_worm_modify();

CREATE TRIGGER trg_business_rules_audit_no_delete
    BEFORE DELETE ON business_rules_audit
    FOR EACH ROW EXECUTE FUNCTION prevent_worm_modify();


-- ============================================================
-- §2.5.5 v6.15新增字段：ALTER TABLE workflow_configs ADD COLUMN
-- ============================================================
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS agent_scope VARCHAR(50);
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS routing_rule VARCHAR(100);
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS starter_roles JSONB;
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS starter_depts JSONB;
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS involved_depts JSONB;
ALTER TABLE workflow_configs ADD COLUMN IF NOT EXISTS initiation VARCHAR(20) DEFAULT 'manual';


-- ============================================================
-- §2.5.5 v6.15 流程实例表：workflow_instances
-- ============================================================
CREATE TABLE workflow_instances (
    instance_id     SERIAL PRIMARY KEY,
    config_id       INTEGER REFERENCES workflow_configs(config_id),
    workflow_type   VARCHAR(50) NOT NULL,
    biz_type        VARCHAR(50) NOT NULL,
    biz_id          VARCHAR(50) NOT NULL,
    agent_scope     VARCHAR(50),
    current_step    INTEGER DEFAULT 1,
    steps_done      JSONB DEFAULT '[]',
    status          VARCHAR(20) DEFAULT 'running',
    routing_hit     VARCHAR(100),
    created_by      VARCHAR(32) REFERENCES users(user_id),
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}',
    UNIQUE(biz_type, biz_id)
);


-- ============================================================
-- §3.9 HR四表
-- ============================================================

-- 报工记录表
CREATE TABLE work_reports (
    report_id       SERIAL PRIMARY KEY,
    user_id         VARCHAR(32) REFERENCES users(user_id),
    work_order_id   VARCHAR(20) REFERENCES work_orders(work_order_id),
    product_code    VARCHAR(20) REFERENCES products(product_code),
    process_step    VARCHAR(100),
    completed_qty   INTEGER,
    defect_qty      INTEGER DEFAULT 0,
    report_date     DATE,
    status          VARCHAR(20) DEFAULT 'submitted',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 计件单价表
CREATE TABLE piece_rate_prices (
    price_id        SERIAL PRIMARY KEY,
    product_code    VARCHAR(20) REFERENCES products(product_code),
    process_step    VARCHAR(100),
    unit_price      DECIMAL(10,2) NOT NULL,
    effective_date  DATE,
    status          VARCHAR(20) DEFAULT 'active',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 工资台账表
CREATE TABLE payroll_records (
    payroll_id      SERIAL PRIMARY KEY,
    user_id         VARCHAR(32) REFERENCES users(user_id),
    period_start    DATE,
    period_end      DATE,
    base_salary     DECIMAL(12,2) DEFAULT 0,
    piece_rate_pay  DECIMAL(12,2) DEFAULT 0,
    overtime_pay    DECIMAL(12,2) DEFAULT 0,
    deductions      DECIMAL(12,2) DEFAULT 0,
    net_pay         DECIMAL(12,2) DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'draft',
    approved_by     VARCHAR(32) REFERENCES users(user_id),
    approved_at     TIMESTAMP,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 考勤记录表
CREATE TABLE attendance_records (
    attendance_id   SERIAL PRIMARY KEY,
    user_id         VARCHAR(32) REFERENCES users(user_id),
    record_date     DATE,
    check_in_time   TIMESTAMP,
    check_out_time  TIMESTAMP,
    status          VARCHAR(20) DEFAULT 'normal',
    overtime_hours  DECIMAL(4,1) DEFAULT 0,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);


-- ============================================================
-- §2.8.11 training保护表
-- ============================================================

-- 训练结果表
CREATE TABLE training_results (
    result_id       SERIAL PRIMARY KEY,
    training_type   VARCHAR(50),
    target_entity   VARCHAR(100),
    config_snapshot JSONB,
    status          VARCHAR(20) DEFAULT 'pending',
    created_by      VARCHAR(32) REFERENCES users(user_id),
    created_at      TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}'
);

-- 训练结果版本表
CREATE TABLE training_result_versions (
    version_id      SERIAL PRIMARY KEY,
    result_id       INTEGER REFERENCES training_results(result_id),
    version_number  INTEGER,
    config_snapshot JSONB,
    rolled_back_by  VARCHAR(32) REFERENCES users(user_id),
    rolled_back_at  TIMESTAMP,
    reason          TEXT,
    extra_data      JSONB DEFAULT '{}'
);

-- 训练结果审计表
CREATE TABLE training_results_audit (
    audit_id        SERIAL PRIMARY KEY,
    result_id       INTEGER,
    action          VARCHAR(20),
    old_value       JSONB,
    new_value       JSONB,
    operator        VARCHAR(32) REFERENCES users(user_id),
    operated_at     TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}'
);


-- ============================================================
-- §3.10.7 AI测试工具表
-- ============================================================

-- 配置测试用例表
CREATE TABLE config_test_cases (
    case_id         SERIAL PRIMARY KEY,
    case_type       VARCHAR(20),
    target_type     VARCHAR(50),
    target_id       VARCHAR(100),
    test_data       JSONB,
    expected_result JSONB,
    description     TEXT,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 测试执行报告表
CREATE TABLE test_execution_reports (
    report_id       SERIAL PRIMARY KEY,
    case_id         INTEGER REFERENCES config_test_cases(case_id),
    execution_status VARCHAR(20),
    actual_result   JSONB,
    diff_summary    TEXT,
    executed_at     TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}'
);


-- ============================================================
-- §2.8.1 SOD冲突规则表 + 预置数据
-- ============================================================
CREATE TABLE sod_conflict_rules (
    sod_id          VARCHAR(10) PRIMARY KEY,
    permission_a    VARCHAR(100) NOT NULL,
    permission_b    VARCHAR(100) NOT NULL,
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    extra_data      JSONB DEFAULT '{}'
);

INSERT INTO sod_conflict_rules (sod_id, permission_a, permission_b, description) VALUES
('SOD-001', 'can_modify_order',   'can_approve_order',   '销售接单+订单审批不相容'),
('SOD-002', 'can_create_purchase','can_approve_payment',  '采购下单+付款审批不相容'),
('SOD-003', 'can_create_drawing', 'can_approve_drawing',  '图纸创建+审批不相容'),
('SOD-004', 'can_modify_inventory','can_view_audit',      '库存操作+审计不相容'),
('SOD-005', 'can_upload_training','can_approve_training', '训练数据上传+审批不相容'),
('SOD-006', 'can_submit_report',  'can_confirm_payroll',  '报工+工资确认不相容')
ON CONFLICT (sod_id) DO NOTHING;


-- ============================================================
-- §2.1 审计日志表 audit_logs —— 已在 005_system_configs.sql 中创建
-- 此处仅补充缺失的 layer_name 单列索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_audit_logs_layer_name ON audit_logs(layer_name);


-- ============================================================
-- §A.5 其他业务表
-- ============================================================

-- 供应商表
CREATE TABLE suppliers (
    supplier_id     VARCHAR(20) PRIMARY KEY,
    supplier_name   VARCHAR(100) NOT NULL,
    contact_person  VARCHAR(50),
    contact_phone   VARCHAR(20),
    address         TEXT,
    rating          INTEGER DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'active',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 采购订单表
CREATE TABLE purchase_orders (
    po_id           VARCHAR(20) PRIMARY KEY,
    supplier_id     VARCHAR(20) REFERENCES suppliers(supplier_id),
    product_code    VARCHAR(20) REFERENCES products(product_code),
    quantity        INTEGER,
    unit_price      DECIMAL(10,2),
    total_amount    DECIMAL(12,2),
    status          VARCHAR(20) DEFAULT 'draft',
    expected_date   DATE,
    received_qty    INTEGER DEFAULT 0,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 应付账款表
CREATE TABLE accounts_payable (
    ap_id           SERIAL PRIMARY KEY,
    supplier_id     VARCHAR(20) REFERENCES suppliers(supplier_id),
    po_id           VARCHAR(20) REFERENCES purchase_orders(po_id),
    invoice_no      VARCHAR(50),
    invoice_amount  DECIMAL(12,2),
    paid_amount     DECIMAL(12,2) DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'unpaid',
    due_date        DATE,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 供应商发票匹配表
CREATE TABLE supplier_invoice_matches (
    match_id        SERIAL PRIMARY KEY,
    po_id           VARCHAR(20) REFERENCES purchase_orders(po_id),
    invoice_no      VARCHAR(50),
    received_qty    INTEGER,
    invoice_qty     INTEGER,
    match_status    VARCHAR(20) DEFAULT 'pending',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 成本核算表
CREATE TABLE cost_accounting (
    cost_id         SERIAL PRIMARY KEY,
    product_code    VARCHAR(20) REFERENCES products(product_code),
    order_id        VARCHAR(20) REFERENCES orders(order_id),
    material_cost   DECIMAL(12,2),
    labor_cost      DECIMAL(12,2),
    overhead_cost   DECIMAL(12,2),
    total_cost      DECIMAL(12,2),
    period_start    DATE,
    period_end      DATE,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 退货单表
CREATE TABLE return_orders (
    return_id       VARCHAR(20) PRIMARY KEY,
    original_order_id VARCHAR(20) REFERENCES orders(order_id),
    customer_id     VARCHAR(20) REFERENCES customers(customer_id),
    reason          TEXT,
    status          VARCHAR(20) DEFAULT 'pending',
    refund_amount   DECIMAL(12,2),
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 客户投诉表
CREATE TABLE customer_complaints (
    complaint_id    SERIAL PRIMARY KEY,
    customer_id     VARCHAR(20) REFERENCES customers(customer_id),
    order_id        VARCHAR(20) REFERENCES orders(order_id),
    complaint_type  VARCHAR(50),
    description     TEXT,
    status          VARCHAR(20) DEFAULT 'open',
    handled_by      VARCHAR(32),
    resolution      TEXT,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 部门表
CREATE TABLE departments (
    dept_id         SERIAL PRIMARY KEY,
    dept_name       VARCHAR(50) NOT NULL UNIQUE,
    parent_dept_id  INTEGER REFERENCES departments(dept_id),
    manager_id      VARCHAR(32) REFERENCES users(user_id),
    sort_order      INTEGER DEFAULT 0,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 部门变更记录表
CREATE TABLE department_changes (
    change_id       SERIAL PRIMARY KEY,
    dept_id         INTEGER REFERENCES departments(dept_id),
    change_type     VARCHAR(20),
    old_value       JSONB,
    new_value       JSONB,
    changed_by      VARCHAR(32) REFERENCES users(user_id),
    changed_at      TIMESTAMP DEFAULT NOW(),
    extra_data      JSONB DEFAULT '{}'
);

-- 用户调岗记录表
CREATE TABLE user_transfers (
    transfer_id     SERIAL PRIMARY KEY,
    user_id         VARCHAR(32) REFERENCES users(user_id),
    from_dept       VARCHAR(50),
    to_dept         VARCHAR(50),
    effective_date  DATE,
    approved_by     VARCHAR(32) REFERENCES users(user_id),
    status          VARCHAR(20) DEFAULT 'pending',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 脱敏字段映射表
CREATE TABLE desensitization_map (
    map_id          SERIAL PRIMARY KEY,
    original_field  VARCHAR(100),
    masked_field    VARCHAR(100),
    mask_type       VARCHAR(20),
    is_active       BOOLEAN DEFAULT TRUE,
    extra_data      JSONB DEFAULT '{}'
);

-- 订单阶段表
CREATE TABLE order_stages (
    stage_id        SERIAL PRIMARY KEY,
    order_id        VARCHAR(20) REFERENCES orders(order_id),
    stage_name      VARCHAR(50) NOT NULL,
    stage_order     INTEGER NOT NULL,
    status          VARCHAR(20) DEFAULT 'pending',
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 知识文档表
CREATE TABLE knowledge_documents (
    doc_id          SERIAL PRIMARY KEY,
    title           VARCHAR(200) NOT NULL,
    doc_type        VARCHAR(50),
    content         TEXT,
    tags            JSONB DEFAULT '[]',
    uploaded_by     VARCHAR(32) REFERENCES users(user_id),
    status          VARCHAR(20) DEFAULT 'active',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- error_codes 表已在 005_system_configs.sql 中创建，此处跳过

-- 事件Schema表
CREATE TABLE event_schemas (
    schema_id        SERIAL PRIMARY KEY,
    topic            VARCHAR(100) NOT NULL UNIQUE,
    event_type       VARCHAR(50),
    schema_definition JSONB,
    description      TEXT,
    extra_data       JSONB DEFAULT '{}',
    created_at       TIMESTAMP DEFAULT NOW()
);

-- Saga配置表
CREATE TABLE saga_configs (
    saga_id         SERIAL PRIMARY KEY,
    saga_name       VARCHAR(100) NOT NULL UNIQUE,
    steps           JSONB NOT NULL,
    compensation    JSONB,
    is_active       BOOLEAN DEFAULT TRUE,
    description     TEXT,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 意图规则表
CREATE TABLE intent_rules (
    rule_id         SERIAL PRIMARY KEY,
    intent_code     VARCHAR(10) NOT NULL UNIQUE,
    intent_name     VARCHAR(50) NOT NULL,
    regex_pattern   TEXT,
    keywords        JSONB,
    target_agent    VARCHAR(50),
    channel         VARCHAR(20) DEFAULT 'business',
    priority        INTEGER DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE,
    extra_data      JSONB DEFAULT '{}'
);

-- 提示词模板表
CREATE TABLE prompt_templates (
    template_id     SERIAL PRIMARY KEY,
    template_name   VARCHAR(100) NOT NULL UNIQUE,
    template_content TEXT NOT NULL,
    variables       JSONB DEFAULT '[]',
    description     TEXT,
    extra_data      JSONB DEFAULT '{}',
    updated_at      TIMESTAMP DEFAULT NOW()
);


-- ============================================================
-- 图纸表 drawings（§A.5，bom.ref_drawing_id 待关联）
-- ============================================================
CREATE TABLE drawings (
    drawing_id      SERIAL PRIMARY KEY,
    product_code    VARCHAR(20) REFERENCES products(product_code),
    drawing_no      VARCHAR(50) NOT NULL,
    version         VARCHAR(20) DEFAULT '1.0',
    status          VARCHAR(20) DEFAULT 'draft',
    file_path       VARCHAR(500),
    uploaded_by     VARCHAR(32) REFERENCES users(user_id),
    fields_data     JSONB DEFAULT '{}',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 补充 bom.ref_drawing_id 外键约束（006中标注"待drawings表创建后添加FK约束"）
ALTER TABLE bom ADD CONSTRAINT fk_bom_ref_drawing
    FOREIGN KEY (ref_drawing_id) REFERENCES drawings(drawing_id);


-- ============================================================
-- 索引：为所有新表的外键字段和常用查询字段创建索引
-- ============================================================

-- business_rules_audit
CREATE INDEX idx_business_rules_audit_rule ON business_rules_audit(rule_id);
CREATE INDEX idx_business_rules_audit_changed_by ON business_rules_audit(changed_by);
CREATE INDEX idx_business_rules_audit_changed_at ON business_rules_audit(changed_at);

-- business_rules 新增字段索引
CREATE INDEX IF NOT EXISTS idx_business_rules_active ON business_rules(is_active);
CREATE INDEX IF NOT EXISTS idx_business_rules_priority ON business_rules(priority);
CREATE INDEX IF NOT EXISTS idx_business_rules_operation_type ON business_rules(operation_type);

-- workflow_instances
CREATE INDEX idx_workflow_instances_config ON workflow_instances(config_id);
CREATE INDEX idx_workflow_instances_workflow_type ON workflow_instances(workflow_type);
CREATE INDEX idx_workflow_instances_biz ON workflow_instances(biz_type, biz_id);
CREATE INDEX idx_workflow_instances_status ON workflow_instances(status);
CREATE INDEX idx_workflow_instances_created_by ON workflow_instances(created_by);

-- workflow_configs 新增字段索引
CREATE INDEX IF NOT EXISTS idx_workflow_configs_agent_scope ON workflow_configs(agent_scope);
CREATE INDEX IF NOT EXISTS idx_workflow_configs_initiation ON workflow_configs(initiation);

-- work_reports
CREATE INDEX idx_work_reports_user ON work_reports(user_id);
CREATE INDEX idx_work_reports_work_order ON work_reports(work_order_id);
CREATE INDEX idx_work_reports_product ON work_reports(product_code);
CREATE INDEX idx_work_reports_date ON work_reports(report_date);
CREATE INDEX idx_work_reports_status ON work_reports(status);

-- piece_rate_prices
CREATE INDEX idx_piece_rate_prices_product ON piece_rate_prices(product_code);
CREATE INDEX idx_piece_rate_prices_process_step ON piece_rate_prices(process_step);
CREATE INDEX idx_piece_rate_prices_status ON piece_rate_prices(status);

-- payroll_records
CREATE INDEX idx_payroll_records_user ON payroll_records(user_id);
CREATE INDEX idx_payroll_records_period ON payroll_records(period_start, period_end);
CREATE INDEX idx_payroll_records_status ON payroll_records(status);

-- attendance_records
CREATE INDEX idx_attendance_records_user ON attendance_records(user_id);
CREATE INDEX idx_attendance_records_date ON attendance_records(record_date);
CREATE INDEX idx_attendance_records_status ON attendance_records(status);

-- training_results
CREATE INDEX idx_training_results_type ON training_results(training_type);
CREATE INDEX idx_training_results_target ON training_results(target_entity);
CREATE INDEX idx_training_results_status ON training_results(status);
CREATE INDEX idx_training_results_created_by ON training_results(created_by);

-- training_result_versions
CREATE INDEX idx_training_result_versions_result ON training_result_versions(result_id);
CREATE INDEX idx_training_result_versions_rolled_back_by ON training_result_versions(rolled_back_by);

-- training_results_audit
CREATE INDEX idx_training_results_audit_result ON training_results_audit(result_id);
CREATE INDEX idx_training_results_audit_operator ON training_results_audit(operator);

-- config_test_cases
CREATE INDEX idx_config_test_cases_type ON config_test_cases(case_type);
CREATE INDEX idx_config_test_cases_target ON config_test_cases(target_type, target_id);

-- test_execution_reports
CREATE INDEX idx_test_execution_reports_case ON test_execution_reports(case_id);
CREATE INDEX idx_test_execution_reports_status ON test_execution_reports(execution_status);

-- sod_conflict_rules
CREATE INDEX idx_sod_conflict_rules_active ON sod_conflict_rules(is_active);

-- suppliers
CREATE INDEX idx_suppliers_status ON suppliers(status);
CREATE INDEX idx_suppliers_name ON suppliers(supplier_name);

-- purchase_orders
CREATE INDEX idx_purchase_orders_supplier ON purchase_orders(supplier_id);
CREATE INDEX idx_purchase_orders_product ON purchase_orders(product_code);
CREATE INDEX idx_purchase_orders_status ON purchase_orders(status);

-- accounts_payable
CREATE INDEX idx_accounts_payable_supplier ON accounts_payable(supplier_id);
CREATE INDEX idx_accounts_payable_po ON accounts_payable(po_id);
CREATE INDEX idx_accounts_payable_status ON accounts_payable(status);
CREATE INDEX idx_accounts_payable_due_date ON accounts_payable(due_date);

-- supplier_invoice_matches
CREATE INDEX idx_supplier_invoice_matches_po ON supplier_invoice_matches(po_id);
CREATE INDEX idx_supplier_invoice_matches_status ON supplier_invoice_matches(match_status);

-- cost_accounting
CREATE INDEX idx_cost_accounting_product ON cost_accounting(product_code);
CREATE INDEX idx_cost_accounting_order ON cost_accounting(order_id);
CREATE INDEX idx_cost_accounting_period ON cost_accounting(period_start, period_end);

-- return_orders
CREATE INDEX idx_return_orders_original_order ON return_orders(original_order_id);
CREATE INDEX idx_return_orders_customer ON return_orders(customer_id);
CREATE INDEX idx_return_orders_status ON return_orders(status);

-- customer_complaints
CREATE INDEX idx_customer_complaints_customer ON customer_complaints(customer_id);
CREATE INDEX idx_customer_complaints_order ON customer_complaints(order_id);
CREATE INDEX idx_customer_complaints_status ON customer_complaints(status);

-- departments
CREATE INDEX idx_departments_parent ON departments(parent_dept_id);
CREATE INDEX idx_departments_manager ON departments(manager_id);
CREATE INDEX idx_departments_sort ON departments(sort_order);

-- department_changes
CREATE INDEX idx_department_changes_dept ON department_changes(dept_id);
CREATE INDEX idx_department_changes_changed_by ON department_changes(changed_by);

-- user_transfers
CREATE INDEX idx_user_transfers_user ON user_transfers(user_id);
CREATE INDEX idx_user_transfers_status ON user_transfers(status);
CREATE INDEX idx_user_transfers_effective_date ON user_transfers(effective_date);

-- order_stages
CREATE INDEX idx_order_stages_order ON order_stages(order_id);
CREATE INDEX idx_order_stages_status ON order_stages(status);
CREATE INDEX idx_order_stages_order_sort ON order_stages(order_id, stage_order);

-- knowledge_documents
CREATE INDEX idx_knowledge_documents_type ON knowledge_documents(doc_type);
CREATE INDEX idx_knowledge_documents_status ON knowledge_documents(status);
CREATE INDEX idx_knowledge_documents_uploaded_by ON knowledge_documents(uploaded_by);

-- event_schemas
CREATE INDEX idx_event_schemas_event_type ON event_schemas(event_type);

-- saga_configs
CREATE INDEX idx_saga_configs_active ON saga_configs(is_active);

-- intent_rules
CREATE INDEX idx_intent_rules_target_agent ON intent_rules(target_agent);
CREATE INDEX idx_intent_rules_channel ON intent_rules(channel);
CREATE INDEX idx_intent_rules_active ON intent_rules(is_active);
CREATE INDEX idx_intent_rules_priority ON intent_rules(priority);

-- drawings
CREATE INDEX idx_drawings_product ON drawings(product_code);
CREATE INDEX idx_drawings_drawing_no ON drawings(drawing_no);
CREATE INDEX idx_drawings_status ON drawings(status);
CREATE INDEX idx_drawings_uploaded_by ON drawings(uploaded_by);


-- ============================================================
-- 触发器：为含 updated_at 字段的新表自动更新 updated_at
-- （update_updated_at 函数已在 001 中定义）
-- ============================================================
CREATE TRIGGER trg_workflow_instances_updated BEFORE UPDATE ON workflow_instances
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_suppliers_updated BEFORE UPDATE ON suppliers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_purchase_orders_updated BEFORE UPDATE ON purchase_orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_accounts_payable_updated BEFORE UPDATE ON accounts_payable
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_return_orders_updated BEFORE UPDATE ON return_orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_customer_complaints_updated BEFORE UPDATE ON customer_complaints
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_departments_updated BEFORE UPDATE ON departments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_knowledge_documents_updated BEFORE UPDATE ON knowledge_documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_prompt_templates_updated BEFORE UPDATE ON prompt_templates
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_drawings_updated BEFORE UPDATE ON drawings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ============================================================
-- 预置Saga配置数据（9个Saga）
-- ============================================================
INSERT INTO saga_configs (saga_name, steps, compensation, description) VALUES
('订单确认Saga',
 '["validate_order","check_credit","reserve_inventory","create_work_order","confirm_order"]'::jsonb,
 '["cancel_work_order","release_inventory","restore_credit","notify_sales"]'::jsonb,
 '销售订单确认全流程：校验订单→信用检查→库存预留→创建工单→确认订单'),
('订单取消Saga',
 '["validate_cancellation","release_inventory","cancel_work_orders","refund_payment"]'::jsonb,
 '["restore_work_orders","re_reserve_inventory","mark_order_active"]'::jsonb,
 '订单取消全流程：校验取消→释放库存→取消工单→退款'),
('库存调整Saga',
 '["validate_adjustment","update_inventory","log_movement"]'::jsonb,
 '["revert_inventory","remove_movement_log"]'::jsonb,
 '库存调整全流程：校验调整→更新库存→记录流水'),
('图纸版本升级Saga',
 '["submit_new_version","approve_version","activate_version"]'::jsonb,
 '["deactivate_version","revert_to_previous"]'::jsonb,
 '图纸版本升级全流程：提交新版本→审批版本→激活版本'),
('训练结果应用Saga',
 '["validate_training","snapshot_config","apply_training","verify_training"]'::jsonb,
 '["rollback_training","restore_config_snapshot"]'::jsonb,
 '训练结果应用全流程：校验训练→快照配置→应用训练→验证训练'),
('工资发放Saga',
 '["calculate_payroll","approve_payroll","execute_payment","confirm_payment"]'::jsonb,
 '["reverse_payment","mark_payroll_draft"]'::jsonb,
 '工资发放全流程：计算工资→审批工资→执行付款→确认付款'),
('采购全流程Saga',
 '["create_purchase_order","supplier_confirm","receive_goods","match_invoice","process_payment"]'::jsonb,
 '["cancel_payment","unmatch_invoice","return_goods","cancel_po"]'::jsonb,
 '采购全流程：创建采购单→供应商确认→收货→发票匹配→付款'),
('离职交接Saga',
 '["initiate_resignation","transfer_responsibilities","revoke_permissions","archive_user"]'::jsonb,
 '["restore_user","restore_permissions","restore_responsibilities"]'::jsonb,
 '离职交接全流程：发起离职→交接职责→撤销权限→归档用户'),
('客诉处理Saga',
 '["log_complaint","investigate","propose_resolution","execute_resolution"]'::jsonb,
 '["cancel_resolution","reopen_complaint"]'::jsonb,
 '客诉处理全流程：记录投诉→调查→提出方案→执行方案')
ON CONFLICT (saga_name) DO NOTHING;


-- ============================================================
-- 预置Event Schema数据（22个Topic）
-- ============================================================
INSERT INTO event_schemas (topic, event_type, schema_definition, description) VALUES
('order.created',
 'order',
 '{"required":["order_id","customer_id","product_code","quantity","total_amount"],"properties":{"order_id":{"type":"string"},"customer_id":{"type":"string"},"product_code":{"type":"string"},"quantity":{"type":"integer"},"total_amount":{"type":"number"}}}'::jsonb,
 '订单创建事件'),
('order.confirmed',
 'order',
 '{"required":["order_id","confirmed_by"],"properties":{"order_id":{"type":"string"},"confirmed_by":{"type":"string"},"confirmed_at":{"type":"string","format":"date-time"}}}'::jsonb,
 '订单确认事件'),
('order.cancelled',
 'order',
 '{"required":["order_id","reason"],"properties":{"order_id":{"type":"string"},"reason":{"type":"string"},"cancelled_by":{"type":"string"}}}'::jsonb,
 '订单取消事件'),
('order.completed',
 'order',
 '{"required":["order_id"],"properties":{"order_id":{"type":"string"},"completed_at":{"type":"string","format":"date-time"}}}'::jsonb,
 '订单完成事件'),
('order.shipped',
 'order',
 '{"required":["order_id","carrier"],"properties":{"order_id":{"type":"string"},"carrier":{"type":"string"},"tracking_no":{"type":"string"}}}'::jsonb,
 '订单发货事件'),
('inventory.adjusted',
 'inventory',
 '{"required":["product_code","adjustment","reason"],"properties":{"product_code":{"type":"string"},"adjustment":{"type":"integer"},"reason":{"type":"string"},"operator_id":{"type":"string"}}}'::jsonb,
 '库存调整事件'),
('inventory.low_stock',
 'inventory',
 '{"required":["product_code","current_qty","safety_stock"],"properties":{"product_code":{"type":"string"},"current_qty":{"type":"integer"},"safety_stock":{"type":"integer"}}}'::jsonb,
 '库存低库存预警事件'),
('product.created',
 'product',
 '{"required":["product_code","product_name"],"properties":{"product_code":{"type":"string"},"product_name":{"type":"string"},"category":{"type":"string"},"price":{"type":"number"}}}'::jsonb,
 '产品创建事件'),
('product.updated',
 'product',
 '{"required":["product_code"],"properties":{"product_code":{"type":"string"},"changed_fields":{"type":"array"},"updated_by":{"type":"string"}}}'::jsonb,
 '产品更新事件'),
('drawing.version_changed',
 'drawing',
 '{"required":["drawing_no","old_version","new_version"],"properties":{"drawing_no":{"type":"string"},"old_version":{"type":"string"},"new_version":{"type":"string"},"changed_by":{"type":"string"}}}'::jsonb,
 '图纸版本变更事件'),
('customer.created',
 'customer',
 '{"required":["customer_id","customer_name"],"properties":{"customer_id":{"type":"string"},"customer_name":{"type":"string"},"credit_limit":{"type":"number"}}}'::jsonb,
 '客户创建事件'),
('customer.credit_changed',
 'customer',
 '{"required":["customer_id","old_limit","new_limit"],"properties":{"customer_id":{"type":"string"},"old_limit":{"type":"number"},"new_limit":{"type":"number"},"approved_by":{"type":"string"}}}'::jsonb,
 '客户信用额度变更事件'),
('work_order.created',
 'work_order',
 '{"required":["work_order_id","order_id","product_code","quantity"],"properties":{"work_order_id":{"type":"string"},"order_id":{"type":"string"},"product_code":{"type":"string"},"quantity":{"type":"integer"}}}'::jsonb,
 '工单创建事件'),
('work_order.completed',
 'work_order',
 '{"required":["work_order_id","actual_qty"],"properties":{"work_order_id":{"type":"string"},"actual_qty":{"type":"integer"},"completed_at":{"type":"string","format":"date-time"}}}'::jsonb,
 '工单完成事件'),
('qc.passed',
 'quality',
 '{"required":["qc_id","product_code","batch_no"],"properties":{"qc_id":{"type":"integer"},"product_code":{"type":"string"},"batch_no":{"type":"string"},"inspector_id":{"type":"string"}}}'::jsonb,
 '质检通过事件'),
('qc.failed',
 'quality',
 '{"required":["qc_id","product_code","batch_no","defect_qty"],"properties":{"qc_id":{"type":"integer"},"product_code":{"type":"string"},"batch_no":{"type":"string"},"defect_qty":{"type":"integer"},"inspector_id":{"type":"string"}}}'::jsonb,
 '质检不合格事件'),
('payment.received',
 'finance',
 '{"required":["order_id","amount"],"properties":{"order_id":{"type":"string"},"amount":{"type":"number"},"payment_method":{"type":"string"},"received_at":{"type":"string","format":"date-time"}}}'::jsonb,
 '收款事件'),
('payment.made',
 'finance',
 '{"required":["ap_id","supplier_id","amount"],"properties":{"ap_id":{"type":"integer"},"supplier_id":{"type":"string"},"amount":{"type":"number"},"payment_method":{"type":"string"}}}'::jsonb,
 '付款事件'),
('training.data_uploaded',
 'training',
 '{"required":["agent_type","data_count"],"properties":{"agent_type":{"type":"string"},"data_count":{"type":"integer"},"uploaded_by":{"type":"string"}}}'::jsonb,
 '训练数据上传事件'),
('training.approved',
 'training',
 '{"required":["result_id","training_type"],"properties":{"result_id":{"type":"integer"},"training_type":{"type":"string"},"approved_by":{"type":"string"}}}'::jsonb,
 '训练结果审批事件'),
('payroll.confirmed',
 'hr',
 '{"required":["payroll_id","user_id","net_pay"],"properties":{"payroll_id":{"type":"integer"},"user_id":{"type":"string"},"net_pay":{"type":"number"},"period_start":{"type":"string","format":"date"},"period_end":{"type":"string","format":"date"}}}'::jsonb,
 '工资确认事件'),
('user.transferred',
 'hr',
 '{"required":["user_id","from_dept","to_dept"],"properties":{"user_id":{"type":"string"},"from_dept":{"type":"string"},"to_dept":{"type":"string"},"effective_date":{"type":"string","format":"date"}}}'::jsonb,
 '用户调岗事件')
ON CONFLICT (topic) DO NOTHING;
