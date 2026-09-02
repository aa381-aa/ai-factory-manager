-- ============================================================
-- 009_intent_rules.sql
-- 意图识别规则表（DB优先 + 内置兜底 + 训练变更）
-- 对应技术规格v6.29：§A.8 意图识别规则定义（正则变量化）
-- 说明：
--   008_v616_schema_upgrade.sql 已按 v6.16 旧结构创建 intent_rules 表
--   （SERIAL主键/is_active/channel，与规格§A.8 DDL不一致，且无种子数据）。
--   本迁移重建为规格 §A.8 DDL（rule_id VARCHAR主键 / target_channel /
--   enabled / adjusted_by / approval_id / accuracy_rate / version / updated_at），
--   并预置种子规则（= coordinator.INTENT_REGEXES + intent_recognition.DEFAULT_RULES
--   合并去重，adjusted_by='SEED'，version=1，enabled=TRUE，priority按代码匹配顺序分层）。
--   同时预置 rule_config_change 审批链模板（L2训练产出/LLM建议/人工修改经审批后生效）。
--   表当前无数据、无外部引用，DROP+CREATE 无风险。
-- 依赖：先执行 008_v616_schema_upgrade.sql（或等价建表）
-- ============================================================

-- ============================================================
-- 1. 重建 intent_rules 表（规格 §A.8 DDL）
-- ============================================================
DROP TABLE IF EXISTS intent_rules;

CREATE TABLE intent_rules (
    rule_id          VARCHAR(32) PRIMARY KEY,      -- 规则ID（如RULE-INT-001）
    intent_name      VARCHAR(30) NOT NULL,         -- 意图名称
    regex_pattern    TEXT NOT NULL,                 -- 正则表达式
    target_agent     VARCHAR(30),                   -- 对应Agent
    target_channel   VARCHAR(20) NOT NULL,         -- 通道: business/consulting/system
    priority         INT DEFAULT 50,                -- 优先级（数字越小优先级越高）
    enabled          BOOLEAN DEFAULT TRUE,          -- 是否启用
    adjusted_by      VARCHAR(20) DEFAULT 'MANUAL',  -- 调整方式: SEED/MANUAL/L1/L2/LLM
    approval_id      VARCHAR(50),                   -- 关联审批单ID（workflow_configs.config_id）
    accuracy_rate    FLOAT,                         -- 识别准确率（训练评估）
    version          INT DEFAULT 1,
    updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_intent_rules_enabled ON intent_rules(enabled);
CREATE INDEX idx_intent_rules_priority ON intent_rules(priority);
CREATE INDEX idx_intent_rules_target_agent ON intent_rules(target_agent);
CREATE INDEX idx_intent_rules_channel ON intent_rules(target_channel);

-- ============================================================
-- 2. 种子规则（业务操作意图，priority 10~22）
--    来源：coordinator.INTENT_REGEXES（含路由）+ DEFAULT_RULES 独有词补充
-- ============================================================
INSERT INTO intent_rules (rule_id, intent_name, regex_pattern, target_agent, target_channel, priority, enabled, adjusted_by, version) VALUES
-- 销售：订单/合同（动作优先）
('RULE-INT-001', 'create_order', '(下[个张笔]?单|(?<![查看找询一])下订单|下一笔订单|下个订单|下笔订单|创建订单|新建订单|订个货|帮我订|要订货|采购|订购|订货|开单|想买|要买)', 'sales', 'business', 10, TRUE, 'SEED', 1),
('RULE-INT-002', 'modify_order', '(修改订单|改单|变更|追加|加单|加数量|改成|修改数量)', 'sales', 'business', 11, TRUE, 'SEED', 1),
('RULE-INT-003', 'order_cancel', '(取消.{0,2}订单|取消单子|退单|撤销订单|不要这个订单)', 'sales', 'business', 12, TRUE, 'SEED', 1),
('RULE-INT-004', 'contract', '(生成合同|起草合同|拟合同|签合同|签订合同|合同管理|合同模板|查合同|查询合同|我的合同|合同列表|合同状态|合同详情)', 'sales', 'business', 13, TRUE, 'SEED', 1),
('RULE-INT-005', 'contract', '(生成|起草|拟|签).{0,20}合同', 'sales', 'business', 13, TRUE, 'SEED', 1),
-- 技术：图纸/BOM/工艺
('RULE-INT-006', 'drawing_management', '(图纸|图号|上传图纸|版本管理|图纸变更)', 'technical', 'business', 14, TRUE, 'SEED', 1),
('RULE-INT-007', 'bom_management', '(BOM展开|物料清单|BOM结构|BOM查询|bom|BOM)', 'technical', 'business', 14, TRUE, 'SEED', 1),
('RULE-INT-008', 'process_route', '(工艺路线|工序|加工工艺|工艺卡|工艺流程|加工工序|工艺参数)', 'technical', 'business', 14, TRUE, 'SEED', 1),
-- 仓储：出入库/库存调整
('RULE-INT-009', 'stock_in', '(入库|收货)', 'warehouse', 'business', 15, TRUE, 'SEED', 1),
('RULE-INT-010', 'stock_out', '(出库|发货)', 'warehouse', 'business', 15, TRUE, 'SEED', 1),
('RULE-INT-011', 'inventory_adjust', '(库存调整|调整库存|盘盈|盘亏|库存修正|库存盘点)', 'warehouse', 'business', 15, TRUE, 'SEED', 1),
-- 生产：工单/设备/报修
('RULE-INT-012', 'work_order', '(工单|创建工单|工单管理|派工|派单|工单状态)', 'production', 'business', 16, TRUE, 'SEED', 1),
('RULE-INT-013', 'work_order_query', '(工单查询|查工单|工单进度)', 'production', 'business', 16, TRUE, 'SEED', 1),
('RULE-INT-014', 'equipment', '(设备|设备状态|设备保养|设备故障|设备维修|设备管理|机台|机床)', 'production', 'business', 16, TRUE, 'SEED', 1),
('RULE-INT-015', 'equipment_query', '(设备查询|查设备|设备列表)', 'production', 'business', 16, TRUE, 'SEED', 1),
('RULE-INT-016', 'report_issue', '(停机|设备故障|维修|TPM|保养)', 'production', 'business', 16, TRUE, 'SEED', 1),
-- 采购/退货/客诉
-- v6.47："采购X原料/材料/物料"属采购申请（INT-20->warehouse），与 create_order 裸"采购"区分
('RULE-INT-017', 'purchase', '(采购单|下采购单|供应商|采购订单|采购.{0,6}(原料|材料|物料|物资|耗材)|采购申请|请购|申购)', 'warehouse', 'business', 17, TRUE, 'SEED', 1),
('RULE-INT-018', 'return_order', '(退货|退货申请|退货单)', 'sales', 'business', 17, TRUE, 'SEED', 1),
('RULE-INT-019', 'complaint', '(客诉|投诉|客户投诉|质量问题投诉)', 'qc', 'business', 17, TRUE, 'SEED', 1),
-- 财务操作
('RULE-INT-020', 'financial_operation', '(付款|收款|开票|开发票|财务操作|付钱|收钱)', 'finance', 'business', 18, TRUE, 'SEED', 1),
-- HR
('RULE-INT-021', 'work_report', '(报工|工时|报工记录|提交报工)', 'hr', 'business', 19, TRUE, 'SEED', 1),
('RULE-INT-022', 'payroll', '(工资|薪酬|计件工资|工资单|发工资)', 'hr', 'business', 19, TRUE, 'SEED', 1),
('RULE-INT-023', 'attendance', '(考勤|打卡|出勤|迟到|请假|加班)', 'hr', 'business', 19, TRUE, 'SEED', 1),
('RULE-INT-024', 'onboarding', '(入职|新员工|建档)', 'hr', 'business', 19, TRUE, 'SEED', 1),
('RULE-INT-025', 'resignation', '(离职|辞职|交接|离职手续)', 'hr', 'business', 19, TRUE, 'SEED', 1),
('RULE-INT-026', 'org_query', '(组织架构|部门|人员列表|员工列表|组织结构)', 'hr', 'business', 19, TRUE, 'SEED', 1),
-- 流程启动/引导
('RULE-INT-027', 'workflow_start', '(发起流程|启动流程|发起审批|开始流程|提交申请)', 'knowledge', 'business', 20, TRUE, 'SEED', 1),
('RULE-INT-028', 'workflow_guide', '(流程列表|可发起什么|有哪些流程|流程引导|能发起什么流程)', 'knowledge', 'consulting', 20, TRUE, 'SEED', 1),
-- 质量动作（CAPA/FMEA/PPAP等强动作）
('RULE-INT-029', 'quality_action', '(纠正措施|预防措施|CAPA|8D|FMEA|失效模式|风险分析|PPAP|生产件批准)', 'qc', 'business', 21, TRUE, 'SEED', 1),
-- 排产
('RULE-INT-030', 'schedule_production', '(排产|安排生产|计划生产|生产计划|换线|换模|SMED|快速换模)', 'production', 'business', 22, TRUE, 'SEED', 1),
('RULE-INT-031', 'query_schedule', '(排产|产能|排班|插单|还能排|排多少|负荷|外协|交期)', 'production', 'business', 22, TRUE, 'SEED', 1),
-- ============================================================
-- 3. 种子规则（查询意图，priority 30~36）
-- ============================================================
('RULE-INT-032', 'query_order', '(查订单|查看订单|查询订单|订单状态|订单进度|订单情况|订单详情|所有订单|订单列表|订单看板|我的订单|我下的单|查一下我的订单|现有订单|现在的订单|有哪些订单|订单有哪些|订单编号|全部订单|查一下订单)', 'sales', 'business', 30, TRUE, 'SEED', 1),
('RULE-INT-033', 'query_order', '(查一下.+的订单|查询.+的订单|查.+的订单|看看.+的订单)', 'sales', 'business', 30, TRUE, 'SEED', 1),
('RULE-INT-034', 'query_inventory', '(查库存|查一下库存|库存查询|查询库存|库存多少|库存情况|现货情况|还有多少|剩多少|有没有货|备货情况|看看库存|有没有库存)', 'warehouse', 'business', 31, TRUE, 'SEED', 1),
('RULE-INT-035', 'query_inventory', '(查.*库存|库存.*多少|库存.*情况)', 'warehouse', 'business', 31, TRUE, 'SEED', 1),
('RULE-INT-036', 'query_inventory', '(在制品|WIP|在制|半成品|物料齐套|备料|齐套率|盘点|循环盘点|安全库存|最低库存|再订货点|追溯|批次追溯|序列号追溯|Traceability)', 'warehouse', 'business', 31, TRUE, 'SEED', 1),
('RULE-INT-037', 'query_progress', '(生产进度|进度查询|瓶颈|产能不足|工时|节拍|CT|CT时间)', 'production', 'business', 32, TRUE, 'SEED', 1),
('RULE-INT-038', 'query_production_progress', '(生产进度|生产看板|生产状态|产线状态|进度)', 'production', 'business', 32, TRUE, 'SEED', 1),
('RULE-INT-039', 'query_qc', '(质检|品检|QC|qc|合格率|不良品)', 'qc', 'business', 33, TRUE, 'SEED', 1),
('RULE-INT-040', 'query_qc', '(首件检验|首检|FAI|过程审核|过程检查|巡检|不合格品|NCR|不合格报告|SPC|统计过程控制|控制图|Cpk|过程能力|MSA|测量系统分析|量具分析|质量目标|质量方针|KPI|绩效指标|来料检验|IQC|进料检验|质量怎么样|质量情况|质量水平)', 'qc', 'business', 33, TRUE, 'SEED', 1),
('RULE-INT-041', 'query_price', '(多少钱|价格多少|报价|售价|单价|询价|价格查询)', 'sales', 'business', 34, TRUE, 'SEED', 1),
('RULE-INT-042', 'query_customer', '(客户|信用|额度|账期|欠款|应收|客户信息)', 'sales', 'business', 34, TRUE, 'SEED', 1),
('RULE-INT-043', 'financial_query', '(对账|应收|应付|财务|回款|发票|利润|毛利|净利润|成本分析|账龄|财务报表|资金流|成本核算|BOM成本|利润率|毛利率|净利率|付款条件|信用期)', 'finance', 'business', 35, TRUE, 'SEED', 1),
('RULE-INT-044', 'query_audit', '(审计|内审|查账|审核记录|合规|日志|操作记录|违规|越权)', 'audit', 'business', 36, TRUE, 'SEED', 1),
('RULE-INT-045', 'query_overview', '(数据总览|数据看板|经营概况|工厂概况|整体情况|数据汇总|总览|概览)', 'sales', 'business', 36, TRUE, 'SEED', 1),
-- ============================================================
-- 4. 种子规则（咨询/知识/系统，priority 40~47）
-- ============================================================
('RULE-INT-046', 'knowledge_management', '(知识管理|知识库|文档管理|经验库)', 'knowledge', 'consulting', 40, TRUE, 'SEED', 1),
('RULE-INT-047', 'knowledge_query', '(问一下|查询资料|说明书|文档|怎么办|怎么解决|有什么建议|怎么改善)', 'knowledge', 'consulting', 41, TRUE, 'SEED', 1),
('RULE-INT-048', 'management_consulting', '(管理咨询|管理制度|流程制度|管理建议|管理优化|如何管理|怎么管理)', 'knowledge', 'consulting', 42, TRUE, 'SEED', 1),
('RULE-INT-049', 'data_analysis', '(数据分析|数据统计|数据报表|经营分析|报表分析|趋势分析)', 'knowledge', 'consulting', 42, TRUE, 'SEED', 1),
('RULE-INT-050', 'confirm', '(确认|同意|批准|执行|提交|没问题|就这样)', NULL, 'business', 45, TRUE, 'SEED', 1),
('RULE-INT-051', 'cancel', '(取消|放弃|不要了|算了|不了|不用了)', NULL, 'business', 45, TRUE, 'SEED', 1),
('RULE-INT-052', 'greeting', '(你好|您好|hi|hello|早上好|下午好|晚上好|在吗|哈喽)', 'knowledge', 'consulting', 46, TRUE, 'SEED', 1),
('RULE-INT-053', 'thanks', '(谢谢|感谢|多谢|thanks|辛苦了)', 'knowledge', 'consulting', 46, TRUE, 'SEED', 1),
('RULE-INT-054', 'farewell', '(再见|拜拜|bye|走了|回见)', 'knowledge', 'consulting', 46, TRUE, 'SEED', 1),
('RULE-INT-055', 'help', '(你是谁|你能做什么|功能|帮助|怎么用|有什么用)', 'knowledge', 'consulting', 46, TRUE, 'SEED', 1),
('RULE-INT-056', 'system', '(登录|切换|谁在用|退出|我是谁|当前用户|登出|注销)', 'knowledge', 'business', 47, TRUE, 'SEED', 1),
-- v6.93.1 T18：口语化库存状态问句（与 DEFAULT_RULES/SEED_RULES_FALLBACK 同步）
('RULE-INT-057', 'query_inventory', '库存.{0,6}(快见底|见底了|快没|快用完|快用光|还够吗|还够不够|够不够|够用吗|还多吗|充足吗|缺货|断货|会不会断|能撑|剩多少|还有多少)', 'warehouse', 'business', 31, TRUE, 'SEED', 1)
ON CONFLICT (rule_id) DO NOTHING;

-- ============================================================
-- 5. 意图规则变更审批链模板（rule_config_change）
--    变更（L2训练产出/LLM建议/人工修改）提交为待审批记录，
--    审批通过后回填 approval_id 并 enabled=TRUE / version+1 生效。
-- ============================================================
INSERT INTO workflow_configs (workflow_type, workflow_name, owner_dept, approval_chain, is_trained)
SELECT 'rule_config_change', '意图规则变更审批', 'system',
       '[{"step":1,"role":"manager","action":"审批"}]'::jsonb, FALSE
WHERE NOT EXISTS (
    SELECT 1 FROM workflow_configs WHERE workflow_type = 'rule_config_change'
);
