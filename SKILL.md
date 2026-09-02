---
name: skill-tccc-flow
description: 根据业务需求生成腾讯云呼叫中心 TCCC「AI 画布 / 语音智能体」可导入的流程 JSON（ivrData + voiceSettings），也支持把已导出的画布 JSON 逆编译成 Markdown 设计稿再改回去。当用户要做外呼/呼入话术流程、语音 IVR 流程、AI 画布节点编排、对话节点分支与意图设计、systemPrompt 人设编写，或提到 TCCC 画布、agent-flow、ivrData、chatNode、voiceSettings、导入画布 JSON 时使用。
description_zh: TCCC AI 画布流程生成
description_en: Generate TCCC AI-canvas flow JSON
disable: false
agent_created: true
---

# skill-tccc-flow

把业务需求编译成 TCCC AI 画布可直接导入的 JSON。**唯一真源是 Markdown 设计稿**，
JSON 由 Python 脚本生成，绝不手写 JSON。

## When to use

- 用户描述一个电话场景（呼入/外呼），要生成画布流程或话术。
- 用户给了一份已导出的画布 JSON，要改造、扩流程、批量调话术。
- 用户要评审流程设计（节点怎么拆、分支怎么设、变量怎么传）。

## 核心链路

```
业务需求 ──▶ ①写 md 设计稿 ──▶ ② build.py 编译 ──▶ 画布 JSON + 校验报告
                  ▲                                        │
                  └────── ③ decompile（改存量画布时） ◀──────┘
```

一把到底：不中途停下等确认。信息缺失时自行做**合理假设**，并在设计稿的
`## 假设与待确认` 表里逐条列出，让用户回头核对。

## 执行步骤

### Step 0：判断入口

| 用户给的东西 | 走法 |
|---|---|
| 只有业务需求 | 直接写新设计稿（Step 1） |
| 已有画布 JSON，要改 | 先 `decompile` 成 md，改 md，再 `build --base 原JSON`（Step 3） |
| 有 JSON 想体检 | 只跑 `validate` |

存量画布只要传了 `--base`，md 里没表达的字段会从原 JSON 逐字继承，
节点 id / 分支 id / 词槽 slotId 全部保留，改动范围可控。

### Step 1：写 md 设计稿

先读 `references/design-doc-spec.md`（语法权威）和 `references/prompt-guide.md`（话术与分支写法）。
照抄 `assets/example-催件查询.md` 的骨架最省事。

设计原则（按优先级）：

1. **流程尽量少节点**。能靠一个对话节点的多个分支解决的，不要拆成多个节点。
2. **一个对话节点只做一件事**：问一个问题 / 播一段信息 / 收一个变量（画布限制：一个节点只能收一个变量）。
3. **纯播报用 `[announce]`**，不要用带分支的 `[chat]` 硬凑。
4. **默认终点是 `[end]`**；`环境配置` 里给了技能组 id / 智能体 id 时才升级成转接节点。
5. **默认不加 else / silent 兜底分支**。未命中任何分支 = 自动重复本节点，这是画布机制，
   无应答由 `静默提示` + `静默提示次数` 兜底。只有需求明确要"沉默 N 秒后走某节点"才加。
6. **全局节点默认不建**。只有需求出现"任何时候都可以…"才建，最多 3 个——
   全局分支会参与**每一个**节点的意图识别，多了就是全局污染。
7. 每条主路径必须有明确出口，别让客户困在某个节点里转圈。

### Step 2：编译

```bash
PY=/Users/lizhenwen/.workbuddy/binaries/python/versions/3.13.12/bin/python3
$PY <skill>/scripts/tccc_flow.py build 设计稿.md -o 流程.json --report 校验报告.md
```

有 error 时**不会写出 JSON**，按报告里的编码定位修 md 再编译。
warning 不阻断，但要在最终回复里向用户点明（尤其 W1 环境 id 待补）。

### Step 3：改存量画布

```bash
$PY <skill>/scripts/tccc_flow.py decompile 导出的画布.json -o 设计稿.md
#   改 md …
$PY <skill>/scripts/tccc_flow.py build 设计稿.md -o 新流程.json --base 导出的画布.json
```

### Step 4：交付

一次落盘三份：`xxx-设计稿.md`、`xxx.json`、`xxx-校验报告.md`，然后 present_files。
回复里要说清：节点数/连线数、做了哪些假设、哪些环境 id 需要用户补。

## 红线（违反必翻车，逐条来自画布源码）

1. `contentType` 只有 **`gen`**（智能生成）和 **`fix`**（固定话术）。写 `fixed` 会失效。
2. 对话节点要听用户回复，**必须 `selectBranch: true`**。缺省是 `false`，会被判成单出口节点，
   多条出边一保存就报「该节点只能有一条出边」。设计稿里写了分支就会自动置 true。
3. **绝不伪造 `slotId`**。自定义词槽只给临时 id，保存时画布会自动建槽回填真实 slotId；
   伪造的 slotId 前端不报错，运行时指向不存在的词槽，属于隐性事故。
4. `systemPrompt` 上限 **8192 字**，超了静默截断。
5. 技能组 id、智能体 id 留空导入不报错，但**保存必被拦**。留空就要写进待补清单。
6. 终点类节点（`end` / 各种 `transfer`）**不能有出边**。
7. 每个节点都必须能从开始节点或某个全局节点走到，否则报「节点没有连接入主路」。
8. `maxDuration` / `notifyDuration` 用**秒**（导出口径），导入时画布自己 ×1000，不要预先换算。
9. `aiBotId` 导入时被强制覆盖成当前机器人 id，不用管。
10. 画布允许成环（`illicitRingType` 为空），可以放心让分支回到上游节点。

## 参考资料

| 文件 | 什么时候读 |
|---|---|
| `references/design-doc-spec.md` | 写/改设计稿前必读：完整语法、13 类节点属性表 |
| `references/prompt-guide.md` | 写 systemPrompt 和节点话术、设计分支时必读 |
| `references/node-schema.md` | 需要确认外部 JSON 字段含义、默认值、枚举时查 |
| `references/canvas-rules.md` | 排查导入失败/保存被拦时查（含源码出处） |
| `assets/example-催件查询.md` | 直接照抄的完整示例 |
| `README.md` | 给人看的说明文档；用户问"这个工具怎么用"时把它指过去 |

## 已验证

对两份真实导出画布做 decompile → build --base 往返：124 节点 / 339 连线与 14 节点 / 17 连线，
节点 id、分支 id、全部 outEdges、全部 nodeData 字段 100% 一致，0 error。
