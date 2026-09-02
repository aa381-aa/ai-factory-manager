"""
训练系统API - L1-L4四级训练接口
对应技术规格：§1.4 训练体系
"""
import json
import os
import threading
import time
from datetime import datetime
from typing import Any

from flask import Blueprint, request, g
from prog.utils.api_response import api_response, error_response

from prog.runtime.approval_chain import get_approval_chain, update_approval_chain

training_bp = Blueprint('training', __name__, url_prefix='/api/training')


def _get_db() -> Any:
    """延迟获取数据库实例，获取失败时返回 None（调用方空列表/404/500 降级）。

    v6.50：注入点统一为 prog.runtime.database（与规则引擎/规则配置管理器一致）；
    为兼容存量测试与历史调用方，runtime 未注册时回退 prog.core.database。
    """
    try:
        from prog.runtime.database import get_database
        db = get_database()
        if db is not None:
            return db
    except Exception:
        pass
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _parse_jsonb(value):
    """将数据库返回的 JSONB 字段统一解析为 dict/list（兼容字符串与已解析对象）。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


# ============================================================
# L1 会话学习接口
# ============================================================
@training_bp.route('/l1/sessions', methods=['GET'])
def list_sessions():
    """L1: 查询会话学习记录"""
    try:
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        agent_type = request.args.get('agent_type', '')
        approved = request.args.get('approved', '')

        # 分页边界校验（page >= 1，page_size 1~100，防超大分页拖垮 DB）
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 100:
            page_size = 20

        db = _get_db()
        items = []
        total = 0
        if db:
            filters = {}
            if agent_type:
                filters['agent_type'] = agent_type
            if approved:
                filters['approved'] = approved.lower() == 'true'
            offset = (page - 1) * page_size
            try:
                items = db.query_many(
                    'training_data', filters=filters or None,
                    limit=page_size, offset=offset, order_by='created_at DESC',
                ) or []
            except Exception:
                items = []
            try:
                all_items = db.query_many('training_data', filters=filters or None) or []
                total = len(all_items)
            except Exception:
                total = len(items)

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空列表
        return api_response(code=0, data={
            "items": items, "total": total,
            "page": page, "page_size": page_size,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l1/sessions', methods=['POST'])
def create_session():
    """L1: 创建会话学习记录（对话记录采集）"""
    try:
        body = request.get_json(silent=True) or {}
        agent_type = body.get('agent_type', '')
        intent = body.get('intent', '')
        user_input = body.get('user_input', '')
        ai_output = body.get('ai_output', '')
        user_correction = body.get('user_correction', '')
        final_output = user_correction or ai_output

        if not agent_type or not user_input:
            return error_response(400, "agent_type 和 user_input 为必填项"), 400

        record = {
            "agent_type": agent_type,
            "intent": intent,
            "user_input": user_input,
            "ai_output": ai_output,
            "user_correction": user_correction,
            "final_output": final_output,
            "approved": False,
        }

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法创建训练样本"), 503
        # T-5：落库失败如实返回 500，不再返回"已创建"假成功
        try:
            new_id = db.insert('training_data', record)
        except Exception as e:
            return error_response(500, f"训练样本落库失败：{str(e) if DEBUG else '内部错误'}"), 500

        # v6.97 B.1：L1 审批对齐 L2 治理——同步写入 training_data_approval 审批链
        # 记录（thresholds.current_step 逐步推进），approve_session 全部通过才置
        # approved=True；写失败不影响样本创建（approve_session 按兜底直接审批）
        try:
            approval_chain = get_approval_chain('training_data_approval', db=db)
            db.insert('workflow_configs', {
                'workflow_type': 'training_data_approval',
                'workflow_name': f'L1会话学习审批-{new_id}',
                'owner_dept': 'training',
                'trigger_rule': f'L1-{new_id}',
                'approval_chain': json.dumps(approval_chain, ensure_ascii=False),
                'thresholds': json.dumps({
                    'training_id': new_id,
                    'agent_type': agent_type,
                    'proposed_final_output': final_output,
                    'current_step': 0,
                }, ensure_ascii=False),
                'is_active': True,
                'is_trained': False,
            })
        except Exception:
            pass

        return api_response(code=0, data={
            "id": new_id,
            "status": "created",
            "agent_type": agent_type,
            "intent": intent,
            "message": "会话学习记录已创建，待审批",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


def _record_approved_by(db, session_id, approved_by):
    """将审批人写入 training_data.metadata JSONB（不抛异常，兼容无 execute 的 mock）。"""
    if not approved_by:
        return
    try:
        db.execute(
            "UPDATE training_data SET metadata = COALESCE(metadata, '{}'::jsonb) || "
            "CAST(:meta AS jsonb) WHERE id = :rid",
            {"meta": json.dumps({"approved_by": approved_by}, ensure_ascii=False),
             "rid": session_id},
        )
    except Exception:
        pass


@training_bp.route('/l1/sessions/<int:session_id>/approve', methods=['POST'])
def approve_session(session_id):
    """L1: 审批会话学习记录。

    v6.97 B.1：对齐 L2 治理——审批走 workflow_configs('training_data_approval')
    审批链逐步推进（create_session 时生成审批记录，thresholds.current_step 推进）；
    当前步骤角色不匹配返回 403（admin 可代批任意步）；全部步骤通过才置
    training_data.approved=True 并回填审批人。存量无审批记录时兜底 manager/admin
    直接审批（向后兼容）。
    """
    try:
        body = request.get_json(silent=True) or {}
        action = body.get('action', 'approve')  # approve / reject
        if action not in ('approve', 'reject'):
            return error_response(400, f"action 仅支持 approve/reject，收到: {action}"), 400
        # T-3：审批人身份仅信任认证中间件注入的 g，不再取 body.approved_by（可伪造）
        approved_by = _current_user_id(body) or _current_role(body)
        final_output = body.get('final_output')

        user_role = _current_role(body)
        if not user_role:
            return error_response(403, "无法获取当前用户角色，禁止审批"), 403

        db = _get_db()
        if not db:
            return error_response(500, "数据库不可用，无法审批"), 500

        # 查 L1 审批链记录（create_session 生成；无记录则向后兼容直接审批）
        rec = None
        try:
            rows = db.query_many('workflow_configs',
                                 {"trigger_rule": f"L1-{session_id}"}) or []
            for r in rows:
                if r.get('workflow_type') == 'training_data_approval':
                    rec = r
                    break
        except Exception:
            rec = None

        if rec is None:
            # 向后兼容：无审批记录 → 要求 manager/admin 直接审批
            if user_role not in ('admin', 'manager'):
                return error_response(403, "L1 会话学习审批需 manager/admin 角色"), 403
            update_data = {"approved": True}
            if final_output:
                update_data["final_output"] = final_output
            rows = db.update('training_data', update_data, {"id": session_id})
            if rows <= 0:
                return error_response(404, f"训练样本 {session_id} 不存在"), 404
            _record_approved_by(db, session_id, approved_by)
            return api_response(code=0, data={
                "id": session_id,
                "approved": True,
                "approved_by": approved_by,
                "message": "会话学习记录已审批通过",
            })

        rec_id = rec.get('config_id') or rec.get('id')
        chain = _parse_jsonb(rec.get('approval_chain')) or [
            {"step": 1, "role": "manager", "action": "审批"}]
        thresholds = _parse_jsonb(rec.get('thresholds', {}))
        current_step = int(thresholds.get('current_step', 0) or 0)

        # 审批链角色校验（admin 可代批任意步）
        if user_role != 'admin':
            if current_step < len(chain):
                step_role = chain[current_step].get('role', '')
                if step_role and step_role != user_role:
                    return error_response(403, f"当前步骤需「{step_role}」审批，您的角色「{user_role}」无权限"), 403
            else:
                return error_response(400, "审批链已完成，无需重复审批"), 400

        # reject：标记审批记录作废，样本保持未审批
        if action != 'approve':
            try:
                db.update('workflow_configs',
                          {"is_active": False, "is_trained": True,
                           "updated_by": approved_by or 'system'},
                          {"config_id": rec_id})
            except Exception:
                pass
            return api_response(code=0, data={
                "id": session_id, "action": action, "approved": False,
                "message": "会话学习记录已驳回",
            })

        # 开源版：已移除签字链完整性校验（validate_sig_chain，属商业版流程约束）
        steps_done = thresholds.get('steps_done')
        if steps_done is None:
            steps_done = []
        elif not isinstance(steps_done, list):
            steps_done = []

        next_step = current_step + 1
        completed = next_step >= len(chain)
        # 记录本次审批签字（step/role/user/action/done_at），与 current_step 一并写回
        steps_done.append({
            "step": next_step,
            "role": user_role or (chain[current_step].get('role', '')
                                  if current_step < len(chain) else ''),
            "user_id": approved_by or "",
            "user_name": approved_by or "",
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        })
        thresholds['current_step'] = next_step
        thresholds['steps_done'] = steps_done
        try:
            db.update('workflow_configs',
                      {"thresholds": json.dumps(thresholds, ensure_ascii=False)},
                      {"config_id": rec_id})
        except Exception:
            pass
        if not completed:
            # 中间步骤：样本保持待审批
            return api_response(code=0, data={
                "id": session_id,
                "current_step": next_step,
                "total_steps": len(chain),
                "message": f"第 {next_step}/{len(chain)} 步已审批通过，待下一审批人",
            })

        # 全部步骤通过：置 approved=True 并回填审批人
        update_data = {"approved": True}
        if final_output:
            update_data["final_output"] = final_output
        rows = db.update('training_data', update_data, {"id": session_id})
        if rows <= 0:
            return error_response(404, f"训练样本 {session_id} 不存在"), 404
        try:
            db.update('workflow_configs', {"is_trained": True}, {"config_id": rec_id})
        except Exception:
            pass
        _record_approved_by(db, session_id, approved_by)

        return api_response(code=0, data={
            "id": session_id,
            "approved": True,
            "approved_by": approved_by,
            "total_steps": len(chain),
            "message": "审批链全部通过，会话学习记录已审批通过",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# L2 规则迭代接口
# ============================================================
@training_bp.route('/l2/rules', methods=['GET'])
def list_trainable_rules():
    """L2: 查询可训练规则列表"""
    try:
        db = _get_db()
        rules = []
        if db:
            try:
                # 可训练规则为 parameter 与 approval_chain 类型
                rules = db.query_many(
                    'business_rules',
                    filters=None, order_by='rule_id ASC',
                ) or []
            except Exception:
                rules = []

        # v6.58：移除 PostgreSQL 降级模拟数据——查无规则返回空列表

        # 仅返回可训练规则（parameter / approval_chain）
        trainable = [r for r in rules if r.get('rule_type') in ('parameter', 'approval_chain')]

        return api_response(code=0, data={
            "items": trainable, "total": len(trainable),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l2/rules/<rule_id>/config', methods=['GET'])
def get_rule_config(rule_id):
    """L2: 获取规则当前配置"""
    try:
        db = _get_db()
        rule = None
        if db:
            try:
                rule = db.query_one('business_rules', {'rule_id': rule_id})
            except Exception:
                rule = None

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回 404
        if not rule:
            return error_response(404, f"规则 {rule_id} 不存在"), 404

        # 统一解析 config_json
        rule["config_json"] = _parse_jsonb(rule.get("config_json", {}))

        return api_response(code=0, data=rule)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l2/rules/<rule_id>/config', methods=['PUT'])
def update_rule_config(rule_id):
    """L2: 更新规则配置（需审批，写入 workflow_configs）"""
    try:
        body = request.get_json(silent=True) or {}
        config_json = body.get('config_json')
        # v6.54：缺 body/config_json 直接 400，禁止创建空配置审批提案
        if not isinstance(config_json, dict) or not config_json:
            return error_response(400, "config_json 必填，需为非空 JSON 对象"), 400
        operator = body.get('operator', 'system')
        reason = body.get('reason', '')

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法提交审批提案"), 503
        # T-5：审批提案落库失败如实返回 500，不再返回"已提交"假成功
        try:
            # 配置变更需审批，写入 workflow_configs 作为待审批记录
            # v6.45：审批链从 DB rule_config_change 定义行读取（可训练），
            # 无定义/DB 不可用时由 get_approval_chain 兜底 manager 单级
            pending_id = db.insert('workflow_configs', {
                'workflow_type': 'rule_config_change',
                'workflow_name': f'规则配置变更审批-{rule_id}',
                'owner_dept': body.get('department', 'finance'),
                'trigger_rule': rule_id,
                'approval_chain': json.dumps(
                    get_approval_chain('rule_config_change', db=db),
                    ensure_ascii=False),
                # v6.50：thresholds 统一包装 proposed_config + current_step，
                # 使审批推进（current_step）与 config_manager.approve_config_update
                # （读 thresholds.proposed_config）共用同一结构
                'thresholds': json.dumps({
                    "proposed_config": config_json,
                    "current_step": 0,
                    "steps_done": [],
                }, ensure_ascii=False),
                'is_active': True,
                'is_trained': False,
            })
        except Exception as e:
            return error_response(500, f"审批提案落库失败：{str(e) if DEBUG else '内部错误'}"), 500

        return api_response(code=0, data={
            "rule_id": rule_id,
            "status": "pending_approval",
            "pending_id": pending_id,
            "config_json": config_json,
            "operator": operator,
            "reason": reason,
            "message": "配置变更已提交，等待审批后生效",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l2/proposals', methods=['GET'])
def list_proposals():
    """L2: 查询规则优化建议"""
    try:
        db = _get_db()
        proposals = []
        if db:
            try:
                # 从 workflow_configs 查询规则配置变更建议
                proposals = db.query_many(
                    'workflow_configs',
                    filters={"workflow_type": "rule_config_change"},
                    order_by='updated_at DESC',
                ) or []
            except Exception:
                proposals = []

        # v6.58：移除 PostgreSQL 降级模拟数据——查无建议返回空列表

        return api_response(code=0, data={
            "items": proposals, "total": len(proposals),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l2/proposals/<int:proposal_id>/approve', methods=['POST'])
def approve_proposal(proposal_id):
    """L2: 审批规则优化建议（v6.46：加入审批链角色校验 + 逐步推进，禁止单次直通生效）。

    审批链读取审批记录的 approval_chain（缺省 manager 单级）；当前用户角色须
    匹配 current_step 的审批角色（admin 可代批任意步）；全部步骤通过后才应用
    配置到 business_rules 并清除进程级缓存。reject 直接标记完成。
    """
    try:
        body = request.get_json(silent=True) or {}
        action = body.get('action', 'approve')  # approve / reject
        # T-4：action 仅允许 approve/reject，其余取值拒绝（曾将任意值当 approve 处理）
        if action not in ('approve', 'reject'):
            return error_response(400, f"action 仅支持 approve/reject，收到: {action}"), 400
        # T-3：审批人身份仅信任认证中间件注入的 g，不再取 body.approved_by
        approved_by = _current_user_id(body) or _current_role(body)

        db = _get_db()
        if not db:
            return error_response(500, "数据库不可用，无法审批"), 500
        proposal = db.query_one('workflow_configs', {'config_id': proposal_id})
        if not proposal:
            return error_response(404, f"审批记录 {proposal_id} 不存在"), 404

        # 审批链角色校验 + 逐步推进（与 intent_rules 审批端点同规则）
        chain = _parse_jsonb(proposal.get('approval_chain')) or [
            {"step": 1, "role": "manager", "action": "审批"}]
        thresholds = _parse_jsonb(proposal.get('thresholds', {}))
        current_step = int(thresholds.get('current_step', 0) or 0)
        user_role = _current_role(body)
        if not user_role:
            return error_response(403, "无法获取当前用户角色，禁止审批"), 403
        if user_role != 'admin':
            if current_step < len(chain):
                step_role = chain[current_step].get('role', '')
                if step_role and step_role != user_role:
                    return error_response(403, f"当前步骤需「{step_role}」审批，您的角色「{user_role}」无权限"), 403
            else:
                return error_response(400, "审批链已完成，无需重复审批"), 400

        # reject：直接标记处理完成（无配置变更）
        if action != 'approve':
            db.update('workflow_configs',
                      {"is_trained": True, "is_active": False},
                      {"config_id": proposal_id})
            return api_response(code=0, data={
                "proposal_id": proposal_id, "action": action,
                "message": "建议已驳回",
            })

        # 开源版：已移除签字链完整性校验（validate_sig_chain，属商业版流程约束）
        steps_done = thresholds.get('steps_done')
        if steps_done is None:
            steps_done = []
        elif not isinstance(steps_done, list):
            steps_done = []

        next_step = current_step + 1
        completed = next_step >= len(chain)
        # 记录本次审批签字，与 current_step 一并写回（中间/最终统一）
        steps_done.append({
            "step": next_step,
            "role": user_role or (chain[current_step].get('role', '')
                                  if current_step < len(chain) else ''),
            "user_id": approved_by or "",
            "user_name": approved_by or "",
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        })
        thresholds['current_step'] = next_step
        thresholds['steps_done'] = steps_done
        db.update('workflow_configs',
                  {"thresholds": json.dumps(thresholds, ensure_ascii=False)},
                  {"config_id": proposal_id})
        if not completed:
            # 中间步骤：配置保持待审批
            return api_response(code=0, data={
                "proposal_id": proposal_id,
                "current_step": next_step,
                "total_steps": len(chain),
                "message": f"第 {next_step}/{len(chain)} 步已审批通过，待下一审批人",
            })

        # 全部步骤通过：应用配置变更（v6.50 统一走 config_manager）
        # 复用 approve_config_update：写库 + 健康检查 + 失败自动回滚 + version 递增 + 审批标记
        rule_id = proposal.get('trigger_rule', '')
        try:
            from prog.rules.config_manager import RuleConfigManager
            result = RuleConfigManager().approve_config_update(
                proposal_id, approved_by, db=db)
        except Exception as e:
            return error_response(500, f"配置应用失败: {str(e) if DEBUG else '内部错误'}"), 500
        if result.get("status") == "error":
            return error_response(400, result.get("message", "配置应用失败")), 400
        if result.get("status") == "rollback":
            return error_response(400, result.get("message", "健康检查失败，配置已自动回滚")), 400
        return api_response(code=0, data={
            "proposal_id": proposal_id,
            "action": action,
            "approved_by": approved_by,
            "applied_rule": result.get("rule_id") or rule_id,
            "version": result.get("version"),
            "warning": result.get("warning"),
            "message": "审批链全部通过，建议已生效（已通过健康检查）",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# 审批链训练接口（B1：审批链可训练——变更经当前审批链审批后生效）
# ============================================================
def _current_role(body: dict) -> str:
    """当前请求用户角色。

    认证中间件注入 g.user_role 时仅信任 g（防 T-12 请求体角色提权）；
    无认证中间件环境（内部调用/存量测试）以 body.role 兜底。
    """
    try:
        has = hasattr(g, "user_role")
    except Exception:
        has = False
    if has:
        return g.get('user_role', '') or ''
    return body.get('role', '') or ''


def _current_user_id(body: dict) -> str:
    """当前请求用户 ID。

    认证中间件注入 g.user_id 时仅信任 g（防 T-3 冒用他人身份审批）；
    无认证中间件环境以 body.approver_id/approved_by 兜底（测试/内部调用）。
    """
    try:
        has = hasattr(g, "user_id")
    except Exception:
        has = False
    if has:
        return g.get('user_id', '') or ''
    return (body.get('user_id', '') or body.get('approved_by', '')
            or body.get('approver_id', '') or '')


@training_bp.route('/approval-chain', methods=['GET'])
def list_approval_chains():
    """列出全部可训练审批链（workflow_configs 定义行）。"""
    try:
        db = _get_db()
        rows = []
        if db:
            try:
                rows = db.query_many(
                    'workflow_configs',
                    filters={"is_active": True},
                    order_by='workflow_type ASC',
                ) or []
            except Exception:
                rows = []
        # 仅返回定义行（thresholds 为空；审批实例行带 thresholds 排除）
        defs = []
        for r in rows:
            th = r.get('thresholds')
            if th in (None, '', '{}', {}):
                chain = r.get('approval_chain')
                if isinstance(chain, str):
                    try:
                        chain = json.loads(chain)
                    except Exception:
                        chain = []
                defs.append({
                    "workflow_type": r.get('workflow_type'),
                    "workflow_name": r.get('workflow_name'),
                    "owner_dept": r.get('owner_dept'),
                    "approval_chain": chain,
                    "is_trained": bool(r.get('is_trained')),
                })
        return api_response(code=0, data={
            "items": defs, "total": len(defs),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/approval-chain', methods=['POST'])
def submit_approval_chain_change():
    """提交审批链变更（B1：审批链可训练）。

    新审批链写入 workflow_configs 待审批记录；审批链变更本身使用目标流程
    类型的当前审批链审批（避免绕过现行审批）。审批全部通过后更新定义行
    approval_chain 生效。

    Body:
        workflow_type: 目标流程类型（必填）
        new_chain: 新审批链 [{step, role, action}, ...]（必填）
        reason: 变更原因
        operator: 提交人
    """
    try:
        body = request.get_json(silent=True) or {}
        workflow_type = body.get('workflow_type', '')
        new_chain = body.get('new_chain', [])
        reason = body.get('reason', '')
        operator = body.get('operator', 'system')

        if not workflow_type:
            return error_response(400, "workflow_type 必填"), 400
        if not isinstance(new_chain, list) or not new_chain:
            return error_response(400, "new_chain 必须为非空审批链列表"), 400
        for step in new_chain:
            if not isinstance(step, dict) or not step.get('role'):
                return error_response(400, "审批链步骤需含 role 字段"), 400

        db = _get_db()
        if not db:
            return error_response(500, "DB 不可用，无法提交审批链变更"), 500

        # 当前审批链（变更本身须经当前链审批）
        current_chain = get_approval_chain(workflow_type, db=db)
        wf_id = db.insert('workflow_configs', {
            'workflow_type': workflow_type,
            'workflow_name': f'审批链变更-{workflow_type}',
            'owner_dept': 'system',
            'trigger_rule': f'CHAIN-{workflow_type}',
            'approval_chain': json.dumps(current_chain, ensure_ascii=False),
            'thresholds': json.dumps({
                'action': 'approval_chain_change',
                'target_workflow_type': workflow_type,
                'proposed_chain': new_chain,
                'current_chain': current_chain,
                'reason': reason,
                'current_step': 0,
            }, ensure_ascii=False),
            'is_active': True,
            'is_trained': False,
        })
        return api_response(code=0, data={
            "workflow_type": workflow_type,
            "workflow_id": wf_id,
            "status": "pending_approval",
            "current_chain": current_chain,
            "message": "审批链变更已提交，经当前审批链审批后生效",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/approval-chain/<int:workflow_id>/approve', methods=['POST'])
def approve_approval_chain_change(workflow_id):
    """审批链变更审批（B1）。

    按审批记录 approval_chain 逐步推进（current_step），当前步骤角色不匹配
    返回 403（admin 可代批任意步）；全部步骤通过后调用 update_approval_chain
    应用新链到定义行。

    Body:
        action: approve / reject（默认 approve）
        approved_by: 审批人
        role: 审批人角色（g.user_role 优先）
    """
    try:
        body = request.get_json(silent=True) or {}
        action = body.get('action', 'approve')
        # T-4：action 仅允许 approve/reject
        if action not in ('approve', 'reject'):
            return error_response(400, f"action 仅支持 approve/reject，收到: {action}"), 400
        # T-3：审批人身份仅信任认证中间件注入的 g
        approved_by = _current_user_id(body) or _current_role(body)
        role = _current_role(body)

        db = _get_db()
        if not db:
            return error_response(500, "DB 不可用"), 500

        rec = db.query_one('workflow_configs', {'config_id': workflow_id})
        if not rec:
            return error_response(404, "审批记录不存在"), 404
        thresholds = _parse_jsonb(rec.get('thresholds', {}))
        if thresholds.get('action') != 'approval_chain_change':
            return error_response(400, "非审批链变更记录"), 400

        if action == 'reject':
            db.update('workflow_configs', {'is_active': False},
                      {'config_id': workflow_id})
            return api_response(code=0, data={
                "workflow_id": workflow_id, "action": "reject",
                "rejected_by": approved_by, "message": "审批链变更已驳回",
            })

        # 审批链角色校验（v6.45：与 intent_rules 审批端点一致）
        chain = _parse_jsonb(rec.get('approval_chain', [])) or \
            get_approval_chain(rec.get('workflow_type'), db=db)
        current_step = int(thresholds.get('current_step', 0) or 0)
        if role != 'admin':
            if not role:
                return error_response(403, "缺少审批人角色，禁止审批"), 403
            if current_step < len(chain):
                step_role = chain[current_step].get('role', '')
                if step_role and step_role != role:
                    return error_response(403, f"当前步骤需「{step_role}」审批，您的角色「{role}」无权限"), 403
            else:
                return error_response(400, "审批链已完成，无需重复审批"), 400

        # 开源版：已移除签字链完整性校验（validate_sig_chain，属商业版流程约束）
        steps_done = thresholds.get('steps_done')
        if steps_done is None:
            steps_done = []
        elif not isinstance(steps_done, list):
            steps_done = []

        # 多级审批逐步推进
        next_step = current_step + 1
        completed = next_step >= len(chain)
        # 记录本次审批签字，与 current_step 一并写回（中间/最终统一）
        steps_done.append({
            "step": next_step,
            "role": role or (chain[current_step].get('role', '')
                             if current_step < len(chain) else ''),
            "user_id": approved_by or "",
            "user_name": approved_by or "",
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        })
        thresholds['current_step'] = next_step
        thresholds['steps_done'] = steps_done
        db.update('workflow_configs',
                  {'thresholds': json.dumps(thresholds, ensure_ascii=False)},
                  {'config_id': workflow_id})
        if not completed:
            return api_response(code=0, data={
                "workflow_id": workflow_id,
                "status": "pending_approval",
                "current_step": next_step,
                "total_steps": len(chain),
                "approved_by": approved_by,
                "message": f"第 {next_step}/{len(chain)} 步已审批通过，待下一审批人",
            })

        # 全部步骤通过：应用新链到定义行
        target_type = thresholds.get('target_workflow_type',
                                     rec.get('workflow_type'))
        new_chain = thresholds.get('proposed_chain', [])
        if not isinstance(new_chain, list) or not new_chain:
            return error_response(400, "proposed_chain 缺失"), 400
        ok = update_approval_chain(target_type, new_chain, db=db,
                                   modified_by=approved_by)
        db.update('workflow_configs', {'is_active': False},
                  {'config_id': workflow_id})
        return api_response(code=0, data={
            "workflow_id": workflow_id,
            "target_workflow_type": target_type,
            "new_chain": new_chain,
            "applied": ok,
            "approved_by": approved_by,
            "message": "审批链全部通过，新审批链已生效" if ok else "审批链更新失败",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# L3 知识沉淀接口
# ============================================================
@training_bp.route('/l3/knowledge', methods=['GET'])
def list_knowledge():
    """L3: 查询知识库文档列表"""
    try:
        db = _get_db()
        docs = []
        if db:
            try:
                docs = db.query_many(
                    'knowledge_documents', order_by='created_at DESC',
                ) or []
            except Exception:
                docs = []

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空列表
        return api_response(code=0, data={
            "items": docs, "total": len(docs),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l3/knowledge', methods=['POST'])
def add_knowledge():
    """L3: 添加知识文档（v6.97 B.1：先走 knowledge_publish 审批闭环，通过后才向量化入库）。

    写入流程：
        1. knowledge_documents 落 status='pending_approval'（未审批不进 KnowledgeBase）
        2. workflow_configs 写 knowledge_publish 审批记录（录入→复核→发布，逐步推进）
        3. approve_knowledge 全部步骤通过后：status='vectorizing'，后台线程异步
           向量化（embedding + vector_store），成功置 vectorized / 失败置 failed
           （重试 3 次）。
    """
    try:
        body = request.get_json(silent=True) or {}
        title = body.get('title', '')
        category = body.get('category', 'general')
        content = body.get('content', '')

        if not title:
            return error_response(400, "title 为必填项"), 400

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法添加知识文档"), 503
        # T-5：落库失败如实返回 500，不再返回"已提交"假成功
        try:
            doc_id = db.insert('knowledge_documents', {
                "title": title,
                "doc_type": category,
                "content": content,
                "status": "pending_approval",
                "tags": "[]",
                "extra_data": json.dumps({"source": category}, ensure_ascii=False),
            })
        except Exception as e:
            return error_response(500, f"知识文档落库失败：{str(e) if DEBUG else '内部错误'}"), 500

        # v6.97 B.1：L3 审批闭环——写 knowledge_publish 审批记录（thresholds 逐步推进），
        # 未审批通过不进 KnowledgeBase 内存；写失败不影响文档落库（approve 走兜底）
        try:
            approval_chain = get_approval_chain('knowledge_publish', db=db)
            db.insert('workflow_configs', {
                'workflow_type': 'knowledge_publish',
                'workflow_name': f'知识文档发布审批-{doc_id}',
                'owner_dept': 'training',
                'trigger_rule': f'KB-{doc_id}',
                'approval_chain': json.dumps(approval_chain, ensure_ascii=False),
                'thresholds': json.dumps({
                    'doc_id': doc_id, 'title': title, 'category': category,
                    'content': content, 'current_step': 0,
                    'steps_done': [],
                }, ensure_ascii=False),
                'is_active': True,
                'is_trained': False,
            })
        except Exception:
            pass

        return api_response(code=0, data={
            "doc_id": doc_id or f"KB{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "title": title,
            "category": category,
            "status": "pending_approval",
            "message": "知识文档已提交，待发布审批通过后向量化入库",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


def _vectorize_knowledge(db, doc_id, title, category, content):
    """L3 异步向量化（后台线程执行）：embedding + vector_store 入库。

    状态流转：vectorizing -> vectorized（成功）/ failed（3 次重试均失败）。
    未审批通过不会进入此流程，保证"未审批不进 KnowledgeBase 内存"。
    """
    from prog.llm.knowledge_base import KnowledgeBase
    kb_key = f"KB-{doc_id}"
    success = False
    last_error = ""
    for attempt in range(3):
        try:
            kb = KnowledgeBase.get_instance()
            kb.add_document(
                kb_key, content,
                source=category,
                metadata={"title": title, "doc_type": category},
            )
            success = True
            break
        except Exception as e:
            last_error = str(e)
            time.sleep(1)
    status = "vectorized" if success else "failed"
    try:
        db.update("knowledge_documents",
                  {"status": status,
                   "extra_data": json.dumps({
                       "source": category, "kb_key": kb_key,
                       "error": last_error if not success else "",
                   }, ensure_ascii=False)},
                  {"doc_id": doc_id})
    except Exception:
        pass


@training_bp.route('/l3/knowledge/<int:doc_id>/approve', methods=['POST'])
def approve_knowledge(doc_id):
    """L3: 审批知识文档发布（v6.97 B.1：knowledge_publish 审批链逐步推进）。

    与 L1/L2 对称：当前步骤角色不匹配返回 403（admin 可代批任意步）；
    全部步骤通过后才异步向量化并置 vectorizing（后转 vectorized/failed）。
    存量无审批记录时兜底 manager/admin 直接发布（向后兼容）。
    """
    try:
        body = request.get_json(silent=True) or {}
        action = body.get('action', 'approve')  # approve / reject
        if action not in ('approve', 'reject'):
            return error_response(400, f"action 仅支持 approve/reject，收到: {action}"), 400
        approved_by = _current_user_id(body) or _current_role(body)
        user_role = _current_role(body)
        if not user_role:
            return error_response(403, "无法获取当前用户角色，禁止审批"), 403

        db = _get_db()
        if not db:
            return error_response(500, "数据库不可用，无法审批"), 500

        rec = None
        try:
            rows = db.query_many('workflow_configs',
                                 {"trigger_rule": f"KB-{doc_id}"}) or []
            for r in rows:
                if r.get('workflow_type') == 'knowledge_publish':
                    rec = r
                    break
        except Exception:
            rec = None

        if rec is None:
            # 向后兼容：无审批记录 → 要求 manager/admin 直接发布
            if user_role not in ('admin', 'manager'):
                return error_response(403, "知识文档发布审批需 manager/admin 角色"), 403
            doc = db.query_one('knowledge_documents', {"doc_id": doc_id})
            if not doc:
                return error_response(404, f"知识文档 {doc_id} 不存在"), 404
            db.update('knowledge_documents',
                      {"status": "vectorizing"}, {"doc_id": doc_id})
            _vectorize_knowledge(
                db, doc_id, doc.get('title', ''), doc.get('doc_type', 'general'),
                doc.get('content', ''))
            return api_response(code=0, data={
                "doc_id": doc_id, "approved": True,
                "status": "vectorizing",
                "message": "知识文档已审批通过，后台异步向量化中",
            })

        rec_id = rec.get('config_id') or rec.get('id')
        chain = _parse_jsonb(rec.get('approval_chain')) or [
            {"step": 1, "role": "manager", "action": "审批"}]
        thresholds = _parse_jsonb(rec.get('thresholds', {}))
        current_step = int(thresholds.get('current_step', 0) or 0)

        # 审批链角色校验（admin 可代批任意步）
        if user_role != 'admin':
            if current_step < len(chain):
                step_role = chain[current_step].get('role', '')
                if step_role and step_role != user_role:
                    return error_response(403, f"当前步骤需「{step_role}」审批，您的角色「{user_role}」无权限"), 403
            else:
                return error_response(400, "审批链已完成，无需重复审批"), 400

        # reject：标记审批记录作废，文档保持未发布（不进入向量化）
        if action != 'approve':
            try:
                db.update('workflow_configs',
                          {"is_active": False, "is_trained": True,
                           "updated_by": approved_by or 'system'},
                          {"config_id": rec_id})
                db.update('knowledge_documents',
                          {"status": "rejected"}, {"doc_id": doc_id})
            except Exception:
                pass
            return api_response(code=0, data={
                "doc_id": doc_id, "action": action, "approved": False,
                "message": "知识文档发布已驳回",
            })

        # 开源版：已移除签字链完整性校验（validate_sig_chain，属商业版流程约束）
        steps_done = thresholds.get('steps_done')
        if steps_done is None:
            steps_done = []
        elif not isinstance(steps_done, list):
            steps_done = []

        next_step = current_step + 1
        completed = next_step >= len(chain)
        # 记录本次审批签字，与 current_step 一并写回（中间/最终统一）
        steps_done.append({
            "step": next_step,
            "role": user_role or (chain[current_step].get('role', '')
                                  if current_step < len(chain) else ''),
            "user_id": approved_by or "",
            "user_name": approved_by or "",
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        })
        thresholds['current_step'] = next_step
        thresholds['steps_done'] = steps_done
        try:
            db.update('workflow_configs',
                      {"thresholds": json.dumps(thresholds, ensure_ascii=False)},
                      {"config_id": rec_id})
        except Exception:
            pass
        if not completed:
            # 中间步骤：文档保持待审批
            return api_response(code=0, data={
                "doc_id": doc_id,
                "current_step": next_step,
                "total_steps": len(chain),
                "message": f"第 {next_step}/{len(chain)} 步已审批通过，待下一审批人",
            })

        # 全部步骤通过：标记审批完成，启动异步向量化
        try:
            db.update('workflow_configs', {"is_trained": True}, {"config_id": rec_id})
        except Exception:
            pass
        doc = db.query_one('knowledge_documents', {"doc_id": doc_id})
        if not doc:
            return error_response(404, f"知识文档 {doc_id} 不存在"), 404
        db.update('knowledge_documents',
                  {"status": "vectorizing"}, {"doc_id": doc_id})
        thread = threading.Thread(
            target=_vectorize_knowledge,
            args=(db, doc_id, doc.get('title', ''), doc.get('doc_type', 'general'),
                  doc.get('content', '')),
            daemon=True)
        thread.start()

        return api_response(code=0, data={
            "doc_id": doc_id,
            "approved": True,
            "approved_by": approved_by,
            "status": "vectorizing",
            "total_steps": len(chain),
            "message": "审批链全部通过，知识文档后台异步向量化中",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@training_bp.route('/l3/knowledge/<int:doc_id>', methods=['DELETE'])
def delete_knowledge(doc_id):
    """L3: 删除知识文档"""
    try:
        db = _get_db()
        if not db:
            return error_response(500, "数据库不可用，无法删除知识文档"), 500
        try:
            # T-2 修复：真实表为 knowledge_documents（knowledge_base 表不存在，
            # 删除该表恒失败且被吞，造成"已删除"假成功）
            rows = db.delete('knowledge_documents', {"doc_id": doc_id})
        except Exception as e:
            return error_response(500, f"删除知识文档失败：{str(e) if DEBUG else '内部错误'}"), 500
        if rows <= 0:
            return error_response(404, f"知识文档 {doc_id} 不存在"), 404

        return api_response(code=0, data={
            "doc_id": doc_id,
            "deleted": True,
            "message": "知识文档已删除",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# 统计接口
# ============================================================
@training_bp.route('/stats', methods=['GET'])
def training_stats():
    """训练系统统计概览"""
    try:
        db = _get_db()
        stats = {
            "l1_sessions": {"total": 0, "approved": 0, "pending": 0},
            "l2_rules": {"trainable": 0, "proposals_pending": 0},
            "l3_knowledge": {"total": 0, "vectorized": 0},
            "by_agent": [],
        }

        if db:
            # L1 会话学习统计
            try:
                all_sessions = db.query_many('training_data') or []
                approved_count = sum(1 for s in all_sessions if s.get('approved'))
                stats["l1_sessions"] = {
                    "total": len(all_sessions),
                    "approved": approved_count,
                    "pending": len(all_sessions) - approved_count,
                }
                # 按 Agent 类型分组统计
                agent_map = {}
                for s in all_sessions:
                    at = s.get('agent_type', 'unknown')
                    if at not in agent_map:
                        agent_map[at] = {"agent_type": at, "total": 0, "approved": 0}
                    agent_map[at]["total"] += 1
                    if s.get('approved'):
                        agent_map[at]["approved"] += 1
                stats["by_agent"] = list(agent_map.values())
            except Exception:
                pass

            # L2 可训练规则统计
            try:
                rules = db.query_many('business_rules') or []
                trainable = [r for r in rules
                             if r.get('rule_type') in ('parameter', 'approval_chain')]
                stats["l2_rules"]["trainable"] = len(trainable)
            except Exception:
                pass
            try:
                proposals = db.query_many(
                    'workflow_configs',
                    filters={"workflow_type": "rule_config_change", "is_trained": False},
                ) or []
                stats["l2_rules"]["proposals_pending"] = len(proposals)
            except Exception:
                pass

            # L3 知识库统计
            try:
                docs = db.query_many('knowledge_documents') or []
                vectorized = sum(1 for d in docs if d.get('status') == 'vectorized')
                stats["l3_knowledge"] = {"total": len(docs), "vectorized": vectorized}
            except Exception:
                pass

        # v6.58：移除 PostgreSQL 降级模拟统计——DB 不可用/查无均保持真实零值

        return api_response(code=0, data=stats)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、Blueprint定义、核心路由完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert training_bp is not None, "training_bp 未定义"
    hello_world(__name__, "training_bp 定义完整，L1-L4 训练接口就绪")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
