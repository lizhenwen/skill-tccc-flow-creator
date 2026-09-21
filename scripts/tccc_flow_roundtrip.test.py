#!/usr/bin/env python3
"""空分类对话节点：出边要保住，但不能勾上「用户回复分类」。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tccc_flow as tf  # noqa: E402

FLOW = {
    "version": "1.0",
    "ivrData": {
        "startNode": [{
            "id": "n-start",
            "x": 0, "y": 0, "type": "startNode", "name": "开始通话",
            "nodeData": {},
            "outEdges": [{
                "id": "e1", "source": "n-start", "target": "n-chat",
                "type": "", "name": "",
                "startPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                "edgeData": {},
            }],
        }],
        "chatNode": [{
            "id": "n-chat",
            "x": 100, "y": 0, "type": "chatNode", "name": "对话节点1",
            "nodeData": {
                "welcomeText": "你好啊",
                "content": "你好啊",
                "contentType": "gen",
                "selectBranch": True,
                "allowInterrupt": False,
                "branches": [],
                "entities": [],
                "verifyEntity": False,
                "queryVarName": "",
                "globalNode": False,
            },
            "outEdges": [{
                "id": "e2", "source": "n-chat", "target": "n-end",
                "type": "custom:cubic-horizontal", "name": "",
                "startPoint": {"x": 0, "y": 0, "anchorIndex": 1},
                "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                "edgeData": {},
            }],
        }],
        "hangup": [{
            "id": "n-end",
            "x": 200, "y": 0, "type": "hangup", "name": "结束通话",
            "nodeData": {},
            "outEdges": [],
        }],
    },
    "voiceSettings": {"voiceType": "ZhiXi", "maxDuration": 60, "notifyDuration": 10},
}


def _chat(flow: dict) -> dict:
    return flow["ivrData"]["chatNode"][0]


class EmptyBranchNoSelectBranch(unittest.TestCase):
    def test_decompile_as_announce_keeps_default_next(self):
        md = tf.decompile(FLOW, "测")
        self.assertIn("[announce]", md.split("### N02", 1)[-1].split("### N03", 1)[0])
        self.assertNotIn("- 听用户回复: 是", md)
        self.assertIn("→ N03 结束通话", md)
        self.assertNotIn("- 坐标:", md)

    def test_build_clears_select_branch_and_reply_mode(self):
        md = tf.decompile(FLOW, "测")
        design = tf.parse_design(md)
        rebuilt = tf.build_json(design, FLOW, rewrap=False)
        issues = tf.validate(rebuilt, design)
        errs = [i for i in issues if i.level == "error"]
        self.assertEqual(errs, [], [i.msg for i in errs])
        chat = _chat(rebuilt)
        self.assertFalse(chat["nodeData"]["selectBranch"])
        self.assertNotIn("replyMode", chat["nodeData"])
        self.assertEqual(chat["nodeData"]["branches"], [])
        self.assertEqual(len(chat["outEdges"]), 1)
        self.assertEqual(chat["outEdges"][0]["source"], "n-chat")
        self.assertEqual(chat["outEdges"][0]["target"], "n-end")
        self.assertEqual(rebuilt["ivrData"]["startNode"][0]["x"], 0)
        self.assertEqual(rebuilt["ivrData"]["startNode"][0]["y"], 0)
        self.assertEqual(chat["x"], 100)
        self.assertEqual(chat["y"], 0)

    def test_legacy_listen_flag_without_branches_is_false(self):
        md = (
            "# 流程：测\n\n## 环境配置\n\n- 技能组ID: \n- 转接智能体ID: \n- 环境注入变量: \n\n"
            "## 全局提示词\n\n```prompt\n\n```\n\n## 变量表\n\n"
            "| 变量名 | 来源 | 说明 |\n|---|---|---|\n\n## 节点\n\n"
            "### N01 开始通话 [start]\n- ID: n-start\n→ N02 对话节点1\n\n"
            "### N02 对话节点1 [chat]\n- ID: n-chat\n- 话术模式: 智能生成\n- 允许打断: 否\n"
            "- 听用户回复: 是\n```prompt\n你好啊\n```\n→ N03 结束通话\n\n"
            "### N03 结束通话 [end]\n- ID: n-end\n"
        )
        design = tf.parse_design(md)
        rebuilt = tf.build_json(design, FLOW, rewrap=False)
        chat = _chat(rebuilt)
        self.assertFalse(chat["nodeData"]["selectBranch"])
        self.assertNotIn("replyMode", chat["nodeData"])
        self.assertEqual(chat["outEdges"][0]["target"], "n-end")

    def test_base_reply_mode_is_stripped(self):
        base = {
            "version": "1.0",
            "ivrData": {
                "startNode": FLOW["ivrData"]["startNode"],
                "chatNode": [{
                    **FLOW["ivrData"]["chatNode"][0],
                    "nodeData": {
                        **FLOW["ivrData"]["chatNode"][0]["nodeData"],
                        "replyMode": "intent",
                    },
                }],
                "hangup": FLOW["ivrData"]["hangup"],
            },
            "voiceSettings": FLOW["voiceSettings"],
        }
        md = tf.decompile(FLOW, "测")
        design = tf.parse_design(md)
        rebuilt = tf.build_json(design, base, rewrap=False)
        self.assertNotIn("replyMode", _chat(rebuilt)["nodeData"])


SLOT_MD = (
    "# 流程：测词槽\n\n## 环境配置\n\n- 技能组ID: \n- 转接智能体ID: \n- 环境注入变量: \n\n"
    "## 全局提示词\n\n```prompt\n你是客服\n```\n\n## 变量表\n\n"
    "| 变量名 | 来源 | 说明 |\n|---|---|---|\n| 手机号 | 词槽采集 | 11 位 |\n\n## 节点\n\n"
    "### N01 开始通话 [start]\n- ID: n-start\n→ N02 收集手机号\n\n"
    "### N02 收集手机号 [chat]\n- ID: n-slot\n- 话术模式: 智能生成\n- 允许打断: 否\n"
    "- 收集变量: 手机号\n- 词槽类型: custom\n- 词槽名称: 手机号\n- 收集方式: 开放\n"
    "```prompt\n# 目标\n拿到手机号。\n# 参考示例\n- \"报一下手机号。\"\n"
    "# 应对策略\n- 客户不愿意给：不要反复逼问。\n```\n"
    "分支：\n"
    "- 客户明确拒绝提供手机号。示例：我不想给。 → N03 结束通话\n"
    "- 收集成功 → N03 结束通话\n"
    "- 收集失败 → N03 结束通话\n"
    "- 无应答 → N03 结束通话\n\n"
    "### N03 结束通话 [end]\n- ID: n-end\n"
)


def _slot(flow: dict) -> dict:
    return next(n for n in flow["ivrData"]["chatNode"] if n["name"] == "收集手机号")


class SlotNodeNoEntityFail(unittest.TestCase):
    def test_drops_entity_fail_and_orders_branches(self):
        design = tf.parse_design(SLOT_MD)
        flow = tf.build_json(design, rewrap=False)
        issues = tf.validate(flow, design)
        self.assertEqual([i for i in issues if i.level == "error"], [])
        slot = _slot(flow)
        types = [b["type"] for b in slot["nodeData"]["branches"]]
        self.assertNotIn("entity_fail", types)
        self.assertEqual(types, ["entity_success", "intent", "silent"])
        self.assertEqual(slot["nodeData"]["replyMode"], "intent_with_entity")
        anchors = {
            e["edgeData"]["branchId"]: e["startPoint"]["anchorIndex"]
            for e in slot["outEdges"]
        }
        by_type = {b["type"]: b["id"] for b in slot["nodeData"]["branches"]}
        self.assertEqual(anchors[by_type["entity_success"]], 1)
        self.assertEqual(anchors[by_type["intent"]], 2)
        self.assertEqual(anchors[by_type["silent"]], 3)
        warns = [i for i in issues if i.code == "W13"]
        self.assertTrue(warns)

    def test_validate_rejects_legacy_entity_fail(self):
        design = tf.parse_design(SLOT_MD)
        flow = tf.build_json(design, rewrap=False)
        slot = _slot(flow)
        slot["nodeData"]["branches"].insert(1, {
            "id": "relation-fail",
            "type": "entity_fail",
            "content": "收集失败",
            "name": "",
            "tags": [],
        })
        issues = tf.validate(flow)
        self.assertTrue(any(i.code == "E17" and i.level == "error" for i in issues))

    def test_decompile_skips_entity_fail(self):
        flow = {
            "version": "1.0",
            "ivrData": {
                "startNode": [{
                    "id": "n-start", "x": 0, "y": 0, "type": "startNode",
                    "name": "开始通话", "nodeData": {},
                    "outEdges": [{
                        "id": "e1", "source": "n-start", "target": "n-slot",
                        "type": "", "name": "",
                        "startPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                        "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                        "edgeData": {},
                    }],
                }],
                "chatNode": [{
                    "id": "n-slot", "x": 100, "y": 0, "type": "chatNode",
                    "name": "收集手机号",
                    "nodeData": {
                        "welcomeText": "报一下手机号",
                        "content": "报一下手机号",
                        "contentType": "gen",
                        "selectBranch": True,
                        "allowInterrupt": False,
                        "branches": [
                            {"id": "b-ok", "type": "entity_success",
                             "content": "收集${手机号}结束", "name": "", "tags": []},
                            {"id": "b-fail", "type": "entity_fail",
                             "content": "收集失败", "name": "", "tags": []},
                            {"id": "b-no", "type": "intent",
                             "content": "客户拒绝", "name": "", "tags": []},
                        ],
                        "entities": [{
                            "slotType": "custom", "varName": "手机号",
                            "name": "手机号", "description": "",
                        }],
                        "collectionConfig": {
                            "collectionType": "entity-x",
                            "collectionAsValue": "手机号",
                            "collectionItems": [{
                                "id": "c1", "collectionType": "entity-x",
                                "collectionAsValue": "手机号",
                            }],
                        },
                        "replyMode": "intent_with_entity",
                        "verifyEntity": False,
                        "queryVarName": "",
                        "globalNode": False,
                    },
                    "outEdges": [{
                        "id": "e2", "source": "b-ok", "target": "n-end",
                        "type": "custom:cubic-horizontal", "name": "",
                        "startPoint": {"x": 0, "y": 0, "anchorIndex": 1},
                        "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                        "edgeData": {"branchId": "b-ok"},
                    }],
                }],
                "hangup": [{
                    "id": "n-end", "x": 200, "y": 0, "type": "hangup",
                    "name": "结束通话", "nodeData": {}, "outEdges": [],
                }],
            },
            "voiceSettings": {"voiceType": "ZhiXi", "maxDuration": 60, "notifyDuration": 10},
        }
        md = tf.decompile(flow, "测")
        self.assertIn("- 收集成功", md)
        self.assertNotIn("收集失败", md)
        self.assertIn("客户拒绝", md)


class SlotTypeAlwaysCustom(unittest.TestCase):
    def test_name_type_compiles_as_custom_with_temp_id(self):
        md = SLOT_MD.replace("- 词槽类型: custom\n", "- 词槽类型: name\n")
        design = tf.parse_design(md)
        flow = tf.build_json(design, rewrap=False)
        issues = tf.validate(flow, design)
        slot = _slot(flow)
        ent = slot["nodeData"]["entities"][0]
        self.assertEqual(ent["slotType"], "custom")
        self.assertTrue(ent.get("id"))
        self.assertNotEqual(slot["nodeData"]["collectionConfig"]["collectionType"], "name")
        self.assertEqual(slot["nodeData"]["collectionConfig"]["collectionType"], ent["id"])
        self.assertTrue(any(i.code == "W14" for i in issues))

    def test_decompile_never_writes_name_slot_type(self):
        design = tf.parse_design(SLOT_MD)
        flow = tf.build_json(design, rewrap=False)
        flow["ivrData"]["chatNode"][0]["nodeData"]["entities"][0]["slotType"] = "name"
        md = tf.decompile(flow, "测")
        self.assertNotIn("- 词槽类型: name", md)
        self.assertNotIn("- 词槽类型: custom", md)


def _n02(md: str) -> str:
    return md.split("### N02", 1)[-1].split("### N03", 1)[0]


def _chat_flow(*, content_type: str, allow_interrupt: bool) -> dict:
    return {
        "version": "1.0",
        "ivrData": {
            "startNode": FLOW["ivrData"]["startNode"],
            "chatNode": [{
                **FLOW["ivrData"]["chatNode"][0],
                "nodeData": {
                    **FLOW["ivrData"]["chatNode"][0]["nodeData"],
                    "contentType": content_type,
                    "allowInterrupt": allow_interrupt,
                    "selectBranch": True,
                    "branches": [{"id": "b1", "type": "intent", "content": "好的", "name": "", "tags": []}],
                },
            }],
            "hangup": FLOW["ivrData"]["hangup"],
        },
        "voiceSettings": FLOW["voiceSettings"],
    }


class DecompileOmitsDefaults(unittest.TestCase):
    def test_announce_gen_omits_speech_mode_roundtrip_keeps_gen(self):
        md = tf.decompile(FLOW, "测")
        chunk = _n02(md)
        self.assertIn("[announce]", chunk)
        self.assertNotIn("话术模式", chunk)
        self.assertNotIn("允许打断", chunk)
        rebuilt = tf.build_json(tf.parse_design(md), FLOW, rewrap=False)
        self.assertEqual(_chat(rebuilt)["nodeData"]["contentType"], "gen")

    def test_omitted_speech_mode_is_gen_even_with_fix_base(self):
        md = (
            "# 流程：测\n\n## 环境配置\n\n- 技能组ID: \n- 转接智能体ID: \n- 环境注入变量: \n\n"
            "## 全局提示词\n\n```prompt\n\n```\n\n## 变量表\n\n"
            "| 变量名 | 来源 | 说明 |\n|---|---|---|\n\n## 节点\n\n"
            "### N01 开始通话 [start]\n- ID: n-start\n→ N02 对话节点1\n\n"
            "### N02 对话节点1 [announce]\n- ID: n-chat\n```prompt\n你好啊\n```\n"
            "→ N03 结束通话\n\n"
            "### N03 结束通话 [end]\n- ID: n-end\n"
        )
        design = tf.parse_design(md)
        self.assertEqual(_chat(tf.build_json(design, rewrap=False))["nodeData"]["contentType"], "gen")
        base = {
            "version": "1.0",
            "ivrData": {
                "startNode": FLOW["ivrData"]["startNode"],
                "chatNode": [{
                    **FLOW["ivrData"]["chatNode"][0],
                    "nodeData": {
                        **FLOW["ivrData"]["chatNode"][0]["nodeData"],
                        "contentType": "fix",
                        "selectBranch": False,
                    },
                }],
                "hangup": FLOW["ivrData"]["hangup"],
            },
            "voiceSettings": FLOW["voiceSettings"],
        }
        self.assertEqual(
            _chat(tf.build_json(tf.parse_design(md), base, rewrap=False))["nodeData"]["contentType"],
            "gen",
        )

    def test_announce_fix_writes_speech_mode(self):
        flow = {
            "version": "1.0",
            "ivrData": {
                "startNode": FLOW["ivrData"]["startNode"],
                "chatNode": [{
                    **FLOW["ivrData"]["chatNode"][0],
                    "nodeData": {
                        **FLOW["ivrData"]["chatNode"][0]["nodeData"],
                        "contentType": "fix",
                        "selectBranch": False,
                    },
                }],
                "hangup": FLOW["ivrData"]["hangup"],
            },
            "voiceSettings": FLOW["voiceSettings"],
        }
        chunk = _n02(tf.decompile(flow, "测"))
        self.assertIn("[announce]", chunk)
        self.assertIn("- 话术模式: 固定", chunk)
        self.assertNotIn("允许打断", chunk)

    def test_chat_omits_default_speech_mode_and_interrupt(self):
        chunk = _n02(tf.decompile(_chat_flow(content_type="gen", allow_interrupt=False), "测"))
        self.assertIn("[chat]", chunk)
        self.assertNotIn("话术模式", chunk)
        self.assertNotIn("允许打断", chunk)

    def test_chat_fix_and_interrupt_are_written(self):
        chunk = _n02(tf.decompile(_chat_flow(content_type="fix", allow_interrupt=True), "测"))
        self.assertIn("- 话术模式: 固定", chunk)
        self.assertIn("- 允许打断: 是", chunk)

    def test_sparse_decompile_roundtrip_keeps_json_defaults(self):
        src = _chat_flow(content_type="gen", allow_interrupt=False)
        md = tf.decompile(src, "测")
        design = tf.parse_design(md)
        rebuilt = tf.build_json(design, src, rewrap=False)
        nd = _chat(rebuilt)["nodeData"]
        self.assertEqual(nd["contentType"], "gen")
        self.assertFalse(nd["allowInterrupt"])


SPLIT_MD = """# 流程：测

## 环境配置

- 音色: ZhiXi

## 全局提示词

```prompt
hi
```

## 变量表

| 变量名 | 来源 | 说明 |
|---|---|---|

## 节点

### N01 开始 [start]
- ID: n-start
"""


class SplitCanonicalSeq(unittest.TestCase):
    def test_missing_assumptions_keeps_canonical_numbers(self):
        paths = [p for p, _ in tf.split_design(SPLIT_MD)]
        self.assertEqual(paths[0], "00-流程信息.md")
        self.assertEqual(paths[1], "10-环境配置.md")
        self.assertEqual(paths[2], "30-全局提示词.md")
        self.assertEqual(paths[3], "40-变量表.md")
        self.assertTrue(paths[4].startswith("50-节点/"))
        self.assertFalse(any("20-" in p for p in paths))

    def test_decompile_split_does_not_shift_nodes_dir(self):
        md = tf.decompile(FLOW, "测")
        self.assertNotIn("## 假设与待确认", md)
        self.assertNotIn("- 坐标:", md)
        paths = [p for p, _ in tf.split_design(md)]
        tops = []
        for p in paths:
            tops.append(p.split("/", 1)[0])
        self.assertIn("30-全局提示词.md", tops)
        self.assertIn("40-变量表.md", tops)
        self.assertTrue(any(t == "50-节点" for t in tops))
        self.assertNotIn("20-全局提示词.md", tops)
        self.assertNotIn("40-节点", tops)

    def test_present_assumptions_still_use_20(self):
        md = SPLIT_MD.replace(
            "## 全局提示词",
            "## 假设与待确认\n\n- 待确认 x\n\n## 全局提示词",
        )
        paths = [p for p, _ in tf.split_design(md)]
        self.assertEqual(paths[1], "10-环境配置.md")
        self.assertEqual(paths[2], "20-假设与待确认.md")
        self.assertEqual(paths[3], "30-全局提示词.md")
        self.assertEqual(paths[4], "40-变量表.md")
        self.assertTrue(paths[5].startswith("50-节点/"))

    def test_extra_chapter_fits_in_gap(self):
        md = SPLIT_MD.replace(
            "## 全局提示词",
            "## 变更记录\n\n- 一期\n\n## 全局提示词",
        )
        paths = [p for p, _ in tf.split_design(md)]
        self.assertEqual(paths[2], "20-变更记录.md")
        self.assertEqual(paths[3], "30-全局提示词.md")
        self.assertTrue(any(p.startswith("50-节点/") for p in paths))


class KeepNodeNos(unittest.TestCase):
    def test_override_keeps_sparse_numbers_and_arrows(self):
        md = tf.decompile(FLOW, "测", nos_by_id={
            "n-start": "N01",
            "n-chat": "N10",
            "n-end": "N99",
        })
        self.assertIn("### N01 开始通话 [start]", md)
        self.assertIn("### N10 对话节点1 [announce]", md)
        self.assertIn("### N99 结束通话 [end]", md)
        self.assertIn("→ N10 对话节点1", md)
        self.assertIn("→ N99 结束通话", md)
        self.assertNotIn("### N02 ", md)

    def test_new_id_fills_lowest_gap(self):
        nos = tf.assign_node_nos(["a", "b", "c"], {"b": "N10"})
        self.assertEqual(nos["a"], "N01")
        self.assertEqual(nos["b"], "N10")
        self.assertEqual(nos["c"], "N02")

    def test_load_nos_uses_explicit_id(self):
        d = Path(self._mk())
        (d / "N10-对话.md").write_text("### N10 对话节点1 [announce]\n- ID: n-chat\n", encoding="utf-8")
        self.assertEqual(tf.load_nos_from_src(d), {"n-chat": "N10"})

    def test_load_nos_falls_back_to_stable_id(self):
        d = Path(self._mk())
        (d / "N45-询问.md").write_text("### N45 询问 [chat]\n→ N46\n", encoding="utf-8")
        self.assertEqual(tf.load_nos_from_src(d), {tf.node_id_of("N45", "询问"): "N45"})

    def _mk(self) -> str:
        import tempfile
        d = tempfile.mkdtemp(prefix="tccc-nos-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class BackfillNodeIds(unittest.TestCase):
    def test_patch_inserts_after_header(self):
        src = "### N01 开始 [start]\n→ N02\n"
        self.assertEqual(
            tf.patch_node_id_markdown(src, "node-abc"),
            "### N01 开始 [start]\n- ID: node-abc\n→ N02\n",
        )

    def test_patch_noop_when_same(self):
        src = "### N01 开始 [start]\n- ID: node-abc\n"
        self.assertEqual(tf.patch_node_id_markdown(src, "node-abc"), src)

    def test_resolve_unique_name_from_base(self):
        design = tf.Design(nodes=[tf.Node(no="N01", name="开始通话", kind="start")])
        base = {"ivrData": {"startNode": [{"id": "tccc-uuid-1", "name": "开始通话"}]}}
        self.assertEqual(tf.resolve_node_ids(design, base)["N01"], "tccc-uuid-1")

    def test_resolve_explicit_id_wins(self):
        design = tf.Design(nodes=[tf.Node(no="N01", name="开始通话", kind="start", node_id="kept")])
        base = {"ivrData": {"startNode": [{"id": "tccc-uuid-1", "name": "开始通话"}]}}
        self.assertEqual(tf.resolve_node_ids(design, base)["N01"], "kept")

    def test_backfill_writes_file(self):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tccc-id-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        p = d / "开始.md"
        p.write_text("### N01 开始 [start]\n→ N02\n", encoding="utf-8")
        self.assertEqual(tf.backfill_src_node_ids(d, {"N01": "node-xyz"}), 1)
        self.assertIn("- ID: node-xyz", p.read_text(encoding="utf-8"))
        self.assertEqual(tf.backfill_src_node_ids(d, {"N01": "node-xyz"}), 0)


class NodeFileNames(unittest.TestCase):
    def test_split_uses_node_name(self):
        paths = [p for p, _ in tf.split_design(SPLIT_MD)]
        self.assertIn("50-节点/开始.md", paths)
        self.assertFalse(any("/N01-" in p or p.endswith("/N01.md") for p in paths))

    def test_split_disambiguates_duplicate_names(self):
        md = (
            "# 流程：测\n\n## 节点\n\n"
            "### N01 开始 [start]\n→ N02\n\n"
            "### N02 转人工 [end]\n\n"
            "### N09 转人工 [end]\n"
        )
        paths = [p for p, _ in tf.split_design(md) if p.startswith("50-节点/")]
        self.assertEqual(paths, ["50-节点/开始.md", "50-节点/转人工.md", "50-节点/转人工-N09.md"])

    def test_assemble_orders_by_header_not_filename(self):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tccc-ord-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        nodes = d / "50-节点"
        nodes.mkdir(parents=True)
        (d / "00-流程信息.md").write_text("# 流程：测\n", encoding="utf-8")
        (nodes / "转人工.md").write_text("### N09 转人工 [end]\n", encoding="utf-8")
        (nodes / "开始.md").write_text("### N01 开始 [start]\n→ N09\n", encoding="utf-8")
        (nodes / "询问.md").write_text("### N02 询问 [chat]\n→ N09\n", encoding="utf-8")
        text, _ = tf.assemble_src(d)
        i1 = text.index("### N01 开始")
        i2 = text.index("### N02 询问")
        i9 = text.index("### N09 转人工")
        self.assertLess(i1, i2)
        self.assertLess(i2, i9)

    def test_assemble_nested_dirs_still_global_no_order(self):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tccc-nest-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "00-流程信息.md").write_text("# 流程：测\n", encoding="utf-8")
        a = d / "50-节点" / "20-售后"
        b = d / "50-节点" / "10-主干"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "询问.md").write_text("### N02 询问 [chat]\n→ N03\n", encoding="utf-8")
        (b / "开始.md").write_text("### N01 开始 [start]\n→ N02\n", encoding="utf-8")
        (a / "结束.md").write_text("### N03 结束 [end]\n", encoding="utf-8")
        text, _ = tf.assemble_src(d)
        self.assertLess(text.index("### N01 开始"), text.index("### N02 询问"))
        self.assertLess(text.index("### N02 询问"), text.index("### N03 结束"))

    def test_normalize_renames_legacy_n_prefix(self):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tccc-ren-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        nodes = d / "50-节点"
        nodes.mkdir(parents=True)
        (nodes / "N02-采集运单号.md").write_text("### N02 采集运单号 [chat]\n→ N03\n", encoding="utf-8")
        (nodes / "N01-开始通话.md").write_text("### N01 开始通话 [start]\n→ N02\n", encoding="utf-8")
        moved = tf.normalize_node_filenames(d)
        names = sorted(p.name for p in nodes.iterdir())
        self.assertEqual(names, ["开始通话.md", "采集运单号.md"])
        self.assertEqual(len(moved), 2)
        self.assertTrue((nodes / "开始通话.md").exists())
        self.assertFalse(list(nodes.glob("N0*")))

    def test_normalize_is_noop_when_already_named(self):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp(prefix="tccc-noop-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        nodes = d / "50-节点"
        nodes.mkdir(parents=True)
        (nodes / "开始.md").write_text("### N01 开始 [start]\n", encoding="utf-8")
        self.assertEqual(tf.normalize_node_filenames(d), [])
        self.assertTrue((nodes / "开始.md").exists())


class ConditionOpAliases(unittest.TestCase):
    def test_fmt_maps_english_ops_to_zh(self):
        self.assertEqual(tf._fmt_operator("contains"), "包含")
        self.assertEqual(tf._fmt_operator("exists"), "存在")
        self.assertEqual(tf._fmt_operator("gt"), "大于")
        self.assertEqual(tf._fmt_operator("=="), "==")
        self.assertEqual(
            tf._fmt_condition({"var": "${设备型号}", "operator": "contains", "val": "亲智"}),
            "${设备型号} 包含 亲智",
        )
        self.assertEqual(
            tf._fmt_condition({"var": "${设备型号}", "operator": "exists"}),
            "${设备型号} 存在",
        )

    def test_decompile_writes_zh_ops(self):
        md = tf.decompile(_CONDITION_FLOW, "测")
        chunk = md.split("### N02", 1)[-1].split("### N03", 1)[0]
        self.assertIn("- 如果 ${设备型号} 包含 亲智 或 ${设备型号} 包含 金智", chunk)
        self.assertIn("- 如果 ${设备型号} 存在", chunk)
        self.assertIn("- 如果 ${金额} 大于 100", chunk)
        self.assertNotIn(" contains ", chunk)
        self.assertNotIn(" exists ", chunk)
        self.assertNotIn(" gt ", chunk)

    def test_zh_ops_roundtrip_to_canvas_english(self):
        md = tf.decompile(_CONDITION_FLOW, "测")
        design = tf.parse_design(md)
        rebuilt = tf.build_json(design, _CONDITION_FLOW, rewrap=False)
        branches = rebuilt["ivrData"]["logicSplitNode"][0]["nodeData"]["logicSplitBranches"]
        ops = [(c.get("operator"), c.get("val")) for b in branches for c in b.get("conditions") or []]
        self.assertIn(("contains", "亲智"), ops)
        self.assertIn(("contains", "金智"), ops)
        self.assertIn(("exists", None), ops)
        self.assertIn(("gt", "100"), ops)
        self.assertEqual(
            [b["id"] for b in branches],
            ["b1", "b2", "b3", "else"],
        )


_CONDITION_FLOW = {
    "version": "1.0",
    "ivrData": {
        "startNode": [{
            "id": "n-start", "x": 0, "y": 0, "type": "startNode", "name": "开始",
            "nodeData": {},
            "outEdges": [{
                "id": "e0", "source": "n-start", "target": "n-cond",
                "type": "", "name": "",
                "startPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                "edgeData": {},
            }],
        }],
        "logicSplitNode": [{
            "id": "n-cond", "x": 100, "y": 0, "type": "logicSplitNode", "name": "判断型号",
            "nodeData": {
                "logicSplitBranches": [
                    {
                        "id": "b1", "method": "rule", "logicType": "or",
                        "conditions": [
                            {"var": "${设备型号}", "operator": "contains", "val": "亲智"},
                            {"var": "${设备型号}", "operator": "contains", "val": "金智"},
                        ],
                    },
                    {
                        "id": "b2", "method": "rule", "logicType": "and",
                        "conditions": [{"var": "${设备型号}", "operator": "exists"}],
                    },
                    {
                        "id": "b3", "method": "rule", "logicType": "and",
                        "conditions": [{"var": "${金额}", "operator": "gt", "val": "100"}],
                    },
                    {"id": "else", "method": "rule", "logicType": "else"},
                ],
            },
            "outEdges": [
                {
                    "id": "e1", "source": "b1", "target": "n-end",
                    "type": "", "name": "",
                    "startPoint": {"x": 0, "y": 0, "anchorIndex": 1},
                    "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                    "edgeData": {"branchId": "b1"},
                },
                {
                    "id": "e2", "source": "b2", "target": "n-end",
                    "type": "", "name": "",
                    "startPoint": {"x": 0, "y": 0, "anchorIndex": 2},
                    "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                    "edgeData": {"branchId": "b2"},
                },
                {
                    "id": "e3", "source": "b3", "target": "n-end",
                    "type": "", "name": "",
                    "startPoint": {"x": 0, "y": 0, "anchorIndex": 3},
                    "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                    "edgeData": {"branchId": "b3"},
                },
                {
                    "id": "e4", "source": "else", "target": "n-end",
                    "type": "", "name": "",
                    "startPoint": {"x": 0, "y": 0, "anchorIndex": 4},
                    "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
                    "edgeData": {"branchId": "else"},
                },
            ],
        }],
        "hangup": [{
            "id": "n-end", "x": 200, "y": 0, "type": "hangup", "name": "结束",
            "nodeData": {}, "outEdges": [],
        }],
    },
    "voiceSettings": {"voiceType": "ZhiXi", "maxDuration": 60, "notifyDuration": 10},
}

_COND_HEAD = (
    "# 流程：测\n\n## 环境配置\n\n- 技能组ID: \n- 转接智能体ID: \n"
    "- 环境注入变量: 设备型号\n\n## 节点\n\n"
    "### N01 开始 [start]\n→ N02 判断型号\n\n"
)


class ConditionBranchIds(unittest.TestCase):
    def test_insert_mid_does_not_steal_else_id(self):
        """N42 事故：中间插入 UA 条件后，else 不得被按位置赋给普通分支。"""
        old_md = _COND_HEAD + (
            "### N02 判断型号 [condition]\n分支：\n"
            "- 如果 ${设备型号} 包含 真睿 → N03 结束\n"
            "- 如果 ${设备型号} 存在 → N03 结束\n"
            "- 否则 → N03 结束\n\n"
            "### N03 结束 [end]\n"
        )
        new_md = _COND_HEAD + (
            "### N02 判断型号 [condition]\n分支：\n"
            "- 如果 ${设备型号} 包含 真睿 → N03 真睿\n"
            "- 如果 ${设备型号} 包含 UG → N04 双测\n"
            "- 如果 ${设备型号} 包含 UA 或 ${设备型号} 包含 UE101 → N05 标记\n"
            "- 如果 ${设备型号} 存在 → N06 其他\n"
            "- 否则 → N07 确认\n\n"
            "### N03 真睿 [end]\n\n### N04 双测 [end]\n\n"
            "### N05 标记 [end]\n\n### N06 其他 [end]\n\n### N07 确认 [end]\n"
        )
        old_flow = tf.build_json(tf.parse_design(old_md), rewrap=False)
        new_flow = tf.build_json(tf.parse_design(new_md), old_flow, rewrap=False)
        node = new_flow["ivrData"]["logicSplitNode"][0]
        branches = node["nodeData"]["logicSplitBranches"]
        ids = [b["id"] for b in branches]
        old_ids = [
            b["id"] for b in
            old_flow["ivrData"]["logicSplitNode"][0]["nodeData"]["logicSplitBranches"]
        ]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(ids[0], old_ids[0])          # 真睿 保旧 id
        self.assertEqual(ids[3], old_ids[1])          # 存在 保旧 id（不是被 UG 挤走）
        self.assertEqual(ids[-1], "else")
        self.assertNotIn("else", ids[:-1])
        ua = next(b for b in branches if any(
            c.get("val") == "UA" for c in (b.get("conditions") or [])))
        self.assertNotEqual(ua["id"], "else")
        tgt = {e["edgeData"]["branchId"]: e["target"] for e in node["outEdges"]}
        name_of = {n["id"]: n["name"] for arr in new_flow["ivrData"].values() for n in arr}
        self.assertEqual(name_of[tgt[ua["id"]]], "标记")
        self.assertEqual(name_of[tgt["else"]], "确认")

    def test_corrupted_else_id_on_rule_is_not_reused(self):
        md = _COND_HEAD + (
            "### N02 判断型号 [condition]\n分支：\n"
            "- 如果 ${设备型号} 包含 UA → N03 标记\n"
            "- 否则 → N04 确认\n\n"
            "### N03 标记 [end]\n\n### N04 确认 [end]\n"
        )
        base = tf.build_json(tf.parse_design(md), rewrap=False)
        node = base["ivrData"]["logicSplitNode"][0]
        node["nodeData"]["logicSplitBranches"][0]["id"] = "else"
        node["outEdges"][0]["source"] = "else"
        node["outEdges"][0]["edgeData"]["branchId"] = "else"
        rebuilt = tf.build_json(tf.parse_design(md), base, rewrap=False)
        branches = rebuilt["ivrData"]["logicSplitNode"][0]["nodeData"]["logicSplitBranches"]
        ids = [b["id"] for b in branches]
        self.assertEqual(ids[-1], "else")
        self.assertNotEqual(ids[0], "else")
        self.assertEqual(len(ids), len(set(ids)))

    def test_validate_rejects_duplicate_else_id(self):
        flow = json.loads(json.dumps(_CONDITION_FLOW))
        flow["ivrData"]["logicSplitNode"][0]["nodeData"]["logicSplitBranches"][2]["id"] = "else"
        issues = tf.validate(flow)
        self.assertTrue(any(i.code == "E18" and i.level == "error" for i in issues))


class ConditionCountLimit(unittest.TestCase):
    def _branch_with(self, n: int) -> dict:
        flow = json.loads(json.dumps(_CONDITION_FLOW))
        flow["ivrData"]["logicSplitNode"][0]["nodeData"]["logicSplitBranches"][0]["conditions"] = [
            {"var": "${设备型号}", "operator": "==", "val": f"M{i}"} for i in range(n)
        ]
        return flow

    def _md_with(self, n: int) -> str:
        expr = " 或 ".join(f"${{设备型号}} == M{i}" for i in range(n))
        return _COND_HEAD + (
            "### N02 判断型号 [condition]\n分支：\n"
            f"- 如果 {expr} → N03 结束\n"
            "- 否则 → N03 结束\n\n"
            "### N03 结束 [end]\n"
        )

    def test_validate_rejects_31_conditions_on_one_branch(self):
        issues = tf.validate(self._branch_with(31))
        hit = [i for i in issues if i.code == "E19" and i.level == "error"]
        self.assertTrue(hit, issues)
        self.assertIn("31", hit[0].msg)
        self.assertIn("30", hit[0].msg)

    def test_validate_allows_30_conditions_on_one_branch(self):
        issues = tf.validate(self._branch_with(30))
        self.assertFalse(any(i.code == "E19" for i in issues), issues)

    def test_parse_rejects_31_or_conditions(self):
        with self.assertRaises(tf.DesignError) as cm:
            tf.parse_design(self._md_with(31))
        msg = str(cm.exception)
        self.assertIn("31", msg)
        self.assertIn("30", msg)

    def test_parse_allows_30_or_conditions(self):
        design = tf.parse_design(self._md_with(30))
        cond = next(n for n in design.nodes if n.kind == "condition")
        self.assertEqual(len(cond.branches[0].extra["conditions"]), 30)
        flow = tf.build_json(design, rewrap=False)
        self.assertFalse(any(i.code == "E19" for i in tf.validate(flow, design)))


class WrappedBranchArrow(unittest.TestCase):
    """线上意图 content 常带末尾 \\n，逆编译会把 → 挤到下一行。"""

    def test_decompile_strips_trailing_newline_so_arrow_stays_on_same_line(self):
        flow = _live_like_flow(branch_nl=True)
        md = tf.decompile(flow, "测")
        self.assertIn("已办理。例如：办了。 → N03 标记完成", md)
        self.assertNotIn("\\n", md.split("分支：", 1)[-1])

    def test_parse_attaches_wrapped_arrow_to_previous_branch(self):
        md = (
            "# 流程：测\n\n## 节点\n\n"
            "### N01 开始通话 [start]\n→ N02 询问\n\n"
            "### N02 询问 [chat]\n```prompt\n问\n```\n分支：\n"
            "- 已办理。例如：办了。\n"
            " → N03 标记完成\n"
            "- 未办理。例如：还没办。 → N04 结束通话\n\n"
            "### N03 标记完成 [assign]\n- 变量: 完成 = 固定值: 是\n→ N04 结束通话\n\n"
            "### N04 结束通话 [end]\n"
        )
        design = tf.parse_design(md)
        chat = next(n for n in design.nodes if n.kind == "chat")
        self.assertEqual(chat.branches[0].target, "N03")
        self.assertEqual(chat.branches[0].content, "已办理。例如：办了。")
        rebuilt = tf.build_json(design, rewrap=False)
        assign_id = next(n["id"] for arr in rebuilt["ivrData"].values() for n in arr if n["name"] == "标记完成")
        incoming = [
            e["target"]
            for arr in rebuilt["ivrData"].values()
            for n in arr
            for e in (n.get("outEdges") or [])
        ]
        self.assertIn(assign_id, incoming)
        errs = [i for i in tf.validate(rebuilt, design) if i.level == "error"]
        self.assertEqual(errs, [])

    def test_trailing_newline_roundtrip_keeps_edge(self):
        flow = _live_like_flow(branch_nl=True)
        md = tf.decompile(flow, "测")
        rebuilt = tf.build_json(tf.parse_design(md), flow, rewrap=False)
        chat = rebuilt["ivrData"]["chatNode"][0]
        self.assertEqual(chat["nodeData"]["branches"][0]["content"], "已办理。例如：办了。")
        self.assertEqual(chat["outEdges"][0]["target"], "n-assign")
        errs = [i for i in tf.validate(rebuilt) if i.level == "error"]
        self.assertEqual(errs, [])

    def test_parse_drops_literal_backslash_n_before_arrow(self):
        md = (
            "# 流程：测\n\n## 节点\n\n"
            "### N01 开始通话 [start]\n→ N02 询问\n\n"
            "### N02 询问 [chat]\n```prompt\n问\n```\n分支：\n"
            "- 无其他问题。例如：行，记住了。好的。没有了。\\n → N03 结束通话\n\n"
            "### N03 结束通话 [end]\n"
        )
        chat = next(n for n in tf.parse_design(md).nodes if n.kind == "chat")
        self.assertEqual(chat.branches[0].content, "无其他问题。例如：行，记住了。好的。没有了。")
        self.assertEqual(chat.branches[0].target, "N03")


class GlobalAnnounceWithoutTrigger(unittest.TestCase):
    def test_validate_allows_global_announce_without_intent(self):
        flow = _live_like_flow(global_announce=True)
        issues = tf.validate(flow)
        self.assertFalse(any(i.code == "E7" for i in issues))
        self.assertFalse(any(i.level == "error" for i in issues))

    def test_validate_still_requires_trigger_on_listening_global_chat(self):
        flow = _live_like_flow(global_chat=True)
        issues = tf.validate(flow)
        self.assertTrue(any(i.code == "E7" and i.level == "error" for i in issues))


class TextLengthLimits(unittest.TestCase):
    """画布输入框字数上限：超限报 E12 阻断，到上限 90% 报 W9 预警。"""

    def _flow(self) -> dict:
        return json.loads(json.dumps(_live_like_flow()))

    def _msgs(self, flow: dict, code: str) -> list[str]:
        return [i.msg for i in tf.validate(flow) if i.code == code]

    def _hit(self, flow: dict, code: str, what: str) -> None:
        msgs = self._msgs(flow, code)
        self.assertTrue(any(what in m for m in msgs), msgs)

    def test_chat_prompt_over_limit_is_error(self):
        flow = self._flow()
        flow["ivrData"]["chatNode"][0]["nodeData"]["content"] = "话" * (tf.CHAT_PROMPT_MAX + 1)
        self._hit(flow, "E12", "节点话术")

    def test_chat_prompt_at_limit_is_not_error(self):
        flow = self._flow()
        flow["ivrData"]["chatNode"][0]["nodeData"]["content"] = "话" * tf.CHAT_PROMPT_MAX
        self.assertFalse(self._msgs(flow, "E12"))

    def test_well_under_limit_is_silent(self):
        flow = self._flow()
        flow["ivrData"]["chatNode"][0]["nodeData"]["content"] = "话" * 1200
        self.assertFalse(self._msgs(flow, "E12"))
        self.assertFalse(self._msgs(flow, "W9"))

    def test_reply_class_over_limit_is_error(self):
        flow = self._flow()
        br = flow["ivrData"]["chatNode"][0]["nodeData"]["branches"][0]
        br["content"] = "客户说" + "啊" * tf.REPLY_CLASS_MAX
        self._hit(flow, "E12", "回复分类")

    def test_entity_description_over_limit_is_error(self):
        flow = self._flow()
        flow["ivrData"]["chatNode"][0]["nodeData"]["entities"] = [{
            "slotType": "custom", "varName": "运单号", "name": "运单号",
            "description": "说" * (tf.VAR_DESC_MAX + 1),
        }]
        self._hit(flow, "E12", "词槽「运单号」的说明")

    def test_near_limit_is_warning_not_error(self):
        flow = self._flow()
        flow["ivrData"]["chatNode"][0]["nodeData"]["entities"] = [{
            "slotType": "custom", "varName": "运单号", "name": "运单号",
            "description": "说" * (int(tf.VAR_DESC_MAX * tf.NEAR_LIMIT_RATIO) + 1),
        }]
        self.assertFalse(self._msgs(flow, "E12"))
        self._hit(flow, "W9", "词槽「运单号」的说明")

    def test_assign_var_fields_over_limit_are_errors(self):
        flow = self._flow()
        var = flow["ivrData"]["extractVariableNode"][0]["nodeData"]["vars"][0]
        var["name"] = "名" * (tf.VAR_NAME_MAX + 1)
        var["value"] = "值" * (tf.VAR_DESC_MAX + 1)
        var["description"] = "描" * (tf.VAR_DESC_MAX + 1)
        msgs = self._msgs(flow, "E12")
        self.assertEqual(3, len(msgs), msgs)

    def test_tts_text_over_limit_is_error_but_audio_base64_is_ignored(self):
        flow = self._flow()
        flow["ivrData"]["transfer"] = [{
            "id": "n-tr", "x": 400, "y": 0, "type": "transfer", "name": "转技能组",
            "nodeData": {
                "mode": "manual", "name": "65067",
                "transfer-error": "tts:" + "错" * (tf.TTS_TEXT_MAX + 1),
                "transferring-music": "Cg" * 5000,     # 音频 base64，不该量长度
            },
            "outEdges": [],
        }]
        self._hit(flow, "E12", "转接失败话术")
        self.assertEqual(1, len(self._msgs(flow, "E12")))

    def test_dtmf_prompt_over_limit_is_error(self):
        flow = self._flow()
        flow["ivrData"]["DTMFNode"] = [{
            "id": "n-dtmf", "x": 400, "y": 0, "type": "DTMFNode", "name": "按键导航",
            "nodeData": {"tcccNodeData": {"dtmfConfig": {
                "play_sound": "tts:" + "按" * (tf.TTS_TEXT_MAX + 1)}}},
            "outEdges": [],
        }]
        self._hit(flow, "E12", "按键话术")

    def test_voice_settings_limits(self):
        flow = self._flow()
        flow["voiceSettings"]["systemPrompt"] = "提" * (tf.SYSTEM_PROMPT_MAX + 1)
        flow["voiceSettings"]["notifyMessage"] = "静" * (tf.NOTIFY_TEXT_MAX + 1)
        flow["voiceSettings"]["voicemailMessage"] = "留" * (tf.TTS_TEXT_MAX + 1)
        msgs = self._msgs(flow, "E12")
        self.assertEqual(3, len(msgs), msgs)
        for what in ("全局提示词", "静默提示", "语音留言话术"):
            self.assertTrue(any(what in m for m in msgs), msgs)

    def test_system_prompt_issue_points_back_to_src_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "9001-字数测试"
            src = proj / "src"
            (src / "50-节点").mkdir(parents=True)
            (src / "00-流程信息.md").write_text("# 流程：字数测试\n", encoding="utf-8")
            (src / "30-全局提示词.md").write_text(
                "## 全局提示词\n\n```prompt\n" + "提" * (tf.SYSTEM_PROMPT_MAX + 1) + "\n```\n",
                encoding="utf-8")
            (src / "50-节点" / "N01-开始通话.md").write_text(
                "### N01 开始通话 [start]\n→ N02 结束通话\n", encoding="utf-8")
            (src / "50-节点" / "N02-结束通话.md").write_text(
                "### N02 结束通话 [end]\n", encoding="utf-8")
            text, origin = tf.assemble_src(src)
            design = tf.parse_design(text)
            issues = tf.validate(tf.build_json(design, rewrap=False), design)
            hit = [i for i in issues if i.code == "E12"]
            self.assertTrue(hit, issues)
            f = tf.src_file_of_line(origin, design.system_prompt_line)
            self.assertEqual("30-全局提示词.md", Path(str(f)).name)


def _edge(eid, src, tgt, anchor=1):
    return {
        "id": eid, "source": src, "target": tgt, "type": "", "name": "",
        "startPoint": {"x": 0, "y": 0, "anchorIndex": anchor},
        "endPoint": {"x": 0, "y": 0, "anchorIndex": 0},
        "edgeData": {},
    }


def _live_like_flow(*, branch_nl=False, global_announce=False, global_chat=False):
    content = "已办理。例如：办了。\n" if branch_nl else "已办理。例如：办了。"
    chat_nodes = [{
        "id": "n-chat", "x": 100, "y": 0, "type": "chatNode", "name": "询问",
        "nodeData": {
            "welcomeText": "问一下", "content": "问一下", "contentType": "gen",
            "selectBranch": True, "allowInterrupt": False,
            "branches": [{
                "id": "rel-ok", "type": "intent", "content": content, "name": "", "tags": [],
            }],
            "entities": [], "verifyEntity": False, "queryVarName": "", "globalNode": False,
        },
        "outEdges": [_edge("e-ok", "rel-ok", "n-assign")],
    }]
    if global_announce:
        chat_nodes.append({
            "id": "n-age", "x": 0, "y": 200, "type": "chatNode",
            "name": "年龄不是十五到四十九周岁的女性",
            "nodeData": {
                "welcomeText": "抱歉打扰了", "content": "抱歉打扰了",
                "contentType": "fix", "selectBranch": False, "allowInterrupt": False,
                "branches": [], "entities": [], "verifyEntity": False,
                "queryVarName": "", "globalNode": True,
            },
            "outEdges": [_edge("e-age", "n-age", "n-end")],
        })
    if global_chat:
        chat_nodes.append({
            "id": "n-gchat", "x": 0, "y": 200, "type": "chatNode", "name": "全局追问",
            "nodeData": {
                "welcomeText": "你说什么", "content": "你说什么",
                "contentType": "gen", "selectBranch": True, "allowInterrupt": False,
                "branches": [{
                    "id": "rel-g", "type": "intent", "content": "继续", "name": "", "tags": [],
                }],
                "entities": [], "verifyEntity": False, "queryVarName": "", "globalNode": True,
            },
            "outEdges": [_edge("e-g", "rel-g", "n-end")],
        })
    return {
        "version": "1.0",
        "ivrData": {
            "startNode": [{
                "id": "n-start", "x": 0, "y": 0, "type": "startNode", "name": "开始通话",
                "nodeData": {},
                "outEdges": [_edge("e1", "n-start", "n-chat", 1)],
            }],
            "chatNode": chat_nodes,
            "extractVariableNode": [{
                "id": "n-assign", "x": 200, "y": 0, "type": "extractVariableNode",
                "name": "标记完成",
                "nodeData": {
                    "vars": [{"id": "v1", "name": "完成", "mode": "fix", "value": "是"}],
                    "globalNode": False, "branches": [],
                },
                "outEdges": [_edge("e2", "n-assign", "n-end")],
            }],
            "hangup": [{
                "id": "n-end", "x": 300, "y": 0, "type": "hangup", "name": "结束通话",
                "nodeData": {}, "outEdges": [],
            }],
        },
        "voiceSettings": {"voiceType": "ZhiXi", "maxDuration": 60, "notifyDuration": 10},
    }


if __name__ == "__main__":
    unittest.main()
