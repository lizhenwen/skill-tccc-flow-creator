# 画布导入与保存的硬规则（附源码出处）

排查「导入报错」「导入成功但保存被拦」「连线渲染错乱」时查这里。
路径均相对 `packages/apps/` （monorepo：`workstation-ccc-mui`）。

## 内外格式转换在哪

外部 JSON ↔ 画布内部格式的转换层**不在** `agent-flow` 包，而在 `manage` 包：

| 方向 | 函数 | 位置 |
|---|---|---|
| 导入（外部→画布） | `transIvrToX6Data()` → `transformToFlowData()` | `manage/src/views/RobotManagement/Flow/utils.ts:2421` / `:1734` |
| 导出（画布→外部） | `transX6DataToIvrData()` → `x6NToIvrN()` / `x6EToIvrE()` | 同文件 `:2379` / `:333` / `:412` |
| 字段白名单 | `NODE_KEY_MAP` | 同文件 `:153`（**导出时 pick，不在名单里的字段会被丢弃**） |

type 名映射（`transformToFlowData` 的 switch，`:1741-2369`）：

| 外部 type | 内部 type |
|---|---|
| startNode | voiceStartNode |
| chatNode | voiceConversationNode |
| apiCallNode | voiceInterfaceNode |
| logicSplitNode | voiceJudgementNode |
| workTimeNode | voiceWorkTimeJudgeNode |
| extractVariableNode | voiceVariableAssignNode |
| hangup | voiceEndNode |
| transferAgentNode | voiceTransferToAgentNode |
| DTMFNode | 按 `nodeData.nodeType` 分流为 navigation / collection / extension |
| transfer | 按 `nodeData.mode` 分流为 SkillGroup / Outer / ThirdPartyRoute |

## 导入期：几乎不校验

- 唯一校验是「转换函数不抛异常」：`manage/.../UpdateFlowByImportModal.tsx:84-88`失败提示「请检查 IVR 数据格式是否正确」；JSON 解析失败提示「JSON 格式不合法」。
- **没有 schema 校验**，缺字段一律走兜底默认值（`utils.ts:1784-1802`）。
- 导入时会被改写的字段：
  - `aiBotId` → 强制覆盖为当前机器人 id（`UpdateFlowByImportModal.tsx:76`）
  - `maxDuration` / `notifyDuration` → **×1000**（同文件 `:70-75`）

**结论：导入成功不代表能用。真正的门槛在保存。**

## 坐标与锚点

- 节点 `x`/`y` 原样采用；G6 实例不带 layout，`draw()` 直接 `read()`（`QaTree.vue:2157`, `:1109`）。
  自动布局只在用户点「整理」时执行（`:1072 organizeLayout`）。
- 边的 `startPoint.x/y`、`endPoint.x/y` **被忽略**，只读 `anchorIndex`（`ivrEToX6E` `utils.ts:438-439`）。
- 多出口节点的出锚点由 `transformMultiExportNodeFlowDataOutEdges`（`:538`）重算：`branchId = edgeData.branchId || 按 source 反查` → `sourceAnchor = branchIndex + 1`；
  查不到才退回 `startPoint.anchorIndex`。
- 锚点语义：`0` = 左侧入锚点，`1..n` = 第 n 个分支出锚点（`MultiExportNode.js:1905`, `:1949`；反查 `newStyleUtil.ts:79-83`）。
- `chatNode` 的画布分支 = `filterGlobalReplyClasses(branches)`，即**剔除 `global_intent` 后**的列表（`utils.ts:1820`）。锚点序号要按剔除后的下标算。
- `chatNode.selectBranch` → 内部 `conversationData.listenUserReply`（`utils.ts:1823`）。
  缺省 `false`，**会让节点被判为单出口**。

## 保存期硬校验（会直接拦住保存）

入口 `agent-flow/src/flow/validate/validateXGraph.ts:2011`：`validateNodeDataStep` → `validateRingList` → `validateNodeIsInTree` →（仅保存）`validateFlowDataOnSave`。

### validateFlowDataOnSave.ts

| # | 规则 | 报错文案 | 出处 |
|---|---|---|---|
| 1 | 禁止自连（单出口不能连自己；多出口至少一条非自连边） | 节点不能连接自己 | `:26-39`, `:60` |
| 2 | 单出口节点只能 1 条出边，且出边 `source` == 节点 id | 该节点只能有一条出边 / 出边的起始节点不正确 | `:82-146` |
| 3 | 节点 id / 边 id 非空且画布内唯一 | 节点 id 不能为空 / 节点 id 重复 | `:152-227` |
| 4 | 至少 2 个节点 | 画布中只有一个节点时不允许保存 | `:232-250` |
| 5 | 不能只有开始 + 结束节点 | —— | `:255-283` |

**单出口节点清单**：`voiceStartNode`、`voiceConversationNode` 且 `listenUserReply===false`、`voiceVariableAssignNode`、`voiceLabelCollectionNode`、全部转接类、`voiceEndNode`。

### validateXGraph.ts

| # | 规则 | 报错文案 | 出处 |
|---|---|---|---|
6 | 成环校验 | —— | `:1865`；但 `QaTree.vue:957 illicitRingType: []`，**当前配置下不生效，允许成环** |
| 7 | 所有节点必须从开始节点或某个全局节点可达 | 节点没有连接入主路 | `:1962`, `:2001` |
| 8 | 除开始节点与全局节点外，必须有入边 | 节点缺少入口连线 | `:621-659` |
| 9 | 条件判断 / 工时判断分支必须全连线 | 节点存在悬空的出口连线 / 存在未连接的分支 | `:575-606` |
| 10 | DTMF 三类分支必须全连线 | —— | `:388-421` |
| 11 | 接口节点分支 branchId 不可重复 | —— | `:423-438` |
| 12 | 结束/转接类节点不能有出边 | 该节点不能有出口连线 | `:496` |
| 13 | 全局节点必须至少一个非空 `global_intent` | —— | `:1096-1113` |
| 14 | 词槽收集必须填收集变量名 | 词槽收集功能必须填写收集变量名 | `:1035-1049` |
| 15 | `vadLevel` ∈ {0,1,2,3,100} | 远场人声抑制配置异常 | `:986-991` |
| 16 | 必须选择热词表（0=不使用也算有效） | 请在「聆听设置-热词表」中选择热词表 | `:994-996` |
| 17 | 外接音色时 `customTTSConfig` 必填 | 外接音色配置不完整 | `:970-976` |
| 18 | `replyMode=fixed` 时 `fallbackFixMessage` 必填 | —— | `:978-983` |
| 19 | 技能组 id 必填 | 请选择技能组 | `:1412-1414` |
| 20 | 智能体 id 必填 | 请选择智能体 | `:1506-1510` |
| 21 | 对话节点分支 content 不能为空 | 存在空回复 | —— |

**注意**：
- `chatNode` 的「分支必须全连线」只在开始节点 `replyMode==='fixed'` 且监听回复时才要求（`:501-559`），所以正常流程里**允许悬空分支**（命中后重复本节点）。
- **没有**「节点名必须唯一」的校验（同名不会报错，但人工排查会混乱）。
- **没有**「必须存在终点节点」的强制校验。

## 词槽同步（保存前自动跑）

`agent-flow/src/flow/validate/voiceConversationSlotSync.ts`

- `getEntitiesNeedingSync()`：过滤条件是 `slotId === undefined || !slotType`
  → **不填 slotId 才会触发建槽**。
- `syncConversationSlotsBeforeSave()`：调 `/tcccadmin/aislot/getAISlotList` 拉取，`/tcccadmin/aislot/updateAISlot` 创建/更新并回填 `slotId`（`:136-199`）。
- 临时 id → 真实 slotId 的替换：`idMap[String(collectionConfig.collectionType)]`（`:83-98`），所以 `entity.id` 和 `collectionConfig.collectionType` 必须写成**同一个临时 id**。
- 填了假的 `slotId` + `slotType` → 不会触发同步，前端不报错，**运行时指向不存在的词槽**。
- 本工具只同步 `slotType=custom`。不要写 date/name 等字面量：TCCC aislot 接口不接受这些 type，`collectionType` 也不会指向账号词槽目录。

## 其它上限

画布输入框的 `maxlength` / 截断都是**静默**的：导入不报错、保存不报错，运行时才发现话术被砍了半句。
所以这几条只能在编译期拦（校验 E12 超限阻断 / W9 到 90% 预警）：

| 内容 | 上限（字） | JSON 字段 | 出处（`agent-flow/src/`） |
|---|---|---|---|
| 全局提示词 | 8192 | `voiceSettings.systemPrompt` | `components/voice-node/voice-start-settings/persona-requirements/persona-requirements.vue:130`：`MAX_PROMPT_LENGTH`，`value.substring(0, 8192)` |
| 兜底固定话术 | 8192 | `voiceSettings.fallbackFixMessage` | 同上（走同一个 `limitPersonaContent`） |
| 对话 / 播报节点话术 | 5000 | `chatNode.nodeData.content`（`welcomeText` 同值） | `utils/const.ts:237` `MAX_WELCOME_LENGTH` → `voice-conversation-node-component.vue:26` |
| 单条用户回复分类 | 300 | `branches[].content`（`intent` / `global_intent`） | `utils/const.ts:236` `MAX_REPLY_LENGTH` → `voice-conversation-settings/reply-classification.vue:43` |
| 词槽说明 / 词槽名 | 1000 / 50 | `entities[].description` / `entities[].name` | `voice-conversation-settings/entity-management-modal.vue:50` / `:37`（固定选项 `fixedOptions` 是 50000） |
| 赋值变量的提取描述、固定值 / 变量名 | 1000 / 50 | `extractVariableNode.nodeData.vars[].description`、`.value` / `.name` | `voice-variable-assign-node/voice-variable-assign-node.validation.ts:11-12`。**这几条是保存硬拦**，报「提取描述长度不能超过 1000 个字符」「固定赋值长度不能超过 1000 个字符」「变量名称不能为空，且长度不能超过 50 个字符」 |
| 播报类短文本 | 300 | 转接节点 `transferring-sound` / `transfer-timeout-music` / `transfer-error` / `broadcast-sound`；DTMF `dtmfConfig.play_sound` / `invalid_sound` / `timeout_sound` | `voice-transfer-node-component.vue:206,229,251`、`voice-dtmf-node-component.vue:10,161,180` |
| 语音留言话术 | 300 | `voiceSettings.voicemailMessage` | `voice-start-settings/voicemail-settings.vue:36` |
| 静默提示 / 挂机话术 | 100 | `voiceSettings.notifyMessage` / `hungUpMessage` | `voice-start-settings/no-response-settings.vue:64` / `:101` |

- 字数按**字符数**算（回复分类是 `Array.from(content).length`，即码点数，与 Python `len()` 一致）。
  换行、空格、`${变量名}` 的字面量都计入；`${}` 按原文长度计，不是运行时替换后的长度。
- 播报类字段的值可能是 `tts:<文本>`，也可能是音频 base64。`tts:` 前缀不计入 300，
  音频 base64 不受这条限制（校验只量 `tts:` 开头的值）。
- 导入期不校验长度（见上文「导入期：几乎不校验」），**"导入成功"不等于没超限**，
  超长内容要么在画布里编辑该字段时被 `maxlength` 砍掉，要么运行时按截断后的内容执行。
