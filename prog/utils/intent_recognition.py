"""
意图识别（业务软件层 re-export）
================================
框架能力：IntentRecognizer 意图识别（规则优先 + LLM 兜底、多意图消歧、反馈修正
规则动态注入）与 Intent 结构由AI工厂管家框架运行时（prog/runtime）提供。
业务侧 INT-01~29 意图规则表（intent_rules 表
变量化配置）见附录 A.8。本文件仅作 re-export。
"""
from prog.runtime.intent_recognition import IntentRecognizer, Intent

__all__ = ["IntentRecognizer", "Intent"]
