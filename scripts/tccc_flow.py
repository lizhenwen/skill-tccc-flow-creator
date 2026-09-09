#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tccc_flow.py — 腾讯云呼叫中心 TCCC「AI 画布」流程编译工具（零三方依赖）

三个子命令：
  build      Markdown 设计稿  ->  画布可导入 JSON（+ 校验报告）
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
SYSTEM_PROMPT_MAX = 8192  # persona-requirements.vue:130
VALID_VAD_LEVELS = {0, 1, 2, 3, 100}
BUILTIN_VARS = {"StaffNo", "SessionId"}
BUILTIN_SLOT_TYPES = {
    "date", "time", "datetime", "address", "surname", "name", "original_words",
}

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
    content = raw.strip()
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
    return conds, logic


def _parse_worktime(c: str, ln: int) -> tuple[str, list[dict[str, Any]]]:
    """解析：工作时间: 工作日 08:00-21:00; 节假日 08:00-21:00"""
    if ":" in c or "：" in c:
        name, spec = re.split(r"[:：]", c, maxsplit=1)
    else:
        name, spec = c, ""
    name = name.strip() or "工作时间"
    cfgs: list[dict[str, Any]] = []
    day_map = {"工作日": "BusinessDay", "节假日": "Holiday", "每周": "Weekly", "指定日期": "Specific"}
    for seg in [x for x in re.split(r"[;；]", spec) if x.strip()]:
        seg = seg.strip()
        day_type = "BusinessDay"
        for zh, en in day_map.items():
            if seg.startswith(zh):
                day_type = en
                seg = seg[len(zh):].strip()
                break
        periods = []
        for pm in re.finditer(r"(\d{1,2})[:：](\d{2})\s*[-~到至]\s*(\d{1,2})[:：](\d{2})", seg):
            sh, sm, eh, em = (int(g) for g in pm.groups())
            periods.append({
                "startTime": {"hour": sh, "minute": sm, "nextDay": False},
                "endTime": {"hour": eh % 24, "minute": em, "nextDay": eh >= 24},
            })
        if not periods:
            raise DesignError(f"第 {ln} 行：工作时间分支缺少时段（形如 08:00-21:00）")
        cfgs.append({
            "dayType": day_type, "daysOfWeek": [], "specificDates": [],
            "workTimePeriods": periods,
        })
    if not cfgs:
        raise DesignError(f"第 {ln} 行：工作时间分支需写明时段，如「工作时间: 工作日 08:00-21:00 → N05」")
    return name, cfgs


# ============================================================================
# 4. 生成画布 JSON
# ============================================================================

def canvas_branch_list(n: Node) -> list[Branch]:
    """参与连线与锚点计算的分支。global_intent 是全局触发语，不占画布锚点。"""
    return [b for b in n.branches if b.kind != "global_intent"]


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


def _normalize_branches(n: Node) -> None:
    """规范化：触发属性 -> global_intent 分支；系统分支 content 用画布固定写法。"""
    existing = {b.content for b in n.branches if b.kind == "global_intent"}
    gl: list[Branch] = []
    for spec in n.alist("触发"):
        content, tags = _split_tags(spec)
        if content and content not in existing:
            gl.append(Branch(kind="global_intent", content=content, tags=tags))
    if gl:
        n.branches = gl + n.branches   # 全局触发语放最前，与线上导出一致
    if [b for b in n.branches if b.kind == "global_intent"]:
        n.is_global = True

    if n.kind not in ("chat", "announce"):
        return
    var = n.a("收集变量")
    wait = n.aint("静默等待(秒)", 10)
    for b in n.branches:
        if b.kind == "entity_success":
            b.content = f"收集${{{var}}}结束" if var else "收集完成"
        elif b.kind == "entity_fail":
            b.content = "收集失败"
        elif b.kind == "silent" and re.match(r"^(无应答|静默|沉默)\s*$", b.content):
            b.content = f"用户无响应{wait}秒"


def _resolve_branch_ids(n: Node, base_node: dict[str, Any] | None) -> None:
    """给分支分配 id：消耗式复用 --base 里的既有 id，保证回编译后 diff 最小。

    匹配优先级：① type+content 完全相同 ② 同 type 按出现顺序 ③ 基于 key 的稳定 hash。
    「消耗式」= 每个底座 id 只会被认领一次，避免同名分支拿到重复 id。
    """
    pool: list[dict[str, Any]] = []
    positional: list[str] = []
    if base_node:
        nd = base_node.get("nodeData", {})
        pool = [dict(b) for b in (nd.get("branches") or []) if b.get("id")]
        if n.kind == "condition":
            positional = [str(b.get("id")) for b in (nd.get("logicSplitBranches") or [])]
        elif n.kind == "worktime":
            positional = [str(b.get("id")) for b in (nd.get("workTimeBranches") or [])]
    taken: set[int] = set()

    def claim(kind: str, content: str) -> str | None:
        for i, b in enumerate(pool):          # ① type + content
            if i in taken:
                continue
            if str(b.get("type")) == kind and str(b.get("content") or "") == content:
                taken.add(i)
                return str(b["id"])
        for i, b in enumerate(pool):          # ② 同 type 按顺序
            if i in taken:
                continue
            if str(b.get("type")) == kind:
                taken.add(i)
                return str(b["id"])
        return None

    j = -1
    for i, b in enumerate(n.branches):
        if b.kind != "global_intent":
            j += 1
        if b.bid:
            continue
        if n.kind == "condition" and b.kind == "else":
            b.bid = "else"
            continue
        if positional and b.kind != "global_intent" and 0 <= j < len(positional):
            b.bid = positional[j]
            continue
        b.bid = claim(b.kind, b.content) or branch_id_of(n.key, i, b.content)


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

# 布局参数：层距要容得下 260 宽的节点 + 连线弯折空间
LAYER_GAP, ORIGIN_X, ORIGIN_Y, ROW_GAP = 440, 350, 300, 48


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


def layout(design: Design) -> dict[str, tuple[float, float]]:
    """分层布局：主干水平推进，同层纵向堆叠。

    节点高度用 estimate_node_height() 精确估算（1:1 对齐画布的几何估算器），
    所以分支多的对话节点会自动拿到更大的纵向空间，不会压到下一个节点。
    """
    by_no = design.by_no
    succ: dict[str, list[str]] = {}
    for n in design.nodes:
        outs = [b.target for b in canvas_branch_list(n) if b.target]
        if n.next:
            outs.append(n.next)
        succ[n.no] = [t for t in outs if t in by_no]

    roots = [n.no for n in design.nodes if n.kind == "start"] + \
            [n.no for n in design.nodes if n.is_global]
    # BFS 最短层（流程允许成环，必须用 visited 收敛，不能算最长路）
    layer: dict[str, int] = {}
    queue: list[tuple[str, int]] = [(r, 0) for r in roots]
    head = 0
    while head < len(queue):
        no, lv = queue[head]
        head += 1
        if no in layer:
            continue
        layer[no] = lv
        for t in succ.get(no, []):
            if t not in layer:
                queue.append((t, lv + 1))
    max_lv = max(layer.values()) if layer else 0
    for n in design.nodes:
        layer.setdefault(n.no, max_lv + 1)

    order = {n.no: i for i, n in enumerate(design.nodes)}
    groups: dict[int, list[str]] = {}
    for no, lv in layer.items():
        groups.setdefault(lv, []).append(no)
    pos: dict[str, tuple[float, float]] = {}
    for lv in sorted(groups):
        cursor = ORIGIN_Y
        for no in sorted(groups[lv], key=lambda k: order[k]):
            n = by_no[no]
            if n.pos:                       # decompile 带回来的原坐标，原样保留
                pos[no] = n.pos
                continue
            pos[no] = (ORIGIN_X + lv * LAYER_GAP, cursor)
            cursor += estimate_node_height(n) + ROW_GAP
    return pos


STATIC_H_HALF = 86              # 画布以静态 nodeStyle.height(172) 的一半做绘制原点


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
               rewrap: bool = True) -> dict[str, Any]:
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

    ids: dict[str, str] = {}
    for n in design.nodes:
        ids[n.no] = n.node_id or node_id_of(n.no, n.name)
    pos = layout(design)

    ivr: dict[str, list[dict[str, Any]]] = {}
    for n in design.nodes:
        nid = ids[n.no]
        bnode = base_by_id.get(nid)
        _normalize_branches(n)
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

        cbrs = canvas_branch_list(n)
        edges: list[dict[str, Any]] = []
        if n.kind in MULTI_EXIT_KINDS and not (n.kind == "chat" and not _listens(n)):
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
                    pos[n.no], pos[tgt], 1, etype, None,
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


def _listens(n: Node) -> bool:
    """chat 节点是否听用户回复（外部 JSON 的 selectBranch == 内部 listenUserReply）。"""
    if n.kind == "announce":
        return False
    if n.a("听用户回复") is not None:
        return _to_bool(n.a("听用户回复"))
    return bool([b for b in n.branches if b.kind != "global_intent"])


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
        slot_type = n.a("词槽类型", "custom")
        content_mode = n.a("话术模式", "固定" if k == "announce" else "智能生成")
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
            is_builtin = slot_type in BUILTIN_SLOT_TYPES
            if is_builtin:
                ctype = slot_type                      # date / address / name ...
            elif slot_id:
                ctype = str(slot_id)                   # 已存在的自定义词槽
            else:
                ctype = entity_tmp_id_of(n.key, var)   # 待建槽的临时 id
            ent: dict[str, Any] = {
                "slotType": slot_type if (is_builtin or slot_id) else "custom",
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
            elif not is_builtin:
                # 前端临时 id：保存时 syncConversationSlots 会建槽并回填 slotId
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


def validate(flow: dict[str, Any], design: Design | None = None) -> list[Issue]:
    out: list[Issue] = []
    ivr = flow.get("ivrData") or {}
    nodes: list[dict[str, Any]] = [n for arr in ivr.values() for n in arr]
    by_id = {n.get("id"): n for n in nodes}

    def add(lv: str, code: str, where: str, msg: str) -> None:
        out.append(Issue(lv, code, where, msg))

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
        single = typ in ("startNode", "extractVariableNode") or (
            typ == "chatNode" and not nd.get("selectBranch"))
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

        # E7 全局节点必须有非空 global_intent
        if is_global and typ == "chatNode":
            gi = [b for b in (nd.get("branches") or [])
                  if b.get("type") == "global_intent" and str(b.get("content") or "").strip()]
            if not gi:
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
            if nd.get("selectBranch") and not [
                    b for b in (nd.get("branches") or []) if b.get("type") != "global_intent"]:
                add("error", "E14", name, "listenUserReply=true（selectBranch）但没有任何分支")
            if nd.get("contentType") not in ("gen", "fix"):
                add("error", "E15", name,
                    f"contentType 只能是 gen 或 fix，实际是 {nd.get('contentType')!r}（注意不是 fixed）")

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
    if len(sp) > SYSTEM_PROMPT_MAX:
        add("error", "E12", "voiceSettings.systemPrompt",
            f"长度 {len(sp)} 超过上限 {SYSTEM_PROMPT_MAX}，画布会静默截断")
    elif len(sp) > SYSTEM_PROMPT_MAX * 0.85:
        add("warn", "W9", "voiceSettings.systemPrompt", f"长度 {len(sp)}，接近 {SYSTEM_PROMPT_MAX} 上限")
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
            lines.append(f"| {i.code} | {i.where} | {i.msg.replace('|', '/')} |")
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
DAY_ZH = {"BusinessDay": "工作日", "Holiday": "节假日", "Weekly": "每周", "Specific": "指定日期"}


def _kind_of(node: dict[str, Any]) -> str:
    t = node.get("type")
    nd = node.get("nodeData") or {}
    if t == "chatNode":
        return "chat" if nd.get("selectBranch") else "announce"
    if t == "DTMFNode":
        return "dtmf-nav" if nd.get("nodeType") == "navigation" else "dtmf-collect"
    if t == "transfer":
        return {"manual": "transfer-skill", "outer": "transfer-outer",
                "third_party_route": "transfer-3rd"}.get(nd.get("mode", "manual"), "transfer-skill")
    return TYPE_TO_KIND.get(t, "chat")


def decompile(flow: dict[str, Any], title: str = "") -> str:
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
    no_of = {nid: f"N{i + 1:02d}" for i, nid in enumerate(order)}

    vs = flow.get("voiceSettings") or {}
    out: list[str] = [f"# 流程：{title or '未命名流程'}", ""]

    out += ["## 环境配置", ""]
    inv_env = {v[0]: k for k, v in ENV_TO_VOICE.items()}
    for fld, zh in inv_env.items():
        if fld in vs and vs[fld] not in (None, ""):
            val = vs[fld]
            if isinstance(val, bool):
                val = "是" if val else "否"
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
        out.append(f"- 坐标: {round(float(n.get('x', 0)), 1)},{round(float(n.get('y', 0)), 1)}")
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
        a.append(f"- 话术模式: {'固定' if nd.get('contentType') == 'fix' else '智能生成'}")
        a.append(f"- 允许打断: {'是' if nd.get('allowInterrupt') else '否'}")
        if nd.get("silentWaitTime") is not None:
            a.append(f"- 静默等待(秒): {nd['silentWaitTime']}")
        if nd.get("verifyEntity"):
            a.append("- 校验实体: 是")
        for e in nd.get("entities") or []:
            a.append(f"- 收集变量: {e.get('varName', '')}")
            a.append(f"- 词槽类型: {e.get('slotType') or 'custom'}")
            if e.get("slotId") is not None:
                a.append(f"- 词槽ID: {e['slotId']}")
            if e.get("name"):
                a.append(f"- 词槽名称: {e['name']}")
            if e.get("description"):
                a.append("- 词槽说明: " + _esc(e["description"]))
            if e.get("collectionMode") is not None:
                a.append(f"- 收集方式: {'固定选项' if e['collectionMode'] == 'fixed' else '开放'}")
            if e.get("fixedOptions"):
                a.append(f"- 固定选项: {_esc(e['fixedOptions'])}")
    elif kind == "api":
        a.append(f"- URL: {nd.get('url', '')}")
        a.append(f"- 超时(毫秒): {nd.get('callbackTimeout', 4500)}")
        a.append(f"- 异步: {'是' if nd.get('async') else '否'}")
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
        a.append(f"- 时区: {nd.get('timeZoneName', 'Asia/Shanghai')}")
    elif kind in ("dtmf-nav", "dtmf-collect"):
        cfg = (nd.get("tcccNodeData") or {}).get("dtmfConfig") or {}
        a.append("- 话术: " + str(cfg.get("play_sound", "")).replace("tts:", ""))
        a.append(f"- 超时(毫秒): {cfg.get('dtmf_timeout', 5000)}")
        a.append(f"- 失败次数: {cfg.get('max_failures', 1)}")
        if kind == "dtmf-collect":
            a.append(f"- 收集变量: {nd.get('varName', '')}")
            a.append(f"- 位数: {cfg.get('dtmf_len', 1)}")
    elif kind.startswith("transfer-") and kind != "transfer-agent":
        if nd.get("name"):
            a.append(f"- 技能组ID: {nd['name']}")
        if nd.get("caller"):
            a.append(f"- 主叫: {nd['caller']}")
        if nd.get("callee"):
            a.append(f"- 被叫: {nd['callee']}")
        if (nd.get("aiTransferContext") or {}).get("enableSummary"):
            a.append("- 转人工带摘要: 是")
    elif kind == "transfer-agent":
        a.append(f"- 智能体ID: {nd.get('varAgentID', '')}")
    return a


def _decompile_branches(kind: str, n: dict[str, Any], no_of: dict[str, str],
                        by_id: dict[str, Any]) -> list[str]:
    nd = n.get("nodeData") or {}
    edges = n.get("outEdges") or []
    tgt_by_src: dict[str, str] = {}
    for e in edges:
        tgt_by_src[str(e.get("source"))] = str(e.get("target"))

    def arrow(bid: str) -> str:
        t = tgt_by_src.get(str(bid))
        if not t or t not in by_id:
            return ""
        return f" → {no_of[t]} {by_id[t].get('name')}"

    out: list[str] = []
    if kind in ("start", "announce", "assign"):
        t = tgt_by_src.get(str(n.get("id")))
        if t and t in by_id:
            out.append(f"→ {no_of[t]} {by_id[t].get('name')}")
        return out
    if kind in TERMINAL_KINDS:
        return out

    out.append("分支：")
    if kind == "condition":
        for b in nd.get("logicSplitBranches") or []:
            if b.get("logicType") == "else":
                out.append(f"- 否则{arrow(b.get('id'))}")
            elif b.get("method") == "llm":
                out.append(f"- 如果 LLM: {b.get('llmPrompt', '')}{arrow(b.get('id'))}")
            else:
                joiner = " 或 " if b.get("logicType") == "or" else " 且 "
                expr = joiner.join(
                    f"{c.get('var')} {c.get('operator')} {c.get('val', '')}".strip()
                    for c in b.get("conditions") or [])
                out.append(f"- 如果 {expr}{arrow(b.get('id'))}")
    elif kind == "worktime":
        for b in nd.get("workTimeBranches") or []:
            if b.get("type") == "other":
                out.append(f"- 其他时间{arrow(b.get('id'))}")
            else:
                segs = []
                for c in b.get("workTimeConfigs") or []:
                    ps = "、".join(
                        f"{p['startTime']['hour']:02d}:{p['startTime']['minute']:02d}-"
                        f"{p['endTime']['hour']:02d}:{p['endTime']['minute']:02d}"
                        for p in c.get("workTimePeriods") or [])
                    segs.append(f"{DAY_ZH.get(c.get('dayType'), '工作日')} {ps}")
                out.append(f"- {b.get('name') or '工作时间'}: {'; '.join(segs)}{arrow(b.get('id'))}")
    else:
        for b in nd.get("branches") or []:
            t = b.get("type")
            c = str(b.get("content") or "")
            tagpart = ""
            if b.get("tags"):
                tagpart = " {标签: " + "; ".join(
                    f"{x.get('tagName')}={x.get('tagValue')}" for x in b["tags"]) + "}"
            if t == "global_intent":
                continue
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
    design = parse_design(src.read_text(encoding="utf-8"))
    base = _load_json(args.base) if args.base else None
    # 新建流程默认修硬折行；--base 改存量画布时默认保真不动内容
    rewrap = args.rewrap if args.rewrap is not None else (base is None)
    flow = build_json(design, base, rewrap=rewrap)
    issues = validate(flow, design)
    errs = [i for i in issues if i.level == "error"]

    out = Path(args.output or src.with_suffix(".json"))
    report_path = Path(args.report) if args.report else out.with_name(out.stem + "-校验报告.md")
    report_path.write_text(render_report(issues, flow, design.title), encoding="utf-8")

    if errs and not args.force:
        print(f"[FAIL] {len(errs)} 项 error，未写出 JSON。报告：{report_path}", file=sys.stderr)
        for i in errs[:20]:
            print(f"  - [{i.code}] {i.where}：{i.msg}", file=sys.stderr)
        return 2
    out.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
    n_nodes = sum(len(v) for v in flow["ivrData"].values())
    n_edges = sum(len(n["outEdges"]) for v in flow["ivrData"].values() for n in v)
    print(f"[OK] {out}  节点 {n_nodes} / 连线 {n_edges} / "
          f"error {len(errs)} / warn {len(issues) - len(errs)}")
    if rewrap and design.rewrap_stats:
        print(f"[整形] 合并了 {design.rewrap_stats} 处「一句话没写完就换行」的排版折行")
    print(f"[报告] {report_path}")
    if args.mermaid:
        Path(args.mermaid).write_text(mermaid(design), encoding="utf-8")
        print(f"[流程图] {args.mermaid}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    flow = _load_json(args.flow)
    design = parse_design(Path(args.design).read_text(encoding="utf-8")) if args.design else None
    issues = validate(flow, design)
    errs = [i for i in issues if i.level == "error"]
    text = render_report(issues, flow, design.title if design else "")
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
        print(f"[报告] {args.report}")
    else:
        print(text)
    return 2 if errs else 0


def cmd_decompile(args: argparse.Namespace) -> int:
    flow = _load_json(args.flow)
    md = decompile(flow, args.title or Path(args.flow).stem)
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

    b = sub.add_parser("build", help="设计稿 -> 画布 JSON")
    b.add_argument("design")
    b.add_argument("-o", "--output")
    b.add_argument("--base", help="已导出的真实画布 JSON，作为 voiceSettings / 未表达字段的底座")
    b.add_argument("--report")
    b.add_argument("--mermaid")
    b.add_argument("--force", action="store_true", help="即使有 error 也写出 JSON（仅调试用）")
    b.add_argument("--rewrap", dest="rewrap", action="store_true", default=None,
                   help="把话术里的排版硬折行合回一行（不带 --base 时默认开启）")
    b.add_argument("--no-rewrap", dest="rewrap", action="store_false",
                   help="保留话术里的换行原样不动")
    b.set_defaults(func=cmd_build)

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
    dcp.set_defaults(func=cmd_decompile)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except DesignError as e:
        print(f"[设计稿错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
