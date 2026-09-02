-- ============================================================
-- 001_init_schema.sql
-- 业务数据表结构初始化迁移
-- 对应技术规格v6.08：§1.7 数据库表结构设计
-- 说明：
--   创建AI工厂管家v6.08的核心业务数据表，包括产品、客户、订单、库存、
--   库存流水、生产线、质检、通知、操作日志共9张表。
--   业务编码作主键，库存支持五阶段状态流转（raw/wip_cnc/wip_anode/wip_qc/finished），
--   订单支持8状态全生命周期。
--   注意：notifications/operation_logs/inventory_movements/production_lines/
--   qc_records 中引用 users(user_id) 的外键约束因 users 表在 003 中创建，
--   此处暂不添加，将在 003_rules_schema.sql 中通过 ALTER TABLE 补充。
-- 执行顺序：第一个执行，所有业务表的基础结构。
-- ============================================================

-- 产品表（§1.7.2.7）
CREATE TABLE products (
    product_code    VARCHAR(20) PRIMARY KEY,
    product_name    VARCHAR(200) NOT NULL,
    category        VARCHAR(100),
    spec            VARCHAR(500),
    unit            VARCHAR(10) DEFAULT '套',
    price           DECIMAL(10,2) NOT NULL,
    cost_price      DECIMAL(10,2) NOT NULL,
    stock_qty       INTEGER DEFAULT 0,
    min_stock_qty   INTEGER DEFAULT 0,
    description     TEXT,
    drawing_version VARCHAR(50) DEFAULT '1.0',
    parent_product_code VARCHAR(20) REFERENCES products(product_code),
    is_independent  BOOLEAN DEFAULT False,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 客户表（§1.7.2.4a）
CREATE TABLE customers (
    customer_id     VARCHAR(20) PRIMARY KEY,
    customer_name   VARCHAR(100) NOT NULL,
    contact_person  VARCHAR(50),
    contact_phone   VARCHAR(20),
    address         TEXT,
    credit_limit    DECIMAL(12,2) DEFAULT 0,
    credit_used     DECIMAL(12,2) DEFAULT 0,
    credit_score    INTEGER DEFAULT 60,
    status          VARCHAR(20) DEFAULT 'active',
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    CONSTRAINT chk_customer_status CHECK (status IN ('active','inactive','blacklist'))
);

-- 订单表（§1.7.2.4a）
CREATE TABLE orders (
    order_id        VARCHAR(20) PRIMARY KEY,
    customer_id     VARCHAR(20) REFERENCES customers(customer_id),
    sales_user      VARCHAR(100),
    product_code    VARCHAR(20) REFERENCES products(product_code),
    quantity        INTEGER NOT NULL,
    unit_price      DECIMAL(10,2) NOT NULL,
    total_amount    DECIMAL(12,2),
    order_date      TIMESTAMP DEFAULT NOW(),
    delivery_date   TIMESTAMP,
    status          VARCHAR(20) DEFAULT 'draft',
    audit_chain_id  VARCHAR(64),
    notes           TEXT,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    CONSTRAINT chk_order_status CHECK (status IN ('draft','pending_audit','approved','in_production','completed','shipped','paid','closed'))
);

-- 库存表（§1.7.2.4 - 五阶段状态设计）
CREATE TABLE inventory (
    product_code VARCHAR(20) PRIMARY KEY REFERENCES products(product_code),
    raw INTEGER DEFAULT 0,
    wip_cnc INTEGER DEFAULT 0,
    wip_anode INTEGER DEFAULT 0,
    wip_qc INTEGER DEFAULT 0,
    finished INTEGER DEFAULT 0,
    unit VARCHAR(10) DEFAULT '套',
    raw_value DECIMAL(12,2),
    wip_value DECIMAL(12,2),
    finished_value DECIMAL(12,2),
    safety_stock INTEGER,
    version INTEGER DEFAULT 0,
    extra_data      JSONB DEFAULT '{}',
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 库存流水表
-- 注意：operator_id 引用 users(user_id) 的外键在 003 中通过 ALTER TABLE 添加
CREATE TABLE inventory_movements (
    movement_id SERIAL PRIMARY KEY,
    product_code VARCHAR(20) REFERENCES products(product_code),
    movement_type VARCHAR(20) NOT NULL,
    from_stage VARCHAR(20),
    to_stage VARCHAR(20),
    qty INTEGER NOT NULL,
    balance_after INTEGER,
    order_id VARCHAR(20) REFERENCES orders(order_id),
    work_order_id VARCHAR(20),
    operator_id VARCHAR(32),
    extra_data      JSONB DEFAULT '{}',
    timestamp TIMESTAMP DEFAULT NOW()
);

-- 生产线表（§1.7.2.7 v6.07新增）
-- 注意：manager_id 引用 users(user_id) 的外键在 003 中通过 ALTER TABLE 添加
CREATE TABLE production_lines (
    line_id         VARCHAR(20) PRIMARY KEY,
    line_name       VARCHAR(100) NOT NULL,
    capacity_per_hour INTEGER NOT NULL DEFAULT 0,
    oee             DECIMAL(5,2) DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'active',
    shift_pattern   VARCHAR(50),
    manager_id      VARCHAR(32),
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- 质检记录表
-- 注意：inspector_id 引用 users(user_id) 的外键在 003 中通过 ALTER TABLE 添加
CREATE TABLE qc_records (
    qc_id           SERIAL PRIMARY KEY,
    product_code    VARCHAR(20) REFERENCES products(product_code),
    batch_no        VARCHAR(50),
    order_id        VARCHAR(20) REFERENCES orders(order_id),
    work_order_id   VARCHAR(20),
    inspector_id    VARCHAR(32),
    result          VARCHAR(20) DEFAULT 'pending',
    qc_type         VARCHAR(20) DEFAULT 'full',
    sample_size     INTEGER,
    aql_value       DECIMAL(3,2),
    items           JSONB,
    extra_data      JSONB DEFAULT '{}',
    created_at      TIMESTAMP DEFAULT NOW()
);

-- 通知表
-- 注意：target_user 引用 users(user_id) 的外键在 003 中通过 ALTER TABLE 添加
CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(200),
    content TEXT,
    target_user VARCHAR(32),
    is_read BOOLEAN DEFAULT FALSE,
    extra_data      JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- 操作日志表
-- 注意：user_id 引用 users(user_id) 的外键在 003 中通过 ALTER TABLE 添加
CREATE TABLE operation_logs (
    log_id SERIAL PRIMARY KEY,
    user_id VARCHAR(32),
    action VARCHAR(200),
    details JSONB,
    extra_data      JSONB DEFAULT '{}',
    timestamp TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- 索引（提升查询性能）
-- ============================================================
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_products_parent ON products(parent_product_code);
CREATE INDEX idx_products_drawing_version ON products(drawing_version);
CREATE INDEX idx_customers_status ON customers(status);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_product ON orders(product_code);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_orders_sales_user ON orders(sales_user);
CREATE INDEX idx_orders_audit_chain ON orders(audit_chain_id);
CREATE INDEX idx_inventory_movements_product ON inventory_movements(product_code);
CREATE INDEX idx_inventory_movements_order ON inventory_movements(order_id);
CREATE INDEX idx_inventory_movements_type ON inventory_movements(movement_type);
CREATE INDEX idx_inventory_movements_timestamp ON inventory_movements(timestamp);
CREATE INDEX idx_production_lines_status ON production_lines(status);
CREATE INDEX idx_production_lines_manager ON production_lines(manager_id);
CREATE INDEX idx_qc_records_product ON qc_records(product_code);
CREATE INDEX idx_qc_records_order ON qc_records(order_id);
CREATE INDEX idx_qc_records_batch ON qc_records(batch_no);
CREATE INDEX idx_notifications_target_user ON notifications(target_user, is_read);
CREATE INDEX idx_operation_logs_user ON operation_logs(user_id, timestamp);

-- ============================================================
-- 触发器：自动更新 updated_at 字段
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_products_updated BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_customers_updated BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_inventory_updated BEFORE UPDATE ON inventory
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_orders_updated BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_production_lines_updated BEFORE UPDATE ON production_lines
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
