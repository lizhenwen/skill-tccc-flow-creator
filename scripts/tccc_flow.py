#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tccc_flow.py — 腾讯云呼叫中心 TCCC「AI 画布」流程编译工具（零三方依赖）

子命令：
  build      Markdown 设计稿 / src 目录 ->  画布可导入 JSON（+ 合并稿 md + 校验报告）
  split      整篇设计稿 / 画布 JSON     ->  src/ 分片目录（一节点一文件）
  assemble   src/ 分片目录              ->  整篇设计稿 md
  validate   画布 JSON        ->  校验报告（error 阻断 / warning 提示）
  decompile  画布 JSON        ->  Markdown 设计稿（可再 build --base 回编译）

设计稿语法见 references/design-doc-spec.md；字段字典见 references/node-schema.md。

只用标准库：json / re / hashlib / argparse / dataclasses / pathlib。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ============================================================================
# 0. 常量：外部 JSON 默认值（1:1 对齐线上真实导出，勿凭记忆改）
# ============================================================================

VERSION = "1.0"
# 画布输入框的字数上限。除赋值节点（保存时有明确报错）外都是静默截断，
# 导入不报错、保存不报错，只能编译期拦。数值全部来自画布源码，勿凭记忆改。
SYSTEM_PROMPT_MAX = 8192   # 全局提示词 / 兜底固定话术（persona-requirements.vue:130 substring 截断）
CHAT_PROMPT_MAX = 5000     # 对话节点话术 nodeData.content（const.ts:237 MAX_WELCOME_LENGTH）
REPLY_CLASS_MAX = 300      # 用户回复分类 branches[].content（const.ts:236 MAX_REPLY_LENGTH）
VAR_DESC_MAX = 1000        # 变量描述：词槽说明、赋值变量的提取描述/固定值（assign 节点保存时硬拦）
VAR_NAME_MAX = 50          # 变量名 / 词槽名
TTS_TEXT_MAX = 300         # 播报类短文本：转接提示音、超时/失败话术、DTMF 各段话术、留言话术
NOTIFY_TEXT_MAX = 100      # 静默提示 / 挂机话术（no-response-settings.vue:64,101）
NEAR_LIMIT_RATIO = 0.9     # 超过上限这个比例就提前预警（W9）
# 逻辑条件判断节点：单条分支 conditions 不得超过 30，超了运行时每次经过都触发超限检测。
LOGIC_SPLIT_BRANCH_CONDITION_MAX = 30
VALID_VAD_LEVELS = {0, 1, 2, 3, 100}
BUILTIN_VARS = {"StaffNo", "SessionId"}

DEFAULT_TRANSFER_MUSIC = (
    "CgAAADE0MDAwMzcwMjULAAAAc3dpdGNoYm9hcmQ7AAAAMTQwMDAzNzAyNV9zd2l0Y2hib2"
    "FyZF8xMzM1QUVCREFDRTBERDRGNzg3OTI5RTk4NEQ1NEI0OC5tcDM="
)

DEFAULT_VOICE_SETTINGS: dict[str, Any] = {
    "customTTSConfig": "",
    "maxDuration": 60,
    "notifyDuration": 10,
    "notifyMaxCount": 1,
    "notifyType": 0,
    "notifyMessage": "抱歉，我没有听清楚，您可以重复一遍吗？",
    "voiceType": "zh-CN-ArticulateJuan",
    "executionMode": "sequentialMode",
    "endFunctionEnable": True,
    "enableComplianceAudio": True,
    "transferFunctionEnable": True,
    "languages": ["zh-CN"],
    "faqMenuList": [],
    "aiBotId": 0,          # 导入时会被当前机器人 id 强制覆盖，填什么都无所谓
    "vadLevel": 100,
    "vadSilenceTime": 500,
    "interruptSpeechDuration": 500,
    "ttsSpeed": 1,
    "hungUpMessage": "那就不打扰您了，再见。",
    "systemPrompt": "",
    "disableLLM": False,
    "fallbackFixMessage": "",
    "voicemailMessage": "你好，抱歉无法接待，如果方便请回拨。",
    "voicemailAction": 0,
    "enableVoicemailDetection": False,
    "maxCallDurationMs": 0,
    "ambientSoundType": "none",
    "ambientSoundVolume": 1,
}

DEFAULT_AI_TRANSFER_CONTEXT: dict[str, Any] = {
    "enableSummary": False,
    "summaryPrompt": (
        "请用简洁的语言总结本次通话的关键信息，包括：用户的核心诉求、已确认的关键信息、"
        "当前处理进展，以及尚未解决的问题，便于人工座席快速了解情况并接手。"
    ),
    "enableMessages": False,
    "enableVariables": False,
    "variableNames": [],
    "pushUrl": "",
    "pushAuthType": "nil",
    "pushAuthConfig": "",
}

# 节点种类 -> 外部 JSON type
KIND_TO_TYPE = {
    "start": "startNode",
    "chat": "chatNode",
    "announce": "chatNode",
    "api": "apiCallNode",
    "assign": "extractVariableNode",
    "condition": "logicSplitNode",
    "worktime": "workTimeNode",
    "dtmf-nav": "DTMFNode",
    "dtmf-collect": "DTMFNode",
    "transfer-skill": "transfer",
    "transfer-outer": "transfer",
    "transfer-3rd": "transfer",
    "transfer-agent": "transferAgentNode",
    "end": "hangup",
}
MULTI_EXIT_KINDS = {"chat", "api", "condition", "worktime", "dtmf-nav", "dtmf-collect"}
TERMINAL_KINDS = {"end", "transfer-skill", "transfer-outer", "transfer-3rd", "transfer-agent"}
SINGLE_EXIT_KINDS = {"start", "announce", "assign"}
# 系统分支：一个节点里最多一个，--base 复用时可只按 type 匹配
SYSTEM_BRANCH_TYPES = {
    "entity_success", "entity_fail", "silent", "else",
    "api_call_success", "api_call_fail", "dtmf_fail", "transfer_agent_fail",
}

# 环境配置 -> voiceSettings 映射：中文键 -> (字段, 类型)
ENV_TO_VOICE = {
    "音色": ("voiceType", "str"),
    "语速": ("ttsSpeed", "num"),
    "单轮最长时长(秒)": ("maxDuration", "int"),
    "静默提示": ("notifyMessage", "str"),
    "静默提示次数": ("notifyMaxCount", "int"),
    "静默提示等待(秒)": ("notifyDuration", "int"),
    "静默提示类型": ("notifyType", "int"),
    "挂机话术": ("hungUpMessage", "str"),
    "打断检测(毫秒)": ("interruptSpeechDuration", "int"),
    "静音判定(毫秒)": ("vadSilenceTime", "int"),
    "远场人声抑制": ("vadLevel", "int"),
    "背景音": ("ambientSoundType", "str"),
    "背景音音量": ("ambientSoundVolume", "num"),
    "语音留言检测": ("enableVoicemailDetection", "bool"),
    "合规录音": ("enableComplianceAudio", "bool"),
    "外接音色配置": ("customTTSConfig", "str"),
    "兜底固定话术": ("fallbackFixMessage", "str"),
    "语言": ("languages", "list"),
}
# 仅 env 用、不进 voiceSettings 的键
ENV_LOCAL_KEYS = {"技能组ID", "转接智能体ID", "环境注入变量", "流程说明"}

ARROW = re.compile(r"\s*(?:→|->|=>)\s*")
NODE_HEADER = re.compile(r"^###\s+(N\d+)\s+(.*?)\s*\[([a-z0-9\-]+)\]\s*(\{全局\})?\s*$")
ATTR_LINE = re.compile(r"^-\s*([^:：]+?)\s*[:：]\s*(.*)$")
TAG_BLOCK = re.compile(r"\{标签[:：]([^}]*)\}")
VAR_REF = re.compile(r"\$\{([^}]+)\}")
SYM_OPS = [">=", "<=", "==", "!=", ">", "<"]
WORD_OPS = {
    "大于等于": "gte", "小于等于": "lte", "大于": "gt", "小于": "lt",
    "等于": "==", "不等于": "!=", "包含": "contains", "不包含": "not_contains",
    "属于": "in", "不属于": "not_in", "存在": "exists", "不存在": "not_exists",
    "非空": "exists", "为空": "not_exists",
    "gte": "gte", "lte": "lte", "gt": "gt", "lt": "lt", "eq": "==", "neq": "!=",
    "contains": "contains", "not_contains": "not_contains", "in": "in", "not_in": "not_in",
    "exists": "exists", "not_exists": "not_exists",
}
UNARY_OPS = {"exists", "not_exists"}
# 逆编译写中文别名。画布 JSON 里是 contains/exists，设计稿习惯写「包含」「存在」；
# 「用线上覆盖」若原样吐英文，每个判断节点都会冒出一堆无意义 diff。
OP_ZH = {
    "gt": "大于", "lt": "小于", "gte": "大于等于", "lte": "小于等于",
    "eq": "==", "neq": "!=",
    "contains": "包含", "not_contains": "不包含",
    "in": "属于", "not_in": "不属于",
    "exists": "存在", "not_exists": "不存在",
}


class DesignError(Exception):
    """设计稿语法错误（带行号）。"""


# ============================================================================
# 1. 稳定 id：同一编号+名称重编译得到同一 id，便于 diff 与 --base 合并
# ============================================================================

def _uuid_like(seed: str, salt: str = "") -> str:
    h = hashlib.sha1(f"{salt}::{seed}".encode("utf-8")).hexdigest()
    variant = "89ab"[int(h[16], 16) % 4]
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{variant}{h[17:20]}-{h[20:32]}"


def node_id_of(no: str, name: str) -> str:
    return "node-" + _uuid_like(f"{no}::{name}", "node")


def resolve_node_ids(design: Design, base: dict[str, Any] | None = None) -> dict[str, str]:
    """每个节点写入 JSON 的 id。

    优先级：分片 `- ID:` → 与 --base 同哈希 id → --base 同名且唯一 → 新建哈希。
    这样发布能把线上真实 id 回填进 src，改名也不会拆成新节点。
    """
    base_nodes: list[dict[str, Any]] = []
    if base:
        for arr in (base.get("ivrData") or {}).values():
            if not isinstance(arr, list):
                continue
            for nd in arr:
                if isinstance(nd, dict) and nd.get("id"):
                    base_nodes.append(nd)
    by_id = {str(n["id"]): n for n in base_nodes}
    name_ids: dict[str, list[str]] = {}
    for n in base_nodes:
        name_ids.setdefault(str(n.get("name") or ""), []).append(str(n["id"]))

    used: set[str] = set()
    ids: dict[str, str] = {}
    for n in design.nodes:
        explicit = (n.node_id or "").strip()
        hashed = node_id_of(n.no, n.name)
        if explicit:
            chosen = explicit
        elif hashed in by_id and hashed not in used:
            chosen = hashed
        else:
            cands = [i for i in name_ids.get(n.name, []) if i not in used]
            chosen = cands[0] if len(cands) == 1 else hashed
        ids[n.no] = chosen
        used.add(chosen)
    return ids


def branch_id_of(node_key: str, index: int, content: str) -> str:
    return "relation-" + _uuid_like(f"{node_key}::{index}::{content}", "branch")


def edge_id_of(src_key: str, target_key: str) -> str:
    return "node-" + _uuid_like(f"{src_key}->{target_key}", "edge")


def assign_var_id_of(node_key: str, var_name: str) -> str:
    return "voice-variable-assign-relation-" + _uuid_like(f"{node_key}::{var_name}", "assign")


def collection_item_id_of(node_key: str, var_name: str) -> str:
    return "relation-" + _uuid_like(f"{node_key}::col::{var_name}", "collection")


def entity_tmp_id_of(node_key: str, var_name: str) -> str:
    """自定义词槽的前端临时 id：保存时由 syncConversationSlots 换成真实 slotId。"""
    return "entity-" + _uuid_like(f"{node_key}::slot::{var_name}", "slot")


# ============================================================================
# 2. 数据结构
# ============================================================================

@dataclass
class Branch:
    kind: str                       # intent/global_intent/entity_success/api_call_success/...
    content: str
    target: str | None = None       # 目标节点编号 Nxx
    target_name: str = ""
    tags: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    bid: str | None = None          # 显式 id（来自 --base 复用）
    line: int = 0


@dataclass
class Node:
    no: str
    name: str
    kind: str
    is_global: bool = False
    attrs: dict[str, list[str]] = field(default_factory=dict)
    prompt: str = ""
    branches: list[Branch] = field(default_factory=list)
    next: str | None = None
    next_name: str = ""
    node_id: str | None = None
    pos: tuple[float, float] | None = None
    line: int = 0

    def a(self, key: str, default: str | None = None) -> str | None:
        v = self.attrs.get(key)
        return v[0] if v else default

    def alist(self, key: str) -> list[str]:
        return self.attrs.get(key, [])

    def abool(self, key: str, default: bool) -> bool:
        v = self.a(key)
        return default if v is None else _to_bool(v)

    def aint(self, key: str, default: int) -> int:
        v = self.a(key)
        try:
            return int(str(v).strip()) if v not in (None, "") else default
        except ValueError:
            return default

    @property
    def key(self) -> str:
        return f"{self.no} {self.name}"


@dataclass
class Design:
    title: str = ""
    env: dict[str, str] = field(default_factory=dict)
    system_prompt: str = ""
    nodes: list[Node] = field(default_factory=list)
    passthrough: list[tuple[str, str]] = field(default_factory=list)  # (section 标题, 原文)
    injected_vars: list[str] = field(default_factory=list)
    rewrap_stats: int = 0            # build 时合并掉的硬折行数量
    notes: list[tuple[str, str]] = field(default_factory=list)  # (节点 key, 编译期 warning)
    node_ids: dict[str, str] = field(default_factory=dict)
    system_prompt_line: int = 0      # 「## 全局提示词」所在行，用于把校验问题指回 src 分片

    @property
    def by_no(self) -> dict[str, Node]:
        return {n.no: n for n in self.nodes}


def _to_bool(v: str) -> bool:
    return str(v).strip() in {"是", "true", "True", "yes", "1", "开", "允许", "启用"}


def _to_num(v: str) -> float | int:
    s = str(v).strip()
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return 0


# ============================================================================
# 2.5 硬折行修复（rewrap）
#
# 背景：AI 写 Markdown 时习惯按 80~100 字符硬折行，中文句子常在逗号处被劈成两行。
# Markdown 预览会把单换行合并，所以看不出问题；但 ```prompt 围栏是原样逐字写进
# JSON 的，这些排版换行就变成了真实 \n，等于把断句直接发给大模型。
# 这里把「明显属于排版折行」的地方合回一行，只处理高置信度情形，宁可漏改不误改。
# ============================================================================

SENT_END = "。！？；!?;…"                    # 句末标点：到这里才算一句说完
CLOSERS = "”’\"'」』）)】》>*_`~"              # 收尾符号/强调符号，要剥掉再判断句末
# 这些开头的行不可能是「上一句的续写」
STRUCT_PREFIX = ("#", "-", "*", "+", ">", "|", "```", "~~~", "=", "→", "->", "=>")
NUM_LIST_RE = re.compile(r"^\s*(?:\d+\s*[.)、．]|[①-⑳]|[a-zA-Z]\s*[.)])")
DIALOG_RE = re.compile(r"^\s*(?:你|我|客户|用户|机器人|坐席|AI|Agent)\s*[:：]")
# 「字段名：值」式的清单行（如「对话示例二：」「客户的收货省：${收件省}」），
# 这类行是独立条目，不是上一句的续写
FIELD_LINE_RE = re.compile(r"^\s*[^：:，。！？；\s]{1,20}\s*[：:]")
# 设计稿的结构关键字，绝不能被并到上一行去
DESIGN_KEYWORD_RE = re.compile(r"^\s*(?:分支|环境配置|变量表|假设与待确认|节点|全局提示词)\s*[:：]?\s*$")
HR_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,}|_{3,})\s*$")
ASCII_WORD_RE = re.compile(r"[0-9A-Za-z_$}{)\]]")
VAR_TAIL_RE = re.compile(r"\$\{[^}]*\}\s*$")     # 以 ${变量} 收尾，多半是字段行


def _can_absorb(line: str) -> bool:
    """这一行是否「有资格」把下一行吸收进来。

    标题 / 围栏标记 / 表格 / 水平线 / 引用是独立结构块，哪怕行尾没有标点，
    也绝不能把下一行并进来（否则会出现「# 人设你是本公司客服」这种事故）。
    以 ${变量} 收尾的行多半是「字段名：${变量}」清单，同样不吸收。
    """
    s = line.strip()
    if not s:
        return False
    if s.startswith(("#", "```", "~~~", "|", ">")):
        return False
    if VAR_TAIL_RE.search(s):
        return False
    return not HR_RE.match(s)


def _ends_sentence(line: str) -> bool:
    """这一行是否已经把话说完（不需要和下一行合并）。"""
    s = line.rstrip()
    if not s:
        return True
    if line.endswith("  "):                 # Markdown 的显式硬换行，尊重作者意图
        return True
    core = s.rstrip(CLOSERS)
    return (core[-1] in SENT_END) if core else True


def _is_continuation(line: str) -> bool:
    """下一行是否是上一句的续写（而不是新的结构块）。"""
    s = line.strip()
    if not s:
        return False
    if s.startswith(STRUCT_PREFIX):
        return False
    if DESIGN_KEYWORD_RE.match(s):
        return False
    if FIELD_LINE_RE.match(s):              # 「字段名：值」清单行是独立条目
        return False
    return not (NUM_LIST_RE.match(s) or DIALOG_RE.match(s))


def _join_two(prev: str, nxt: str) -> str:
    """拼接两行：中英文之间补空格，纯中文直接接上。"""
    a, b = prev.rstrip(), nxt.strip()
    if not a:
        return b
    if not b:
        return a
    need_space = bool(ASCII_WORD_RE.search(a[-1])) and bool(ASCII_WORD_RE.search(b[0]))
    return a + (" " if need_space else "") + b


def rewrap_text(text: str, *, in_fence_only: bool | None = None) -> tuple[str, int]:
    """把排版硬折行合回一行。返回 (新文本, 合并次数)。

    - 表格行（|）、代码围栏、列表/标题等结构行一律不动
    - 只有「上一行没说完 + 下一行是续写」才合并
    - in_fence_only=None：不区分围栏（用于纯话术文本）
    """
    lines = text.split("\n")
    out: list[str] = []
    merged = 0
    fence: str | None = None
    fence_kind = ""
    for raw in lines:
        stripped = raw.lstrip()
        # 围栏开合
        if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
            fence_kind = stripped[3:].strip().lower()
            out.append(raw)
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence, fence_kind = None, ""
                out.append(raw)
                continue
            # 只有 ```prompt 围栏里的话术需要修；```bash / ```json / 无标记的
            # 目录树和命令输出一律原样保留
            if in_fence_only is False or fence_kind != "prompt":
                out.append(raw)
                continue
        elif in_fence_only is True:
            out.append(raw)
            continue

        if (out and _can_absorb(out[-1]) and "|" not in out[-1]
                and not _ends_sentence(out[-1]) and _is_continuation(raw)):
            out[-1] = _join_two(out[-1], raw)
            merged += 1
            continue
        out.append(raw)
    return "\n".join(out), merged


def find_hard_wraps(text: str) -> list[str]:
    """找出疑似排版折行的位置，返回可读提示（用于校验报告）。"""
    hits: list[str] = []
    lines = text.split("\n")
    fence: str | None = None
    for i, raw in enumerate(lines):
        stripped = raw.lstrip()
        if fence is None and (stripped.startswith("```") or stripped.startswith("~~~")):
            fence = stripped[:3]
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if (_can_absorb(raw) and "|" not in raw and not _ends_sentence(raw)
                and i + 1 < len(lines) and _is_continuation(lines[i + 1])):
            hits.append(f"「…{raw.strip()[-14:]}」+「{lines[i + 1].strip()[:14]}…」")
    return hits



# ============================================================================
# 3. Markdown 设计稿解析
# ============================================================================

def parse_design(text: str) -> Design:
    lines = text.replace("\r\n", "\n").split("\n")
    d = Design()

    # --- 先按 ## 切分 section ---
    sections: list[tuple[str, int, list[str]]] = []
    cur_title, cur_start, cur_body = None, 0, []
    in_fence = False
    for i, raw in enumerate(lines):
        if raw.lstrip().startswith("```") or raw.lstrip().startswith("~~~"):
            in_fence = not in_fence
        if not in_fence and raw.startswith("# ") and cur_title is None:
            d.title = re.sub(r"^#\s*(流程[:：]?|流程名称[:：]?)\s*", "", raw).strip()
            continue
        if not in_fence and raw.startswith("## "):
            if cur_title is not None:
                sections.append((cur_title, cur_start, cur_body))
            cur_title, cur_start, cur_body = raw[3:].strip(), i + 1, []
            continue
        if cur_title is not None:
            cur_body.append(raw)
    if cur_title is not None:
        sections.append((cur_title, cur_start, cur_body))

    saw_nodes = False
    for title, start, body in sections:
        t = title.replace(" ", "")
        if t.startswith("环境配置"):
            _parse_env(d, body, start)
        elif t.startswith("全局提示词") or t.startswith("systemPrompt"):
            d.system_prompt = _extract_fence(body).strip()
            d.system_prompt_line = start
        elif t.startswith("节点"):
            _parse_nodes(d, body, start)
            saw_nodes = True
        else:
            d.passthrough.append((title, "\n".join(body).strip("\n")))

    if not saw_nodes:
        raise DesignError("设计稿缺少「## 节点」章节")
    if not d.nodes:
        raise DesignError("「## 节点」章节里没有解析到任何节点（节点标题格式：### N01 名称 [kind]）")

    # 引用完整性：编号唯一 + 连线目标必须存在
    seen_no: dict[str, int] = {}
    for n in d.nodes:
        if n.no in seen_no:
            raise DesignError(f"第 {n.line} 行：节点编号 {n.no} 重复（上一次出现在第 {seen_no[n.no]} 行）")
        seen_no[n.no] = n.line
    known = set(seen_no)
    for n in d.nodes:
        if n.next and n.next not in known:
            raise DesignError(f"节点 {n.key}（第 {n.line} 行）的出口指向了不存在的编号 {n.next}")
        for b in n.branches:
            if b.target and b.target not in known:
                raise DesignError(
                    f"第 {b.line} 行：分支「{b.content[:20]}」指向了不存在的编号 {b.target}")
    return d


def _parse_env(d: Design, body: list[str], start: int) -> None:
    for off, raw in enumerate(body):
        line = raw.strip()
        if not line.startswith("-"):
            continue
        m = ATTR_LINE.match(line)
        if not m:
            continue
        k, v = m.group(1).strip(), m.group(2).strip()
        k = k.replace("（", "(").replace("）", ")")
        if k == "环境注入变量":
            d.injected_vars = [x.strip() for x in re.split(r"[,，、;；\s]+", v) if x.strip()]
        d.env[k] = v
        if k not in ENV_TO_VOICE and k not in ENV_LOCAL_KEYS:
            # 不认识的键不报错，进 env 供人查阅，但提示
            pass
    _ = start


def _extract_fence(body: list[str]) -> str:
    out: list[str] = []
    fence: str | None = None
    for raw in body:
        s = raw.lstrip()
        if fence is None and (s.startswith("```") or s.startswith("~~~")):
            fence = s[:3]
            continue
        if fence is not None and s.startswith(fence):
            break
        if fence is not None:
            out.append(raw)
    if fence is None:
        # 没有围栏就把整段当正文（去掉空行首尾）
        return "\n".join(body)
    return "\n".join(out)


def _parse_nodes(d: Design, body: list[str], start: int) -> None:
    state: dict[str, Any] = {"cur": None, "fenced": [], "plain": [], "branch_mode": False}
    fence: str | None = None

    def close() -> None:
        cur: Node | None = state["cur"]
        if cur is not None:
            src = state["fenced"] if state["fenced"] else state["plain"]
            # 只去掉开头空行：结尾换行是话术的一部分，必须保真
            cur.prompt = "\n".join(src).lstrip("\n")
            d.nodes.append(cur)
        state["cur"], state["fenced"], state["plain"], state["branch_mode"] = None, [], [], False

    for off, raw in enumerate(body):
        ln = start + off + 1
        s = raw.strip()
        stripped = raw.lstrip()

        # 围栏内：原样收集为话术
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
                continue
            state["fenced"].append(raw)
            continue

        m = NODE_HEADER.match(s)
        if m:
            close()
            no, name, kind, gl = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
            if kind not in KIND_TO_TYPE:
                raise DesignError(
                    f"第 {ln} 行：未知节点类型 [{kind}]，可用：{', '.join(sorted(KIND_TO_TYPE))}"
                )
            state["cur"] = Node(no=no, name=name, kind=kind, is_global=bool(gl), line=ln)
            continue

        cur = state["cur"]
        if cur is None:
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            continue

        if re.match(r"^分支\s*[:：]?\s*$", s):
            state["branch_mode"] = True
            continue

        if s.startswith(("→", "->", "=>")):
            tgt, tname = _parse_target(s, ln)
            # 意图文案末尾带 \n 时，逆编译会把「 → Nxx」挤到下一行。
            # 上一条分支还没写目标，就归属它；否则才是单出口节点的默认下一跳。
            last = cur.branches[-1] if cur.branches else None
            if last is not None and not last.target:
                last.target, last.target_name = tgt, tname
            else:
                cur.next, cur.next_name = tgt, tname
            continue

        if s.startswith("-"):
            m2 = ATTR_LINE.match(s)
            if state["branch_mode"] or (m2 is None and ARROW.search(s)):
                cur.branches.append(_parse_branch(cur, s, ln))
            elif m2:
                k = m2.group(1).strip().lstrip("-").strip()
                k = k.replace("（", "(").replace("）", ")")
                cur.attrs.setdefault(k, []).append(m2.group(2).strip())
            continue

        if s and not s.startswith("#"):
            state["plain"].append(raw)

    close()

    # 处理显式 ID / 坐标
    for n in d.nodes:
        if n.a("ID"):
            n.node_id = n.a("ID")
        pos = n.a("坐标")
        if pos:
            try:
                x, y = [float(p) for p in re.split(r"[,，]", pos)[:2]]
                n.pos = (x, y)
            except (ValueError, IndexError):
                n.pos = None


def _parse_target(s: str, ln: int) -> tuple[str | None, str]:
    body = ARROW.sub("", s, count=1).strip() if ARROW.match(s) else s.lstrip("→->= ").strip()
    if not body:
        return None, ""
    m = re.match(r"^(N\d+)\s*(.*)$", body)
    if not m:
        raise DesignError(f"第 {ln} 行：连线目标必须是节点编号（形如 → N03 询问工单），实际是「{body}」")
    return m.group(1), m.group(2).strip()


def _parse_branch(node: Node, s: str, ln: int) -> Branch:
    raw = s.lstrip("-").strip()
    tags: list[tuple[str, str]] = []
    mt = TAG_BLOCK.search(raw)
    if mt:
        for kv in re.split(r"[;；]", mt.group(1)):
            if "=" in kv:
                k, v = kv.split("=", 1)
                tags.append((k.strip(), v.strip()))
        raw = TAG_BLOCK.sub("", raw).strip()

    target, tname = None, ""
    if ARROW.search(raw):
        left, right = ARROW.split(raw, maxsplit=1)
        raw = left.strip()
        if right.strip():
            target, tname = _parse_target("→ " + right.strip(), ln)
    content = _branch_text(_unesc(raw.strip()))
    if not content:
        raise DesignError(f"第 {ln} 行：分支内容为空")

    b = Branch(kind="intent", content=content, target=target, target_name=tname, tags=tags, line=ln)
    _classify_branch(node, b, ln)
    return b


def _classify_branch(node: Node, b: Branch, ln: int) -> None:
    c = b.content
    k = node.kind
    if k == "api":
        b.kind = "api_call_fail" if c.startswith(("失败", "调用失败", "接口调用失败")) else "api_call_success"
        b.content = "接口调用失败" if b.kind == "api_call_fail" else "接口调用成功"
    elif k == "condition":
        if re.match(r"^否则|^else$", c):
            b.kind, b.content = "else", "否则"
        else:
            b.kind = "judgement"
            expr = re.sub(r"^如果\s*", "", c).strip()
            if expr.lower().startswith(("llm:", "llm：", "大模型:", "大模型：")):
                b.extra["method"] = "llm"
                b.extra["llmPrompt"] = re.split(r"[:：]", expr, maxsplit=1)[1].strip()
                b.content = b.extra["llmPrompt"]
            else:
                b.extra["method"] = "rule"
                b.extra["conditions"], b.extra["logicType"] = _parse_conditions(expr, ln)
                b.content = expr
    elif k == "worktime":
        if c.startswith(("其他时间", "非工作时间", "其它时间")):
            b.kind, b.content = "other", "其他时间"
        else:
            b.kind = "worktime"
            name, cfgs = _parse_worktime(c, ln)
            b.content, b.extra["configs"] = name, cfgs
    elif k == "dtmf-nav":
        if "失败" in c:
            b.kind, b.content = "dtmf_fail", "按键失败"
        else:
            m = re.match(r"^按键\s*([0-9*#]+)\s*(.*)$", c)
            if not m:
                raise DesignError(f"第 {ln} 行：按键导航分支格式应为「按键 1 售前咨询 → Nxx」")
            b.kind, b.content = "dtmf_success", m.group(1)
            label = m.group(2).strip()
            b.extra["label"] = label or m.group(1)
            if label and not b.tags:
                b.tags = [("按键标签", label)]
    elif k == "dtmf-collect":
        b.kind = "dtmf_fail" if "失败" in c else "dtmf_success"
        b.content = "收号失败" if b.kind == "dtmf_fail" else "收号成功"
    else:  # chat / announce
        if c.startswith(("触发", "全局触发")):
            b.kind = "global_intent"
            b.content = re.split(r"[:：]", c, maxsplit=1)[-1].strip()
        elif re.match(r"^(收集成功|采集成功)", c):
            b.kind = "entity_success"
        elif re.match(r"^(收集失败|采集失败)", c):
            b.kind = "entity_fail"
        elif re.match(r"^(无应答|静默|沉默|用户无响应)", c):
            b.kind = "silent"
        elif re.match(r"^(其他|兜底|else)", c):
            b.kind = "else"
        else:
            b.kind = "intent"


def _parse_conditions(expr: str, ln: int) -> tuple[list[dict[str, Any]], str]:
    logic = "or" if ("或" in expr and "且" not in expr) else "and"
    parts = re.split(r"\s*(?:且|并且|\band\b|或者|或|\bor\b)\s*", expr)
    conds: list[dict[str, Any]] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        op = None
        left = right = ""
        for sym in SYM_OPS:                       # 先匹配符号运算符（长的优先）
            if sym in p:
                op = sym
                left, right = p.split(sym, 1)
                break
        if op is None:                            # 再匹配单词/中文运算符
            for token in sorted(WORD_OPS, key=len, reverse=True):
                m = re.search(rf"(?:\s|^){re.escape(token)}(?:\s|$)", p)
                if m:
                    op = WORD_OPS[token]
                    left, right = p[:m.start()], p[m.end():]
                    break
        if op is None:
            raise DesignError(
                f"第 {ln} 行：条件「{p}」缺少比较运算符。"
                f"可用符号：{', '.join(SYM_OPS)}；可用单词：{', '.join(sorted(set(WORD_OPS)))}"
            )
        cond: dict[str, Any] = {"var": left.strip(), "operator": op}
        if op not in UNARY_OPS:
            cond["val"] = right.strip().strip("\"'")
        conds.append(cond)
    if len(conds) > LOGIC_SPLIT_BRANCH_CONDITION_MAX:
        raise DesignError(f"第 {ln} 行：{_over_limit_branch_conditions_msg(len(conds))}")
    return conds, logic


def _over_limit_branch_conditions_msg(count: int) -> str:
    cap = LOGIC_SPLIT_BRANCH_CONDITION_MAX
    return (
        f"单个分支配置了 {count} 个条件，超过系统上限 {cap}。"
        f"运行时每次经过该节点都会触发超限检测。"
        f"请拆成级联判断节点：每条分支最多 {cap} 个条件，命中走目标，"
        f"否则接到下一判断节点继续匹配剩余值"
    )


# 工作时间判断：dayType 只有这四种，画布导入时用它反查规则类型，
# 写错（比如 Weekly / Specific）会被整条丢弃，节点变成「未配置」，保存报「请配置工作时间判断」。
DAY_TYPE_ALIASES = [
    ("法定工作日", "BusinessDay"), ("工作日", "BusinessDay"),
    ("法定休息日", "Holiday"), ("节假日", "Holiday"), ("休息日", "Holiday"),
    ("自定义星期", "Custom"), ("每周", "Custom"), ("星期", "Custom"),
    ("自定义日期", "CustomDate"), ("指定日期", "CustomDate"), ("日期", "CustomDate"),
]
DAY_TYPE_ZH = {"BusinessDay": "法定工作日", "Holiday": "法定休息日",
               "Custom": "自定义星期", "CustomDate": "自定义日期"}
# 只有大陆时区能用法定工作日/法定休息日（画布 checkIsMainlandChinaTZ）
MAINLAND_CHINA_TIME_ZONES = {"Asia/Shanghai", "Asia/Chongqing", "Asia/Chungking",
                             "Asia/Harbin", "Asia/Urumqi", "PRC"}
WEEKDAY_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
WEEKDAY_ZH = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
_WD = r"(?:周|星期|礼拜)"
WEEKDAY_RANGE = re.compile(rf"{_WD}([一二三四五六日天])\s*[-~～到至]\s*{_WD}?([一二三四五六日天])")
WEEKDAY_ONE = re.compile(rf"{_WD}([一二三四五六日天])")
WEEKEND = re.compile(rf"{_WD}末")
_DATE = r"\d{4}-\d{1,2}-\d{1,2}"
DATE_RANGE = re.compile(rf"({_DATE})\s*[-~～到至]\s*({_DATE})")
DATE_ONE = re.compile(_DATE)
TIME_PERIOD = re.compile(
    r"(\d{1,2})[:：](\d{2})\s*[-~～到至]\s*(次日|次天|第二天)?\s*(\d{1,2})[:：](\d{2})")


def _parse_weekdays(seg: str) -> list[int]:
    """周一、周三 / 周一~周五 / 周末 → [1,3] / [1,2,3,4,5] / [6,7]，星期几用 1-7（周日=7）。"""
    days: set[int] = set()
    if WEEKEND.search(seg):
        days.update((6, 7))
    rest = WEEKEND.sub(" ", seg)
    for m in WEEKDAY_RANGE.finditer(rest):
        a, b = WEEKDAY_NUM[m.group(1)], WEEKDAY_NUM[m.group(2)]
        days.update(range(a, b + 1) if a <= b else list(range(a, 8)) + list(range(1, b + 1)))
    rest = WEEKDAY_RANGE.sub(" ", rest)
    for m in WEEKDAY_ONE.finditer(rest):
        days.add(WEEKDAY_NUM[m.group(1)])
    return sorted(days)


def _norm_date(s: str) -> str:
    y, m, d = s.split("-")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _parse_date_ranges(seg: str) -> list[dict[str, str]]:
    """2026-01-01 / 2026-01-01~2026-01-03 → [{startDate, endDate}]。"""
    out: list[dict[str, str]] = []
    for m in DATE_RANGE.finditer(seg):
        a, b = _norm_date(m.group(1)), _norm_date(m.group(2))
        out.append({"startDate": min(a, b), "endDate": max(a, b)})
    rest = DATE_RANGE.sub(" ", seg)
    for m in DATE_ONE.finditer(rest):
        d = _norm_date(m.group(0))
        out.append({"startDate": d, "endDate": d})
    return out


def _parse_periods(seg: str, ln: int) -> list[dict[str, Any]]:
    periods: list[dict[str, Any]] = []
    for pm in TIME_PERIOD.finditer(seg):
        sh, sm = int(pm.group(1)), int(pm.group(2))
        eh, em = int(pm.group(4)), int(pm.group(5))
        # 画布只按「结束早于开始」判定跨天，这里保持同一口径，写 26:00 或「次日02:00」都归一到 2:00
        cross = bool(pm.group(3)) or eh >= 24
        eh %= 24
        if not cross and (eh, em) < (sh, sm):
            cross = True
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
            raise DesignError(f"第 {ln} 行：时段 {pm.group(0)} 不是合法时间")
        periods.append({
            "startTime": {"hour": sh, "minute": sm, "nextDay": False},
            "endTime": {"hour": eh, "minute": em, "nextDay": cross},
        })
    return periods


def _parse_worktime(c: str, ln: int) -> tuple[str, list[dict[str, Any]]]:
    """解析：工作时间: 法定工作日 08:00-21:00; 自定义星期 周一~周五 09:00-18:00"""
    if ":" in c or "：" in c:
        name, spec = re.split(r"[:：]", c, maxsplit=1)
    else:
        name, spec = c, ""
    name = name.strip() or "工作时间"
    cfgs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for seg in [x for x in re.split(r"[;；]", spec) if x.strip()]:
        seg = seg.strip()
        day_type = "BusinessDay"
        for zh, en in DAY_TYPE_ALIASES:
            if seg.startswith(zh):
                day_type, seg = en, seg[len(zh):].strip()
                break
        if day_type in seen:
            raise DesignError(
                f"第 {ln} 行：同一个工作时间分支里「{DAY_TYPE_ZH[day_type]}」写了两次，"
                f"画布按类型合并，后一条会覆盖前一条；请合并成一条（多个时段用空格分隔）")
        seen.add(day_type)
        periods = _parse_periods(seg, ln)
        if not periods:
            raise DesignError(f"第 {ln} 行：工作时间分支缺少时段（形如 08:00-21:00）")
        cfg: dict[str, Any] = {
            "dayType": day_type, "daysOfWeek": [], "specificDates": [],
            "workTimePeriods": periods,
        }
        if day_type == "Custom":
            cfg["daysOfWeek"] = _parse_weekdays(seg)
            if not cfg["daysOfWeek"]:
                raise DesignError(
                    f"第 {ln} 行：「自定义星期」必须写明星期几，如「自定义星期 周一~周五 09:00-18:00」")
        elif day_type == "CustomDate":
            cfg["specificDates"] = _parse_date_ranges(seg)
            if not cfg["specificDates"]:
                raise DesignError(
                    f"第 {ln} 行：「自定义日期」必须写明日期，如"
                    f"「自定义日期 2026-01-01~2026-01-03 09:00-18:00」")
        cfgs.append(cfg)
    if not cfgs:
        raise DesignError(
            f"第 {ln} 行：工作时间分支需写明时段，如「工作时间: 法定工作日 08:00-21:00 → N05」")
    return name, cfgs


# ============================================================================
# 4. 生成画布 JSON
# ============================================================================

def canvas_branch_list(n: Node) -> list[Branch]:
    """参与连线与锚点计算的分支。global_intent 是全局触发语，不占画布锚点。"""
    return [b for b in n.branches if b.kind != "global_intent"]


def _listens(n: Node) -> bool:
    """是否勾选「用户回复分类」（外部 JSON selectBranch / 内部 listenUserReply）。

    没有画布分类分支时必须是 false：空 branches + selectBranch=true
    会让线上编辑器勾上分类并报错。
    """
    if n.kind == "announce":
        return False
    has_canvas_br = bool([b for b in n.branches if b.kind != "global_intent"])
    if not has_canvas_br:
        return False
    if n.a("听用户回复") is not None:
        return _to_bool(n.a("听用户回复"))
    return True


def node_outgoing(n: Node, by_no: dict[str, Node]) -> list[str]:
    """与 build_json 实际写出的出边一致，顺序即画布 sourceAnchor 从小到大。"""
    cbrs = canvas_branch_list(n)
    use_multi = (
        n.kind in MULTI_EXIT_KINDS
        and bool(cbrs)
        and not (n.kind == "chat" and not _listens(n))
    )
    if use_multi:
        return [b.target for b in cbrs if b.target and b.target in by_no]
    tgt = n.next or (cbrs[0].target if cbrs else None)
    return [tgt] if tgt and tgt in by_no else []


def _split_tags(spec: str) -> tuple[str, list[tuple[str, str]]]:
    tags: list[tuple[str, str]] = []
    m = TAG_BLOCK.search(spec)
    if m:
        for kv in re.split(r"[;；]", m.group(1)):
            if "=" in kv:
                k, v = kv.split("=", 1)
                tags.append((k.strip(), v.strip()))
        spec = TAG_BLOCK.sub("", spec).strip()
    return spec.strip(), tags


def _unesc(s: str) -> str:
    """设计稿里的 \\n 字面量还原成真实换行。"""
    return s.replace("\\n", "\n")


def _esc(s: str) -> str:
    return str(s).replace("\n", "\\n")


def _branch_text(s: Any) -> str:
    """分支文案写成设计稿单行。线上意图常带末尾换行，丢掉不影响识别，避免稿子里出现明文 \\n。"""
    t = str(s or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return t.replace("\n", "")


def _normalize_branches(n: Node) -> list[str]:
    """规范化：触发属性 -> global_intent 分支；系统分支 content 用画布固定写法。

    对话节点还会：
    - 丢掉已废弃的 entity_fail（画布 QaTree.removeDeprecatedCollectionFailBranch）
    - 按画布视觉顺序重排：global_intent → entity_success → intent/else → silent
    """
    warns: list[str] = []
    existing = {b.content for b in n.branches if b.kind == "global_intent"}
    gl: list[Branch] = []
    for spec in n.alist("触发"):
        content, tags = _split_tags(spec)
        content = _unesc(content)
        if content and content not in existing:
            gl.append(Branch(kind="global_intent", content=content, tags=tags))
    if gl:
        n.branches = gl + n.branches   # 全局触发语放最前，与线上导出一致
    if [b for b in n.branches if b.kind == "global_intent"]:
        n.is_global = True

    if n.kind not in ("chat", "announce"):
        return warns
    var = n.a("收集变量")
    wait = n.aint("静默等待(秒)", 10)
    kept: list[Branch] = []
    for b in n.branches:
        if b.kind == "entity_fail":
            if b.target:
                warns.append(
                    "「收集失败」是画布已废弃分支，已忽略；"
                    f"原指向 {b.target} 的连线不会生成（未抽中词槽会停留在本节点重试）")
            else:
                warns.append(
                    "「收集失败」是画布已废弃分支，已忽略；未抽中词槽会停留在本节点重试")
            continue
        if b.kind == "entity_success":
            b.content = f"收集${{{var}}}结束" if var else "收集完成"
        elif b.kind == "silent" and re.match(r"^(无应答|静默|沉默)\s*$", b.content):
            b.content = f"用户无响应{wait}秒"
        kept.append(b)
    n.branches = kept
    _order_chat_canvas_branches(n)
    return warns


def _order_chat_canvas_branches(n: Node) -> None:
    """与画布 orderVoiceConversationBranches 对齐，保证导入锚点不错位。

    视觉顺序：收集成功在上，意图分类在下，silent 垫底。global_intent 仍置顶
    （导入时 filterGlobalReplyClasses 会剥掉，不占锚点）。
    """
    gl, success, silent, rest = [], [], [], []
    for b in n.branches:
        if b.kind == "global_intent":
            gl.append(b)
        elif b.kind == "entity_success":
            success.append(b)
        elif b.kind == "silent":
            silent.append(b)
        else:
            rest.append(b)
    n.branches = gl + success + rest + silent


def _condition_fp(b: Branch) -> tuple[Any, ...]:
    if b.kind == "else":
        return ("else",)
    if b.extra.get("method") == "llm":
        return ("llm", b.extra.get("llmPrompt") or b.content)
    conds = tuple(
        (c.get("var"), c.get("operator"), c.get("val"))
        for c in (b.extra.get("conditions") or [])
    )
    return ("rule", b.extra.get("logicType", "and"), conds)


def _base_condition_fp(raw: dict[str, Any]) -> tuple[Any, ...]:
    if raw.get("logicType") == "else":
        return ("else",)
    if raw.get("method") == "llm":
        return ("llm", raw.get("llmPrompt") or raw.get("content") or "")
    conds = tuple(
        (c.get("var"), c.get("operator"), c.get("val"))
        for c in (raw.get("conditions") or [])
    )
    return ("rule", raw.get("logicType", "and"), conds)


def _resolve_branch_ids(n: Node, base_node: dict[str, Any] | None) -> None:
    """给分支分配 id：消耗式复用 --base 里的既有 id，保证回编译后 diff 最小。

    匹配优先级：① type+content（条件节点=条件指纹）完全相同 ② 对话节点同 type 按出现顺序
    ③ 基于 key 的稳定 hash。
    「消耗式」= 每个底座 id 只会被认领一次，避免同名分支拿到重复 id。

    条件 / 工时节点绝不能按位置继承 id。中间插入新分支时，按位置会把保留字
    ``else`` 错赋给普通条件；导入时 ``edgeData.branchId`` 按 id 反查锚点，
    同 id 的出边会全部叠到第一个匹配分支上，真正的否则分支变成没连线。
    """
    pool: list[dict[str, Any]] = []
    cond_pool: list[dict[str, Any]] = []
    wt_pool: list[dict[str, Any]] = []
    if base_node:
        nd = base_node.get("nodeData", {})
        pool = [dict(b) for b in (nd.get("branches") or []) if b.get("id")]
        if n.kind == "condition":
            cond_pool = [dict(b) for b in (nd.get("logicSplitBranches") or []) if b.get("id")]
        elif n.kind == "worktime":
            wt_pool = [dict(b) for b in (nd.get("workTimeBranches") or []) if b.get("id")]
    taken: set[int] = set()
    cond_taken: set[int] = set()
    wt_taken: set[int] = set()
    used_bids: set[str] = set()

    def take_id(bid: str, idx: int | None = None, bucket: set[int] | None = None) -> str:
        if idx is not None and bucket is not None:
            bucket.add(idx)
        used_bids.add(bid)
        return bid

    def claim(kind: str, content: str) -> str | None:
        for i, b in enumerate(pool):          # ① type + content
            if i in taken:
                continue
            bid = str(b["id"])
            if bid in used_bids:
                continue
            if str(b.get("type")) == kind and str(b.get("content") or "") == content:
                return take_id(bid, i, taken)
        for i, b in enumerate(pool):          # ② 同 type 按顺序
            if i in taken:
                continue
            bid = str(b["id"])
            if bid in used_bids:
                continue
            if str(b.get("type")) == kind:
                return take_id(bid, i, taken)
        return None

    def claim_condition(b: Branch) -> str | None:
        fp = _condition_fp(b)
        for i, raw in enumerate(cond_pool):
            if i in cond_taken:
                continue
            if _base_condition_fp(raw) != fp:
                continue
            bid = str(raw["id"])
            # 底座若已被污染（普通条件占用了 else），丢掉这个 id 另发新的
            if b.kind != "else" and bid == "else":
                continue
            if bid in used_bids:
                continue
            return take_id(bid, i, cond_taken)
        return None

    def claim_worktime(b: Branch) -> str | None:
        for i, raw in enumerate(wt_pool):
            if i in wt_taken:
                continue
            bid = str(raw["id"])
            if bid in used_bids:
                continue
            if str(raw.get("type") or "") == b.kind and str(raw.get("name") or "") == b.content:
                return take_id(bid, i, wt_taken)
        return None

    for i, b in enumerate(n.branches):
        if b.bid:
            used_bids.add(b.bid)
            continue
        if n.kind == "condition" and b.kind == "else":
            b.bid = "else"
            used_bids.add("else")
            for j, raw in enumerate(cond_pool):
                if j not in cond_taken and (
                    raw.get("logicType") == "else" or str(raw.get("id")) == "else"
                ):
                    cond_taken.add(j)
            continue
        claimed = None
        if n.kind == "condition":
            claimed = claim_condition(b)
        elif n.kind == "worktime":
            claimed = claim_worktime(b)
        else:
            claimed = claim(b.kind, b.content)
        if claimed:
            b.bid = claimed  # take_id 已写入 used_bids
            continue
        bid = branch_id_of(n.key, i, b.content)
        n_try = 0
        while bid in used_bids:
            n_try += 1
            bid = branch_id_of(n.key, i, f"{b.content}::{n_try}")
        b.bid = bid
        used_bids.add(bid)


# ============================================================================
# 4.0 节点几何估算（1:1 移植画布 src/flow/utils/manualLayoutNodeSizeEstimate.ts）
#
# 画布「一键整理」用的就是这套常量。核心规律：分支区高度 =
#   24（分支标题+间距） + n × 40（每个分支块） + (n-1) × 12（分支间距）
# 即**每多一个分支，节点高 +52px**（40 分支块 + 12 间距，正好是 208 的 1/4）。
# 改画布样式后要回来核对这些常量，否则自动布局会出现视觉重叠。
# ============================================================================

NODE_W = 260                    # DEFAULT_NODE_WIDTH
H_DEFAULT = 120                 # DEFAULT_NODE_HEIGHT
V_PADDING = 24                  # NODE_VERTICAL_PADDING
LABEL_H = 36                    # LABEL_CONTENT_HEIGHT（图标 24 + 间距 12）
TITLE_GAP_H = 24                # TITLE_WITH_GAP_HEIGHT（标题 12 + 间距 12）
WELCOME_H = 108                 # WELCOME_CONTENT_HEIGHT（话术框）
COMPACT_H = 40                  # COMPACT_CONTENT_HEIGHT
CONTENT_GAP = 12                # 元素间距
BRANCH_ITEM_H = 40              # 单个分支块高度
PARAM_ROW_H = 36                # 接口节点每行参数
JUDGE_MIN_H = 56                # 条件分支最小高度
JUDGE_LINE_H = 28               # 条件分支每行
JUDGE_V_PADDING = 34            # 条件分支上下留白
GLOBAL_TIPS_H = 46              # 全局节点顶部的「全局」标记条
API_MIN_H, API_MAX_H = 420, 680
CHAT_MIN_H = 168
GLOBAL_CHAT_MIN_H = 214
HANGUP_H = 48                   # 结束通话节点（画布估算器未覆盖，用真实节点样式高度）

# 布局参数：对齐画布「一键整理」manualLayoutHumanize.ts
# 层距 = 节点宽 260 + 主干间距 160，容得下节点本身和连线弯折
MAIN_CHAIN_GAP = 160
COLUMN_GAP = NODE_W + MAIN_CHAIN_GAP          # 420
LAYER_GAP = COLUMN_GAP                        # 旧名，文档/测试仍可能引用
BRANCH_GAP = 40
ROW_GAP = BRANCH_GAP
ORIGIN_X, ORIGIN_Y = 350, 300
STATIC_H_HALF = 86                            # 画布以静态 nodeStyle.height(172) 的一半做绘制原点
BRANCH_MENU_EDGE_THRESHOLD = 4                # 出边 ≥4 时第一出口不再延续主干
BRANCH_MENU_MAX_ROWS = 3
MIN_VERTICAL_GAP = 16                         # 同列防重叠的最小视觉间隙
VERTICAL_LANE_X_TOLERANCE = 24
MERGE_NODE_MIN_INCOMING = 3
ORPHAN_FOREST_COLUMNS = 4
ORPHAN_FOREST_SAFE_Y_GAP = 240
DETACHED_LEAF_PARKING_COLUMNS = 1


def branch_section_height(count: int) -> float:
    """分支区高度：0 个分支时整块不渲染。"""
    if count <= 0:
        return 0
    return TITLE_GAP_H + count * BRANCH_ITEM_H + (count - 1) * CONTENT_GAP


def _judge_branch_height(b: Branch) -> float:
    if b.kind == "else":
        return JUDGE_MIN_H
    lines = max(2, len(b.extra.get("conditions") or []) * 2)
    return max(JUDGE_MIN_H, JUDGE_V_PADDING + lines * JUDGE_LINE_H)


def estimate_node_height(n: Node) -> float:
    """估算节点渲染高度，口径与画布「一键整理」完全一致。

    注意：分支数按 nodeData.branches 的**全量**计（含 global_intent），
    与画布估算器保持一致——偏保守，多留空间不会造成重叠。
    """
    k = n.kind
    nb = len(n.branches)
    tips = GLOBAL_TIPS_H if n.is_global else 0

    if k == "start":
        return H_DEFAULT
    if k == "end":
        return HANGUP_H

    if k == "api":
        url_lines = max(1, -(-len(n.a("URL", "") or "") // 52))
        rows = len(n.alist("请求头")) + len(n.alist("参数")) + len(n.alist("返回"))
        h = (V_PADDING + LABEL_H + TITLE_GAP_H + COMPACT_H
             + url_lines * 24 + rows * PARAM_ROW_H
             + branch_section_height(nb) + tips)
        return min(max(h, API_MIN_H), API_MAX_H)

    if k in ("chat", "announce"):
        h = V_PADDING + LABEL_H + WELCOME_H + branch_section_height(nb) + tips
        return max(GLOBAL_CHAT_MIN_H if n.is_global else CHAT_MIN_H, h)

    if k in ("dtmf-nav", "dtmf-collect"):
        return max(H_DEFAULT, 132 + branch_section_height(nb))

    if k == "condition":
        real = canvas_branch_list(n)
        bh = sum(_judge_branch_height(b) for b in real)
        gap = max(len(real) - 1, 0) * CONTENT_GAP
        return max(H_DEFAULT,
                   V_PADDING + LABEL_H + TITLE_GAP_H + bh + gap + tips)

    if k == "worktime":
        # 画布估算器未单列工时节点，按「话术框(时区) + 分支区」同构处理
        return max(H_DEFAULT,
                   V_PADDING + LABEL_H + COMPACT_H + CONTENT_GAP
                   + branch_section_height(len(canvas_branch_list(n))) + tips)

    if k == "assign":
        return max(H_DEFAULT,
                   144 + len(n.alist("变量")) * 56 + branch_section_height(nb))

    if k.startswith("transfer-"):
        return max(H_DEFAULT, 156 + branch_section_height(nb))

    return H_DEFAULT


@dataclass
class _LayoutMeta:
    parent: str | None = None
    depth: int = 0
    branch_index: int = 0
    is_main: bool = False
    is_orphan: bool = False
    orphan_rank: int = 0


@dataclass
class _BranchGroup:
    parent_id: str
    root_ids: list[str] = field(default_factory=list)
    ordered_ids: list[str] = field(default_factory=list)


@dataclass
class _LayoutModel:
    main_chain: list[str]
    branch_groups: list[_BranchGroup]
    meta: dict[str, _LayoutMeta]
    orphan_ids: list[str]
    incoming: dict[str, list[str]]


def _collect_preserved_pos(design: Design, ids: dict[str, str] | None,
                           base: dict[str, Any] | None) -> dict[str, tuple[float, float]]:
    """存量坐标：md 里残留的 `- 坐标:` 优先，否则从 --base JSON 按节点 id 继承。"""
    base_pos: dict[str, tuple[float, float]] = {}
    if base:
        for arr in (base.get("ivrData") or {}).values():
            if not isinstance(arr, list):
                continue
            for nd in arr:
                if not isinstance(nd, dict) or not nd.get("id"):
                    continue
                try:
                    base_pos[str(nd["id"])] = (float(nd.get("x") or 0), float(nd.get("y") or 0))
                except (TypeError, ValueError):
                    pass
    preserved: dict[str, tuple[float, float]] = {}
    id_of = ids or {}
    for n in design.nodes:
        if n.pos:
            preserved[n.no] = n.pos
            continue
        nid = id_of.get(n.no)
        if nid and nid in base_pos:
            preserved[n.no] = base_pos[nid]
    return preserved


def build_layout_model(design: Design) -> _LayoutModel:
    """按画布 manualLayoutOrder.ts 走主干 + 分支组 + 离散节点。

    主干 = 从开始节点沿「第一出边」走，直到某个节点出边 ≥4（意图菜单）或走到已访问节点。
    其余出边延后成分支组，绕圈用 visited 收敛。
    """
    by_no = design.by_no
    succ = {n.no: node_outgoing(n, by_no) for n in design.nodes}
    incoming: dict[str, list[str]] = {}
    for src, targets in succ.items():
        for tgt in targets:
            bucket = incoming.setdefault(tgt, [])
            if src not in bucket:
                bucket.append(src)

    start = next((n.no for n in design.nodes if n.kind == "start"), None)
    if start is None and design.nodes:
        start = design.nodes[0].no

    visited: set[str] = set()
    queued: set[str] = set()
    main_chain: list[str] = []
    branch_groups: list[_BranchGroup] = []
    group_map: dict[str, _BranchGroup] = {}
    meta: dict[str, _LayoutMeta] = {}
    branch_queue: list[tuple[str, str, int, str, bool]] = []

    def group_of(parent_id: str) -> _BranchGroup:
        existed = group_map.get(parent_id)
        if existed:
            return existed
        grp = _BranchGroup(parent_id=parent_id)
        group_map[parent_id] = grp
        branch_groups.append(grp)
        return grp

    def append_node(no: str, info: _LayoutMeta) -> bool:
        if no not in by_no or no in visited:
            return False
        visited.add(no)
        meta[no] = info
        return True

    def queue_branch(parent_id: str, target: str, branch_index: int,
                     root_parent: str, track_root: bool) -> None:
        grp = group_of(root_parent)
        if track_root and target not in grp.root_ids:
            grp.root_ids.append(target)
        if target not in grp.ordered_ids:
            grp.ordered_ids.append(target)
        if target in visited or target in queued:
            return
        queued.add(target)
        branch_queue.append((target, parent_id, branch_index, root_parent, track_root))

    def visit_chain(first: str, *, is_main: bool, parent: str | None,
                    branch_index: int, root_parent: str | None, depth: int) -> None:
        current: str | None = first
        parent_id = parent
        d = depth
        while current and append_node(current, _LayoutMeta(
            parent=parent_id, depth=d, branch_index=branch_index,
            is_main=is_main,
        )):
            if is_main:
                main_chain.append(current)
            elif root_parent:
                grp = group_of(root_parent)
                if current not in grp.ordered_ids:
                    grp.ordered_ids.append(current)
            outs = succ.get(current, [])
            stop_menu = len(outs) >= BRANCH_MENU_EDGE_THRESHOLD
            branch_edges = outs if stop_menu else outs[1:]
            for i, tgt in enumerate(branch_edges):
                queue_branch(
                    current, tgt, i,
                    current if stop_menu else (root_parent or current),
                    (not root_parent) or stop_menu,
                )
            nxt = None if stop_menu else (outs[0] if outs else None)
            if not nxt or nxt not in by_no or nxt in visited:
                break
            parent_id = current
            current = nxt
            d += 1

    def drain_branches() -> None:
        while branch_queue:
            node_id, parent_id, bidx, root_parent, _track = branch_queue.pop(0)
            parent_depth = meta[parent_id].depth if parent_id in meta else 0
            visit_chain(node_id, is_main=False, parent=parent_id,
                        branch_index=bidx, root_parent=root_parent,
                        depth=parent_depth + 1)

    if start:
        visit_chain(start, is_main=True, parent=None,
                    branch_index=0, root_parent=None, depth=0)
        drain_branches()

    def orphan_rank(no: str) -> int:
        if any(t in visited for t in succ.get(no, [])):
            return 2
        if any(no in ts and src in visited for src, ts in succ.items()):
            return 2
        return 1 if succ.get(no) else 0

    orphan_ids: list[str] = []
    for n in design.nodes:
        if n.no in visited:
            continue
        rank = orphan_rank(n.no)
        if not append_node(n.no, _LayoutMeta(
            is_main=False, is_orphan=True, orphan_rank=rank,
        )):
            continue
        orphan_ids.append(n.no)
        for i, tgt in enumerate(succ.get(n.no, [])):
            if tgt not in visited or meta.get(tgt, _LayoutMeta()).is_orphan:
                queue_branch(n.no, tgt, i, n.no, True)
        drain_branches()

    return _LayoutModel(main_chain=main_chain, branch_groups=branch_groups,
                        meta=meta, orphan_ids=orphan_ids, incoming=incoming)


def visual_box(n: Node, xy: tuple[float, float]) -> tuple[float, float, float, float]:
    """G6 绘制盒：原点在静态 172×260 的中心，节点向下长出真实高度。"""
    x, y = xy
    left = x - NODE_W / 2
    top = y - STATIC_H_HALF
    return left, top, left + NODE_W, top + estimate_node_height(n)


def boxes_overlap(a: tuple[float, float, float, float],
                  b: tuple[float, float, float, float],
                  gap: float = MIN_VERTICAL_GAP) -> bool:
    l1, t1, r1, b1 = a
    l2, t2, r2, b2 = b
    return not (r1 + gap <= l2 or r2 + gap <= l1 or b1 + gap <= t2 or b2 + gap <= t1)


def _place_if_free(pos: dict[str, tuple[float, float]], fixed: set[str],
                   no: str, x: float, y: float) -> tuple[float, float]:
    if no in pos and no in fixed:
        return pos[no]
    pos[no] = (x, y)
    return x, y


def _place_branch_descendants(
    no: str, x: float, y: float, model: _LayoutModel,
    pos: dict[str, tuple[float, float]], fixed: set[str],
) -> None:
    children = [
        (m.branch_index, child)
        for child, m in model.meta.items()
        if m.parent == no and not m.is_main
    ]
    children.sort()
    for _, child in children:
        cx, cy = _place_if_free(pos, fixed, child, x + COLUMN_GAP, y)
        _place_branch_descendants(child, cx, cy, model, pos, fixed)


def _balanced_menu_rows(count: int, row_gap: float) -> int:
    if count <= 0:
        return 1
    balanced = max(1, int(-(-((count * (COLUMN_GAP / max(row_gap, 1))) ** 0.5) // 1)))
    return max(1, min(BRANCH_MENU_MAX_ROWS, count, balanced))


def _place_branch_menu_grid(
    parent_x: float, parent_y: float, roots: list[str],
    heights: dict[str, float], model: _LayoutModel,
    pos: dict[str, tuple[float, float]], fixed: set[str],
) -> None:
    row_gap = (heights[roots[0]] if roots else H_DEFAULT) + BRANCH_GAP
    row_count = _balanced_menu_rows(len(roots), row_gap)
    for index, root in enumerate(roots):
        col = index // row_count
        row = index % row_count
        rows_in_col = min(row_count, len(roots) - col * row_count)
        x = parent_x + COLUMN_GAP + col * COLUMN_GAP
        y = parent_y + (row - (rows_in_col - 1) / 2) * (heights[root] + BRANCH_GAP)
        rx, ry = _place_if_free(pos, fixed, root, x, y)
        _place_branch_descendants(root, rx, ry, model, pos, fixed)


def _place_branch_column(
    parent_x: float, parent_y: float, roots: list[str],
    heights: dict[str, float], model: _LayoutModel,
    pos: dict[str, tuple[float, float]], fixed: set[str],
) -> None:
    total_h = sum(heights[r] for r in roots) + max(len(roots) - 1, 0) * BRANCH_GAP
    cursor_y = parent_y - total_h / 2
    branch_x = parent_x + COLUMN_GAP
    for root in roots:
        y = cursor_y + heights[root] / 2
        rx, ry = _place_if_free(pos, fixed, root, branch_x, y)
        _place_branch_descendants(root, rx, ry, model, pos, fixed)
        cursor_y += heights[root] + BRANCH_GAP


def _place_orphan_forest(
    model: _LayoutModel, heights: dict[str, float],
    pos: dict[str, tuple[float, float]], fixed: set[str],
) -> None:
    if not model.orphan_ids or not model.main_chain:
        return
    order_map = {no: i for i, no in enumerate(model.orphan_ids)}
    roots = [no for no in model.orphan_ids if no in heights]
    roots.sort(key=lambda no: (
        -(model.meta[no].orphan_rank if no in model.meta else 0),
        order_map.get(no, 0),
    ))
    start_x = min(pos[no][0] for no in model.main_chain if no in pos)
    main_h = max((heights[no] for no in model.main_chain), default=H_DEFAULT)
    baseline_y = pos[model.main_chain[0]][1] if model.main_chain[0] in pos else ORIGIN_Y
    top_y = baseline_y + main_h / 2 + ORPHAN_FOREST_SAFE_Y_GAP

    def is_detached_leaf(no: str) -> bool:
        m = model.meta.get(no)
        if not m or not m.is_orphan or m.orphan_rank > 0:
            return False
        return not any(
            g.parent_id == no or no in g.ordered_ids for g in model.branch_groups
        )

    has_connected = any(not is_detached_leaf(no) for no in roots)
    detached = [no for no in roots if is_detached_leaf(no)] if has_connected else []
    regular = [no for no in roots if not is_detached_leaf(no)] if has_connected else roots

    def place_in_columns(items: list[str], origin_x: float, columns: int) -> None:
        if not items:
            return
        cursors = [top_y] * columns
        for no in items:
            col = min(range(columns), key=lambda i: cursors[i])
            _place_if_free(
                pos, fixed, no,
                origin_x + col * COLUMN_GAP,
                cursors[col] + heights[no] / 2,
            )
            cursors[col] += heights[no] + BRANCH_GAP

    place_in_columns(regular, start_x, ORPHAN_FOREST_COLUMNS)
    place_in_columns(
        detached,
        start_x - COLUMN_GAP * DETACHED_LEAF_PARKING_COLUMNS,
        DETACHED_LEAF_PARKING_COLUMNS,
    )


def _place_merge_nodes(
    model: _LayoutModel, pos: dict[str, tuple[float, float]], locked: set[str],
) -> None:
    protected = {
        rid
        for g in model.branch_groups
        if len(g.root_ids) >= BRANCH_MENU_EDGE_THRESHOLD
        for rid in g.root_ids
    }
    for no, preds in model.incoming.items():
        if (no in locked or no in protected or no in model.main_chain
                or len(preds) < MERGE_NODE_MIN_INCOMING):
            continue
        current = pos.get(no)
        incoming_pos = [pos[p] for p in preds if p in pos]
        if not current or len(incoming_pos) < MERGE_NODE_MIN_INCOMING:
            continue
        right = [p for p in incoming_pos if p[0] > current[0]]
        if len(right) <= len(incoming_pos) / 2:
            continue
        ys = sorted(p[1] for p in incoming_pos)
        mid = ys[len(ys) // 2] if len(ys) % 2 else (ys[len(ys) // 2 - 1] + ys[len(ys) // 2]) / 2
        pos[no] = (max(p[0] for p in incoming_pos) + COLUMN_GAP, mid)


def _resolve_column_collisions(
    design: Design, model: _LayoutModel, heights: dict[str, float],
    pos: dict[str, tuple[float, float]], fixed: set[str],
) -> None:
    """同列按 G6 视觉盒推开。主干尽量保持水平；`--base` / 手写坐标绝不移动。"""
    by_no = design.by_no
    order = {n.no: i for i, n in enumerate(design.nodes)}
    columns: list[list[str]] = []
    for no in sorted(pos, key=lambda k: (pos[k][0], pos[k][1], order.get(k, 0))):
        x = pos[no][0]
        if columns and abs(x - pos[columns[-1][0]][0]) <= VERTICAL_LANE_X_TOLERANCE:
            columns[-1].append(no)
        else:
            columns.append([no])

    for col in columns:
        items = [{
            "no": no,
            "y": pos[no][1],
            "h": heights[no],
            "fixed": no in fixed,
            "sticky": no in fixed or no in model.main_chain,
            "bidx": model.meta[no].branch_index if no in model.meta else order.get(no, 0),
            "idx": order.get(no, 0),
        } for no in col if no in by_no]

        def sort_key(it: dict[str, Any]) -> tuple:
            return (it["y"], it["bidx"], it["idx"])

        for _ in range(len(items) + 2):
            items.sort(key=sort_key)
            changed = False
            for i in range(1, len(items)):
                prev, cur = items[i - 1], items[i]
                min_y = prev["y"] + prev["h"] + MIN_VERTICAL_GAP
                if cur["y"] >= min_y:
                    continue
                if not cur["sticky"]:
                    cur["y"] = min_y
                    changed = True
                    continue
                if not prev["sticky"]:
                    prev["y"] = cur["y"] - prev["h"] - MIN_VERTICAL_GAP
                    for j in range(i - 1, 0, -1):
                        upper, lower = items[j - 1], items[j]
                        need = lower["y"] - upper["h"] - MIN_VERTICAL_GAP
                        if upper["y"] <= need or upper["sticky"]:
                            break
                        upper["y"] = need
                    changed = True
                    continue
                if not cur["fixed"]:
                    cur["y"] = min_y
                    changed = True
                    continue
                if not prev["fixed"]:
                    prev["y"] = cur["y"] - prev["h"] - MIN_VERTICAL_GAP
                    for j in range(i - 1, 0, -1):
                        upper, lower = items[j - 1], items[j]
                        need = lower["y"] - upper["h"] - MIN_VERTICAL_GAP
                        if upper["y"] <= need or upper["fixed"]:
                            break
                        upper["y"] = need
                    changed = True
            if not changed:
                break
        for it in items:
            x, _ = pos[it["no"]]
            pos[it["no"]] = (x, it["y"])


def layout(design: Design, ids: dict[str, str] | None = None,
           base: dict[str, Any] | None = None,
           relayout: bool = False) -> dict[str, tuple[float, float]]:
    """按流程顺序排版：主干从左到右，分支在父节点右侧纵向展开，同列不重叠。

    算法对齐画布「一键整理」（manualLayoutOrder + manualLayoutHumanize +
    manualLayoutCollision）。节点高度用 estimate_node_height() 估算。
    默认存量节点坐标取 --base JSON 的 x/y（md 不维护坐标）；分片里若仍写了
    `- 坐标:`（旧稿）则仍尊重。`--relayout` 时忽略旧坐标，全部按流程重排。
    """
    if not design.nodes:
        return {}
    by_no = design.by_no
    heights = {n.no: estimate_node_height(n) for n in design.nodes}
    model = build_layout_model(design)
    preserved = {} if relayout else _collect_preserved_pos(design, ids, base)
    pos: dict[str, tuple[float, float]] = dict(preserved)
    locked = set(preserved)

    for i, no in enumerate(model.main_chain):
        if no in pos:
            locked.add(no)
            continue
        if i == 0:
            _place_if_free(pos, locked, no, ORIGIN_X, ORIGIN_Y)
        else:
            px, py = pos.get(model.main_chain[i - 1], (ORIGIN_X, ORIGIN_Y))
            _place_if_free(pos, locked, no, px + COLUMN_GAP, py)
        locked.add(no)

    _place_orphan_forest(model, heights, pos, locked)

    large_menu_owner: dict[str, str] = {}
    for grp in model.branch_groups:
        if len(grp.root_ids) < BRANCH_MENU_EDGE_THRESHOLD:
            continue
        for rid in grp.root_ids:
            large_menu_owner.setdefault(rid, grp.parent_id)

    for grp in model.branch_groups:
        parent_pos = pos.get(grp.parent_id)
        if not parent_pos:
            continue
        roots = [
            rid for rid in grp.root_ids
            if rid in by_no
            and rid not in locked
            and large_menu_owner.get(rid, grp.parent_id) == grp.parent_id
        ]
        if not roots:
            continue
        if len(roots) >= BRANCH_MENU_EDGE_THRESHOLD:
            _place_branch_menu_grid(
                parent_pos[0], parent_pos[1], roots, heights, model, pos, locked)
        else:
            _place_branch_column(
                parent_pos[0], parent_pos[1], roots, heights, model, pos, locked)

    _place_merge_nodes(model, pos, locked)
    for n in design.nodes:
        if n.no not in pos:
            pos[n.no] = (ORIGIN_X, ORIGIN_Y)
    _resolve_column_collisions(design, model, heights, pos, set(preserved))
    return pos


def branch_anchor_dy(kind: str, anchor: int) -> float:
    """第 anchor(1-based) 个分支出锚点相对节点 y 的偏移。

    导入时 startPoint 的 x/y 会被忽略（只读 anchorIndex），这里算准只为了
    导出后坐标看着合理、以及万一画布哪天用上这个值不至于错位。
    """
    if kind in ("chat", "announce"):
        head = V_PADDING + LABEL_H + WELCOME_H + TITLE_GAP_H
    elif kind in ("api", "worktime", "dtmf-nav", "dtmf-collect"):
        head = V_PADDING + LABEL_H + COMPACT_H + CONTENT_GAP + TITLE_GAP_H
    else:
        head = V_PADDING + LABEL_H + TITLE_GAP_H
    idx = max(1, anchor) - 1
    return head + idx * (BRANCH_ITEM_H + CONTENT_GAP) + BRANCH_ITEM_H / 2 - STATIC_H_HALF


def _edge(src_key: str, tgt_key: str, source_id: str, target_id: str,
          spos: tuple[float, float], tpos: tuple[float, float],
          anchor: int, etype: str, branch_id: str | None,
          kind: str = "chat") -> dict[str, Any]:
    dy = branch_anchor_dy(kind, anchor) if branch_id else 0
    return {
        "id": edge_id_of(src_key, tgt_key),
        "source": source_id,
        "target": target_id,
        "type": etype,
        "name": "",
        "startPoint": {"x": spos[0] + 134, "y": spos[1] + dy, "anchorIndex": anchor},
        "endPoint": {"x": tpos[0], "y": tpos[1], "anchorIndex": 0},
        # edgeData.branchId 在导入时优先级最高，写上可完全免疫 anchorIndex 偏差
        "edgeData": {"branchId": branch_id} if branch_id else {},
    }


def build_json(design: Design, base: dict[str, Any] | None = None,
               rewrap: bool = True, relayout: bool = False) -> dict[str, Any]:
    """rewrap=True 时把话术里的排版硬折行合回一行（--base 保真模式下调用方会关掉）。"""
    if rewrap:
        design.rewrap_stats = 0
        for _n in design.nodes:
            if _n.prompt:
                _n.prompt, _m = rewrap_text(_n.prompt)
                design.rewrap_stats += _m
        if design.system_prompt:
            design.system_prompt, _m = rewrap_text(design.system_prompt)
            design.rewrap_stats += _m
    by_no = design.by_no
    base_by_id: dict[str, dict[str, Any]] = {}
    if base:
        for arr in (base.get("ivrData") or {}).values():
            for nd in arr:
                if nd.get("id"):
                    base_by_id[nd["id"]] = nd

    ids = resolve_node_ids(design, base)
    design.node_ids = ids
    pos = layout(design, ids=ids, base=base, relayout=relayout)

    ivr: dict[str, list[dict[str, Any]]] = {}
    for n in design.nodes:
        nid = ids[n.no]
        bnode = base_by_id.get(nid)
        for w in _normalize_branches(n):
            design.notes.append((n.key, w))
        _resolve_branch_ids(n, bnode)
        node_data = _node_data(n, design, nid,
                                (bnode or {}).get("nodeData", {}).get("vars"))
        if bnode:  # --base：md 未表达的字段从底座继承
            merged = dict(bnode.get("nodeData") or {})
            merged.update(node_data)
            bcc = (bnode.get("nodeData") or {}).get("collectionConfig") or {}
            ncc = node_data.get("collectionConfig") or {}
            if (bcc and ncc
                    and str(bcc.get("collectionType")) == str(ncc.get("collectionType"))
                    and bcc.get("collectionAsValue") == ncc.get("collectionAsValue")
                    and bcc.get("collectionItems")):
                merged["collectionConfig"] = bcc      # 收集项 id 属于服务端资源，原样保留
            node_data = merged

        if n.kind in ("chat", "announce") and not _listens(n):
            node_data.pop("replyMode", None)

        cbrs = canvas_branch_list(n)
        edges: list[dict[str, Any]] = []
        # 没有回复分类时按单出口生成（source=节点 id），不能走多出口分支循环。
        use_multi = (
            n.kind in MULTI_EXIT_KINDS
            and bool(cbrs)
            and not (n.kind == "chat" and not _listens(n))
        )
        if use_multi:
            for i, b in enumerate(cbrs):
                if not b.target:
                    continue
                edges.append(_edge(
                    f"{n.key}:{b.content}", by_no[b.target].key, b.bid or "", ids[b.target],
                    pos[n.no], pos[b.target], i + 1, "custom:cubic-horizontal", b.bid, n.kind,
                ))
        elif n.kind not in TERMINAL_KINDS:
            tgt = n.next or (cbrs[0].target if cbrs else None)
            if tgt:
                etype = "custom:cubic-horizontal" if n.kind in ("chat", "announce") else ""
                edges.append(_edge(
                    f"{n.key}:next", by_no[tgt].key, nid, ids[tgt],
                    pos[n.no], pos[tgt], 1, etype, None, n.kind,
                ))

        node = {
            "id": nid,
            "x": pos[n.no][0],
            "y": pos[n.no][1],
            "type": KIND_TO_TYPE[n.kind],
            "name": n.name,
            "nodeData": node_data,
            "outEdges": edges,
        }
        ivr.setdefault(KIND_TO_TYPE[n.kind], []).append(node)

    vs = dict(DEFAULT_VOICE_SETTINGS)
    if base and base.get("voiceSettings"):
        vs.update(base["voiceSettings"])
    for k, v in design.env.items():
        if k in ENV_TO_VOICE:
            fld, typ = ENV_TO_VOICE[k]
            if v == "":
                continue
            vs[fld] = {"str": lambda x: x, "int": lambda x: int(_to_num(x)),
                       "num": _to_num, "bool": _to_bool,
                       "list": lambda x: [i.strip() for i in re.split(r"[,，\s]+", x) if i.strip()],
                       }[typ](v)
    vs["systemPrompt"] = design.system_prompt
    return {"version": VERSION, "ivrData": ivr, "voiceSettings": vs}


def _mk_branch(b: Branch) -> dict[str, Any]:
    return {
        "id": b.bid,
        "type": b.kind,
        "content": b.content,
        "name": "",
        "tags": [{"tagName": k, "tagValue": v} for k, v in b.tags],
    }


def _global_items(n: Node) -> list[dict[str, Any]]:
    """全局触发分支：condition/worktime/assign/transfer 等把它放在 nodeData.branches。"""
    return [_mk_branch(b) for b in n.branches if b.kind == "global_intent"]


def _node_data(n: Node, d: Design, nid: str,
               base_vars: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    k = n.kind
    if k == "start" or k == "end":
        return {}

    if k in ("chat", "announce"):
        listens = _listens(n)
        var = n.a("收集变量")
        content_mode = n.a("话术模式", "智能生成")
        data: dict[str, Any] = {
            "welcomeText": n.prompt,
            "content": n.prompt,
            "fileId": "",
            "contentType": "fix" if content_mode.startswith("固定") else "gen",
            "selectBranch": listens,
            "allowInterrupt": n.abool("允许打断", False),
            "branches": [_mk_branch(b) for b in n.branches],
            "entities": [],
            "verifyEntity": n.abool("校验实体", False),
            "queryVarName": n.a("查询变量", "") or "",
            "globalNode": n.is_global,
        }
        if n.a("静默等待(秒)") is not None:
            data["silentWaitTime"] = n.aint("静默等待(秒)", 10)
        if var:
            slot_id = n.a("词槽ID")
            # 词槽目录只认 custom。原稿即使写了 date/name，这里也强制 custom，并给出临时 id 以便发布建槽。
            if slot_id:
                ctype = str(slot_id)
            else:
                ctype = entity_tmp_id_of(n.key, var)
            ent: dict[str, Any] = {
                "slotType": "custom",
                "varName": var,
                "name": n.a("词槽名称", var) or var,
                "description": _unesc(n.a("词槽说明", "") or ""),
            }
            if n.a("收集方式") is not None:
                ent["collectionMode"] = (
                    "fixed" if (n.a("收集方式") or "").startswith("固定") else "open")
            if slot_id:
                # 复用真实环境已存在的服务端词槽，避免重复建槽
                try:
                    ent["slotId"] = int(slot_id)
                except ValueError:
                    ent["slotId"] = slot_id
                ent = {"slotId": ent["slotId"], **{k: v for k, v in ent.items() if k != "slotId"}}
            else:
                # 前端临时 id：发布/保存时会建槽并回填真实 slotId
                ent["id"] = ctype
            if ent.get("collectionMode") == "fixed" and n.a("固定选项"):
                ent["fixedOptions"] = n.a("固定选项")
            data["entities"] = [ent]
            data["collectionConfig"] = {
                "collectionType": ctype,
                "collectionAsValue": var,
                "collectionItems": [{
                    "id": collection_item_id_of(n.key, var),
                    "collectionType": ctype,
                    "collectionAsValue": var,
                }],
            }
            data["replyMode"] = "intent_with_entity"
        elif listens:
            data["replyMode"] = "intent"
        return data

    if k == "api":
        auth = (n.a("鉴权", "无") or "无").lower()
        auth_type = {"无": 0, "none": 0, "basic": 1, "bearer": 2, "custom": 3, "oauth2": 4}.get(auth, 0)
        return {
            "url": n.a("URL", "") or n.a("地址", "") or "",
            "callbackTimeout": n.aint("超时(毫秒)", 4500),
            "headerParams": [_kv(x) for x in n.alist("请求头")],
            "params": [_param(x) for x in n.alist("参数")],
            "returns": [_ret(x) for x in n.alist("返回")],
            "async": n.abool("异步", False),
            "retryTimes": n.aint("重试次数", 0),
            "authType": auth_type,
            "basicAuth": {"basicToken": n.a("basicToken", "") or ""},
            "bearerAuth": {"bearerToken": n.a("bearerToken", "") or ""},
            "customAuth": {"key": "", "value": ""},
            "oauth2Auth": {"tokenURL": "", "clientId": "", "clientSecret": ""},
            "globalNode": n.is_global,
            "branches": [_mk_branch(b) for b in n.branches],
            "globalReplyClasses": [],
        }

    if k == "assign":
        vars_: list[dict[str, Any]] = []
        reuse_id = {v.get("name"): v.get("id") for v in (base_vars or []) if v.get("name")}
        for spec in n.alist("变量"):
            name, mode, desc, val = _parse_assign(spec)
            vars_.append({
                "id": reuse_id.get(name) or assign_var_id_of(n.key, name),
                "name": name,
                "mode": mode,
                "description": _unesc(desc),
                "value": val,
                "needSave": True,
                "messageRounds": n.aint("记忆轮数", 5),
            })
        return {"vars": vars_, "globalNode": n.is_global, "branches": _global_items(n)}

    if k == "condition":
        lsb: list[dict[str, Any]] = []
        for b in canvas_branch_list(n):
            if b.kind == "else":
                lsb.append({"id": "else", "method": "rule", "logicType": "else"})
            elif b.extra.get("method") == "llm":
                lsb.append({"id": b.bid, "method": "llm", "logicType": "and",
                            "content": b.content, "llmPrompt": b.extra.get("llmPrompt", "")})
            else:
                lsb.append({"id": b.bid, "method": "rule",
                            "logicType": b.extra.get("logicType", "and"),
                            "conditions": b.extra.get("conditions", [])})
        return {"branches": _global_items(n), "logicSplitBranches": lsb,
                "globalNode": n.is_global}

    if k == "worktime":
        wtb: list[dict[str, Any]] = []
        for b in canvas_branch_list(n):
            wtb.append({
                "id": b.bid,
                "type": b.kind,
                "name": b.content,
                "workTimeConfigs": b.extra.get("configs", []),
            })
        return {
            "timeZoneName": n.a("时区", "Asia/Shanghai") or "Asia/Shanghai",
            "globalNode": n.is_global,
            "workTimeBranches": wtb,
            "branches": _global_items(n),
        }

    if k in ("dtmf-nav", "dtmf-collect"):
        nav = k == "dtmf-nav"
        keys = [b.content for b in n.branches if b.kind == "dtmf_success"]
        cfg = {
            "play_sound": _tts(n.a("话术", "") or n.prompt),
            "max_failures": str(n.aint("失败次数", 1)),
            "dtmf_timeout": str(n.aint("超时(毫秒)", 5000)),
            "invalid_sound": _tts(n.a("按错话术", "您的输入错误，请重新输入。")),
            "timeout_sound": _tts(n.a("超时话术", "您的输入超时，请重新输入。")),
            "dtmf_type": "fixed",
            "dtmf_len": str(n.aint("位数", 1)),
        }
        if nav:
            cfg["keywords"] = keys
        return {
            "nodeType": "navigation" if nav else "collection",
            "varName": (n.a("收集变量", "") or "") if not nav else "",
            "globalNode": n.is_global,
            "branches": [_mk_branch(b) for b in n.branches],
            "tcccNodeData": {"dtmfConfig": cfg},
        }

    if k in ("transfer-skill", "transfer-outer", "transfer-3rd"):
        mode = {"transfer-skill": "manual", "transfer-outer": "outer",
                "transfer-3rd": "third_party_route"}[k]
        voice = d.env.get("音色") or DEFAULT_VOICE_SETTINGS["voiceType"]
        sg = n.a("技能组ID", d.env.get("技能组ID", "")) or ""
        data = {
            "name": sg if mode == "manual" else "",
            "caller": n.a("主叫", "") or "",
            "callee": n.a("被叫", "") or "",
            "uui": n.a("uui", "") or "",
            "mode": mode,
            "transferring-sound-locale": "zh-CN",
            "transferring-sound-voice": voice,
            "transferring-sound": _tts(n.a("转接提示音", "正在为您转接，请稍后")),
            "transferring-music-locale": "zh-CN",
            "transferring-music-voice": voice,
            "transferring-music": DEFAULT_TRANSFER_MUSIC,
            "broadcast-sound-locale": "zh-CN",
            "broadcast-sound-voice": voice,
            "broadcast-sound": _tts("客服${StaffNo}号为您服务"),
            "transfer-timeout": n.aint("转接超时(秒)", 30) if mode == "manual" else 0,
            "transfer-timeout-unit": "s",
            "timeout": 0 if mode == "manual" else n.aint("转接超时(秒)", 30) * 1000,
            "transfer-timeout-music-locale": "zh-CN",
            "transfer-timeout-music-voice": voice,
            "transfer-timeout-music": _tts(
                n.a("超时话术", "座席繁忙，暂时无法为您转接。感谢您的来电，祝您生活愉快！再见！")),
            "transfer-error-locale": "zh-CN",
            "transfer-error-voice": voice,
            "transfer-error": _tts(n.a("失败话术", "当前所有人工座席不在线，欢迎您于工作时间再次联系")),
            "transfer-error-speed": 1,
            "broadcast-sound-speed": 1,
            "transferring-music-speed": 1,
            "transferring-sound-speed": 1,
            "transfer-timeout-music-speed": 1,
            "overflow": [],
            "skillGroupOverflow": "",
            "client-priority": n.a("优先级", "5") or "5",
            "assignedAgentPriority": "",
            "assignedSkillPriority": "",
            "aiTransferContext": dict(DEFAULT_AI_TRANSFER_CONTEXT),
            "globalNode": n.is_global,
            "branches": _global_items(n),
        }
        if n.abool("转人工带摘要", False):
            data["aiTransferContext"]["enableSummary"] = True
        return data

    if k == "transfer-agent":
        agent = n.a("智能体ID", d.env.get("转接智能体ID", "")) or ""
        return {
            "varAgentID": agent,
            "globalNode": n.is_global,
            "branches": [{**g, "examples": []} for g in _global_items(n)] + [{
                "id": nid, "type": "transfer_agent_fail", "content": "转接失败",
                "name": "", "examples": [], "tags": [],
            }],
        }

    raise DesignError(f"未实现的节点类型：{k}")


def _tts(s: str | None) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return s if s.startswith("tts:") else "tts:" + s


def _kv(spec: str) -> dict[str, str]:
    k, _, v = spec.partition("=")
    return {"key": k.strip(), "value": v.strip()}


def _param(spec: str) -> dict[str, str]:
    body, _, vt = spec.rpartition(":")
    if not body:
        body, vt = spec, "string"
    k, _, v = body.partition("=")
    return {"key": k.strip(), "value": v.strip(), "valueType": (vt.strip() or "string")}


def _ret(spec: str) -> dict[str, str]:
    parts = ARROW.split(spec, maxsplit=1)
    if len(parts) != 2:
        raise DesignError(f"接口返回映射格式应为「- 返回: data.eta → 预计送达时间」，实际是「{spec}」")
    return {"key": parts[0].strip(), "alias": parts[1].strip()}


def _parse_assign(spec: str) -> tuple[str, str, str, str]:
    """解析：变量名 = LLM提取: 描述 / = 固定值: xxx / = 表达式: xxx"""
    name, _, rhs = spec.partition("=")
    name, rhs = name.strip(), rhs.strip()
    # 画布只认两种 mode：llm（大模型提取）/ fix（固定值或 ${变量} 表达式）
    mode, payload = "llm", rhs
    for kw, m in (("LLM提取", "llm"), ("LLM", "llm"), ("固定值", "fix"),
                  ("表达式", "fix"), ("fix", "fix")):
        if rhs.startswith(kw):
            mode = m
            payload = re.split(r"[:：]", rhs, maxsplit=1)[-1].strip() if re.search(r"[:：]", rhs) else ""
            break
    if mode == "llm":
        return name, mode, payload, ""
    return name, mode, "", payload


# ============================================================================
# 5. 校验
# ============================================================================

def _clean_var_names(names: Any) -> set[str]:
    """过滤脏变量名：空、嵌套 ${}、含花括号的一律丢弃。"""
    out: set[str] = set()
    for v in names:
        s = str(v).strip()
        if not s or "$" in s or "{" in s or "}" in s:
            continue
        out.add(s)
    return out


@dataclass
class Issue:
    level: str   # error / warn
    code: str
    where: str
    msg: str
    src: str = ""    # 出问题的 src 分片相对路径（build src 时回填）


def validate(flow: dict[str, Any], design: Design | None = None) -> list[Issue]:
    out: list[Issue] = []
    ivr = flow.get("ivrData") or {}
    nodes: list[dict[str, Any]] = [n for arr in ivr.values() for n in arr]
    by_id = {n.get("id"): n for n in nodes}

    def add(lv: str, code: str, where: str, msg: str) -> None:
        out.append(Issue(lv, code, where, msg))

    def check_len(text: Any, limit: int, where: str, what: str) -> None:
        """E12/W9：画布输入框字数上限。超长会被静默截断，必须编译期拦住。"""
        n_chars = len(str(text or ""))
        if n_chars > limit:
            add("error", "E12", where, f"{what} {n_chars} 字，超过画布上限 {limit} 字")
        elif n_chars > limit * NEAR_LIMIT_RATIO:
            add("warn", "W9", where, f"{what} {n_chars} 字，接近画布上限 {limit} 字")

    def check_tts_len(val: Any, where: str, what: str) -> None:
        """播报类字段：`tts:` 开头才是文本，其余是音频 base64，不量长度。"""
        s = str(val or "")
        if s.startswith("tts:"):
            check_len(s[4:], TTS_TEXT_MAX, where, what)

    # E1 唯一 startNode
    starts = ivr.get("startNode") or []
    if len(starts) != 1:
        add("error", "E1", "startNode", f"必须有且仅有 1 个开始节点，实际 {len(starts)} 个")
    elif len(starts[0].get("outEdges") or []) != 1:
        add("error", "E1", starts[0].get("name", "开始通话"),
            f"开始节点必须恰好 1 条出边，实际 {len(starts[0].get('outEdges') or [])} 条")

    # E2 id 唯一非空
    seen: set[str] = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            add("error", "E2", n.get("name", "?"), "节点 id 为空")
        elif nid in seen:
            add("error", "E2", n.get("name", "?"), f"节点 id 重复：{nid}")
        else:
            seen.add(nid)
    if len(nodes) < 2:
        add("error", "E2", "画布", "画布至少要有 2 个节点才允许保存")

    edge_ids: set[str] = set()
    for n in nodes:
        for e in n.get("outEdges") or []:
            eid = e.get("id")
            if not eid:
                add("error", "E2", n.get("name", "?"), "存在 id 为空的连线")
            elif eid in edge_ids:
                add("error", "E2", n.get("name", "?"), f"连线 id 重复：{eid}")
            else:
                edge_ids.add(eid)
            if e.get("target") not in by_id:
                add("error", "E4", n.get("name", "?"), f"连线指向了不存在的节点 {e.get('target')}")

    for n in nodes:
        name = n.get("name", "?")
        nd = n.get("nodeData") or {}
        typ = n.get("type")
        edges = n.get("outEdges") or []
        is_global = bool(nd.get("globalNode"))
        chat_canvas_br = [
            b for b in (nd.get("branches") or []) if b.get("type") != "global_intent"
        ] if typ == "chatNode" else []
        # 没配意图分支时仍是单出口（source=节点 id），不论 selectBranch
        single = typ in ("startNode", "extractVariableNode") or (
            typ == "chatNode" and (not nd.get("selectBranch") or not chat_canvas_br))
        terminal = typ in ("hangup", "transfer", "transferAgentNode")

        # E3 单出口 / 终点
        if terminal and edges:
            add("error", "E3", name, "结束通话/转接类节点不允许有出口连线")
        if single and len(edges) > 1:
            add("error", "E3", name,
                f"该节点是单出口（纯播报对话节点 selectBranch=false 也算），只能有 1 条出边，实际 {len(edges)} 条")
        if single:
            for e in edges:
                if e.get("source") != n.get("id"):
                    add("error", "E3", name, "单出口节点的连线 source 必须等于节点自身 id")

        # E13 自连
        for e in edges:
            if e.get("target") == n.get("id") and single:
                add("error", "E13", name, "单出口节点不能连接自己")

        # E4 多出口 source 必须是本节点分支 id
        if not single and not terminal:
            pool = {str(b.get("id")) for b in (nd.get("branches") or [])}
            pool |= {str(b.get("id")) for b in (nd.get("logicSplitBranches") or [])}
            pool |= {str(b.get("id")) for b in (nd.get("workTimeBranches") or [])}
            for e in edges:
                if str(e.get("source")) not in pool:
                    add("error", "E4", name,
                        f"连线 source={e.get('source')} 不在本节点分支 id 列表里")

        # E18 同一节点内分支 id 必须唯一。条件节点的保留字 else 只能给否则分支。
        # 重复 id 会让导入按 branchId 反查锚点时把多条出边叠到第一个匹配分支上。
        branch_objs: list[dict[str, Any]] = []
        for key in ("branches", "logicSplitBranches", "workTimeBranches"):
            branch_objs.extend(nd.get(key) or [])
        bid_count: dict[str, int] = {}
        for b in branch_objs:
            bid = str(b.get("id") or "")
            if not bid:
                continue
            bid_count[bid] = bid_count.get(bid, 0) + 1
        for bid, cnt in bid_count.items():
            if cnt > 1:
                add("error", "E18", name,
                    f"分支 id 重复：{bid} 出现 {cnt} 次。导入后同 id 的出边会叠到同一个锚点")
        if typ == "logicSplitNode":
            for b in nd.get("logicSplitBranches") or []:
                if str(b.get("id")) == "else" and b.get("logicType") != "else":
                    add("error", "E18", name,
                        "非否则分支占用了保留 id「else」，导入后否则分支的连线会错接到该分支")
                ncond = len(b.get("conditions") or [])
                if ncond > LOGIC_SPLIT_BRANCH_CONDITION_MAX:
                    bn = b.get("name") or b.get("content") or b.get("id")
                    add("error", "E19", name,
                        f"分支「{bn}」{_over_limit_branch_conditions_msg(ncond)}")

        # E7 听回复的全局对话节点必须有非空 global_intent。
        # 纯播报全局节点（selectBranch=false）线上允许没有触发语：存量画布会把
        # 孤立节点标成全局，只为通过「必须连入主路」校验。
        if is_global and typ == "chatNode":
            gi = [b for b in (nd.get("branches") or [])
                  if b.get("type") == "global_intent" and str(b.get("content") or "").strip()]
            if not gi and nd.get("selectBranch") and chat_canvas_br:
                add("error", "E7", name, "全局节点必须至少有一条非空的全局触发语（global_intent）")

        # E8 分支 content 非空
        for b in nd.get("branches") or []:
            if b.get("type") in ("intent", "global_intent") and not str(b.get("content") or "").strip():
                add("error", "E8", name, "存在空的回复分类（intent 分支 content 不能为空）")

        # E9 分支必须全连线
        if typ in ("logicSplitNode", "workTimeNode", "DTMFNode"):
            key = {"logicSplitNode": "logicSplitBranches",
                   "workTimeNode": "workTimeBranches", "DTMFNode": "branches"}[typ]
            srcs = {str(e.get("source")) for e in edges}
            for b in nd.get(key) or []:
                if str(b.get("id")) not in srcs:
                    add("error", "E9", name,
                        f"分支「{b.get('name') or b.get('content') or b.get('id')}」没有连线，"
                        f"这类节点不允许悬空分支")

        # E16 工作时间配置：dayType 写错或缺 daysOfWeek/specificDates，
        # 导入时整条规则会被静默丢弃，节点退回「未配置」，保存报「请配置工作时间判断」
        if typ == "workTimeNode":
            wtb = nd.get("workTimeBranches") or []
            if not [b for b in wtb if b.get("type") == "other"]:
                add("error", "E16", name, "缺少 type=other 的「其他时间」分支")
            for b in wtb:
                if b.get("type") == "other":
                    continue
                bn = b.get("name") or b.get("id")
                cfgs = b.get("workTimeConfigs") or []
                if not cfgs:
                    add("error", "E16", name, f"分支「{bn}」没有任何时间规则")
                types_seen: set[str] = set()
                for c in cfgs:
                    dt = c.get("dayType")
                    if dt not in DAY_TYPE_ZH:
                        add("error", "E16", name,
                            f"分支「{bn}」的 dayType={dt!r} 非法，只能是 "
                            f"{'/'.join(DAY_TYPE_ZH)}")
                        continue
                    if dt in types_seen:
                        add("error", "E16", name, f"分支「{bn}」重复配置了 {dt}，画布只会保留最后一条")
                    types_seen.add(dt)
                    if not (c.get("workTimePeriods") or []):
                        add("error", "E16", name, f"分支「{bn}」的 {dt} 规则没有时段")
                    if dt == "Custom" and not (c.get("daysOfWeek") or []):
                        add("error", "E16", name, f"分支「{bn}」的 Custom 规则缺 daysOfWeek（1-7，周日=7）")
                    if dt == "CustomDate" and not (c.get("specificDates") or []):
                        add("error", "E16", name,
                            f"分支「{bn}」的 CustomDate 规则缺 specificDates（[{{startDate,endDate}}]）")
            tz = str(nd.get("timeZoneName") or "")
            if not tz:
                add("error", "E16", name, "timeZoneName 为空，保存会报「请选择时区」")
            elif tz not in MAINLAND_CHINA_TIME_ZONES and any(
                    c.get("dayType") in ("BusinessDay", "Holiday")
                    for b in (nd.get("workTimeBranches") or [])
                    for c in (b.get("workTimeConfigs") or [])):
                add("error", "E16", name,
                    f"时区 {tz} 非中国大陆，不支持法定工作日/法定休息日（BusinessDay/Holiday）")

        # E10 对话节点收集变量
        if typ == "chatNode":
            ents = nd.get("entities") or []
            if len(ents) > 1:
                add("error", "E10", name, f"一个对话节点只能收集 1 个变量，实际 {len(ents)} 个")
            if ents:
                if nd.get("replyMode") != "intent_with_entity":
                    add("error", "E10", name, "配置了词槽收集，replyMode 必须为 intent_with_entity")
                cc = nd.get("collectionConfig") or {}
                if not cc.get("collectionAsValue"):
                    add("error", "E10", name, "词槽收集必须填写收集变量名（collectionAsValue）")
                if not [b for b in (nd.get("branches") or []) if b.get("type") == "entity_success"]:
                    add("error", "E10", name, "配置了词槽收集但缺少「收集成功」(entity_success) 分支")
                if any(e.get("slotId") for e in ents):
                    add("warn", "W8", name,
                        "实体带了 slotId，若不是从真实环境导出的请删除，否则会指向不存在的服务端词槽")
            if any(b.get("type") == "entity_fail" for b in (nd.get("branches") or [])):
                add("error", "E17", name,
                    "branches 含已废弃的 entity_fail。画布导入后会剥掉该分支，后续连线锚点全部错位")
            types = [b.get("type") for b in chat_canvas_br]
            if "entity_success" in types:
                si = types.index("entity_success")
                first_class = next(
                    (i for i, t in enumerate(types) if t in ("intent", "else", "silent")), None)
                if first_class is not None and si > first_class:
                    add("error", "E17", name,
                        "entity_success 必须排在意图分类之前，否则导入后连线会错到错误的分支锚点")
            if "silent" in types:
                last_silent = max(i for i, t in enumerate(types) if t == "silent")
                if any(t in ("intent", "else", "entity_success")
                       for t in types[last_silent + 1:]):
                    add("error", "E17", name,
                        "silent 必须排在意图分类之后（画布把无应答垫在分类组末尾）")
            if nd.get("selectBranch") and not chat_canvas_br:
                add("warn", "W12", name,
                    "selectBranch=true 但没有回复分类，线上编辑会勾选「用户回复分类」并报错")
            if nd.get("contentType") not in ("gen", "fix"):
                add("error", "E15", name,
                    f"contentType 只能是 gen 或 fix，实际是 {nd.get('contentType')!r}（注意不是 fixed）")

        # E12/W9 字数上限
        if typ == "chatNode":
            check_len(nd.get("content"), CHAT_PROMPT_MAX, name, "节点话术")
        for b in nd.get("branches") or []:
            if b.get("type") in ("intent", "global_intent"):
                c = str(b.get("content") or "")
                check_len(c, REPLY_CLASS_MAX, name, f"回复分类「{c[:12]}…」")
        for e in nd.get("entities") or []:
            raw = str(e.get("varName") or e.get("name") or "?")
            vn = raw[:16] + "…" if len(raw) > 16 else raw
            check_len(e.get("description"), VAR_DESC_MAX, name, f"词槽「{vn}」的说明")
            check_len(e.get("name"), VAR_NAME_MAX, name, f"词槽「{vn}」的名称")
        for v in nd.get("vars") or []:      # 赋值节点：超长保存时会被直接拦住
            raw = str(v.get("name") or "?")
            vn = raw[:16] + "…" if len(raw) > 16 else raw
            check_len(raw, VAR_NAME_MAX, name, f"变量名「{vn}」")
            check_len(v.get("description"), VAR_DESC_MAX, name, f"变量「{vn}」的提取描述")
            check_len(v.get("value"), VAR_DESC_MAX, name, f"变量「{vn}」的固定值")
        for key, what in (("transferring-sound", "转接提示音"),
                          ("transfer-timeout-music", "转接超时话术"),
                          ("transfer-error", "转接失败话术"),
                          ("broadcast-sound", "转接成功播报")):
            check_tts_len(nd.get(key), name, what)
        dtmf_cfg = ((nd.get("tcccNodeData") or {}).get("dtmfConfig") or {})
        for key, what in (("play_sound", "按键话术"), ("invalid_sound", "按错话术"),
                          ("timeout_sound", "超时话术")):
            check_tts_len(dtmf_cfg.get(key), name, what)

        # 环境相关 id 待补
        if typ == "transfer" and nd.get("mode") == "manual" and not str(nd.get("name") or "").strip():
            add("warn", "W1", name, "技能组 id 为空，导入不报错但画布保存时会提示「请选择技能组」")
        if typ == "transferAgentNode" and not str(nd.get("varAgentID") or "").strip():
            add("warn", "W1", name, "智能体 id 为空，导入不报错但画布保存时会提示「请选择智能体」")
        if typ == "apiCallNode" and not str(nd.get("url") or "").strip():
            add("warn", "W1", name, "接口 URL 为空，需在画布补齐")

        # W2/W5
        cb = [b for b in (nd.get("branches") or []) if b.get("type") != "global_intent"]
        if typ == "chatNode" and len(cb) > 12:
            add("warn", "W2", name, f"分支数 {len(cb)} 偏多，意图识别精度会下降，建议拆节点或收敛分支")
        if typ == "chatNode" and nd.get("selectBranch"):
            srcs = {str(e.get("source")) for e in edges}
            dangling = [b for b in cb if str(b.get("id")) not in srcs]
            if dangling:
                add("warn", "W5", name,
                    "悬空分支（命中后会重复本节点）：" + "、".join(
                        str(b.get("content"))[:16] for b in dangling))

    # E5/E6 入边与可达性
    targets: set[str] = set()
    for n in nodes:
        for e in n.get("outEdges") or []:
            targets.add(str(e.get("target")))
    roots = [n for n in nodes if n.get("type") == "startNode" or (n.get("nodeData") or {}).get("globalNode")]
    for n in nodes:
        if n in roots:
            continue
        if str(n.get("id")) not in targets:
            add("error", "E5", n.get("name", "?"), "节点缺少入口连线（不可能被走到）")
    reach: set[str] = set()
    stack = [str(r.get("id")) for r in roots]
    while stack:
        cur = stack.pop()
        if cur in reach:
            continue
        reach.add(cur)
        for e in (by_id.get(cur, {}).get("outEdges") or []):
            stack.append(str(e.get("target")))
    for n in nodes:
        if str(n.get("id")) not in reach:
            add("error", "E6", n.get("name", "?"), "节点没有连接入主路（从开始节点或全局节点都到不了）")

    # 名称重复
    name_count: dict[str, int] = {}
    for n in nodes:
        name_count[n.get("name", "")] = name_count.get(n.get("name", ""), 0) + 1
    for nm, c in name_count.items():
        if c > 1:
            add("warn", "W4", nm, f"有 {c} 个节点同名，画布不报错但人工排查会混乱")

    # voiceSettings
    vs = flow.get("voiceSettings") or {}
    sp = vs.get("systemPrompt") or ""
    check_len(sp, SYSTEM_PROMPT_MAX, "voiceSettings.systemPrompt", "全局提示词")
    for f_, lim_, what_ in (("fallbackFixMessage", SYSTEM_PROMPT_MAX, "兜底固定话术"),
                            ("notifyMessage", NOTIFY_TEXT_MAX, "静默提示"),
                            ("hungUpMessage", NOTIFY_TEXT_MAX, "挂机话术"),
                            ("voicemailMessage", TTS_TEXT_MAX, "语音留言话术")):
        check_len(vs.get(f_), lim_, f"voiceSettings.{f_}", what_)
    if vs.get("vadLevel") not in VALID_VAD_LEVELS:
        add("warn", "W6", "voiceSettings.vadLevel",
            f"vadLevel={vs.get('vadLevel')} 不在 {sorted(VALID_VAD_LEVELS)} 内，保存会报「远场人声抑制配置异常」")
    if not str(vs.get("voiceType") or "").strip():
        add("warn", "W1", "voiceSettings.voiceType", "音色为空，请在画布确认")

    # E11 变量引用
    defined: set[str] = set(BUILTIN_VARS)
    if design:
        defined |= set(design.injected_vars)
    external = set(defined)          # 外部传入变量（通话开始就有值）
    internal: dict[str, str] = {}    # 流程内部变量 -> 产生它的节点名
    for n in nodes:
        nd = n.get("nodeData") or {}
        nm = n.get("name", "?")
        for r in nd.get("returns") or []:
            if r.get("alias"):
                defined.add(r["alias"])
                internal.setdefault(r["alias"], f"{nm} 接口返回")
        for v in nd.get("vars") or []:
            if v.get("name"):
                defined.add(v["name"])
                internal.setdefault(v["name"], f"{nm} 变量赋值")
        for e in nd.get("entities") or []:
            if e.get("varName"):
                defined.add(e["varName"])
                internal.setdefault(e["varName"], f"{nm} 词槽采集")
        if nd.get("varName"):
            defined.add(nd["varName"])
            internal.setdefault(nd["varName"], f"{nm} 收号")
    for v in sorted(_clean_var_names(VAR_REF.findall(sp))):
        if v not in defined:
            add("error", "E11", "voiceSettings.systemPrompt",
                f"引用了未定义的变量 ${{{v}}}；若来自外部注入，请写进「环境配置 - 环境注入变量」")
        elif v in internal and v not in external:
            # 通话开始时内部变量还没有值，写进全局提示词会诱导大模型提前编造
            add("warn", "W10", "voiceSettings.systemPrompt",
                f"${{{v}}} 是流程内部变量（{internal[v]}），不该出现在全局提示词里；"
                f"「相关信息」只列外部传入变量，内部变量请挪到产生它之后的节点话术中说明")
    for n in nodes:
        blob = json.dumps(n.get("nodeData") or {}, ensure_ascii=False)
        for v in sorted(_clean_var_names(VAR_REF.findall(blob))):
            if v not in defined:
                add("error", "E11", n.get("name", "?"),
                    f"引用了未定义的变量 ${{{v}}}；若来自外部注入，请写进「环境配置 - 环境注入变量」")
    # 全局节点数量
    gcount = len([n for n in nodes if (n.get("nodeData") or {}).get("globalNode")])
    if gcount > 3:
        add("warn", "W3", "画布", f"全局节点 {gcount} 个：全局分支会参与每个节点的意图识别，建议 ≤3")

    # 硬折行（一句话没写完就换行）
    if design:
        for nd_ in design.nodes:
            hits = find_hard_wraps(nd_.prompt) if nd_.prompt else []
            if hits:
                add("warn", "W11", nd_.key,
                    f"话术里有 {len(hits)} 处一句话没写完就换行，这些换行会原样写进 JSON 传给大模型："
                    + "；".join(hits[:3]) + ("…" if len(hits) > 3 else "")
                    + "。修复：build 时加 --rewrap，或把断句合成一行")
        sp_hits = find_hard_wraps(design.system_prompt or "")
        if sp_hits:
            add("warn", "W11", "全局提示词",
                f"systemPrompt 里有 {len(sp_hits)} 处一句话没写完就换行："
                + "；".join(sp_hits[:3]) + ("…" if len(sp_hits) > 3 else ""))

    # 话术三段结构
    if design:
        for where, msg in design.notes:
            add("warn", "W13", where, msg)
        for nd_ in design.nodes:
            st = (nd_.a("词槽类型") or "").strip()
            if nd_.a("收集变量") and st and st != "custom":
                add("warn", "W14", nd_.key,
                    f"词槽类型只支持 custom，已按 custom 处理（原稿是 {st!r}）")
        for nd_ in design.nodes:
            if nd_.kind == "chat" and nd_.prompt:
                miss = [s for s in ("目标", "参考示例", "应对策略") if s not in nd_.prompt]
                if miss:
                    add("warn", "W7", nd_.key, "话术缺少段落：" + "、".join(miss))
    # 去重
    uniq: list[Issue] = []
    seen_key: set[tuple[str, str, str, str]] = set()
    for i in out:
        k = (i.level, i.code, i.where, i.msg)
        if k not in seen_key:
            seen_key.add(k)
            uniq.append(i)
    return uniq


def print_issues(issues: list[Issue], agg_from: int = 10, name_per_line: int = 4) -> None:
    """把校验结果打到终端。默认不落盘报告，所以 warning 必须在这里说全。

    条数多了逐条列会淹掉真正要看的东西，超过 agg_from 条就按「编码 + 同一句说明」聚合，
    位置逐个列出（同一类问题往往横跨十几个节点，只列前几个等于让人自己去猜剩下的）。
    """
    for lv in ("error", "warn"):
        arr = [i for i in issues if i.level == lv]
        if not arr:
            continue
        print(f"[{lv}] {len(arr)} 项")
        if len(arr) <= agg_from:
            for i in arr:
                loc = f"（{i.src}）" if i.src else ""
                print(f"  {i.code} {i.where}{loc}：{i.msg}")
            continue
        groups: dict[tuple[str, str], list[Issue]] = {}
        for i in arr:
            groups.setdefault((i.code, i.msg), []).append(i)
        for (code, msg), g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            print(f"  {code} ×{len(g)}  {msg}")
            names = [i.where for i in g]
            for k in range(0, len(names), name_per_line):
                print("       " + "、".join(names[k:k + name_per_line]))


def render_report(issues: list[Issue], flow: dict[str, Any], title: str = "") -> str:
    ivr = flow.get("ivrData") or {}
    errs = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warn"]
    lines = [f"# 画布校验报告{('：' + title) if title else ''}", ""]
    lines.append(f"- 结论：**{'不可导入，先修 error' if errs else '可导入'}**"
                 f"（error {len(errs)} 项 / warning {len(warns)} 项）")
    lines.append("- 节点统计：" + "、".join(f"{k} {len(v)}" for k, v in sorted(ivr.items())))
    lines.append(f"- 连线总数：{sum(len(n.get('outEdges') or []) for a in ivr.values() for n in a)}")
    lines.append("")
    for lv, arr, head in (("error", errs, "## 必须修（error）"), ("warn", warns, "## 建议关注（warning）")):
        lines.append(head)
        if not arr:
            lines.append("")
            lines.append("无。")
            lines.append("")
            continue
        lines.append("")
        lines.append("| 编码 | 位置 | 说明 |")
        lines.append("|---|---|---|")
        for i in arr:
            where = f"{i.where}<br>`{i.src}`" if i.src else i.where
            lines.append(f"| {i.code} | {where} | {i.msg.replace('|', '/')} |")
        lines.append("")
    lines += [
        "## 导入后人工验收清单",
        "",
        "1. 导入后先点画布「整理」，确认没有节点重叠、没有断线。",
        "2. 逐个补齐上表 W1 里的环境 id（技能组 / 智能体 / 音色 / 接口地址）。",
        "3. 词槽收集节点：打开节点确认「收集变量」已自动建槽（保存时才会向服务端建槽并回填 slotId）。",
        "4. 点保存，若报错按提示对照本报告的 error 列表定位。",
        "5. 用画布自带的对话测试跑一遍主路径 + 至少两条异常路径。",
        "",
    ]
    return "\n".join(lines)


# ============================================================================
# 6. 逆编译：画布 JSON -> Markdown 设计稿
# ============================================================================

TYPE_TO_KIND = {
    "startNode": "start", "apiCallNode": "api", "extractVariableNode": "assign",
    "logicSplitNode": "condition", "workTimeNode": "worktime", "hangup": "end",
    "transferAgentNode": "transfer-agent",
}
DAY_ZH = DAY_TYPE_ZH


def _kind_of(node: dict[str, Any]) -> str:
    t = node.get("type")
    nd = node.get("nodeData") or {}
    if t == "chatNode":
        canvas_br = [b for b in (nd.get("branches") or []) if b.get("type") != "global_intent"]
        return "chat" if nd.get("selectBranch") and canvas_br else "announce"
    if t == "DTMFNode":
        return "dtmf-nav" if nd.get("nodeType") == "navigation" else "dtmf-collect"
    if t == "transfer":
        return {"manual": "transfer-skill", "outer": "transfer-outer",
                "third_party_route": "transfer-3rd"}.get(nd.get("mode", "manual"), "transfer-skill")
    return TYPE_TO_KIND.get(t, "chat")


ID_ATTR_RE = re.compile(r"^-\s*ID\s*[:：]\s*(\S+)\s*$")
NODE_NO_TOKEN_RE = re.compile(r"^N\d+$", re.I)


def assign_node_nos(order: list[Any], override: dict[str, str] | None = None) -> dict[Any, str]:
    """按节点 id 沿用已有 Nxx；新节点从空号补。避免 BFS 重排把整棵 src 的编号和互引一起改掉。"""
    ov = {str(k): str(v) for k, v in (override or {}).items()}
    mapped: dict[Any, str] = {}
    used: set[str] = set()
    for nid in order:
        label = ov.get(str(nid))
        if label and NODE_NO_TOKEN_RE.match(label) and label not in used:
            mapped[nid] = label
            used.add(label)
    nxt = 1

    def alloc() -> str:
        nonlocal nxt
        while f"N{nxt:02d}" in used:
            nxt += 1
        n = f"N{nxt:02d}"
        used.add(n)
        nxt += 1
        return n

    return {nid: mapped.get(nid) or alloc() for nid in order}


def load_nos_from_src(src_dir: Path) -> dict[str, str]:
    """从已有 src 分片读 id→Nxx。有 `- ID:` 用显式 id，否则用编号+名称派生的稳定 id。"""
    out: dict[str, str] = {}
    if not src_dir.is_dir():
        return out
    for f in sorted(src_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8")
        header: tuple[str, str] | None = None
        nid: str | None = None
        for line in text.splitlines():
            s = line.strip()
            if header is None:
                m = NODE_HEADER.match(s)
                if m:
                    header = (m.group(1), m.group(2).strip())
                continue
            m = ID_ATTR_RE.match(s)
            if m:
                nid = m.group(1)
                break
        if not header:
            continue
        no, name = header
        out[nid or node_id_of(no, name)] = no
    return out


def load_keep_nos(args: argparse.Namespace) -> dict[str, str]:
    out: dict[str, str] = {}
    src = getattr(args, "keep_nos_from", None)
    if src:
        out.update(load_nos_from_src(Path(src)))
    path = getattr(args, "keep_nos", None)
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            out.update({str(k): str(v) for k, v in raw.items()})
    return out


def decompile(flow: dict[str, Any], title: str = "",
              nos_by_id: dict[str, str] | None = None) -> str:
    ivr = flow.get("ivrData") or {}
    nodes = [n for arr in ivr.values() for n in arr]
    by_id = {n.get("id"): n for n in nodes}

    # 编号：start 优先，之后 BFS，再补孤岛
    order: list[str] = []
    starts = [n["id"] for n in ivr.get("startNode") or []]
    globals_ = [n["id"] for n in nodes if (n.get("nodeData") or {}).get("globalNode")]
    queue = starts + [g for g in globals_ if g not in starts]
    while queue:
        cur = queue.pop(0)
        if cur in order or cur not in by_id:
            continue
        order.append(cur)
        for e in by_id[cur].get("outEdges") or []:
            queue.append(str(e.get("target")))
    for n in nodes:
        if n["id"] not in order:
            order.append(n["id"])
    no_of = assign_node_nos(order, nos_by_id)

    vs = flow.get("voiceSettings") or {}
    out: list[str] = [f"# 流程：{title or '未命名流程'}", ""]

    out += ["## 环境配置", ""]
    inv_env = {v[0]: k for k, v in ENV_TO_VOICE.items()}
    for fld, zh in inv_env.items():
        if fld in vs and vs[fld] not in (None, ""):
            val = vs[fld]
            if isinstance(val, bool):
                val = "是" if val else "否"
            elif fld in ("maxDuration", "notifyDuration") and isinstance(val, (int, float)) and val >= 1000:
                val = int(round(val / 1000))
            elif isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            out.append(f"- {zh}: {val}")
    sg = ""
    for n in ivr.get("transfer") or []:
        if (n.get("nodeData") or {}).get("mode") == "manual" and (n["nodeData"].get("name") or ""):
            sg = n["nodeData"]["name"]
            break
    out.append(f"- 技能组ID: {sg}")
    agents = [(n.get("nodeData") or {}).get("varAgentID") for n in ivr.get("transferAgentNode") or []]
    out.append(f"- 转接智能体ID: {next((a for a in agents if a), '')}")

    # 自动把未定义变量收进环境注入变量，保证可回编译
    defined: set[str] = set(BUILTIN_VARS)
    for n in nodes:
        nd = n.get("nodeData") or {}
        defined |= {r.get("alias") for r in nd.get("returns") or [] if r.get("alias")}
        defined |= {v.get("name") for v in nd.get("vars") or [] if v.get("name")}
        defined |= {e.get("varName") for e in nd.get("entities") or [] if e.get("varName")}
        if nd.get("varName"):
            defined.add(nd["varName"])
    used: set[str] = _clean_var_names(VAR_REF.findall(json.dumps(flow, ensure_ascii=False)))
    injected = sorted(v for v in used - defined if v)
    out.append("- 环境注入变量: " + ", ".join(injected))
    out += ["", "## 全局提示词", "", "```prompt", vs.get("systemPrompt") or "", "```", ""]

    out += ["## 变量表", "", "| 变量名 | 来源 | 说明 |", "|---|---|---|"]
    rows: list[tuple[str, str, str]] = []
    for n in nodes:
        nd = n.get("nodeData") or {}
        tag = f"{no_of[n['id']]} {n.get('name')}"
        for r in nd.get("returns") or []:
            rows.append((r.get("alias", ""), f"{tag} 接口返回", r.get("key", "")))
        for v in nd.get("vars") or []:
            rows.append((v.get("name", ""), f"{tag} 变量赋值", (v.get("description") or v.get("value") or "")[:40]))
        for e in nd.get("entities") or []:
            rows.append((e.get("varName", ""), f"{tag} 词槽采集", (e.get("description") or "")[:40]))
        if nd.get("varName"):
            rows.append((nd["varName"], f"{tag} 收号", ""))
    for v in injected:
        rows.append((v, "外部注入", "IVR / 业务系统传入"))
    for a, b, c in rows:
        out.append(f"| {a} | {b} | {str(c).replace(chr(10), ' ').replace('|', '/')} |")
    out += ["", "## 节点", ""]

    for nid in order:
        n = by_id[nid]
        nd = n.get("nodeData") or {}
        kind = _kind_of(n)
        gl = " {全局}" if nd.get("globalNode") else ""
        out.append(f"### {no_of[nid]} {n.get('name')} [{kind}]{gl}")
        out.append(f"- ID: {nid}")
        out += _decompile_attrs(kind, nd)
        prompt = nd.get("welcomeText") or nd.get("content") or ""
        if kind in ("chat", "announce") and prompt:
            fence = "~~~" if "```" in prompt else "```"
            out += [f"{fence}prompt", prompt, fence]
        out += _decompile_branches(kind, n, no_of, by_id)
        out.append("")
    return "\n".join(out)


def _decompile_attrs(kind: str, nd: dict[str, Any]) -> list[str]:
    a: list[str] = []
    for b in nd.get("branches") or []:
        if b.get("type") == "global_intent":
            tp = ""
            if b.get("tags"):
                tp = " {标签: " + "; ".join(
                    f"{x.get('tagName')}={x.get('tagValue')}" for x in b["tags"]) + "}"
            a.append(f"- 触发: {_esc(b.get('content') or '')}{tp}")
    if kind in ("chat", "announce"):
        # 「智能生成」一律不写（chat / announce 默认相同）。「固定」必须写出。
        if nd.get("contentType") == "fix":
            a.append("- 话术模式: 固定")
        if nd.get("allowInterrupt"):
            a.append("- 允许打断: 是")
        if nd.get("silentWaitTime") is not None:
            a.append(f"- 静默等待(秒): {nd['silentWaitTime']}")
        if nd.get("verifyEntity"):
            a.append("- 校验实体: 是")
        for e in nd.get("entities") or []:
            var = e.get("varName", "")
            a.append(f"- 收集变量: {var}")
            if e.get("slotId") is not None:
                a.append(f"- 词槽ID: {e['slotId']}")
            name = e.get("name") or ""
            if name and name != var:
                a.append(f"- 词槽名称: {name}")
            if e.get("description"):
                a.append("- 词槽说明: " + _esc(e["description"]))
            if e.get("collectionMode") is not None:
                a.append(f"- 收集方式: {'固定选项' if e['collectionMode'] == 'fixed' else '开放'}")
            if e.get("fixedOptions"):
                a.append(f"- 固定选项: {_esc(e['fixedOptions'])}")
    elif kind == "api":
        if nd.get("url"):
            a.append(f"- URL: {nd.get('url', '')}")
        try:
            timeout = int(nd.get("callbackTimeout", 4500))
        except (TypeError, ValueError):
            timeout = 4500
        if timeout != 4500:
            a.append(f"- 超时(毫秒): {timeout}")
        if nd.get("async"):
            a.append("- 异步: 是")
        if nd.get("retryTimes"):
            a.append(f"- 重试次数: {nd['retryTimes']}")
        for h in nd.get("headerParams") or []:
            a.append(f"- 请求头: {h.get('key')} = {h.get('value')}")
        for p in nd.get("params") or []:
            a.append(f"- 参数: {p.get('key')} = {p.get('value')} : {p.get('valueType', 'string')}")
        for r in nd.get("returns") or []:
            a.append(f"- 返回: {r.get('key')} → {r.get('alias')}")
    elif kind == "assign":
        for v in nd.get("vars") or []:
            mode = v.get("mode", "llm")
            if mode == "llm":
                a.append(f"- 变量: {v.get('name')} = LLM提取: " + _esc(v.get("description", "")))
            else:
                a.append(f"- 变量: {v.get('name')} = 固定值: {_esc(v.get('value', ''))}")
    elif kind == "worktime":
        tz = nd.get("timeZoneName") or "Asia/Shanghai"
        if tz != "Asia/Shanghai":
            a.append(f"- 时区: {tz}")
    elif kind in ("dtmf-nav", "dtmf-collect"):
        cfg = (nd.get("tcccNodeData") or {}).get("dtmfConfig") or {}
        sound = str(cfg.get("play_sound", "")).replace("tts:", "")
        if sound:
            a.append("- 话术: " + sound)
        try:
            dto = int(cfg.get("dtmf_timeout", 5000))
        except (TypeError, ValueError):
            dto = 5000
        if dto != 5000:
            a.append(f"- 超时(毫秒): {dto}")
        try:
            fails = int(cfg.get("max_failures", 1))
        except (TypeError, ValueError):
            fails = 1
        if fails != 1:
            a.append(f"- 失败次数: {fails}")
        if kind == "dtmf-collect":
            if nd.get("varName"):
                a.append(f"- 收集变量: {nd.get('varName', '')}")
            try:
                dlen = int(cfg.get("dtmf_len", 1))
            except (TypeError, ValueError):
                dlen = 1
            if dlen != 1:
                a.append(f"- 位数: {dlen}")
    elif kind.startswith("transfer-") and kind != "transfer-agent":
        if nd.get("name"):
            a.append(f"- 技能组ID: {nd['name']}")
        if nd.get("caller"):
            a.append(f"- 主叫: {nd['caller']}")
        if nd.get("callee"):
            a.append(f"- 被叫: {nd['callee']}")
        # 这三段话术必须带出来：不写的话回编译会被默认话术覆盖，等于偷偷改了线上播报。
        # 只带 tts: 开头的合成语音；录音文件 id 无法用设计稿表达，交给 --base 继承。
        for zh, fld in (("转接提示音", "transferring-sound"),
                        ("超时话术", "transfer-timeout-music"),
                        ("失败话术", "transfer-error")):
            v = str(nd.get(fld) or "")
            if v.startswith("tts:"):
                a.append(f"- {zh}: {_esc(v[4:])}")
        secs = nd.get("transfer-timeout") or int(_to_num(nd.get("timeout") or 0)) // 1000
        if secs:
            a.append(f"- 转接超时(秒): {secs}")
        if (nd.get("aiTransferContext") or {}).get("enableSummary"):
            a.append("- 转人工带摘要: 是")
    elif kind == "transfer-agent":
        if nd.get("varAgentID"):
            a.append(f"- 智能体ID: {nd.get('varAgentID', '')}")
    return a


def _canvas_branch_dicts(kind: str, nd: dict[str, Any]) -> list[dict[str, Any]]:
    """画布出锚点对应的分支列表（chat 要剔除 global_intent）。"""
    if kind == "condition":
        return list(nd.get("logicSplitBranches") or [])
    if kind == "worktime":
        return list(nd.get("workTimeBranches") or [])
    raw = list(nd.get("branches") or [])
    if kind == "chat":
        return [b for b in raw if b.get("type") != "global_intent"]
    return raw


def _branch_edge_targets(n: dict[str, Any]) -> dict[str, str]:
    """分支 id -> 目标节点 id。

    兼容两种 JSON：
    - 本工具编译：source / edgeData.branchId = 分支 id
    - TCCC 控制台导出：source = 节点 id，靠 startPoint.anchorIndex 或 edgeData.branchId 对应分支
    """
    nd = n.get("nodeData") or {}
    nid = str(n.get("id") or "")
    blist = _canvas_branch_dicts(_kind_of(n), nd)
    out: dict[str, str] = {}
    for e in n.get("outEdges") or []:
        t = str(e.get("target") or "")
        if not t:
            continue
        src = str(e.get("source") or "")
        bid = (e.get("edgeData") or {}).get("branchId")
        if bid:
            out[str(bid)] = t
        if src and src != nid:
            out[src] = t
        if src == nid or not src:
            anchor = (e.get("startPoint") or {}).get("anchorIndex")
            if isinstance(anchor, int) and 1 <= anchor <= len(blist):
                b = blist[anchor - 1]
                if b.get("id"):
                    out[str(b["id"])] = t
    return out


def _fmt_operator(op: Any) -> str:
    token = str(op or "").strip()
    return OP_ZH.get(token, token)


def _fmt_condition(c: dict[str, Any]) -> str:
    left = str(c.get("var") or "").strip()
    op = _fmt_operator(c.get("operator"))
    if str(c.get("operator") or "").strip() in UNARY_OPS:
        return f"{left} {op}".strip()
    return f"{left} {op} {c.get('val', '')}".strip()


def _decompile_branches(kind: str, n: dict[str, Any], no_of: dict[str, str],
                        by_id: dict[str, Any]) -> list[str]:
    nd = n.get("nodeData") or {}
    nid = str(n.get("id") or "")
    edges = n.get("outEdges") or []
    tgt_by_src: dict[str, str] = {}
    for e in edges:
        tgt_by_src[str(e.get("source"))] = str(e.get("target"))
    tgt_by_bid = _branch_edge_targets(n)

    def arrow(bid: str) -> str:
        t = tgt_by_bid.get(str(bid)) or tgt_by_src.get(str(bid))
        if not t or t not in by_id:
            return ""
        return f" → {no_of[t]} {by_id[t].get('name')}"

    def next_line(tid: str | None) -> list[str]:
        if not tid or tid not in by_id:
            return []
        return [f"→ {no_of[tid]} {by_id[tid].get('name')}"]

    out: list[str] = []
    if kind in ("start", "announce", "assign"):
        return next_line(tgt_by_src.get(nid))
    if kind in TERMINAL_KINDS:
        return out

    canvas = _canvas_branch_dicts(kind, nd)
    if kind == "chat" and not canvas:
        tid = tgt_by_src.get(nid) or (str(edges[0].get("target") or "") if edges else "")
        return next_line(tid)

    out.append("分支：")
    if kind == "condition":
        for b in nd.get("logicSplitBranches") or []:
            if b.get("logicType") == "else":
                out.append(f"- 否则{arrow(b.get('id'))}")
            elif b.get("method") == "llm":
                out.append(f"- 如果 LLM: {b.get('llmPrompt', '')}{arrow(b.get('id'))}")
            else:
                joiner = " 或 " if b.get("logicType") == "or" else " 且 "
                expr = joiner.join(_fmt_condition(c) for c in b.get("conditions") or [])
                out.append(f"- 如果 {expr}{arrow(b.get('id'))}")
    elif kind == "worktime":
        for b in nd.get("workTimeBranches") or []:
            if b.get("type") == "other":
                out.append(f"- 其他时间{arrow(b.get('id'))}")
            else:
                segs = []
                for c in b.get("workTimeConfigs") or []:
                    ps = " ".join(
                        f"{p['startTime']['hour']:02d}:{p['startTime']['minute']:02d}-"
                        + ("次日" if (p.get("endTime") or {}).get("nextDay") else "")
                        + f"{p['endTime']['hour']:02d}:{p['endTime']['minute']:02d}"
                        for p in c.get("workTimePeriods") or [])
                    dt = c.get("dayType")
                    qual = ""
                    if dt == "Custom":
                        qual = "、".join(WEEKDAY_ZH.get(d, str(d))
                                        for d in c.get("daysOfWeek") or []) + " "
                    elif dt == "CustomDate":
                        qual = "、".join(
                            r.get("startDate", "") if r.get("startDate") == r.get("endDate")
                            else f"{r.get('startDate', '')}~{r.get('endDate', '')}"
                            for r in c.get("specificDates") or []) + " "
                    segs.append(f"{DAY_ZH.get(dt, '法定工作日')} {qual}{ps}")
                out.append(f"- {b.get('name') or '工作时间'}: {'; '.join(segs)}{arrow(b.get('id'))}")
    else:
        for b in nd.get("branches") or []:
            t = b.get("type")
            c = _branch_text(b.get("content") or "")
            tagpart = ""
            if b.get("tags"):
                tagpart = " {标签: " + "; ".join(
                    f"{x.get('tagName')}={x.get('tagValue')}" for x in b["tags"]) + "}"
            if t == "global_intent":
                continue
            elif t == "entity_fail":
                continue  # 画布已废弃，不写回设计稿
            elif t == "entity_success":
                out.append(f"- 收集成功{tagpart}{arrow(b.get('id'))}")
            elif t == "api_call_success":
                out.append(f"- 成功{arrow(b.get('id'))}")
            elif t == "api_call_fail":
                out.append(f"- 失败{arrow(b.get('id'))}")
            elif t == "dtmf_success" and kind == "dtmf-nav":
                label = ""
                for x in b.get("tags") or []:
                    label = x.get("tagValue", "")
                out.append(f"- 按键 {c} {label}{arrow(b.get('id'))}")
            elif t == "dtmf_success":
                out.append(f"- 收号成功{arrow(b.get('id'))}")
            elif t == "dtmf_fail":
                out.append(f"- {'按键失败' if kind == 'dtmf-nav' else '收号失败'}{arrow(b.get('id'))}")
            elif t == "else":
                out.append(f"- {c or '其他'}{tagpart}{arrow(b.get('id'))}")
            elif t == "silent":
                out.append(f"- {c or '无应答'}{tagpart}{arrow(b.get('id'))}")
            else:
                out.append(f"- {c}{tagpart}{arrow(b.get('id'))}")
    return out


# ============================================================================
# 6.5 src 分片：整篇设计稿 <-> src/ 目录（一个节点一个文件）
#
# 为什么要拆：一份 100+ 节点的设计稿有几万字，改一个节点要在几千行里找，
# diff 也看不清。拆成 src/ 后每个节点是独立小文件，改哪个节点就动哪个文件。
#
# 目录约定（src/ 下的条目按「数字前缀」排序，顺序 = 合并稿里的章节顺序）：
#   src/
#   ├── 00-流程信息.md        # `# 流程：xxx` 标题 + 模块说明
#   ├── 10-环境配置.md        # ## 环境配置
#   ├── 20-假设与待确认.md    # 任意其它 ## 章节，一章一文件，原样透传（JSON 没有这一章也要占 20）
#   ├── 30-全局提示词.md      # ## 全局提示词，整篇一个文件（systemPrompt 本来就是一整块）
#   ├── 40-变量表.md
#   └── 50-节点/              # 一个节点一个文件，文件名是节点名
#       ├── 开始通话.md
#       └── 开场问题识别.md
#
# 章节按数字前缀排。节点文件名不再带 Nxx（编号只写在正文 `### Nxx 名称 [kind]` 里，
# 连线也只认正文编号）。合并节点章节时按正文编号数值排，与文件名、子目录无关。
# 只有节点是拆片的（几十上百个），章节一律一章一文件。目录角色靠目录名识别
# （节点/nodes；全局提示词/prompt 目录仅为兼容手工拆片保留），认不出来时嗅探首行是不是
# `### Nxx …`。合并 = 按序拼接，不做任何语义加工，所以 split → assemble 是无损往返。
# ============================================================================

GEN_BANNER = "<!-- 本文件由 src/ 目录编译生成，请勿直接编辑；改动请改 src/ 下的分片再重新 build。 -->"

SEQ_PREFIX_RE = re.compile(r"^(\d+)\s*[-_.]?\s*")
NODE_SEQ_RE = re.compile(r"^N(\d+)", re.I)
NODES_DIR_ALIASES = {"节点", "nodes", "node"}
PROMPT_DIR_ALIASES = {"全局提示词", "prompt", "prompts", "systemprompt", "system-prompt"}
FNAME_BAD_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_]+")

# 约定序号：某一章缺失时仍占用这个号。线上 JSON 没有「假设与待确认」，
# 若按出现顺序 10/20/30… 编号，「用线上覆盖」会把 50-节点/ 整目录改成 40-节点/。
CANONICAL_SECTION_SEQ = {
    "环境配置": 10,
    "假设与待确认": 20,
    "全局提示词": 30,
    "变量表": 40,
    "节点": 50,
}


def _safe_name(s: str, limit: int = 32) -> str:
    """把章节名/节点名压成安全文件名：保留中文与字母数字，其它一律换成 -。"""
    out = FNAME_BAD_RE.sub("-", s.strip()).strip("-")
    return (out[:limit].rstrip("-") or "未命名")


def _canonical_section_seq(title: str) -> int | None:
    t = title.replace(" ", "")
    if t.startswith("节点"):
        return CANONICAL_SECTION_SEQ["节点"]
    return CANONICAL_SECTION_SEQ.get(t)


def _next_section_seq(last: int, used: set[int], upcoming: set[int]) -> int:
    """未登记章节：能插进与下一约定章之间的空隙就插，否则往后排。"""
    nxt = min(upcoming) if upcoming else None
    cand = last + 10
    if nxt is None or cand < nxt:
        while cand in used and (nxt is None or cand < nxt):
            cand += 10
        if cand not in used and (nxt is None or cand < nxt):
            return cand
    if nxt is not None:
        for n in range(last + 1, nxt):
            if n not in used:
                return n
    cand = last + 10
    blocked = used | upcoming
    while cand in blocked:
        cand += 10
    return cand


def _strip_seq(name: str) -> str:
    return SEQ_PREFIX_RE.sub("", name)


def _src_sort_key(p: Path) -> tuple[int, int, str]:
    """章节/子目录：数字前缀按数值排；其余按文件名。节点正文编号不走这里。"""
    m = SEQ_PREFIX_RE.match(p.name)
    if m:
        return (0, int(m.group(1)), p.name)
    return (1, 0, p.name)


def _node_header_from_text(text: str) -> tuple[str, str] | None:
    for line in text.splitlines():
        m = NODE_HEADER.match(line.strip())
        if m:
            return (m.group(1), m.group(2).strip())
    return None


def _peek_node_header(p: Path) -> tuple[str, str] | None:
    try:
        return _node_header_from_text(p.read_text(encoding="utf-8"))
    except OSError:
        return None


def _node_no_value(no: str) -> int:
    m = NODE_SEQ_RE.match(no)
    return int(m.group(1)) if m else 0


def _node_file_stem(no: str, name: str, used: set[str]) -> str:
    """节点文件名用节点名；同目录重名时追加 -Nxx。"""
    base = _safe_name(name)
    stems = [base, f"{base}-{no}"]
    n = 2
    while True:
        for stem in stems:
            fname = f"{stem}.md"
            if fname not in used:
                used.add(fname)
                return stem
        stems = [f"{base}-{no}-{n}"]
        n += 1


def _sort_node_src_files(files: list[Path]) -> list[Path]:
    """节点目录内：说明类（数字前缀）在前，节点按正文 `### Nxx` 数值排。"""
    def key(p: Path) -> tuple[int, int, str]:
        header = _peek_node_header(p)
        if header:
            return (1, _node_no_value(header[0]), p.name)
        m = SEQ_PREFIX_RE.match(p.name)
        if m:
            return (0, int(m.group(1)), p.name)
        return (2, 0, p.name)
    return sorted(files, key=key)


def _print_renames(moved: list[tuple[Path, Path]], base: Path) -> None:
    if not moved:
        return
    print(f"[重命名] {len(moved)} 个节点分片改为按节点名命名")
    for old, new in moved[:20]:
        print(f"  {rel_path(old, base)} → {rel_path(new, base)}")
    if len(moved) > 20:
        print(f"  … 另 {len(moved) - 20} 个")


def normalize_node_filenames(src_dir: Path) -> list[tuple[Path, Path]]:
    """把旧的 `Nxx-名称.md`（以及改名后未跟上的文件）改成 `名称.md`。"""
    if not src_dir.is_dir():
        return []
    by_dir: dict[Path, list[tuple[Path, str, str]]] = {}
    reserved: dict[Path, set[str]] = {}
    for f in src_dir.rglob("*.md"):
        if f.name.startswith("."):
            continue
        header = _peek_node_header(f)
        if header:
            by_dir.setdefault(f.parent, []).append((f, header[0], header[1]))
        else:
            reserved.setdefault(f.parent, set()).add(f.name)
    moved: list[tuple[Path, Path]] = []
    for d, items in by_dir.items():
        used = set(reserved.get(d, ()))
        plan: list[tuple[Path, str]] = []
        for p, no, name in items:
            plan.append((p, f"{_node_file_stem(no, name, used)}.md"))
        need = [(p, fname) for p, fname in plan if p.name != fname]
        if not need:
            continue
        temps: list[tuple[Path, Path, Path]] = []
        for p, fname in need:
            tmp = p.with_name(f"{p.stem}.__renaming__{p.suffix}")
            n = 2
            while tmp.exists():
                tmp = p.with_name(f"{p.stem}.__renaming__{n}{p.suffix}")
                n += 1
            p.rename(tmp)
            temps.append((tmp, d / fname, p))
        for tmp, dest, orig in temps:
            tmp.rename(dest)
            moved.append((orig, dest))
    return moved


def _src_entries(d: Path) -> list[Path]:
    return sorted(
        [p for p in d.iterdir()
         if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() == ".md")],
        key=_src_sort_key,
    )


def _iter_src_md(d: Path) -> list[Path]:
    """递归取目录下的 md，按序号排序（允许用子目录给节点分组）。"""
    out: list[Path] = []
    for p in _src_entries(d):
        out.extend(_iter_src_md(p) if p.is_dir() else [p])
    return out


def _dir_role(p: Path) -> str:
    """判断 src 子目录的角色：nodes / prompt / other。"""
    base = _strip_seq(p.name).strip().lower()
    if base in NODES_DIR_ALIASES:
        return "nodes"
    if base in PROMPT_DIR_ALIASES:
        return "prompt"
    for f in _iter_src_md(p):                     # 认不出名字就嗅探内容
        for line in f.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                return "nodes" if NODE_HEADER.match(line.strip()) else "other"
    return "other"


class _Emitter:
    """按行拼接合并稿，同时记录每行来自哪个源文件（用于把报错行号映射回 src）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.origin: list[tuple[Path | None, int]] = []

    def add(self, line: str, src: Path | None = None, ln: int = 0) -> None:
        self.lines.append(line)
        self.origin.append((src, ln))

    def add_file(self, path: Path, text: str) -> None:
        for i, line in enumerate(text.split("\n")):
            self.add(line, path, i + 1)

    def blank(self) -> None:
        if self.lines and self.lines[-1].strip():
            self.add("")

    @property
    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def assemble_src(src_dir: Path) -> tuple[str, list[tuple[Path | None, int]]]:
    """src/ 目录 -> 整篇设计稿文本 + 行来源表。"""
    if not src_dir.is_dir():
        raise DesignError(f"src 目录不存在：{src_dir}")
    entries = _src_entries(src_dir)
    if not entries:
        raise DesignError(f"src 目录里没有 md 文件或子目录：{src_dir}")

    em = _Emitter()
    saw_nodes = False
    for p in entries:
        if p.is_file():
            em.add_file(p, p.read_text(encoding="utf-8").strip("\n"))
            em.blank()
            continue
        role = _dir_role(p)
        files = _iter_src_md(p)
        if role == "nodes":
            saw_nodes = True
            files = _sort_node_src_files(files)
            em.add("## 节点")
            em.blank()
            for f in files:
                em.add_file(f, f.read_text(encoding="utf-8").strip("\n"))
                em.blank()
        elif role == "prompt":
            frags = [(f, f.read_text(encoding="utf-8").strip("\n")) for f in files]
            fence = "~~~" if any("```" in t for _, t in frags) else "```"
            em.add("## 全局提示词")
            em.blank()
            em.add(fence + "prompt")
            for i, (f, t) in enumerate(frags):
                if i:
                    em.add("")                    # 片与片之间恰好一个空行
                em.add_file(f, t)
            em.add(fence)
            em.blank()
        else:                                      # 认不出角色的目录：原样拼接
            for f in files:
                em.add_file(f, f.read_text(encoding="utf-8").strip("\n"))
                em.blank()
    if not saw_nodes:
        raise DesignError(
            f"{src_dir} 下没有节点目录。请建一个名为「节点」（或 nodes）的子目录，一个节点一个 md")
    return em.text, em.origin


def rel_path(f: Path, base: Path) -> Path:
    """尽量给出相对 base 的短路径，跨盘/跨树时退回原路径。"""
    try:
        return f.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return f


def remap_src_lines(msg: str, origin: list[tuple[Path | None, int]], base: Path) -> str:
    """把「第 N 行」（合并稿行号）改写成「src 文件:行号」，报错才好定位。"""
    def rep(m: re.Match[str]) -> str:
        i = int(m.group(1))
        if 1 <= i <= len(origin) and origin[i - 1][0] is not None:
            f, ln = origin[i - 1]
            assert f is not None
            return f"{rel_path(f, base)} 第 {ln} 行（合并稿第 {i} 行）"
        return m.group(0)
    return re.sub(r"第 (\d+) 行", rep, msg)


def src_file_of_line(origin: list[tuple[Path | None, int]], line: int) -> Path | None:
    return origin[line - 1][0] if 1 <= line <= len(origin) else None


def _split_nodes(body: list[str]) -> list[tuple[str, str]]:
    """`## 节点` 章节按 `### Nxx 名称 [kind]` 拆片，一个节点一个文件。"""
    chunks: list[tuple[str, str, list[str]]] = []
    pre: list[str] = []
    cur: tuple[str, str, list[str]] | None = None
    fence: str | None = None
    for raw in body:
        s = raw.lstrip()
        if fence is None and (s.startswith("```") or s.startswith("~~~")):
            fence = s[:3]
        elif fence is not None and s.startswith(fence):
            fence = None
        elif fence is None:
            m = NODE_HEADER.match(raw.strip())
            if m:
                if cur:
                    chunks.append(cur)
                cur = (m.group(1), m.group(2).strip(), [raw])
                continue
        (cur[2] if cur else pre).append(raw)
    if cur:
        chunks.append(cur)
    if not chunks:
        raise DesignError("「## 节点」章节里没有解析到任何节点（节点标题格式：### N01 名称 [kind]）")

    out: list[tuple[str, str]] = []
    used: set[str] = set()
    if [x for x in pre if x.strip()]:
        used.add("000-节点说明.md")
        out.append(("000-节点说明.md", "\n".join(pre).strip("\n") + "\n"))
    for no, name, lines in chunks:
        fname = f"{_node_file_stem(no, name, used)}.md"
        out.append((fname, "\n".join(lines).strip("\n") + "\n"))
    return out


def split_design(text: str) -> list[tuple[str, str]]:
    """整篇设计稿 -> [(src 内相对路径, 文件内容)]。纯文本切分，不做语义加工。"""
    lines = text.replace("\r\n", "\n").split("\n")
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    fence: str | None = None
    for raw in lines:
        s = raw.lstrip()
        if fence is None and (s.startswith("```") or s.startswith("~~~")):
            fence = s[:3]
        elif fence is not None and s.startswith(fence):
            fence = None
        elif fence is None and raw.startswith("## "):
            if cur:
                sections.append(cur)
            cur = (raw[3:].strip(), [])
            continue
        (cur[1] if cur else head).append(raw)
    if cur:
        sections.append(cur)
    if not sections:
        raise DesignError("设计稿里没有任何「## 章节」，无法拆分")

    files: list[tuple[str, str]] = []
    head_txt = "\n".join(x for x in head if x.strip() != GEN_BANNER).strip("\n")
    head_txt = re.sub(r"\n{3,}", "\n\n", head_txt)     # 去掉 banner 留下的空行
    files.append(("00-流程信息.md", (head_txt or "# 流程：未命名流程") + "\n"))

    used = {0}
    last = 0
    upcoming = {n for t, _ in sections if (n := _canonical_section_seq(t)) is not None}
    for title, body in sections:
        canon = _canonical_section_seq(title)
        upcoming.discard(canon)
        if canon is not None and canon not in used:
            n = canon
        else:
            n = _next_section_seq(last, used, upcoming)
        used.add(n)
        last = n
        t = title.replace(" ", "")
        if t.startswith("节点"):
            d = f"{n:02d}-节点"
            files += [(f"{d}/{x}", c) for x, c in _split_nodes(body)]
        else:
            files.append((f"{n:02d}-{_safe_name(title)}.md",
                          f"## {title}\n\n" + "\n".join(body).strip("\n") + "\n"))
    return files


def patch_node_id_markdown(text: str, nid: str) -> str:
    """在节点分片标题下写入或更新 `- ID:`，已是同一 id 则不动。"""
    lines = text.replace("\r\n", "\n").split("\n")
    hi = next((i for i, line in enumerate(lines) if NODE_HEADER.match(line.strip())), None)
    if hi is None:
        return text
    new_line = f"- ID: {nid}"
    id_i = next((i for i, line in enumerate(lines) if ID_ATTR_RE.match(line.strip())), None)
    if id_i is not None:
        cur = ID_ATTR_RE.match(lines[id_i].strip())
        if cur and cur.group(1) == nid:
            return text
        lines[id_i] = new_line
        return "\n".join(lines)
    lines.insert(hi + 1, new_line)
    return "\n".join(lines)


def backfill_src_node_ids(src_dir: Path, ids: dict[str, str]) -> int:
    """把编译得到的节点 id 写回 src 分片。返回改动文件数。"""
    changed = 0
    for f in src_dir.rglob("*.md"):
        text = f.read_text(encoding="utf-8")
        header = next((line.strip() for line in text.splitlines() if line.strip()), "")
        m = NODE_HEADER.match(header)
        if not m:
            continue
        nid = ids.get(m.group(1))
        if not nid:
            continue
        new = patch_node_id_markdown(text, nid)
        if new != text:
            f.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def write_src(files: list[tuple[str, str]], target: Path, force: bool = False) -> list[Path]:
    """落盘 src/。目标目录已有 md 时必须 --force（会先清掉旧 md，避免残留幽灵节点）。"""
    existing = sorted(target.rglob("*.md")) if target.exists() else []
    if existing and not force:
        raise DesignError(
            f"{target} 下已有 {len(existing)} 个 md 文件。确认要覆盖请加 --force（会先删除旧分片）")
    for f in existing:
        f.unlink()
    for d in sorted((p for p in target.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    written: list[Path] = []
    for rel, content in files:
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(p)
    return written


def rewrap_files(paths: list[Path]) -> tuple[int, list[Path]]:
    """就地修复一批 md 的硬折行，返回 (合并处数, 被改动的文件)。"""
    total, changed = 0, []
    for f in paths:
        src = f.read_text(encoding="utf-8")
        new, merged = rewrap_text(src)
        if merged:
            f.write_text(new, encoding="utf-8")
            total += merged
            changed.append(f)
    return total, changed


# ============================================================================
# 7. mermaid（写进设计稿供人审阅）
# ============================================================================

def mermaid(design: Design) -> str:
    by_no = design.by_no
    shape = {
        "start": lambda t: f"([{t}])", "end": lambda t: f"([{t}])",
        "condition": lambda t: f"{{{t}}}", "worktime": lambda t: f"{{{t}}}",
        "api": lambda t: f"[({t})]",
    }
    lines = ["flowchart LR"]
    for n in design.nodes:
        label = f"{n.no} {n.name}".replace('"', "'")
        body = shape.get(n.kind, lambda t: f"[{t}]")(label)
        lines.append(f"  {n.no}{body}")
    for n in design.nodes:
        for b in canvas_branch_list(n):
            if b.target and b.target in by_no:
                lb = b.content.split("。")[0][:12].replace('"', "'")
                lines.append(f"  {n.no} -->|{lb}| {b.target}")
        if n.next and n.next in by_no:
            lines.append(f"  {n.no} --> {n.next}")
    return "\n".join(lines)


# ============================================================================
# 8. CLI
# ============================================================================

def _load_json(p: str) -> dict[str, Any]:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.design)
    base = _load_json(args.base) if args.base else None
    # 新建流程默认修硬折行；--base 改存量画布时默认保真不动内容
    rewrap = args.rewrap if args.rewrap is not None else (base is None)

    origin: list[tuple[Path | None, int]] = []
    md_out: Path | None = None
    proj = src.resolve().parent
    if src.is_dir():
        # src/ 分片模式：先把旧 Nxx- 文件名迁到节点名，再就地修硬折行，再合并
        _print_renames(normalize_node_filenames(src), proj)
        if rewrap:
            merged, changed = rewrap_files(_iter_src_md(src))
            if merged:
                print(f"[整形] src 分片里合并了 {merged} 处硬折行（{len(changed)} 个文件）")
        text, origin = assemble_src(src)
        stem = proj.name                     # 产物名跟随项目目录名
        md_out = None if args.no_emit_md else Path(args.emit_md or proj / f"{stem}-设计稿.md")
        default_json = proj / f"{stem}.json"
    else:
        text = src.read_text(encoding="utf-8")
        md_out = Path(args.emit_md) if args.emit_md else None
        default_json = src.with_suffix(".json")

    try:
        design = parse_design(text)
    except DesignError as e:
        raise DesignError(remap_src_lines(str(e), origin, proj) if origin else str(e)) from None

    flow = build_json(design, base, rewrap=rewrap, relayout=bool(getattr(args, "relayout", False)))
    issues = validate(flow, design)
    if origin:                       # 问题位置带上源文件，便于直接打开修改
        # 校验里的位置有两种写法：设计稿口径「N02 采集运单号」和 JSON 口径「采集运单号」
        line_of: dict[str, int] = {}
        for n in design.nodes:
            line_of[n.key] = n.line
            line_of.setdefault(n.name, n.line)
        if design.system_prompt_line:     # 全局提示词的问题指回 30-全局提示词.md
            line_of["全局提示词"] = design.system_prompt_line
            line_of["voiceSettings.systemPrompt"] = design.system_prompt_line
        for i in issues:
            ln = line_of.get(i.where)
            f = src_file_of_line(origin, ln) if ln else None
            if f is not None:
                i.src = str(rel_path(f, proj))
    errs = [i for i in issues if i.level == "error"]

    out = Path(args.output) if args.output else default_json
    report_path = Path(args.report) if args.report else None
    if report_path:                  # 校验结果默认只打终端，显式要报告才落盘
        report_path.write_text(render_report(issues, flow, design.title), encoding="utf-8")

    if errs and not args.force:
        print(f"[FAIL] {len(errs)} 项 error，未写出 JSON", file=sys.stderr)
        for i in errs[:20]:
            loc = f"（{i.src}）" if i.src else ""
            print(f"  - [{i.code}] {i.where}{loc}：{i.msg}", file=sys.stderr)
        if report_path:
            print(f"[报告] {report_path}", file=sys.stderr)
        return 2
    if md_out is not None:
        body = text
        if src.is_dir():                 # 合并稿是产物，顶上盖个「别直接改我」的戳
            rest = text.split("\n")
            head = [rest.pop(0)] if rest and rest[0].startswith("# ") else []
            while rest and not rest[0].strip():
                rest.pop(0)
            body = "\n".join(head + ([""] if head else []) + [GEN_BANNER, ""] + rest)
        md_out.write_text(body, encoding="utf-8")
        print(f"[设计稿] {md_out}  （由 {src}/ 合并生成）")
    out.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
    if src.is_dir() and getattr(args, "write_src_ids", False) and design.node_ids:
        n_ids = backfill_src_node_ids(src, design.node_ids)
        if n_ids:
            print(f"[回填] 写入 {n_ids} 个节点 ID 到 src 分片")
    n_nodes = sum(len(v) for v in flow["ivrData"].values())
    n_edges = sum(len(n["outEdges"]) for v in flow["ivrData"].values() for n in v)
    print(f"[OK] {out}  节点 {n_nodes} / 连线 {n_edges} / "
          f"error {len(errs)} / warn {len(issues) - len(errs)}")
    if rewrap and design.rewrap_stats:
        print(f"[整形] 合并了 {design.rewrap_stats} 处「一句话没写完就换行」的排版折行")
    print_issues(issues)
    if report_path:
        print(f"[报告] {report_path}")
    if args.mermaid:
        Path(args.mermaid).write_text(mermaid(design), encoding="utf-8")
        print(f"[流程图] {args.mermaid}")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    src = Path(args.design)
    keep = load_keep_nos(args)
    if src.suffix.lower() == ".json":
        text = decompile(_load_json(str(src)), args.title or src.stem, nos_by_id=keep)
    else:
        text = src.read_text(encoding="utf-8")
    files = split_design(text)
    target = Path(args.output) if args.output else src.parent / "src"
    target.mkdir(parents=True, exist_ok=True)
    written = write_src(files, target, force=args.force)
    n_nodes = len([p for p in written if NODE_HEADER.match(
        p.read_text(encoding="utf-8").split("\n")[0].strip())])
    print(f"[OK] {target}  {len(written)} 个分片（其中节点 {n_nodes} 个）")
    for p in written:
        print(f"  {p.relative_to(target)}")
    print(f"\n下一步：build 回整篇 md + JSON\n"
          f"  {Path(sys.argv[0]).name} build {target} [--base 上一版.json]")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    src = Path(args.src)
    _print_renames(normalize_node_filenames(src), src)
    text, _ = assemble_src(src)
    out = Path(args.output) if args.output else src.parent / f"{src.parent.name}-设计稿.md"
    out.write_text(text, encoding="utf-8")
    print(f"[OK] {out}  {len(text.splitlines())} 行")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    flow = _load_json(args.flow)
    design = parse_design(Path(args.design).read_text(encoding="utf-8")) if args.design else None
    issues = validate(flow, design)
    errs = [i for i in issues if i.level == "error"]
    if args.report:
        Path(args.report).write_text(
            render_report(issues, flow, design.title if design else ""), encoding="utf-8")
        print(f"[报告] {args.report}")
    ivr = flow.get("ivrData") or {}
    print(f"[{'FAIL' if errs else 'OK'}] {args.flow}  "
          f"节点 {sum(len(v) for v in ivr.values())} / "
          f"连线 {sum(len(n.get('outEdges') or []) for v in ivr.values() for n in v)} / "
          f"error {len(errs)} / warn {len(issues) - len(errs)}")
    print_issues(issues)
    return 2 if errs else 0


def cmd_decompile(args: argparse.Namespace) -> int:
    flow = _load_json(args.flow)
    md = decompile(flow, args.title or Path(args.flow).stem, nos_by_id=load_keep_nos(args))
    out = Path(args.output or Path(args.flow).with_suffix(".md"))
    out.write_text(md, encoding="utf-8")
    print(f"[OK] {out}")
    return 0


def cmd_rewrap(args: argparse.Namespace) -> int:
    """把 Markdown 里「一句话没写完就换行」的排版折行合回一行。

    正文段落与 ```prompt 围栏会被处理；```bash / ```json 等代码围栏、表格、
    列表/标题等结构行一律不动。
    """
    targets: list[Path] = []
    for t in args.paths:
        pt = Path(t)
        targets.extend(sorted(pt.rglob("*.md")) if pt.is_dir() else [pt])
    total = 0
    for f in targets:
        src = f.read_text(encoding="utf-8")
        new_text, merged = rewrap_text(src)
        if merged and not args.dry_run:
            f.write_text(new_text, encoding="utf-8")
        if merged:
            total += merged
            mark = "[待修]" if args.dry_run else "[已修]"
            print(f"{mark} {f}  合并 {merged} 处")
            if args.verbose:
                for h in find_hard_wraps(src)[:8]:
                    print(f"        {h}")
        elif args.verbose:
            print(f"[干净] {f}")
    print(f"共 {len(targets)} 个文件，{'待' if args.dry_run else '已'}合并 {total} 处硬折行")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TCCC AI 画布：Markdown 设计稿 <-> 画布 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="设计稿 md 或 src/ 目录 -> 画布 JSON")
    b.add_argument("design", help="设计稿 md 文件，或 src/ 分片目录（推荐）")
    b.add_argument("-o", "--output",
                   help="输出 JSON；传目录时默认 <src父目录>/<父目录名>.json")
    b.add_argument("--emit-md", help="合并稿输出路径；传目录时默认 <父目录名>-设计稿.md")
    b.add_argument("--no-emit-md", action="store_true", help="只出 JSON，不写合并稿 md")
    b.add_argument("--base", help="已导出的真实画布 JSON，作为 voiceSettings / 未表达字段的底座")
    b.add_argument("--relayout", action="store_true",
                   help="忽略 --base / 旧稿坐标，按流程顺序重新排版（应用编译默认开启）")
    b.add_argument("--report", help="额外把校验结果落成 markdown 报告（默认只打终端）")
    b.add_argument("--mermaid")
    b.add_argument("--force", action="store_true", help="即使有 error 也写出 JSON（仅调试用）")
    b.add_argument("--rewrap", dest="rewrap", action="store_true", default=None,
                   help="把话术里的排版硬折行合回一行（不带 --base 时默认开启）")
    b.add_argument("--no-rewrap", dest="rewrap", action="store_false",
                   help="保留话术里的换行原样不动")
    b.add_argument("--write-src-ids", action="store_true",
                   help="把编译得到的节点 id 写回 src 分片的 `- ID:`（发布时用，预览不要加）")
    b.set_defaults(func=cmd_build)

    sp = sub.add_parser("split", help="整篇设计稿 / 画布 JSON -> src/ 分片目录")
    sp.add_argument("design", help="设计稿 md，或画布 JSON（会先 decompile）")
    sp.add_argument("-o", "--output", help="src 目录，默认 <设计稿同级>/src")
    sp.add_argument("--title", help="输入是 JSON 时的流程名")
    sp.add_argument("--force", action="store_true", help="目标目录已有分片时覆盖（先删旧 md）")
    sp.add_argument("--keep-nos-from", help="已有 src 目录，按节点 id 沿用其中的 Nxx 编号")
    sp.add_argument("--keep-nos", help="id→Nxx 的 JSON 文件，覆盖/补充 --keep-nos-from")
    sp.set_defaults(func=cmd_split)

    asm = sub.add_parser("assemble", help="src/ 分片目录 -> 整篇设计稿 md（不编译 JSON）")
    asm.add_argument("src")
    asm.add_argument("-o", "--output")
    asm.set_defaults(func=cmd_assemble)

    v = sub.add_parser("validate", help="校验画布 JSON")
    v.add_argument("flow")
    v.add_argument("--design")
    v.add_argument("--report")
    v.set_defaults(func=cmd_validate)

    rw = sub.add_parser("rewrap", help="修复 Markdown 里一句话没写完就换行的排版折行")
    rw.add_argument("paths", nargs="+", help="md 文件或目录（目录会递归找 *.md）")
    rw.add_argument("--dry-run", action="store_true", help="只报告不改文件")
    rw.add_argument("-v", "--verbose", action="store_true")
    rw.set_defaults(func=cmd_rewrap)

    dcp = sub.add_parser("decompile", help="画布 JSON -> 设计稿")
    dcp.add_argument("flow")
    dcp.add_argument("-o", "--output")
    dcp.add_argument("--title")
    dcp.add_argument("--keep-nos-from", help="已有 src 目录，按节点 id 沿用其中的 Nxx 编号")
    dcp.add_argument("--keep-nos", help="id→Nxx 的 JSON 文件")
    dcp.set_defaults(func=cmd_decompile)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except DesignError as e:
        print(f"[设计稿错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
