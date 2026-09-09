# Markdown 设计稿语法规范（权威）

设计稿是整条链路的唯一真源。`scripts/tccc_flow.py` 按本文件严格解析，写错就编译报错（带行号）。

## 整体骨架

```markdown
# 流程：<流程名>

## 环境配置
- 键: 值

## 假设与待确认        ← 自由内容，不参与编译，原样保留供人核对
| # | 假设 | 影响范围 | 待确认 |

## 全局提示词
```prompt
<systemPrompt 全文>
```

## 变量表              ← 自由内容，不参与编译，供人查阅
| 变量名 | 来源 | 说明 |

## 节点
### N01 <节点名> [<kind>]
...
```

- 章节标题用 `## `，节点用 `### `。除 `环境配置` / `全局提示词` / `节点` 外的章节都原样跳过。
- **`## 节点` 章节必须存在且至少一个节点**。
- 节点编号形如 `N01`、`N12`，全稿唯一，连线只认编号。

## 环境配置

| 键 | 落到 voiceSettings | 说明 |
|---|---|---|
| 音色 | voiceType | 也会同步成转接类节点的播报音色 |
| 语速 | ttsSpeed | |
| 单轮最长时长(秒) | maxDuration | **秒**，导入时画布自己 ×1000 |
| 静默提示 | notifyMessage | 用户不说话时的追问语 |
| 静默提示次数 | notifyMaxCount | |
| 静默提示等待(秒) | notifyDuration | **秒** |
| 静默提示类型 | notifyType | |
| 挂机话术 | hungUpMessage | |
| 打断检测(毫秒) | interruptSpeechDuration | |
| 静音判定(毫秒) | vadSilenceTime | |
| 远场人声抑制 | vadLevel | 只能是 0/1/2/3/100 |
| 背景音 / 背景音音量 | ambientSoundType / ambientSoundVolume | |
| 语音留言检测 | enableVoicemailDetection | 是/否 |
| 合规录音 | enableComplianceAudio | 是/否 |
| 外接音色配置 | customTTSConfig | |
| 兜底固定话术 | fallbackFixMessage | |
| 语言 | languages | 逗号分隔 |
| 技能组ID | —— | 供 `[transfer-skill]` 节点缺省取值 |
| 转接智能体ID | —— | 供 `[transfer-agent]` 节点缺省取值 |
| 环境注入变量 | —— | 逗号分隔。**不在这里声明的未定义变量会报 E11** |

布尔值写 `是` / `否`。留空的键视为不设置。

## 节点通用语法

```markdown
### N05 询问是否登记工单 [chat] {全局}
- 属性名: 属性值          ← 可重复的属性写多行即可
```prompt
<话术 / 提示词，原样保留换行>
```
分支：
- <分支内容> {标签: 键=值; 键2=值2} → N07 登记工单
→ N09 结束通话              ← 单出口节点用这种写法
```

规则：

- 标题：`### <编号> <节点名> [<kind>]` 后可跟 `{全局}` 标记为全局节点。
- 属性行必须以 `- ` 开头且含 `:`（中英文冒号都行）。同名属性重复出现按列表处理。
- 话术写在 ```` ```prompt ```` 围栏里；话术里含 ``` 时用 `~~~prompt` 围栏。
  **围栏内不做任何解析**，`-`、`#`、`→` 都是普通字符。
- **围栏内一句话必须写在一行，禁止为了排版折行**。围栏内容是逐字写进 JSON 的，排版换行会变成真实 `\n`，等于把断句发给大模型。换行只用于分段和列表项。

```
✅  你是本快递公司的官方客服，语气自然口语化，用短句，不称呼客户姓名。

❌  你是本快递公司的官方客服，语气自然口语化，用短句，
    不称呼客户姓名。
```

> 写错了不用手改：`build` 不带 `--base` 时会自动合并（终端会打印「[整形] 合并了 N 处」），
> 或用 `tccc_flow.py rewrap 设计稿.md` 一键规整。校验里对应 **W11**。
- `分支：` 单独一行，之后的 `- ` 行都是分支。
- 箭头 `→`（也接受 `->` / `=>`）后面必须是节点编号，编号后可跟节点名（只为可读，不校验）。
- **分支不写箭头 = 悬空分支**，命中后重复本节点，这是合法设计，校验里报 W5 提醒确认。
- `- ID: node-xxx` / `- 坐标: 683,270`：decompile 自动写入，用于 `--base` 精确匹配与保位；
  新写设计稿不用管，坐标会自动分层布局。
- `- 触发: <全局触发语>`：任何节点类型都可用，等价于加一条 `global_intent` 分支，写了就自动标记为全局节点。可带 `{标签: ...}`。

## 13 类节点

| kind | 画布节点 | 出口 | 必填 |
|---|---|---|---|
| `start` | 开始通话 | 单出口，恰好 1 条 | 无 |
| `chat` | 对话节点（听用户回复） | 多出口 | 话术 + 至少 1 个分支 |
| `announce` | 对话节点（纯播报不听） | 单出口 | 话术 |
| `api` | 接口调用 | 成功 / 失败 | URL |
| `assign` | 变量赋值 | 单出口 | 至少 1 个 `变量` |
| `condition` | 条件判断 | 每分支一个出口，**必须全连** | 分支 |
| `worktime` | 工作时间判断 | 同上 | 分支 |
| `dtmf-nav` | 按键导航 | 同上 | 话术 + 按键分支 |
| `dtmf-collect` | 收号 | 成功 / 失败 | 收集变量 |
| `transfer-skill` | 转技能组 | 终点，禁止出边 | 技能组ID |
| `transfer-outer` | 转外线 | 终点 | 被叫 |
| `transfer-3rd` | 转第三方内线 | 终点 | 被叫 |
| `transfer-agent` | 转接智能体 | 终点 | 智能体ID |
| `end` | 结束通话 | 终点 | 无 |

### chat / announce

| 属性 | 默认 | 说明 |
|---|---|---|
| 话术模式 | chat=智能生成 / announce=固定 | 智能生成→`gen`，固定→`fix` |
| 允许打断 | 否 | |
| 静默等待(秒) | 不写则不落字段 | 写了才会出现 `silentWaitTime` |
| 听用户回复 | 有分支即 true | 一般不用手写，`announce` 恒为 false |
| 收集变量 | —— | 一个节点最多一个（画布限制） |
| 词槽类型 | custom | 内置：date/time/datetime/address/surname/name/original_words；其余算自定义 |
| 词槽ID | —— | **仅在复用真实环境已存在的词槽时写**，新建流程绝不要编 |
| 词槽名称 / 词槽说明 | 同变量名 / 空 | 说明里的换行写 `\n` 字面量 |
| 收集方式 | 不写则不落字段 | 开放 / 固定选项 |
| 固定选项 | —— | 收集方式=固定选项时生效 |
| 校验实体 | 否 | |

分支写法（同时决定分支 type）：

| 写法 | type | 说明 |
|---|---|---|
| `- 收集成功 → Nxx` | entity_success | content 自动补成 `收集${变量名}结束` |
| `- 收集失败 → Nxx` | entity_fail | |
| `- 无应答 → Nxx` | silent | content 自动补成 `用户无响应N秒` |
| `- 其他… → Nxx` | else | 系统兜底分支，导入后画布上不可编辑 |
| `- 触发: xxx` | global_intent | 建议写成属性而不是分支 |
| 其它任何文字 | intent | **正常业务分支都是这个** |

### api

```markdown
### N03 查询运单 [api]
- URL: https://example.com/api/query
- 超时(毫秒): 4500
- 异步: 否
- 重试次数: 1
- 鉴权: 无            ← 无/basic/bearer/custom/oauth2
- 请求头: Authorization = Bearer ${token}
- 参数: waybill = ${运单号} : string
- 返回: data.eta → 预计送达时间
分支：
- 成功 → N04 判断状态
- 失败 → N09 转人工
```

`返回:` 的右侧就是新变量名，可在后续任意话术里用 `${}` 引用。

### assign

```markdown
### N06 取单号后四位 [assign]
- 变量: 单号后四位 = LLM提取: 从 ${运单号} 里取最后四位数字，只输出四位数字\n不要输出任何解释
- 变量: 正确单号 = 固定值: ${运单号}
- 记忆轮数: 5
→ N07
```

`LLM提取:` → `mode=llm`（描述即提示词）；`固定值:` → `mode=fix`（值可以是 `${变量}` 表达式）。
多行提示词用 `\n` 字面量。

### condition

```markdown
### N04 判断快件状态 [condition]
分支：
- 如果 ${快件状态} == 3 → N06
- 如果 ${工单状态} == 1 且 ${是否超时} == true → N07
- 如果 ${预计送达时间} exists → N05
- 如果 LLM: 客户情绪激动且反复投诉 → N09
- 否则 → N09
```

- 符号运算符：`==` `!=` `>=` `<=` `>` `<`
- 单词运算符（与画布导出一致）：`gt` `lt` `gte` `lte` `eq` `neq` `contains` `not_contains``in` `not_in` `exists` `not_exists`；中文别名：`大于` `小于` `包含` `属于` `存在` `为空` 等
- `exists` / `not_exists` 是单目，右边不写值
- 多条件用 `且`（and）或 `或`（or）连接，一条分支内不要混用
- `如果 LLM: <判断语>` → `method=llm`
- `- 否则` 必写，id 固定为 `else`

### worktime

```markdown
### N11 工作时间判断 [worktime]
- 时区: Asia/Shanghai
分支：
- 工作时间: 工作日 08:00-21:00; 节假日 09:00-18:00 → N12
- 其他时间 → N08
```

日期类型关键词：`工作日`(BusinessDay) / `节假日`(Holiday) / `每周`(Weekly) / `指定日期`(Specific)。
时段可写多个，用 `;` 分隔；跨天写 `22:00-26:00` 会自动置 `nextDay`。

### dtmf-nav / dtmf-collect

```markdown
### N08 按键导航 [dtmf-nav]
- 话术: 查快递请按1，寄快递请按2
- 超时(毫秒): 5000
- 失败次数: 1
分支：
- 按键 1 查快递 → N02
- 按键 2 寄快递 → N12
- 按键失败 → N10

### N09 收号 [dtmf-collect]
- 收集变量: 身份证号
- 位数: 18
- 话术: 请输入您的身份证号，字母用星号键代替，井号键结束
分支：
- 收号成功 → N10
- 收号失败 → N11
```

`按键 <键值> <标签>`：键值进 `content`，标签自动挂成 `{按键标签: 标签}`。

### transfer-* / end

```markdown
### N12 转技能组 [transfer-skill]
- 技能组ID: 65067          ← 留空则取环境配置里的技能组ID
- 转接提示音: 正在为您转接，请稍后
- 转接超时(秒): 30
- 超时话术: 座席繁忙，暂时无法为您转接
- 失败话术: 当前所有人工座席不在线
- 转人工带摘要: 是          ← 开启 aiTransferContext.enableSummary

### N13 转满意度评价 [transfer-agent]
- 智能体ID: 12345

### N14 转外线 [transfer-outer]
- 主叫: 008602066247697
- 被叫: 13500000000

### N09 结束通话 [end]
```

## 命令行

```bash
PY=/Users/lizhenwen/.workbuddy/binaries/python/versions/3.13.12/bin/python3
$PY scripts/tccc_flow.py build 设计稿.md -o 流程.json [--base 底座.json] [--report 报告.md] [--mermaid 图.mmd] [--rewrap|--no-rewrap]
$PY scripts/tccc_flow.py validate 流程.json [--design 设计稿.md] [--report 报告.md]
$PY scripts/tccc_flow.py decompile 画布.json -o 设计稿.md [--title 名称]
$PY scripts/tccc_flow.py rewrap 设计稿.md|目录 [--dry-run] [-v]
```

- `build` 有 error 时不写 JSON（`--force` 可强行写出，仅调试用），退出码 2。
- `--base`：md 未表达的字段从底座逐字继承；节点按 `- ID:` 匹配，分支 id 消耗式复用。
- `--rewrap`：合并话术里的排版硬折行。**不带 `--base` 时默认开启**（新建流程），带 `--base` 时默认关闭（改存量画布优先保真）；可用 `--no-rewrap` / `--rewrap` 显式覆盖。
- `rewrap` 子命令：修 Markdown 文件本身。只处理正文段落与 ```prompt 围栏，
  ```bash / ```json / 无标记的代码块、表格、标题、列表标记一律不动。
- `validate` 传 `--design` 才能检查「环境注入变量」、话术三段结构和硬折行。
