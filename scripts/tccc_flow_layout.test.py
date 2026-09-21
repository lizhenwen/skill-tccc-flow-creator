#!/usr/bin/env python3
"""自动布局：主干按流程从左到右，分支展开，节点不能重叠。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tccc_flow as tf  # noqa: E402

EXAMPLE = ROOT.parent / "assets" / "example-催件查询.md"


def _md(*nodes: str) -> str:
    body = "\n\n".join(nodes)
    return (
        "# 流程：测\n\n## 环境配置\n\n- 技能组ID: \n- 转接智能体ID: \n"
        "- 环境注入变量: \n\n## 全局提示词\n\n```prompt\n\n```\n\n"
        "## 变量表\n\n| 变量名 | 来源 | 说明 |\n|---|---|---|\n\n"
        f"## 节点\n\n{body}\n"
    )


def _pos(design: tf.Design, **kwargs) -> dict[str, tuple[float, float]]:
    return tf.layout(design, **kwargs)


def _assert_no_overlap(test: unittest.TestCase, design: tf.Design,
                       pos: dict[str, tuple[float, float]]) -> None:
    nodes = [n for n in design.nodes if n.no in pos]
    for i, a in enumerate(nodes):
        ba = tf.visual_box(a, pos[a.no])
        for b in nodes[i + 1:]:
            bb = tf.visual_box(b, pos[b.no])
            test.assertFalse(
                tf.boxes_overlap(ba, bb),
                f"{a.no} {pos[a.no]} h={tf.estimate_node_height(a)} "
                f"与 {b.no} {pos[b.no]} h={tf.estimate_node_height(b)} 重叠",
            )


class MainChainLayout(unittest.TestCase):
    def test_linear_flow_goes_left_to_right_same_y(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N03 结束通话",
            "### N03 结束通话 [end]",
        ))
        pos = _pos(design)
        model = tf.build_layout_model(design)
        self.assertEqual(model.main_chain, ["N01", "N02", "N03"])
        xs = [pos[no][0] for no in model.main_chain]
        ys = [pos[no][1] for no in model.main_chain]
        self.assertEqual(xs, [tf.ORIGIN_X, tf.ORIGIN_X + tf.COLUMN_GAP,
                              tf.ORIGIN_X + 2 * tf.COLUMN_GAP])
        self.assertEqual(ys, [tf.ORIGIN_Y, tf.ORIGIN_Y, tf.ORIGIN_Y])
        _assert_no_overlap(self, design, pos)

    def test_first_branch_continues_main_chain(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 识别",
            "### N02 识别 [chat]\n```prompt\n请问\n```\n"
            "分支：\n- 意图A → N03 路径A\n- 意图B → N04 路径B",
            "### N03 路径A [announce]\n```prompt\nA\n```\n→ N05 结束通话",
            "### N04 路径B [announce]\n```prompt\nB\n```\n→ N05 结束通话",
            "### N05 结束通话 [end]",
        ))
        pos = _pos(design)
        model = tf.build_layout_model(design)
        self.assertEqual(model.main_chain, ["N01", "N02", "N03", "N05"])
        self.assertEqual(pos["N01"][1], pos["N02"][1])
        self.assertEqual(pos["N02"][1], pos["N03"][1])
        self.assertGreater(pos["N03"][0], pos["N02"][0])
        self.assertGreater(pos["N04"][0], pos["N02"][0])
        self.assertNotEqual(pos["N04"][1], pos["N03"][1])
        _assert_no_overlap(self, design, pos)

    def test_high_outdegree_stops_main_chain(self):
        branches = "\n".join(f"- 意图{i} → N{i + 2:02d} 路径{i}" for i in range(1, 5))
        tails = [
            f"### N{i + 2:02d} 路径{i} [announce]\n```prompt\n{i}\n```\n→ N07 结束通话"
            for i in range(1, 5)
        ]
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 菜单",
            "### N02 菜单 [chat]\n```prompt\n请选\n```\n"
            f"分支：\n{branches}",
            *tails,
            "### N07 结束通话 [end]",
        ))
        model = tf.build_layout_model(design)
        self.assertEqual(model.main_chain, ["N01", "N02"])
        self.assertTrue(any(g.parent_id == "N02" and len(g.root_ids) >= 4
                            for g in model.branch_groups))
        pos = _pos(design)
        _assert_no_overlap(self, design, pos)

    def test_cycle_does_not_loop(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 问",
            "### N02 问 [chat]\n```prompt\n问\n```\n"
            "分支：\n- 继续 → N02 问\n- 结束 → N03 结束通话",
            "### N03 结束通话 [end]",
        ))
        pos = _pos(design)
        self.assertEqual(set(pos), {"N01", "N02", "N03"})
        _assert_no_overlap(self, design, pos)


class CollisionAndGlobals(unittest.TestCase):
    def test_tall_branch_does_not_cover_same_column_main(self):
        many = "\n".join(
            f"- 分项{i}。示例：说法{i}。" for i in range(1, 17)
        )
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 主干",
            "### N02 主干 [announce]\n```prompt\n往下走\n```\n→ N04 结束通话",
            "### N03 高分支 [chat] {全局}\n"
            "- 触发: 客户要投诉。示例：我要投诉。\n"
            "```prompt\n安抚\n```\n"
            f"分支：\n{many}\n→ N04 结束通话",
            "### N04 结束通话 [end]",
        ))
        pos = _pos(design)
        _assert_no_overlap(self, design, pos)
        self.assertGreater(pos["N03"][1], pos["N01"][1])

    def test_global_node_sits_below_main_chain(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N03 结束通话",
            "### N03 结束通话 [end]",
            "### N04 全局投诉 [chat] {全局}\n"
            "- 触发: 客户要投诉。示例：我要投诉。\n"
            "```prompt\n转投诉\n```\n→ N03 结束通话",
        ))
        pos = _pos(design)
        self.assertGreater(pos["N04"][1], pos["N02"][1] + 100)
        _assert_no_overlap(self, design, pos)


class PreserveBaseCoordinates(unittest.TestCase):
    def test_base_keeps_existing_xy(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N03 结束通话",
            "### N03 结束通话 [end]",
        ))
        built = tf.build_json(design, rewrap=False)
        rebuilt = tf.build_json(tf.parse_design(
            tf.decompile(built, "测")
        ), built, rewrap=False)
        old = {n["id"]: (n["x"], n["y"])
               for arr in built["ivrData"].values() for n in arr}
        new = {n["id"]: (n["x"], n["y"])
               for arr in rebuilt["ivrData"].values() for n in arr}
        self.assertEqual(old, new)

    def test_new_node_does_not_overlap_preserved(self):
        original = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N03 结束通话",
            "### N03 结束通话 [end]",
        ))
        base = tf.build_json(original, rewrap=False)
        extended = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N04 补问",
            "### N04 补问 [announce]\n```prompt\n还需要帮忙吗\n```\n→ N03 结束通话",
            "### N03 结束通话 [end]",
        ))
        ids = tf.resolve_node_ids(extended, base)
        pos = tf.layout(extended, ids=ids, base=base)
        start_id = ids["N01"]
        start_node = next(
            n for arr in base["ivrData"].values() for n in arr if n["id"] == start_id
        )
        self.assertEqual(pos["N01"], (start_node["x"], start_node["y"]))
        _assert_no_overlap(self, extended, pos)

    def test_relayout_ignores_overlapping_base_coords(self):
        design = tf.parse_design(_md(
            "### N01 开始通话 [start]\n→ N02 开场",
            "### N02 开场 [announce]\n```prompt\n你好\n```\n→ N03 旁路",
            "### N03 旁路 [announce]\n```prompt\n另一路\n```\n→ N04 结束通话",
            "### N04 结束通话 [end]",
        ))
        stacked = tf.build_json(design, rewrap=False)
        for arr in stacked["ivrData"].values():
            for n in arr:
                n["x"], n["y"] = 800.0, 300.0
        ids = tf.resolve_node_ids(design, stacked)
        stuck = tf.layout(design, ids=ids, base=stacked)
        self.assertTrue(any(
            tf.boxes_overlap(tf.visual_box(a, stuck[a.no]), tf.visual_box(b, stuck[b.no]))
            for i, a in enumerate(design.nodes)
            for b in design.nodes[i + 1:]
        ))
        pos = tf.layout(design, ids=ids, base=stacked, relayout=True)
        _assert_no_overlap(self, design, pos)
        self.assertGreater(pos["N02"][0], pos["N01"][0])


class ExampleFlow(unittest.TestCase):
    def test_example_has_flow_order_and_no_overlap(self):
        design = tf.parse_design(EXAMPLE.read_text(encoding="utf-8"))
        pos = _pos(design)
        model = tf.build_layout_model(design)
        self.assertEqual(model.main_chain[0], "N01")
        self.assertEqual(len({pos[no][1] for no in model.main_chain}), 1)
        self.assertGreater(pos[model.main_chain[-1]][0], pos["N01"][0])
        _assert_no_overlap(self, design, pos)


if __name__ == "__main__":
    unittest.main()
