-- ============================================================
-- 003_rules_schema.sql
-- 规则配置与RBAC权限表迁移
-- 对应技术规格v6.08：§2.2.2 RBAC权限体系 + §2.6 规则配置存储
-- 说明：
--   创建规则配置表（rule_configs，保留兼容，后续迁移至 business_rules 表），
--   以及RBAC用户角色权限表（roles/users）。
--   roles 使用 role_id 业务编码作主键，users 使用 user_id 作主键并引用 roles。
--   users 表必须在 roles 表之后创建（因 users.role_id 引用 roles.role_id）。
--   本文件还通过 ALTER TABLE 为 001 中创建的业务表补充引用 users(user_id) 的
--   外键约束（notifications/operation_logs/inventory_movements/
--   production_lines/qc_records），因 users 表在本文件中创建。
-- 依赖：先执行 001_init_schema.sql
-- ============================================================

-- 规则配置表（保留兼容，后续迁移至 business_rules 表）
CREATE TABLE rule_configs (
    id SERIAL PRIMARY KEY,
    rule_name VARCHAR(100) NOT NULL UNIQUE,
    rule_type VARCHAR(50),  -- cost/discount/credit/version/schedule/inventory/qc/bom
    config JSONB NOT NULL,  -- 规则参数JSON
    enabled BOOLEAN DEFAULT TRUE,
    priority INTEGER DEFAULT 0,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 用户角色表（§2.2.2 RBAC）
CREATE TABLE roles (
    role_id VARCHAR(20) PRIMARY KEY,
    role_name VARCHAR(100) NOT NULL,
    parent_role_id VARCHAR(20) REFERENCES roles(role_id),
    permissions JSONB NOT NULL,
    description TEXT,
    sort_order INTEGER DEFAULT 0
);

-- 用户表（§2.2.2 RBAC）
-- 必须在 roles 表之后创建，因 users.role_id 引用 roles.role_id
CREATE TABLE users (
    user_id VARCHAR(32) PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(200) NOT NULL,
    name VARCHAR(50) NOT NULL,
    title VARCHAR(50),
    department VARCHAR(50) NOT NULL,
    role_id VARCHAR(20) REFERENCES roles(role_id),
    avatar_color VARCHAR(7),
    avatar_text VARCHAR(4),
    phone VARCHAR(20),
    email VARCHAR(100),
    status VARCHAR(20) DEFAULT 'active',
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================
-- 补充外键约束：001 中业务表引用 users(user_id)
-- （users 表在上方已创建，此时可安全添加约束）
-- ============================================================
ALTER TABLE notifications ADD CONSTRAINT fk_notifications_target_user
    FOREIGN KEY (target_user) REFERENCES users(user_id);
ALTER TABLE operation_logs ADD CONSTRAINT fk_operation_logs_user
    FOREIGN KEY (user_id) REFERENCES users(user_id);
ALTER TABLE inventory_movements ADD CONSTRAINT fk_inventory_movements_operator
    FOREIGN KEY (operator_id) REFERENCES users(user_id);
ALTER TABLE production_lines ADD CONSTRAINT fk_production_lines_manager
    FOREIGN KEY (manager_id) REFERENCES users(user_id);
ALTER TABLE qc_records ADD CONSTRAINT fk_qc_records_inspector
    FOREIGN KEY (inspector_id) REFERENCES users(user_id);

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX idx_rule_configs_type ON rule_configs(rule_type);
CREATE INDEX idx_rule_configs_enabled ON rule_configs(enabled);
CREATE INDEX idx_roles_parent ON roles(parent_role_id);
CREATE INDEX idx_roles_sort ON roles(sort_order);
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_role ON users(role_id);
CREATE INDEX idx_users_department ON users(department);
CREATE INDEX idx_users_status ON users(status);

-- ============================================================
-- 触发器：rule_configs 自动更新 updated_at
-- （update_updated_at 函数在 001 中定义，此处直接使用）
-- ============================================================
CREATE TRIGGER trg_rule_configs_updated BEFORE UPDATE ON rule_configs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
