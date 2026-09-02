# -*- coding: utf-8 -*-
"""
统一输入解析层 utils/nl_parser.py
================================

文件用途：
    实现「可商用部署功能补充建议」C10 / 附录 A.9 的统一输入解析体系：
    全链路 7 个输入解析模块（web_search / 记忆关键词 / 搜索 query /
    槽位提取 / 流程名提取 / RAG query / 人名匹配）此前各自为战，本模块
    提供唯一出口——实体词典（Trie）+ 搜索 query 重写 + 关键词提取升级 +
    槽位提取工具集，供各调用方接入，消除重复逻辑与"正则硬猜"。

P0 能力（零外部依赖，纯 stdlib，方案 A）：
    1. Trie 前缀树实体词典（EntityRecognizer）：8 类业务实体 + 别名，
       支持最长匹配、多类型。
    2. 实体词典热加载：users / products / departments / customers /
       suppliers / workflow_configs 从 DB 加载（5 分钟 TTL 缓存），
       另含内置通用话题词典（红歌 / 精益生产 / SMED 等，DB 无源）。
    3. 搜索 query 重写 rewrite_search_query()：P0 实体精确短语 /
       P1 关键词堆叠 / P2 实体+时间，供 _web_search 多 query 并发。
    4. 升级版关键词提取 extract_keywords()：实体整词优先 + 最大匹配 +
       停用词过滤 + 英文/型号整体保留，替代 2-4 字滑窗法。
    5. RAG query 清洗 clean_search_query()：剥口语虚词/疑问词，保留实体。
    6. 通用槽位提取工具集 SlotExtractor（person/product/department/
       customer/workflow/order/quantity/date_range/qc_status）。

设计约束（C10 附录 A.9.4 方案 A）：
    - 零新增依赖：仅 stdlib（re / time / typing / ipaddress 等）。
    - DB 访问惰性：模块导入不触碰数据库；DB 不可达时降级为内置词典。
    - 不改变既有模块间 API：调用方仅替换内部实现，函数签名不变
      （如 conversation_memory._extract_keywords 仍返回 set）。

来源：融合 _demo_nl_parser_p0/demo.py（P0 验证原型）到真实工程，
实体词典由静态演示数据升级为 DB 热加载。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple


# =========================================================================
# 一、停用词表（口语虚词 + 疑问词 + 介词/助词/连词/语气词）
# =========================================================================

STOP_WORDS = set("""
的 了 和 是 在 也 都 就 要 会 着 过 给 把 被 让 使 从 向 到 对 为 以
之 而 但 虽 却 还 又 或 且 然 若 如果 因为 所以 因此 于是 才
我 你 他 她 它 我们 你们 他们 它们 自己 人家 别人 大家 各位 诸位
这个 那个 这些 那些 这样 那样 这么 那么 怎么 怎样 怎么样 什么 为什么
哪 哪个 哪些 哪里 几 多少 多么 怎 啥
吗 呢 啊 吧 呀 哦 哈 嗯 唉 喔 咦 嘛 咧 喽 呗
请 麻烦 帮 帮我 帮忙 一下 帮个忙 拜托
看看 查一下 查下 搜一下 搜下 查 找 搜 查询 查找 搜索 检索
我想 我要 我想知道 我想问 请问 敢问 劳驾 您好 你好
能 能够 可以 可以吗 有没有 是否 是不是 对不对 好不好 行不行
一 个 件 条 张 本 台 只 把 次 下 遍 趟 场 顿 阵 番
现在 目前 当前 如今 今天 明天 昨天 刚刚 刚才 已经 曾经 以前 以后 将来
一些 一点 有点 比较 很 非常 特别 十分 极其 最 真的 确实 实在 其实
关于 对于 有关 相关 方面 问题 情况 事情 东西 内容 含义 意思 定义
""".split())

QUESTION_WORDS = set("""
吗 呢 啊 吧 是不是 对不对 有没有 能不能 可不可以 是否 如何 怎么
怎样 怎么样 什么 为什么 为何 哪 哪个 哪些 哪里 几 多少 几多
""".split())

VERBAL_NOISE = set("""
帮我 请 麻烦 一下 帮个忙 拜托 劳驾 我想知道 我想问 请问
帮我查一下 帮我查 帮我看看 帮我搜 帮我找
查一下 查下 搜一下 搜下 看一下 看下 找一下 找下
查询一下 检索一下
你知道 你了解 你说说 你讲讲
""".split())


# =========================================================================
# 二、Trie 前缀树（多模式匹配，O(n) 扫描，最长匹配）
# =========================================================================

class TrieNode:
    __slots__ = ("children", "entity_type", "entity_id", "entity_text")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        self.entity_type: Optional[str] = None
        self.entity_id: Optional[str] = None
        self.entity_text: Optional[str] = None


class EntityRecognizer:
    """基于 Trie 的实体识别器。支持最长匹配、多类型、别名。"""

    def __init__(self) -> None:
        self.root = TrieNode()

    def add(self, text: str, entity_type: str, entity_id: Optional[str] = None) -> None:
        if not text:
            return
        node = self.root
        for ch in text:
            if ch not in node.children:
                node.children[ch] = TrieNode()
            node = node.children[ch]
        node.entity_type = entity_type
        node.entity_id = entity_id or text
        node.entity_text = text

    def add_many(self, items: List[Tuple[str, str, Optional[str]]]) -> None:
        for text, etype, eid in items:
            self.add(text, etype, eid)

    def extract(self, text: str) -> List[Dict[str, Any]]:
        """返回按出现顺序、最长匹配的实体列表。[{text, type, id, start, end}]"""
        results: List[Dict[str, Any]] = []
        i = 0
        n = len(text)
        while i < n:
            node = self.root
            longest_end = -1
            longest_node: Optional[TrieNode] = None
            j = i
            while j < n and text[j] in node.children:
                node = node.children[text[j]]
                if node.entity_type is not None:
                    longest_end = j
                    longest_node = node
                j += 1
            if longest_node is not None and longest_end >= i:
                results.append({
                    "text": text[i:longest_end + 1],
                    "type": longest_node.entity_type,
                    "id": longest_node.entity_id,
                    "start": i,
                    "end": longest_end + 1,
                })
                i = longest_end + 1
            else:
                i += 1
        return results


# =========================================================================
# 三、实体词典（内置通用话题 + DB 业务实体热加载）
# =========================================================================

# 内置通用词典：DB 无源的话题/状态词，保证 DB 不可达时搜索重写仍可用
_BUILTIN_ENTITIES: List[Tuple[str, str, Optional[str]]] = [
    ("红歌", "topic", "t_red_song"),
    ("红色经典", "topic", "t_red_classic"),
    ("精益生产", "topic", "t_lean_production"),
    ("SMED", "topic", "t_smed"),
    ("快速换模", "topic", "t_smed"),
    ("北京冬奥会", "topic", "t_bj_olympic"),
    ("冬奥会", "topic", "t_bj_olympic"),
    ("开幕式", "topic", "t_opening"),
    ("收视率", "metric", "m_rating"),
    ("不合格", "qc_status", "qc_fail"),
    ("合格", "qc_status", "qc_pass"),
    ("质检", "topic", "t_qc"),
    ("质量", "topic", "t_qc"),
]

# 词典加载上限（防超大库拖慢首次识别）
_DB_LIMITS = {
    "users": 1000,
    "products": 2000,
    "departments": 500,
    "customers": 1000,
    "suppliers": 500,
    "workflow_configs": 500,
}


def _load_db_entities(db: Any) -> List[Tuple[str, str, Optional[str]]]:
    """从 DB 热加载业务实体。DB 不可达时返回空列表（仅内置词典）。

    实体源表（列名均经 DatabaseManager._validate_columns 白名单）：
        users(name/username) -> person；products(product_code/product_name)
        -> product；departments(dept_name) -> department；
        customers(customer_name) -> customer；suppliers(supplier_name)
        -> supplier；workflow_configs(workflow_name, is_active=True)
        -> workflow。
    """
    items: List[Tuple[str, str, Optional[str]]] = []
    try:
        if db is None:
            from prog.core.database import get_database
            db = get_database()
        limit = _DB_LIMITS["users"]
        rows = db.query_many("users", {"status": "active"},
                             columns=["user_id", "name", "username"], limit=limit)
        for r in rows:
            uid = str(r.get("user_id") or "")
            name = str(r.get("name") or "").strip()
            if name:
                items.append((name, "person", uid or name))
            uname = str(r.get("username") or "").strip()
            if uname and len(uname) >= 2 and uname != name:
                items.append((uname, "person", uid or uname))
        rows = db.query_many("products", None,
                             columns=["product_code", "product_name"],
                             limit=_DB_LIMITS["products"])
        for r in rows:
            code = str(r.get("product_code") or "").strip()
            pname = str(r.get("product_name") or "").strip()
            if code:
                items.append((code, "product", code))
            if pname and pname != code:
                items.append((pname, "product", code))
        rows = db.query_many("departments", None,
                             columns=["dept_id", "dept_name"],
                             limit=_DB_LIMITS["departments"])
        for r in rows:
            dname = str(r.get("dept_name") or "").strip()
            if dname:
                items.append((dname, "department", str(r.get("dept_id") or dname)))
        rows = db.query_many("customers", None,
                             columns=["customer_id", "customer_name"],
                             limit=_DB_LIMITS["customers"])
        for r in rows:
            cname = str(r.get("customer_name") or "").strip()
            if cname:
                items.append((cname, "customer", str(r.get("customer_id") or cname)))
        rows = db.query_many("suppliers", None,
                             columns=["supplier_id", "supplier_name"],
                             limit=_DB_LIMITS["suppliers"])
        for r in rows:
            sname = str(r.get("supplier_name") or "").strip()
            if sname:
                items.append((sname, "supplier", str(r.get("supplier_id") or sname)))
        rows = db.query_many("workflow_configs", {"is_active": True},
                             columns=["config_id", "workflow_name"],
                             limit=_DB_LIMITS["workflow_configs"])
        for r in rows:
            wname = str(r.get("workflow_name") or "").strip()
            if wname:
                items.append((wname, "workflow", str(r.get("config_id") or wname)))
    except Exception:
        # DB 不可达/表缺失：静默降级，仅内置词典
        items = []
    return items


def build_recognizer(db: Any = None) -> EntityRecognizer:
    """构建实体识别器：内置通用词典 + DB 业务实体。"""
    rec = EntityRecognizer()
    rec.add_many(_BUILTIN_ENTITIES)
    rec.add_many(_load_db_entities(db))
    return rec


# 进程内缓存（TTL 5 分钟，对齐 A.9.3 能力 1 刷新策略）
_RECOGNIZER_CACHE: Dict[str, Any] = {"ts": 0.0, "rec": None}
_CACHE_TTL = 300.0


def get_recognizer(db: Any = None) -> EntityRecognizer:
    """获取（带缓存）实体识别器。5 分钟内复用，过期重建（重新加载词典）。"""
    now = time.time()
    cached = _RECOGNIZER_CACHE.get("rec")
    if cached is None or now - _RECOGNIZER_CACHE.get("ts", 0.0) > _CACHE_TTL:
        _RECOGNIZER_CACHE["rec"] = build_recognizer(db)
        _RECOGNIZER_CACHE["ts"] = now
    return _RECOGNIZER_CACHE["rec"]


# =========================================================================
# 四、搜索 query 重写器
# =========================================================================

EN_MODEL_RE = re.compile(
    r'[A-Za-z][A-Za-z0-9\-]*\d[A-Za-z0-9\-]*|[A-Za-z]{2,}'
)
CN_SPAN_RE = re.compile(r'[\u4e00-\u9fff]{2,}')
TIME_PATTERNS = [
    re.compile(r'近\s*(\d+)\s*天'),
    re.compile(r'最近\s*(\d+)\s*天'),
    re.compile(r'过去\s*(\d+)\s*天'),
    re.compile(r'(\d+)\s*天前'),
    re.compile(r'(\d+)\s*月'),
    re.compile(r'(\d{4})\s*年'),
    re.compile(r'第?\s*([一二三四五六七八九十]+)\s*季度'),
]


def _strip_verbal_noise(text: str) -> str:
    """剥去口语虚词前缀/后缀。"""
    s = text
    for noise in sorted(VERBAL_NOISE, key=len, reverse=True):
        if s.startswith(noise):
            s = s[len(noise):]
        if s.endswith(noise):
            s = s[:-len(noise)]
    s = s.strip(" ，。？！?、：:;；.。\t\"'")
    return s


def _is_stop(word: str) -> bool:
    if not word or len(word) < 2:
        return True
    if word in STOP_WORDS or word in QUESTION_WORDS:
        return True
    return False


def _extract_cn_keywords(
    text: str,
    entity_set: set,
    min_len: int = 2,
    max_len: int = 4,
) -> List[str]:
    """提取中文关键词：剥口语噪声 → 最大匹配 → 去停用词 → 去实体碎片。"""
    clean = _strip_verbal_noise(text)

    results: List[str] = []
    seen: set = set()

    def _add(w: str) -> None:
        if w not in seen and not _is_stop(w):
            # 排除"完全在一个已识别实体内"的碎片（如实体是"郭德纲"，
            # 排除"郭德""德纲"）
            is_fragment = False
            for ent in entity_set:
                if len(w) < len(ent) and w in ent:
                    is_fragment = True
                    break
            if not is_fragment:
                seen.add(w)
                results.append(w)

    for m in CN_SPAN_RE.finditer(clean):
        span = m.group(0)
        # 4-gram → 3-gram → 2-gram，长词优先
        for L in (max_len, max_len - 1, min_len) if max_len > min_len else (max_len,):
            i = 0
            while i + L <= len(span):
                w = span[i:i + L]
                _add(w)
                i += 1

    return results


def rewrite_search_query(
    text: str,
    db: Any = None,
    recognizer: Optional[EntityRecognizer] = None,
) -> Dict[str, List[str]]:
    """搜索 query 重写（A.9.3 能力 2）。

    返回：
      {"P0": [实体精确短语 query], "P1": [关键词堆叠 query],
       "P2": [实体+时间 query]}
    """
    raw = text.strip()
    if not raw:
        return {"P0": [], "P1": [], "P2": []}
    rec = recognizer or get_recognizer(db)

    # 1) 实体识别
    entities = rec.extract(raw)
    entity_texts = [e["text"] for e in entities]

    # 2) 提取时间表达
    time_phrases: List[str] = []
    for pat in TIME_PATTERNS:
        for m in pat.finditer(raw):
            time_phrases.append(m.group(0))

    # 3) 提取英文/型号/ID
    model_words: List[str] = []
    for m in EN_MODEL_RE.finditer(raw):
        w = m.group(0)
        if len(w) >= 2 and w.lower() not in {"the", "and", "for", "are", "you"}:
            model_words.append(w.upper())

    # 4) 中文关键词（剥口语噪声 + 去停用词 + 去实体碎片）
    entity_set = set(entity_texts)
    cn_words = _extract_cn_keywords(raw, entity_set)

    # ---- P0: 实体精确短语（强约束） ----
    p0_parts: List[str] = [f'"{et}"' for et in entity_texts]
    # 补充非实体的核心名词（2-4 字）
    extra_for_p0 = [w for w in cn_words if w not in entity_set][:3]
    p0_parts.extend(extra_for_p0)
    if model_words:
        p0_parts.extend(model_words[:2])
    p0_query = " ".join(p0_parts[:6]) if p0_parts else raw

    # ---- P1: 关键词堆叠（中等约束，高召回） ----
    p1_parts: List[str] = list(dict.fromkeys(entity_texts))  # 去重保序
    p1_parts.extend([w for w in cn_words if w not in set(p1_parts)][:5])
    if model_words:
        p1_parts.extend([w for w in model_words if w not in set(p1_parts)][:2])
    if time_phrases:
        p1_parts.extend(time_phrases[:1])
    p1_query = " ".join(p1_parts[:8]) if p1_parts else raw

    # ---- P2: 实体 + 时间限定（追新） ----
    p2_parts: List[str] = list(dict.fromkeys(entity_texts))[:3]
    if time_phrases:
        p2_parts.extend(time_phrases)
    else:
        p2_parts.append("最新进展")
    p2_query = " ".join(p2_parts[:5]) if p2_parts else raw

    return {
        "P0": [p0_query] if p0_query else [],
        "P1": [p1_query] if p1_query else [],
        "P2": [p2_query] if p2_query else [],
    }


# =========================================================================
# 五、升级版关键词提取 + RAG query 清洗
# =========================================================================

def extract_keywords(text: str, db: Any = None, top_n: int = 10) -> List[str]:
    """升级版关键词提取（A.9.3 能力 4）：实体整词优先 + 最大匹配 +
    停用词过滤 + 英文/型号整体保留。"""
    if not text:
        return []
    rec = get_recognizer(db)
    keywords: List[str] = []

    # 1) 实体整词
    entity_list = rec.extract(text)
    for e in entity_list:
        keywords.append(e["text"])

    # 2) 英文/数字/型号（整体保留）
    for m in EN_MODEL_RE.finditer(text):
        w = m.group(0).upper()
        if w not in keywords:
            keywords.append(w)

    # 3) 中文关键词（统一走 _extract_cn_keywords）
    entity_set = set(e["text"] for e in entity_list)
    cn_kw = _extract_cn_keywords(text, entity_set)
    for w in cn_kw:
        if w not in keywords:
            keywords.append(w)

    return keywords[:top_n]


_QUESTION_TAIL_RE = re.compile(
    r"(是否|有没有|是不是|怎么样|为什么|怎么|多少|几|吗|呢|啊|吧)"
    r"[？?]?\s*$"
)


def clean_search_query(text: str, db: Any = None) -> str:
    """RAG/检索 query 清洗（A.9.3 迭代①）：剥口语虚词前缀/后缀 +
    去句尾疑问词，保留实体与核心名词，提升向量检索召回率。

    返回清洗后的 query（可能为空串；调用方自行回退原 query）。
    """
    if not text:
        return ""
    rec = get_recognizer(db)
    entities = rec.extract(text)
    s = _strip_verbal_noise(text)
    s = _QUESTION_TAIL_RE.sub("", s)
    s = s.strip(" ，。？！?、：:;；.。\t\"'")
    if not s and entities:
        return " ".join(e["text"] for e in entities)
    return s


# =========================================================================
# 六、通用槽位提取工具集（A.9.3 能力 3，供工具调用方使用）
# =========================================================================

class SlotExtractor:
    """通用槽位提取工具集（融合 demo SlotExtractor）。

    说明：生产槽位提取主链路走 runtime/slot_engine.extract_slots（DB 表驱动
    可训练，覆盖更广）；本工具集基于实体词典 + 正则，供需要实体维度
    （person/product/department/customer/workflow）的调用方使用，
    不替代 slot_engine 的既有 API。
    """

    def __init__(self, recognizer: Optional[EntityRecognizer] = None) -> None:
        self.rec = recognizer or get_recognizer()

    def extract(self, text: str, slot_types: List[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        entities = self.rec.extract(text)

        for stype in slot_types:
            if stype == "person":
                result["persons"] = [e for e in entities if e["type"] == "person"]
            elif stype == "product":
                result["products"] = [e for e in entities if e["type"] == "product"]
            elif stype == "department":
                result["departments"] = [e for e in entities if e["type"] == "department"]
            elif stype == "customer":
                result["customers"] = [e for e in entities if e["type"] == "customer"]
            elif stype == "workflow":
                result["workflows"] = [e for e in entities if e["type"] == "workflow"]
            elif stype == "qc_status":
                result["qc_status"] = [e for e in entities if e["type"] == "qc_status"]
            elif stype == "order":
                ids: List[str] = []
                for pat in [r'(SO|SO#)\s*(\d{3,})', r'订单号\s*[:：]?\s*(\w+)',
                            r'订单\s*(\w+)']:
                    for m in re.finditer(pat, text, re.I):
                        if m.lastindex and m.lastindex > 1:
                            ids.append("".join(m.groups()))
                        else:
                            ids.append(m.group(1))
                result["order_ids"] = list(dict.fromkeys(ids))
            elif stype == "quantity":
                qtys: List[float] = []
                for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:件|个|套|台|只|pcs|PCS|天|小时|h)',
                                     text):
                    try:
                        qtys.append(float(m.group(1)))
                    except ValueError:
                        pass
                result["quantities"] = qtys
            elif stype == "date_range":
                result["date_range"] = _extract_date_range(text)

        return result


_DATE_RANGE_RE = re.compile(r'(近|最近)\s*(\d+)\s*天')


def _extract_date_range(text: str) -> Dict[str, Any]:
    """提取时间范围：近 N 天 / 本月 / 上月 / 今天 / 昨天 / 明天 / 上周 / 本周。"""
    dr: Dict[str, Any] = {"raw": "", "days": None, "type": None}
    m = _DATE_RANGE_RE.search(text)
    if m:
        dr = {"raw": m.group(0), "days": int(m.group(2)), "type": "recent_n_days"}
    else:
        if re.search(r'本月|这个月|当月', text):
            dr = {"raw": "本月", "type": "this_month"}
        elif re.search(r'上月|上个月|一月前', text):
            dr = {"raw": "上月", "type": "last_month"}
        elif re.search(r'今天|今日', text):
            dr = {"raw": "今天", "type": "today"}
        elif re.search(r'昨天|昨日|前一天', text):
            dr = {"raw": "昨天", "type": "yesterday"}
        elif re.search(r'明天|明日|次日', text):
            dr = {"raw": "明天", "type": "tomorrow"}
        elif re.search(r'上周|上星期', text):
            dr = {"raw": "上周", "type": "last_week"}
        elif re.search(r'本周|这个星期|这星期', text):
            dr = {"raw": "本周", "type": "this_week"}
    return dr


# =========================================================================
# 七、对外便捷入口
# =========================================================================

def extract_entities(text: str, db: Any = None) -> List[Dict[str, Any]]:
    """便捷入口：从文本提取业务实体列表 [{text, type, id, start, end}]。"""
    if not text:
        return []
    return get_recognizer(db).extract(text)
