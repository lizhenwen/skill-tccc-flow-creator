# 外部 JSON 字段字典

导入/导出格式（与画布内部运行时格式不同，画布内部另有一层 `voiceXxxNode` 命名）。
所有默认值 1:1 取自线上真实导出，改动前请先核对真实画布。

## 顶层

```json
{
  "version": "1.0",
  "ivrData": { "<type>": [ <node>, ... ] },
  "voiceSettings": { ... }
}
```

`ivrData` 的 key 就是节点 type，按类型分组：`startNode` / `chatNode` / `apiCallNode` / `extractVariableNode` / `logicSplitNode` /`workTimeNode` / `hangup` / `DTMFNode` / `transfer` / `transferAgentNode`。

## 节点通用

```json
{
  "id": "node-<uuid>",
  "x": 683, "y": 270,
  "type": "chatNode",
  "name": "节点名（画布上显示）",
  "nodeData": { ... },
  "outEdges": [ <edge> ]
}
```

- `x`/`y` **导入时直接采用**，必须给合理坐标，否则全堆在原点。
- `id` 必须全画布唯一且非空。

## 连线

```json
{
  "id": "node-<uuid>",
  "source": "<单出口=节点id / 多出口=分支id>",
  "target": "<目标节点 id>",
  "type": "" | "custom:cubic-horizontal",
  "name": "",
  "startPoint": { "x": 0, "y": 0, "anchorIndex": 2 },
  "endPoint":   { "x": 0, "y": 0, "anchorIndex": 0 },
  "edgeData": { "branchId": "relation-xxx" }
}
```

- `startPoint`/`endPoint` 的 **x/y 被导入逻辑忽略**，只有 `anchorIndex` 生效。
- `anchorIndex`：**0 = 左侧入锚点，1..n = 第 n 个分支的右侧出锚点**。
  计算时要**排除 `global_intent` 分支**（它不占锚点）。
- `edgeData.branchId` 优先级**高于** `anchorIndex`，两个都写最稳。
- `logicSplitNode` 的否则分支 id **必须是**字符串 `"else"`，且同一节点里只能出现一次。普通条件若也写成 `"else"`，导入按 id 反查锚点时会把否则连线和该条件连线全部叠到同一出口。
- 边 `type`：`chatNode` 恒为 `custom:cubic-horizontal`（哪怕是纯播报单出口）；
  `startNode` / `extractVariableNode` 用 `""`。

## voiceSettings

| 字段 | 默认 | 备注 |
|---|---|---|
| voiceType | zh-CN-ArticulateJuan | 环境相关，填错不报错但通话时 TTS 失败 |
| ttsSpeed | 1 | |
| maxDuration | 60 | **秒**；导入时 ×1000 |
| notifyDuration | 10 | **秒**；导入时 ×1000 |
| notifyMaxCount / notifyType / notifyMessage | 1 / 0 / 抱歉，我没有听清楚… | 无应答追问；提示语**上限 100 字** |
| hungUpMessage | 那就不打扰您了，再见。 | **上限 100 字** |
| systemPrompt | "" | **上限 8192 字**，超长静默截断（`fallbackFixMessage` 同） |
| voicemailMessage | 你好，抱歉无法接待… | **上限 300 字** |
| vadLevel | 100 | 只能是 0/1/2/3/100，否则保存报「远场人声抑制配置异常」 |
| vadSilenceTime / interruptSpeechDuration | 500 / 500 | 毫秒 |
| aiBotId | 0 | **导入时被强制覆盖**成当前机器人 id |
| customTTSConfig | "" | 仅外接音色模式必填 |
| fallbackFixMessage | "" | `replyMode=fixed` 时必填 |
| enableComplianceAudio / transferFunctionEnable / endFunctionEnable | true | |
| languages | ["zh"] | 数组只放一项。中文是 `zh`。`zh-en`、`zh-yue` 原样保留，其余取 `-` 前一段 |
| executionMode | sequentialMode | |
| ambientSoundType / ambientSoundVolume | none / 1 | 背景音（如 busy_office） |
| voicemailMessage / voicemailAction / enableVoicemailDetection | …/0/false | |
| maxCallDurationMs | 0 | |
| disableLLM | false | |
| faqMenuList | [] | |

## chatNode.nodeData

```json
{
  "welcomeText": "<话术>",       // 与 content 保持一致
  "content": "<话术>",           // 上限 5000 字
  "fileId": "",
  "contentType": "gen",          // gen=智能生成 / fix=固定话术（不是 fixed！）
  "selectBranch": true,          // == 内部 listenUserReply，false 时该节点算单出口
  "allowInterrupt": false,
  "silentWaitTime": 10,          // 可选，线上很多节点没有这个字段
  "branches": [ { "id", "type", "content", "name": "", "tags": [] } ],   // content 单条上限 300 字
  "entities": [ ... ],
  "collectionConfig": { ... },   // 只有配了词槽才有
  "replyMode": "intent",         // intent / intent_with_entity；纯播报节点无此字段
  "verifyEntity": false,
  "queryVarName": "",
  "globalNode": false
}
```

分支 type 枚举（`VoiceReplyType`）：

| type | 含义 | 谁产生 |
|---|---|---|
| intent | 普通意图分类 | 业务设计 |
| entity_success | 词槽收集成功 | 配了收集变量就必须有；必须排在意图分类前面 |
| silent | 用户无响应 | 系统分支，视觉上垫在分类组末尾 |
| else | 其他兜底 | 系统分支，导入后不可编辑 |
| global_intent | 全局触发语 | 全局节点专用，不占画布锚点 |

> `entity_fail`（收集失败）**已废弃**，不要写进 JSON。画布 `QaTree.removeDeprecatedCollectionFailBranch` 会在导入后剥掉它，但它在导入算锚点时还占一位，后面的连线会全部错位。
> `noreply` 不存在，别用。

### 词槽（entities / collectionConfig）

```json
"entities": [{
  "slotType": "custom",          // 只写 custom
  "varName": "运单号",            // 收集结果存进这个变量
  "name": "运单号",               // 词槽名，上限 50 字
  "description": "提取 13-15 位连续数字…",   // 上限 1000 字
  "id": "entity-<uuid>"          // 无 slotId 时的前端临时 id
}],
"collectionConfig": {
  "collectionType": "entity-<uuid>",   // slotId 或临时 id，不要写 date/name 等字面量
  "collectionAsValue": "运单号",
  "collectionItems": [{ "id": "relation-<uuid>", "collectionType": "…", "collectionAsValue": "运单号" }]
}
```

- `slotId` 是**账号维度的服务端词槽主键**。不填 → 保存时 `syncConversationSlots`自动建槽并把临时 id 换成真实 slotId。**伪造 slotId 前端不报错，运行时指向不存在的词槽。**
- `slotType` 只使用 `custom`。`collectionType` 写 slotId 或临时 id，不要写类型字面量。
- 有 `entities` 时 `replyMode` 必须是 `intent_with_entity`，且必须有 `entity_success` 分支（排在意图分类前面）。**不要**写 `entity_fail`。
- 一个节点只能有一个 entity。

## apiCallNode.nodeData

```json
{
  "url": "", "callbackTimeout": 4500,
  "headerParams": [{ "key", "value" }],
  "params": [{ "key", "value", "valueType": "string" }],
  "returns": [{ "key": "data.eta", "alias": "预计送达时间" }],
  "async": false, "retryTimes": 0,
  "authType": 0,                 // 0无 1basic 2bearer 3custom 4oauth2
  "basicAuth": { "basicToken": "" },
  "bearerAuth": { "bearerToken": "" },
  "customAuth": { "key": "", "value": "" },
  "oauth2Auth": { "tokenURL": "", "clientId": "", "clientSecret": "" },
  "globalNode": false,
  "branches": [ api_call_success, api_call_fail ],
  "globalReplyClasses": []
}
```

`returns[].alias` 就是流程里可用的新变量名。

## extractVariableNode.nodeData

```json
{
  "vars": [{
    "id": "voice-variable-assign-relation-<uuid>",
    "name": "单号后四位",          // 上限 50 字
    "mode": "llm",               // llm=大模型提取 / fix=固定值或 ${} 表达式
    "description": "<mode=llm 时的提示词>",   // 上限 1000 字
    "value": "<mode=fix 时的值>",             // 上限 1000 字
    "needSave": true,
    "messageRounds": 5
  }],
  "globalNode": false,
  "branches": []                 // 只可能放 global_intent
}
```

> 只有 `llm` 和 `fix` 两种 mode，没有 `fixed` / `expression`。
> 这三条长度限制是画布**保存时硬拦**（有明确报错文案），不像别处那样静默截断。

## logicSplitNode.nodeData

```json
{
  "branches": [],                          // 只可能放 global_intent
  "logicSplitBranches": [
    { "id": "relation-…", "method": "rule", "logicType": "and",
      "conditions": [{ "var": "${x}", "operator": "==", "val": "1" }] },
    { "id": "relation-…", "method": "llm", "logicType": "and",
      "content": "…", "llmPrompt": "客户情绪激动" },
    { "id": "else", "method": "rule", "logicType": "else" }
  ],
  "globalNode": false
}
```

- 连线 `source` 用 `logicSplitBranches[i].id`，else 分支就是字符串 `"else"`（只能给 `logicType=else` 那一条，禁止给普通条件）。
- 线上出现过的 operator：`==` `exists` `gt`（还支持 `!=` `>=` `<=` `contains` 等）。
- 单目运算符（`exists` / `not_exists`）**不写 `val` 字段**。
- **单条分支 `conditions` 不得超过 30**（运行时硬上限，校验 E19）。超了每次请求经过该节点都会触发超限检测。

## workTimeNode.nodeData

```json
{
  "timeZoneName": "Asia/Shanghai",
  "globalNode": false,
  "workTimeBranches": [
    { "id": "relation-…", "type": "worktime", "name": "工作时间1",
      "workTimeConfigs": [
        { "dayType": "BusinessDay", "daysOfWeek": [], "specificDates": [],
          "workTimePeriods": [{ "startTime": {"hour":8,"minute":30,"nextDay":false},
                                "endTime": {"hour":18,"minute":0,"nextDay":false} }] },
        { "dayType": "Custom", "daysOfWeek": [1,2,3,4,5], "specificDates": [],
          "workTimePeriods": [{ "startTime": {"hour":9,"minute":0,"nextDay":false},
                                "endTime": {"hour":18,"minute":0,"nextDay":false} }] },
        { "dayType": "CustomDate", "daysOfWeek": [],
          "specificDates": [{ "startDate": "2026-09-09", "endDate": "2026-09-09" }],
          "workTimePeriods": [{ "startTime": {"hour":9,"minute":0,"nextDay":false},
                                "endTime": {"hour":18,"minute":0,"nextDay":false} }] }
      ] },
    { "id": "relation-…", "type": "other", "name": "其他时间", "workTimeConfigs": [] }
  ],
  "branches": []
}
```

- `dayType` **只有** `BusinessDay`（法定工作日）/ `Holiday`（法定休息日）/ `Custom`（自定义星期）/
  `CustomDate`（自定义日期）四种。写别的值（曾误写过 `Weekly` / `Specific`）会在
  `DAY_TYPE_WORK_TIME_RULE_MAP` 查不到，导入时整条规则被静默丢弃，节点退回「未配置」，
  保存报「请配置工作时间判断」。
- `Custom` 必须给非空 `daysOfWeek`（1=周一 … 7=周日）；`CustomDate` 必须给非空
  `specificDates`（`{startDate, endDate}`，同一天则两者相同）。缺了同样保存不过。
- 同一个分支里每种 `dayType` 最多出现一次，画布按类型收敛，重复的后者覆盖前者。
- `startTime.nextDay` 恒为 `false`；`endTime.nextDay` 表示跨天。画布导入时其实会用
  「结束早于开始」重新推导，写反了不会报错但会与预期不符。
- `BusinessDay` / `Holiday` 依赖中国大陆法定节假日历，`timeZoneName` 不在
  `Asia/Shanghai / Asia/Chongqing / Asia/Chungking / Asia/Harbin / Asia/Urumqi / PRC`
  之内时保存会报「非中国大陆时区不支持选择法定工作日或法定休息日」。
- 必须有一条 `type: "other"` 的「其他时间」分支兜底，且每条分支都要连线。

## DTMFNode.nodeData

```json
{
  "nodeType": "navigation",       // navigation=按键导航 / collection=收号 / extension=分机号(已隐藏)
  "varName": "",                  // 收号模式下的收集变量
  "globalNode": false,
  "branches": [
    { "type": "dtmf_success", "content": "1", "tags": [{ "tagName": "按键标签", "tagValue": "售前咨询" }] },
    { "type": "dtmf_fail", "content": "按键失败" }
  ],
  "tcccNodeData": { "dtmfConfig": {
    "play_sound": "tts:…", "max_failures": "1", "dtmf_timeout": "5000",
    "invalid_sound": "tts:您的输入错误，请重新输入。",
    "timeout_sound": "tts:您的输入超时，请重新输入。",
    "dtmf_type": "fixed", "dtmf_len": "1",
    "keywords": ["1","2"]         // 仅导航模式
  } }
}
```

`dtmfConfig` 的值是**下划线命名 + 字符串**，语音字段带 `tts:` 前缀。

## transfer.nodeData

`mode`：`manual`=技能组 / `outer`=外线 / `third_party_route`=第三方内线。

关键字段：

- `name`：**技能组 id**（也可以是变量表达式），仅 `manual` 有效。留空保存时报「请选择技能组」。
- `caller` / `callee`：外线/第三方内线的主被叫。
- `transfer-timeout`（秒，manual 用）与 `timeout`（毫秒，outer/third 用）二选一，另一个填 0。
- 语音字段成对出现：`xxx-locale` / `xxx-voice` / `xxx` / `xxx-speed`，`-voice` 要跟 `voiceSettings.voiceType` 一致。
- `aiTransferContext`：转人工上下文，`enableSummary` 开启后会把通话摘要带给座席。
- `branches` 只可能放 `global_intent`；**不能有出边**。

## transferAgentNode.nodeData

```json
{
  "varAgentID": "12345",          // 智能体 id 或变量；留空保存报「请选择智能体」
  "globalNode": false,
  "branches": [
    { "id": "relation-…", "type": "global_intent", "content": "客户要投诉", "examples": [], "tags": [] },
    { "id": "<节点自身 id>", "type": "transfer_agent_fail", "content": "转接失败", "examples": [], "tags": [] }
  ]
}
```

`transfer_agent_fail` 分支的 id 用**节点自身 id**（画布导出就是这样）。不能有出边。

## startNode / hangup

`nodeData` 都是 `{}`。`startNode` 恰好一条出边；`hangup` 不能有出边。

## 节点几何与自动布局

节点 `x`/`y` 导入时直接采用，所以生成时必须给合理坐标。工具的高度估算
**1:1 移植自画布自己的估算器** `flow/utils/manualLayoutNodeSizeEstimate.ts`（画布「一键整理」用的就是这套常量），常量对照：

| 常量 | 值 | 含义 |
|---|---|---|
| `DEFAULT_NODE_WIDTH` | 260 | 节点宽（结束通话节点更窄，但按 260 排不会错） |
| `NODE_VERTICAL_PADDING` | 24 | 上下内边距合计 |
| `LABEL_CONTENT_HEIGHT` | 36 | 标题行（图标 24 + 间距 12） |
| `TITLE_WITH_GAP_HEIGHT` | 24 | 小节标题（12）+ 间距（12） |
| `WELCOME_CONTENT_HEIGHT` | 108 | 话术框 |
| `BRANCH_ITEM_HEIGHT` | 40 | **单个分支块** |
| `CONTENT_GAP` | 12 | **分支之间的间距** |
| `GLOBAL_TIPS_HEIGHT` | 46 | 全局节点顶部的「全局」标记条 |
| `PARAM_ROW_HEIGHT` | 36 | 接口节点每行 header/param/return |
| `JUDGEMENT_BRANCH_*` | 56 / 28 / 34 | 条件分支最小高 / 每行 / 上下留白 |
| `CHAT_NODE_MIN_HEIGHT` | 168 | 对话节点下限（全局对话节点 214） |
| `API_NODE_MIN/MAX_HEIGHT` | 420 / 680 | 接口节点被夹在这个区间 |

各类节点的高度公式：

```
start                 = 120
end(hangup)           = 48（画布估算器未覆盖，按真实节点样式）
分支区(n)             = 0            (n = 0)
                      = 24 + n×40 + (n-1)×12   (n ≥ 1)   ← 每多一个分支 +52px
chat / announce       = max(168 或 214, 24 + 36 + 108 + 分支区 + 全局条)
api                   = clamp(24+36+24+40 + ceil(len(url)/52)×24
                              + 参数行数×36 + 分支区 + 全局条, 420, 680)
dtmf-nav / -collect   = max(120, 132 + 分支区)
condition             = max(120, 24+36+24 + Σ条件分支高 + 间距 + 全局条)
                        条件分支高 = else ? 56 : max(56, 34 + max(2, 条件数×2)×28)
worktime              = max(120, 24+36+40+12 + 分支区 + 全局条)
assign                = max(120, 144 + 变量数×56 + 分支区)
transfer-*            = max(120, 156 + 分支区)
```

分支数按 `nodeData.branches` **全量**计（含 `global_intent`），与画布估算器一致——偏保守，多留空间不会造成重叠。

布局对齐画布「一键整理」：主干沿第一出边从左到右（层距 `COLUMN_GAP=420` = 节点宽 260 + 间距 160），起点 `(350, 300)`；其余分支在父节点右侧纵向展开，同列视觉间隙 ≥16px。出边 ≥4 时第一出口不再延续主干。

> G6 以静态 `nodeStyle.height`(172) 的一半为绘制原点，节点实际向下延伸真实高度。
> 同列防重叠：`y_{k+1} ≥ y_k + 节点高 + 16`。

存量节点的 `x/y` 默认从 `--base` JSON 按节点 id 继承；加 `--relayout`（应用编译默认）则全部按流程重排。md 里如果还残留 `- 坐标:` 则优先用它（`--relayout` 时忽略）。

## 未支持

`tagNode`（话后标签，内部 `voiceLabelCollectionNode`）和 `extension`（分机号）在画布里默认隐藏，本工具不生成。需要时手工在画布上加。
