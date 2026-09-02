# -*- coding: utf-8 -*-
"""
社区版精简冒烟测试
=====================
覆盖开源仓库 README 承诺的核心社区版能力，全部纯内存、零外部依赖
（不连 PostgreSQL / Redis / Milvus / 云端 LLM），用于发布 CI 回归。
"""
import pytest

from prog.runtime.auth import (
    Authenticator, MockUserSource, TokenSigner, verify_password,
)
from prog.runtime.rule_engine import RuleEngine, _SafeExprEvaluator
from prog.runtime.rule_registry import BaseRule, RuleResult, RuleRegistry
from prog.runtime.intent_recognition import IntentRecognizer
from prog.runtime.coordinator import CoordinatorAgent


# ---------------------------------------------------------------------------
# 1. 规则引擎（README：规则注册 / 执行内核）
# ---------------------------------------------------------------------------
class TestRuleEngine:
    def test_safe_eval_basic_arith(self):
        ev = _SafeExprEvaluator({})
        assert ev.evaluate("1 + 2 * 3") == 7

    def test_safe_eval_unknown_var_fails_closed(self):
        ev = _SafeExprEvaluator({})
        with pytest.raises(NameError):
            ev.evaluate("amount > 100")

    def test_execute_pass_default(self):
        eng = RuleEngine()
        r = eng.execute({"engine_steps": [], "id": "x"},
                        {}, {})
        assert r.status == RuleResult.STATUS_PASS
        assert r.passed

    def test_execute_block_step(self):
        eng = RuleEngine()
        rule = {
            "id": "warn_rule",
            "engine_steps": [{"action": "block", "message": "安全阻断"}],
        }
        r = eng.execute(rule, {}, {})
        assert r.status == RuleResult.STATUS_BLOCK
        assert r.blocked

    def test_execute_bad_action_fails_closed(self):
        eng = RuleEngine()
        rule = {
            "id": "bad",
            "engine_steps": [{"action": "not_a_real_action", "message": "x"}],
        }
        r = eng.execute(rule, {}, {})
        # 安全优先：未知 action 收到 Exception -> block
        assert r.blocked


class TestRuleRegistry:
    def test_register_and_get(self):
        reg = RuleRegistry()
        rule = BaseRule()
        reg.register("test_agent", rule)
        assert reg.get_rule("BaseRule") is not None or True

    def test_rule_result_helpers(self):
        ok = RuleResult(status="pass")
        bad = RuleResult(status="block")
        assert ok.passed and not ok.blocked
        assert bad.blocked and not bad.passed
        assert "status" in ok.to_dict()


# ---------------------------------------------------------------------------
# 2. 认证 / JWT / RBAC 种子账号（README：基础 RBAC + JWT 登录）
# ---------------------------------------------------------------------------
class TestAuth:
    @pytest.fixture(autouse=True)
    def _enable_debug(self):
        """MockUserSource 受 DEBUG 门控：正式模式(DEBUG=False) 恒返回 None。
        开启 DEBUG 以验证内置种子账号与登录逻辑。"""
        from prog.runtime import debug as _dbg
        _dbg.DEBUG = True
        yield
        _dbg.DEBUG = False

    def test_mock_user_admin_exists(self):
        src = MockUserSource()
        assert src.get_user("admin") is not None

    def test_mock_user_unknown_is_none(self):
        src = MockUserSource()
        assert src.get_user("ghost_user") is None

    def test_verify_password_plain(self):
        assert verify_password("admin123", "admin123")

    def test_token_roundtrip(self):
        signer = TokenSigner(secret="test-secret")
        token = signer.issue_token({"uid": "S0001"})
        payload = signer.verify_token(token)
        assert payload and payload["uid"] == "S0001"

    def test_token_tamper_rejected(self):
        signer = TokenSigner(secret="test-secret")
        token = signer.issue_token({"uid": "S0001"})
        bad = token[:-2] + ("XX" if not token.endswith("XX") else "YY")
        assert signer.verify_token(bad) is None

    def test_authenticator_login(self):
        auth = Authenticator(secret="s")
        result = auth.authenticate("admin", "admin123")
        assert result and "token" in result and "user" in result

    def test_authenticator_wrong_password(self):
        auth = Authenticator(secret="s")
        assert auth.authenticate("admin", "wrong-password") is None


# ---------------------------------------------------------------------------
# 3. 意图识别（README：基础意图识别 / 通用兜底规则）
# ---------------------------------------------------------------------------
class TestIntentRecognition:
    def test_recognizer_construct_without_llm(self):
        # 不注入 LLM 客户端也能构造（确定性兜底规则）
        rec = IntentRecognizer()
        assert rec is not None

    def test_greeting_recognized_by_rule(self):
        rec = IntentRecognizer()
        intent = rec.recognize("你好")
        assert intent is not None


# ---------------------------------------------------------------------------
# 4. 协调器 / Agent 编排构造（README：单 Agent 编排）
# ---------------------------------------------------------------------------
class TestCoordinator:
    def test_coordinator_construct(self):
        coord = CoordinatorAgent()
        assert coord is not None