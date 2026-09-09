# skill-tccc-flow

把业务需求编译成腾讯云呼叫中心 TCCC「AI 画布 / 语音智能体」**可直接导入的流程 JSON**，也能把已导出的画布 JSON 逆编译成 Markdown 设计稿改完再编译回去。

- 唯一真源是 **Markdown 设计稿**，JSON 由脚本生成，人和 AI 都不手写 JSON
- 零三方依赖，只用 Python 标准库
- 内置画布保存期校验（21 条硬规则），先在本地拦住"导入成功但保存被拦"的坑
- 对存量画布做 `decompile → build --base` 往返，字段级 100% 保真

---

## 目录

- [为什么要有它](#为什么要有它)
- [安装与环境](#安装与环境)
- [5 分钟上手](#5-分钟上手)
- [三个命令](#三个命令)
- [设计稿长什么样](#设计稿长什么样)
- [支持的节点类型](#支持的节点类型)
- [校验编码全表](#校验编码全表)
- [两种典型工作流](#两种典型工作流)
- [十条红线](#十条红线)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [维护须知](#维护须知)

---

## 为什么要有它

画布上手拖节点，一个真实的呼入流程动辄 120+ 节点、300+ 连线，改一句话术要点开十几个弹窗。
本 skill 用一份 Markdown 设计稿把整个流程管起来，解决四个问题：

| 痛点 | 本 skill 的做法 |
|---|---|
| 手工在画布上改节点/连线繁琐，AI 直接生成 JSON 又容易字段错 | 改成写 **Markdown**，人能读、能评审、能直接 diff |
| 只能从零生成，改不了存量画布 | 支持 `decompile` 反向，且保证可回编译 |
| 生成的 JSON 导入成功但保存时被画布校验拦住 | 内置**保存期**校验，error 阻断并给出定位 |
| 依赖复杂运行时（node/tsx 等）与仓库路径，换机器/移目录就挂 | 纯 Python 标准库，路径无耦合 |

---

## 安装与环境

skill 装在 `~/.workbuddy/skills/skill-tccc-flow/`，在 WorkBuddy 会话里会自动加载。

命令行直接用的话只需要一个 Python 3.9+：

```bash
PY=/Users/lizhenwen/.workbuddy/binaries/python/versions/3.13.12/bin/python3
SK=~/.workbuddy/skills/skill-tccc-flow

$PY $SK/scripts/tccc_flow.py --help
```

无需 `pip install`，无需 node。

---

## 5 分钟上手

编译自带示例，看看产物长什么样：

```bash
$PY $SK/scripts/tccc_flow.py build $SK/assets/example-催件查询.md \
    -o /tmp/催件.json --report /tmp/催件-校验报告.md
```

```
[OK] /tmp/催件.json  节点 13 / 连线 22 / error 0 / warn 3
[报告] /tmp/催件-校验报告.md
```

然后：画布 → 导入 IVR JSON → 选 `/tmp/催件.json` → 点「整理」→ 按报告补齐环境 id → 保存。

想改流程就改 `assets/example-催件查询.md`（复制一份到自己目录），重新 build 即可。

---

## 三个命令

```bash
# 设计稿 → 画布 JSON
tccc_flow.py build 设计稿.md -o 流程.json [--base 底座.json] [--report 报告.md] [--mermaid 图.mmd] [--force]

# 体检已有画布 JSON
tccc_flow.py validate 流程.json [--design 设计稿.md] [--report 报告.md]

# 画布 JSON → 设计稿（改存量画布用）
tccc_flow.py decompile 画布.json -o 设计稿.md [--title 名称]

# 修 Markdown 里「一句话没写完就换行」的排版折行
tccc_flow.py rewrap 设计稿.md|目录 [--dry-run] [-v]
```

| 参数 | 说明 |
|---|---|
| `--base` | 传一份**已从画布导出的真实 JSON** 当底座。md 里没表达的字段逐字继承，节点 id / 分支 id / 词槽 slotId 全部保留，改动范围可控 |
| `--report` | 校验报告落盘路径。不传则默认与输出 JSON 同名加 `-校验报告.md` |
| `--mermaid` | 额外导出一份 mermaid 流程图（`flowchart LR`） |
| `--force` | 有 error 也强行写出 JSON，**仅调试用** |
| `--rewrap` / `--no-rewrap` | 是否合并话术里的排版硬折行。**不带 `--base` 时默认开启**，带 `--base` 时默认关闭（改存量画布优先保真） |
| `--design` | `validate` 时补上设计稿，才能校验「环境注入变量」、话术三段结构和硬折行 |

退出码：`0` 通过；`1` 设计稿语法错误（带行号）；`2` 存在 error（JSON 未写出）。

---

## 设计稿长什么样

```markdown
# 流程：快递催件查询

## 环境配置
- 音色: tccc_custom_1008
- 单轮最长时长(秒): 25          ← 秒，导入时画布自己 ×1000
- 技能组ID:                     ← 留空会进「待补清单」，不阻断
- 环境注入变量: 主叫号码          ← 外部注入的变量必须在这声明

## 假设与待确认                  ← 自由内容，不参与编译，供人核对
| # | 假设 | 影响范围 | 待确认 |

## 全局提示词
```prompt
# 人设 / # 任务 / # 相关信息 / # 要求 / # 业务知识
                    ↑ 只列外部传入的变量，流程内部产生的变量不要写进来（否则报 W10）
```

## 变量表                        ← 自由内容，供人查阅

## 节点

### N01 开始通话 [start]
→ N02 采集运单号

### N02 采集运单号 [chat]
- 允许打断: 是
- 收集变量: 运单号
- 词槽说明: 提取 13 到 15 位连续数字，客户可能分几次说完
```prompt
# 目标
拿到客户完整的快递运单号。
# 参考示例
以下是参考表达，不必逐字照读：
- "麻烦您报一下完整的快递单号。"
# 应对策略
- 客户说不知道单号：说明只能凭运单号查件，建议找商家要。
```
分支：
- 收集成功 → N03 查询运单
- 客户表示不知道单号。示例：我不知道呀。找不到了。 → N10 转人工前告知
- 客户询问派送网点电话。示例：网点电话多少。       ← 不写箭头 = 命中后重复本节点

### N03 查询运单 [api]
- URL: https://example.com/api/query
- 参数: waybill = ${运单号} : string
- 返回: data.eta → 预计送达时间
分支：
- 成功 → N04 判断状态
- 失败 → N10 转人工前告知

### N04 判断状态 [condition]
分支：
- 如果 ${快件状态} == 3 → N06 已签收说明
- 如果 ${预计送达时间} exists → N05 告知进度
- 否则 → N10 转人工前告知
```

要点：

- 节点编号 `Nxx` 全稿唯一，连线只认编号；箭头 `→`（也接受 `->`）
- 话术写在 ```` ```prompt ```` 围栏内，**围栏里不做任何解析**
- 属性行 `- 键: 值`，同名属性可重复多行（如多条 `- 参数:`）
- 分支格式：`分支名称。示例：句1。句2。 → Nxx 目标节点名`，**不能换行**
- 完整语法见 `references/design-doc-spec.md`

---

## 支持的节点类型

| kind | 画布节点 | 出口形态 |
|---|---|---|
| `start` | 开始通话 | 单出口，恰好 1 条 |
| `chat` | 对话节点（听用户回复） | 多出口，每分支一个 |
| `announce` | 对话节点（纯播报不听） | 单出口 |
| `api` | 接口调用 | 成功 / 失败 |
| `assign` | 变量赋值 | 单出口 |
| `condition` | 条件判断 | 每分支一个，**必须全连** |
| `worktime` | 工作时间判断 | 每分支一个，**必须全连** |
| `dtmf-nav` | 按键导航 | 每按键一个 + 失败 |
| `dtmf-collect` | 收号 | 成功 / 失败 |
| `transfer-skill` | 转技能组 | 终点，禁止出边 |
| `transfer-outer` | 转外线 | 终点 |
| `transfer-3rd` | 转第三方内线 | 终点 |
| `transfer-agent` | 转接智能体 | 终点 |
| `end` | 结束通话 | 终点 |

任何节点加 `{全局}` 标记或写 `- 触发: xxx` 属性即成为全局节点。

未支持：`tagNode`（话后标签）、`extension`（分机号）——画布里默认隐藏，需要时手工加。

---

## 校验编码全表

**error 会阻断，不写出 JSON**；warning 只提示。

| 编码 | 检查项 |
|---|---|
| E1 | 必须有且仅有 1 个开始节点，且恰好 1 条出边 |
| E2 | 节点 / 连线 id 非空且唯一；画布至少 2 个节点 |
| E3 | 单出口节点只能 1 条出边且 source 为节点 id；终点类节点禁止出边 |
| E4 | 多出口节点的连线 source 必须是本节点某个分支 id；连线目标必须存在 |
| E5 | 除开始节点与全局节点外，每个节点必须有入边 |
| E6 | 所有节点必须从开始节点或某个全局节点可达 |
| E7 | 全局节点必须至少一条非空 `global_intent` 触发语 |
| E8 | `intent` / `global_intent` 分支 content 不能为空 |
| E9 | 条件判断 / 工时判断 / DTMF 的分支必须全部连线 |
| E10 | 一个对话节点只能收 1 个变量；配了词槽必须有 `entity_success` 分支且 `replyMode=intent_with_entity` |
| E11 | 引用的 `${变量}` 必须有来源（接口返回 / 赋值 / 词槽 / 收号 / 环境注入变量） |
| E12 | `systemPrompt` 不得超过 8192 字 |
| E13 | 单出口节点不能连自己 |
| E14 | `selectBranch=true` 的对话节点必须有分支 |
| E15 | `contentType` 只能是 `gen` 或 `fix` |
| W1 | 环境相关 id 待补（技能组 / 智能体 / 接口 URL / 音色） |
| W2 | 单节点分支数 > 12，意图识别精度会下降 |
| W3 | 全局节点超过 3 个，会污染所有节点的意图识别 |
| W4 | 节点重名（画布不报错，但人工排查会混乱） |
| W5 | 悬空分支（命中后重复本节点）——**多数是有意设计，确认即可** |
| W6 | `vadLevel` 不在 {0,1,2,3,100} 内 |
| W7 | 节点话术缺少「目标 / 参考示例 / 应对策略」段 |
| W8 | 实体带了 `slotId`（非真实导出的话会指向不存在的词槽） |
| W9 | `systemPrompt` 长度接近 8192 上限 |
| W10 | `systemPrompt` 里引用了流程内部变量（词槽/接口/赋值产生），应挪到对应节点话术 |
| W11 | 话术里「一句话没写完就换行」，排版换行会原样进 JSON 发给大模型（build 默认自动合并） |

报告末尾还会附一份「导入后人工验收清单」。

---

## 两种典型工作流

### A. 从零做一个新流程

```bash
# 1. 复制示例当骨架
cp $SK/assets/example-催件查询.md ./我的流程-设计稿.md
# 2. 改内容（节点、话术、分支、接口）
# 3. 编译
$PY $SK/scripts/tccc_flow.py build 我的流程-设计稿.md -o 我的流程.json --report 我的流程-校验报告.md
# 4. 有 error 按报告改 md，重复第 3 步
```

### B. 改造存量画布（推荐用于线上流程）

```bash
# 1. 从画布导出 JSON，逆编译成设计稿
$PY $SK/scripts/tccc_flow.py decompile 导出的画布.json -o 设计稿.md
# 2. 在 md 上改（加节点、调话术、补分支）
# 3. 带上原 JSON 当底座编译回去
$PY $SK/scripts/tccc_flow.py build 设计稿.md -o 新流程.json --base 导出的画布.json
# 4. diff 一下确认改动范围符合预期
```

第 3 步传 `--base` 很关键：md 只承载语义层（结构 / 话术 / 分支 / 连线），其余字段从原 JSON 逐字继承，节点 id、分支 id、词槽 slotId 都不会变，所以导回画布是"局部改动"而不是"整体重建"。

在 WorkBuddy 里更省事：直接说「把这个画布 JSON 的采集单号节点加两个分支」，agent 会自己走完这四步。

---

## 十条红线

来自画布源码，违反必翻车（详细出处见 `references/canvas-rules.md`）：

1. `contentType` 只有 `gen`（智能生成）和 `fix`（固定话术）。写 `fixed` 会失效。
2. 对话节点要听用户回复**必须 `selectBranch: true`**。缺省 false 会被判单出口，多出边保存必挂。
3. **绝不伪造 `slotId`**。不填才会触发保存时自动建槽；伪造的前端不报错、运行时指向不存在的词槽。
4. `systemPrompt` 上限 8192 字，超了静默截断。
5. 技能组 id / 智能体 id 留空导入不报错，但**保存必被拦**。
6. 终点类节点（`end` / 各种 `transfer`）**不能有出边**。
7. 每个节点都必须能从开始节点或某个全局节点走到。
8. `maxDuration` / `notifyDuration` 用**秒**，导入时画布自己 ×1000，不要预先换算。
9. `aiBotId` 导入时被强制覆盖成当前机器人 id，不用管。
10. 画布允许成环，可以放心让分支回到上游节点。

---

## 目录结构

```
skill-tccc-flow/
├── SKILL.md                      agent 入口：链路、设计原则、红线
├── README.md                     本文件（给人看）
├── scripts/
│   └── tccc_flow.py              build / validate / decompile
├── references/
│   ├── design-doc-spec.md        md 设计稿语法权威（写稿前必读）
│   ├── prompt-guide.md           systemPrompt 与节点话术、分支设计规范
│   ├── node-schema.md            外部 JSON 字段字典、默认值、枚举
│   └── canvas-rules.md           画布导入/保存硬校验 + 源码文件行号
└── assets/
    └── example-催件查询.md        13 节点全类型可编译示例
```

`scripts/tccc_flow.py` 的内部分区：常量与默认值 → 稳定 id → md 解析 →JSON 生成（含布局） → 校验 → 逆编译 → mermaid → CLI。

---

## 常见问题

**Q：为什么节点 id 每次编译都一样？**基于「节点编号 + 节点名」做 SHA1 派生，所以只要不改名，重编译 id 不变，diff 干净。
decompile 会把原 id 写成 `- ID:` 属性，`--base` 靠它精确匹配。

**Q：分支不连目标节点是不是漏了？**不一定。画布机制是"未命中任何分支 / 命中的分支没连节点 → 重复本节点"，所以「回答一下就继续问原问题」的场景故意留悬空是推荐做法，报 W5 确认即可。

**Q：为什么不默认给每个对话节点加 else 兜底？**`else` 是系统分支（导入后画布上不可编辑），而且会抢走本该命中具体意图的回复。
未命中已经会重复本节点，无应答由 `静默提示` + `静默提示次数` 兜底。线上真实画布也是这么做的。

**Q：坐标怎么算的？会不会重叠？**BFS 分层：主干水平推进（层距 440px），同层纵向堆叠。节点高度不是拍脑袋估的，而是
**1:1 移植了画布自己的几何估算器** `src/flow/utils/manualLayoutNodeSizeEstimate.ts`（「一键整理」用的就是这套常量）。核心规律：

```
对话节点高度 = 24 + 36 + 108 + 分支区 + (全局节点再 +46)
分支区(n)    = 24 + n×40 + (n-1)×12      →  每多一个分支恰好 +52px
```

所以 2 分支节点 284px、16 分支节点 1012px，纵向间距会跟着分支数自动放大。
`--base` 模式下存量节点保留原坐标，只给新节点排位。导入后点一次画布「整理」效果更好。

**Q：词槽要不要填 slotId？**新建流程**不要填**。自定义词槽会写一个临时 id，保存时画布调 `updateAISlot` 自动建槽并回填真实 slotId。
只有在复用真实环境已存在的词槽时（通常来自 decompile）才会带 `- 词槽ID:`。

**Q：话术里一句话被断成两行会怎样？**
围栏内容是逐字写进 JSON 的，排版换行会变成真实 `\n`，等于把断句发给大模型，弱化语义连贯（断在 `${变量}` 或引号附近更容易误解）。`build` 不带 `--base` 时会自动合并并打印「[整形] 合并了 N 处」；已有文件可以 `tccc_flow.py rewrap 目录` 批量修，加 `--dry-run` 先看要改哪些。判定很保守：只有「上一行没说完 + 下一行是续写」才合并，标题、表格、代码块、列表标记、`字段名：值` 清单行、对话示例（`客户：`）一律不动。

**Q：多行的词槽说明 / LLM 提取提示词怎么写？**用 `\n` 字面量。decompile 会自动转义，build 会自动还原。

**Q：报错说「引用了未定义的变量」但这个变量是 IVR 传进来的？**写进 `## 环境配置` 的 `- 环境注入变量: A, B, C`。decompile 会自动把未定义变量收集进去。

---

## 维护须知

画布前端改动后，以下几处要跟着核对（都在 `scripts/tccc_flow.py` 顶部常量区）：

| 常量 | 对应源码 |
|---|---|
| `DEFAULT_VOICE_SETTINGS` | 画布 `voiceSettings` 默认值 |
| `SYSTEM_PROMPT_MAX` | `persona-requirements.vue` 的 `MAX_PROMPT_LENGTH` |
| `VALID_VAD_LEVELS` | `validateXGraph.ts` 的 vadLevel 校验 |
| `BUILTIN_SLOT_TYPES` | `CollectionType` 枚举 |
| `SYSTEM_BRANCH_TYPES` | `VoiceReplyType` 枚举 |
| `DEFAULT_TRANSFER_MUSIC` / `DEFAULT_AI_TRANSFER_CONTEXT` | `node/TCCC.ts` 的转接节点默认值 |
| 节点高度常量（`LABEL_H` / `WELCOME_H` / `BRANCH_ITEM_H` / `GLOBAL_TIPS_H` …）与 `estimate_node_height()` | `flow/utils/manualLayoutNodeSizeEstimate.ts`（画布「一键整理」的几何估算器）；样式改了这里必须同步，否则自动布局会重叠 |
| 校验规则 | `flow/validate/validateFlowDataOnSave.ts` + `validateXGraph.ts` |

回归自测（改完脚本务必跑一遍）：

```bash
# 1. 从零编译示例
$PY $SK/scripts/tccc_flow.py build $SK/assets/example-催件查询.md -o /tmp/ex.json
# 2. 真实画布往返保真（换成自己手上的导出 JSON）
$PY $SK/scripts/tccc_flow.py decompile 真实画布.json -o /tmp/x.md
$PY $SK/scripts/tccc_flow.py build /tmp/x.md -o /tmp/x-rb.json --base 真实画布.json
# 3. 逐字段比对 /tmp/x-rb.json 与 真实画布.json 的 nodeData 与 outEdges
```

**已验证基线**：两份真实导出画布（124 节点 / 339 连线 与 14 节点 / 17 连线）往返后节点 id、分支 id、全部 `outEdges`、全部 `nodeData` 字段 **100% 一致，0 error**。
