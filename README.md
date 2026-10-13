# qq-chatbox

把 Windows 版 **NTQQ** 的本地聊天记录导出为 **JSON / 纯 TXT**，支持**媒体下载**与**定时增量导出**，并在此基础上把聊天记录「提炼」成可对话的机器人（数字分身 / 历史问答）。

纯 Python 实现，不需要编译 SQLCipher 扩展。解密密钥通过 **NapCat**（开源的 NTQQ 协议框架）在登录时自动截获，本工具负责「解密 → 解析 → 导出 → 对话」。

---

## 为什么需要这个工具

新版 NTQQ 已经移除了旧版「消息管理器 → 导出消息记录」功能，设置里只剩「聊天记录备份与迁移」，那只是把数据在 QQ 内部搬运，产不出可读文件。

聊天记录本身也不是明文：

| 项目 | 说明 |
|---|---|
| 主库位置 | `<QQ数据目录>/<账号>/nt_qq/nt_db/nt_msg.db` |
| 加密方式 | SQLCipher（AES-256-CBC + HMAC-SHA1，PBKDF2-HMAC-SHA512/4000） |
| 文件结构 | 1024 字节明文头部 + 4096 字节页，每页尾部保留 48 字节 |
| 密钥 | 运行时获取的数据库口令；插件可显式保存，不能仅凭数据库文件推导 |

根据 [QQDecrypt](https://github.com/QQBackup/QQDecrypt) 的逆向研究与 NapCat 实现，口令获取与登录后的数据库初始化有关。可在有权限的已登录 QQ 会话中通过 NapCat 获取口令；口令是否跨登录或版本保持不变不作保证，应以目标数据库的解密校验为准。

**NapCat 可在登录时获取数据库口令**，具体 API 与版本兼容性需以所用版本验证。

---

## 快速开始

以下账号、姓名、对话及路径均为虚构示例或占位符，使用时替换为自己的授权数据；示例不包含真实聊天信息。

### 第 1 步：用 NapCat 拿到密钥

安装 [NapCat](https://github.com/NapNeko/NapCatQQ)（Shell 模式即可，自带 QQNT 核心），
用目标账号扫码 / 快速登录。登录成功后 NapCat 会缓存数据库口令：

```js
const dbApi = core.apis.DatabaseApi;
const passphrase = dbApi.getPassphraseBuffer()?.toString('ascii'); // 16 字符 ASCII 口令
```

> 或者让 NapCat 直接解密成明文库，跳过本工具的 `decrypt`：
> ```js
> const plain = dbApi.decryptDatabase('nt_msg.db', 'out/nt_msg_plain.db');
> ```

### 第 2 步：解密 + 导出

```powershell
# 准备环境
uv venv .venv
uv pip install --python .venv\Scripts\python.exe cryptography

# 一条龙：用口令推导密钥 -> 解密 -> 导出
.\.venv\Scripts\python.exe -m qq_exporter all --qq <账号> --passphrase "<16字符口令>" --out out
```

也可以分步：

```powershell
# 1) 列出本机账号与数据库
.\.venv\Scripts\python.exe -m qq_exporter scan

# 2) 解密（若已有明文库可跳过）
.\.venv\Scripts\python.exe -m qq_exporter decrypt --db <nt_msg.db> --passphrase "<口令>" --out plain.db

# 3) 导出
.\.venv\Scripts\python.exe -m qq_exporter export --db plain.db --format json,txt --out out
```

> 如果 QQ 数据目录不在默认位置（例如多磁盘/移动过），用 `scan` 看不到时，
> 直接把 `nt_msg.db` 的完整路径传给 `--db` 即可。

### 输出

```
out/
├── json/
│   ├── index.json          # 会话索引（不含正文）
│   └── 0000_xxx.json       # 每个会话一个文件
└── txt/
    ├── _index.txt
    └── 0000_xxx.txt        # 可读文本：时间 + 收/发方向 + 发送者 + 内容
```

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `scan` | 列出本机账号、数据库、体积、盐值 |
| `keys` | 从运行中的 QQ 进程内存提取密钥（备用） |
| `decrypt` | 用 `--key`（32 字节 hex）或 `--passphrase`（16 字符）解密 |
| `schema` | 打印（明文）数据库的表结构，便于适配新版本 |
| `export` | 从明文库选择性导出 JSON / TXT（可加 `--media` / `--incremental`） |
| `conversations` | 读取会话元数据，列出会话键、消息数与时间范围 |
| `watch` | 定时增量导出（每 N 秒一轮，自动只导出新消息） |
| `all` | 取密钥 → 解密 → 导出，一步到位 |

常用参数：

* `--passphrase <口令>`：直接给 16 字符口令（NapCat 给出的就是它）；
* `--key <64位hex>`：直接给 32 字节 AES 密钥；
* `--names names.json`：会话名称映射，如 `{"group:123456": "项目群", "c2c:10001": "张三"}`；
* `--limit N`：只导匹配结果的前 N 条（调试用，不能与 `--incremental` 同用）；
* `--single-json`：JSON 合并为单文件；
* `--include-content`：JSON 里附带完整的 Protobuf 字段树（很大）；
* `--media`：下载图片/文件/语音/视频/表情到 `out/media/`；
* `--napcat <http地址>`：旧媒体 rkey 过期时，经 NapCat 换新直链补下载；
* `--napcat-token <token>`：NapCat HTTP 的 access_token（若配置了）；
* `--incremental`：增量导出（用 `out/.export_state.json` 记录进度）；
* `--since <消息ID>`：只导出消息 ID 大于该值的消息；
* `--conversation group:<ID>|c2c:<ID>`：只导出选定会话，可重复传入；
* `--kind group|c2c`：限定会话类型；
* `--after` / `--before`：Unix 秒或 ISO 8601 时间区间，包含下界、不包含上界；
* `--self-qq`：私聊身份无法自动可靠识别时明确本账号；
* `--standard-layout`：确认标准整数/文本字段布局后跳过标识兼容预扫描，未知历史字节格式勿用；
* `--explain-query`：检查查询计划及兼容预扫描，不解析或导出正文（使用已有明文库）；
* `--media-dir <nt_data目录>`：QQ 媒体缓存目录（默认从消息里的本地路径自动推导）。

---

## 媒体下载

`export --media`（或 `watch --media`）会把消息里引用的图片/文件/语音/视频/表情存到
`out/media/<会话>/`，查找顺序：

1. 消息里记录的本地缓存路径（原图）；
2. 本地缓存按 **MD5 兜底**（原图被 QQ 清理后，退而求其次拿缩略图）；
3. HTTP 下载（URL 里的 `rkey` 有时效，旧消息可能已过期）；
4. **NapCat 富媒体 API 换新直链**（用稳定的 `fileid` 重新换 rkey）。

```powershell
# 带 NapCat 补下载：旧媒体 rkey 过期时也能补齐
.\.venv\Scripts\python.exe -m qq_exporter export --db plain.db --media --napcat http://127.0.0.1:6099
```

> NapCat 必须**运行且已登录**（换 rkey 需要在线会话）。若 `onebot11.json` 配了
> `access_token`，再加 `--napcat-token <token>`。

## 定时 / 增量导出

```powershell
# 一次性增量：只导出上次之后的新消息
.\.venv\Scripts\python.exe -m qq_exporter export --db plain.db --incremental

# 定时增量：每 1 小时自动跑一轮，只导出新消息
.\.venv\Scripts\python.exe -m qq_exporter watch --db plain.db --interval 3600 --media
```

增量进度记录在 `out/.export_state.json`；删除它即可重新全量导出。
`watch` 读取指定的明文库，不会自动重新解密正在运行的 QQ 数据库。要同步新消息，需先取得一致的数据库快照并更新明文库。

单会话、多会话、区间导出与索引使用说明见 [选择性导出](<docs/selective-export.md>)。筛选在正文解析前执行；来源缺少合适索引时仍可能扫描元数据，不能保证没有全表读取。不同选择范围使用独立输出目录，防止复用不兼容水位。

机器人索引的时间戳增量同步见 [Roadmap 使用指南](<docs/roadmap-usage.md>)：支持水位线、重读窗口和可选差分页解密缓存，仍会扫描加密主库并重建索引；它与此处按消息 ID 的 `--since` 不是同一参数语义。

单私聊累计历史、索引水位隔离和 Windows 离线快照见 [单私聊同步](<docs/scoped-sync.md>)。默认整库严格验收；显式 `--integrity-mode selected` 只允许在目标私聊验收通过时更新其索引，并保留全库错误记录。工具执行一轮，不自动设置任务计划或关闭 QQ。需要更新现有多会话索引并保留其他历史时，使用 [每日私聊更新](<docs/daily-private-sync.md>) 的独立同步与合并发布入口；提供显式任务注册脚本，运行前需确认本地路径、范围与时区。

---

## 原理

### SQLCipher 解密（`qq_exporter/sqlcipher.py`）

* 去掉开头 1024 字节的 NTQQ 自定义头部；
* 口令经 `PBKDF2-HMAC-SHA512(口令, 盐, 4000)` 派生为 32 字节 AES 密钥，盐值明文存放在第 1 页开头；
* 保留区大小**自动探测**（拿第 1 页试几个候选值，看哪个能还原出合法 SQLite 页头）；
* **流式解密**：按页读 → 按页解密 → 按页写，避免将整个主库载入内存；不承诺固定峰值内存；
* WAL 合并已通过标准 SQLite 与合成加密样本测试；真实 QQ 版本及持续写入期间的一致性仍待验收。同步工具当前跳过 WAL，因此仅在 WAL 中的消息可能暂时不可见；
* 解密结果执行 SQLite 完整性检查；同步默认要求全库通过。显式单私聊范围验收仍要求非零页认证和目标表检查通过，全库错误保留在报告中。

SQLite 页头里的「保留区字节」与加密页尾布局应分别处理，不能假定数值相同。版本兼容性以合成测试和目标数据库校验为准。

### 消息体解析（`qq_exporter/wire.py` + `qq_exporter/db.py`）

消息正文是 Protobuf，字段号随版本变化、没有公开的 `.proto`。
`wire.py` 实现了一个不依赖 schema 的通用 wire 解析器；`db.py` 按真实结构解析：

```
MsgBody { repeated MsgContent content = 40800 }
  MsgContent { 45002 内容类型, 45101 文本, ... }
```

* `45002 = 1` 是纯文本 → 取 `45101`；
* 其余类型（图片/文件/语音/视频/表情/回复/小程序/闪传/通话/链接…）只输出
  干净的占位符（如 `[图片]`、`[表情]`），避免把消息里的 URL、MD5、文件名等
  元数据误当正文导出。

### 字段映射（`qq_exporter/db.py`）

列名就是协议字段号字符串（如 `40001`=消息ID、`40050`=时间、`40013`=方向、
`40020/40033`=发送者、`40021/40030`=会话对象）。每个逻辑字段都配了候选列名逐级回退，
缺列不会崩；`schema` 命令可打印真实表结构以便适配新版本。

> 方向判定建议用「发送者是否等于本账号」，比 `40013` 的取值更稳
> （实测 `40013∈{1,2,4,5}` 表示发出、`{0,3}` 表示收到，版本间易变）。

---

## 备用：不装 NapCat 拿密钥

如果不想用 NapCat，本工具还内置了另外两条路（都要求目标账号已登录且库被打开过）：

* **内存扫描**（`keys`）：在 QQ 进程私有堆里搜盐值 → 盐值锚定恢复密钥；找不到时可用
  `--deep`（全量扫描）或 `--brute-passphrase`（口令枚举，很慢）。
* **调试器**（`qq_exporter/keydebug.py`）：解析 `wrapper.node` 定位 `nt_sqlite3_key_v2`，
  以调试方式启动 QQ，在函数入口断点处读出 `R8`（即口令指针）。需要退出 QQ 重登一次。

---

## 验证情况

当前功能进度、本次测试结果与公开发布隐私边界见 [项目进度与发布记录](<docs/PROJECT-STATUS.md>)。

使用合成数据库与虚构聊天样本进行离线验证：

```powershell
python tests/run_tests.py
python -m unittest discover -s tests -p "test_*.py"
python -m qq_chatbot eval --demo --dataset tests/fixtures/retrieval_cases.json --output _work/eval.json
```

覆盖 Protobuf 边界、SQLCipher 往返与错误密钥、JSON/TXT 导出、索引与作用域、时间线、长期记忆和管理接口。测试数量及通过结果以本次实际运行输出为准。

合成测试不代表真实 QQ WAL、所有数据库版本或云端模型效果已验收。各项实现与限制见 [Roadmap](<docs/ROADMAP.md>)，命令示例见 [使用指南](<docs/roadmap-usage.md>)。

---

## 大库 / 页损坏时的导出

`tools/export_large.py` 是针对超大库和「页级损坏」的容错导出：

```powershell
python tools/export_large.py --db plain.db --out out
```

当群聊表的 B-tree 因异常退出而损坏、游标正向/反向都过不去时，它会
**正向 + 反向分别按主键扫描并合并去重**，把能读到的消息尽量全部导出
（坏节点覆盖的那一小段会跳过并提示）。

---

## 目录结构

```
qq_exporter/
├── winmem.py      进程枚举与内存读取（只读，无注入）
├── keyfinder.py   密钥提取（盐值锚定 / 特征串 / 全量扫描 / 口令枚举）
├── keydebug.py    调试器路线（静态分析 + int3 断点读 R8）
├── peinfo.py      wrapper.node 的 PE32+ 静态分析
├── sqlcipher.py   SQLCipher 解密 + WAL 合并 + 保留区探测
├── wire.py        不依赖 schema 的 Protobuf wire 解析器
├── db.py          消息表读取与字段映射
├── collect.py     会话归类
├── exporters.py   JSON / TXT 导出
└── cli.py         命令行入口
tests/run_tests.py 自测套件
tools/             造样本库、大库容错导出
```

---

## 聊天机器人（qq-chatbot）

在导出结果之上，`qq_chatbot` 把聊天记录「提炼」成可对话的机器人，支持两种可切换模式：

* **数字分身（persona）**：模仿某个指定人的说话风格、口头禅、观点与事实。
* **历史问答（qa）**：面向聊天记录做检索式问答，回答带出处，查不到就明说。

```powershell
# 1) 构建索引（二选一：明文库 或 已有 JSON 导出）
.\.venv\Scripts\python.exe -m qq_chatbot build --db plain.db
.\.venv\Scripts\python.exe -m qq_chatbot build --json out/json

# 2) 聊天
.\.venv\Scripts\python.exe -m qq_chatbot chat --mode qa
.\.venv\Scripts\python.exe -m qq_chatbot chat --mode persona --person c2c:10001

# 3) 单次提问 / 人格档案 / HTTP 服务
.\.venv\Scripts\python.exe -m qq_chatbot ask "我们上次约饭是什么时候"
.\.venv\Scripts\python.exe -m qq_chatbot profile --person c2c:10001
.\.venv\Scripts\python.exe -m qq_chatbot serve --port 8000
```

默认 **hash 向量 + mock LLM 零额外依赖即可跑通**（便于演示与自测）。要接真实模型，写一个 `chatbot.yaml`：

```yaml
llm: { provider: ollama, model: qwen2.5:7b, base_url: http://localhost:11434 }
embedding: { provider: ollama, model: nomic-embed-text, base_url: http://localhost:11434, dim: 768 }
retrieval: { top_k: 8, vector_weight: 0.7, min_score: 0.35 }
```

也支持 OpenAI 兼容端点（`provider: openai`，配 `api_key`，可接通义/DeepSeek 等）。完整结构见 [`docs/chatbot-structure.md`](docs/chatbot-structure.md)。

> 注意：云端后端会把聊天内容发送到远程 API。默认开启**发送前脱敏**（`llm.redact: true`）——把语料里的真实昵称、QQ 号、手机号、邮箱替换成占位符再上云，回复后再还原昵称，云端拿不到真实身份；QQ 号/手机/邮箱不回填。本地 ollama 模式全程离线。索引与人格档案落在 `.chatbot/`，已被 `.gitignore` 排除。

### 接入 QQ（群聊 + 私聊）

机器人可以挂在 NapCat 的 OneBot11 WebSocket 上，作为**群聊 + 私聊**的问答/数字分身机器人：

```powershell
pip install websocket-client
.\.venv\Scripts\python.exe -m qq_chatbot qq --ws ws://127.0.0.1:3001
# 数字分身机器人：
.\.venv\Scripts\python.exe -m qq_chatbot qq --mode persona --person c2c:10001
```

行为：私聊直接回复；群聊默认 `@机器人` 才回复（可配 `group_trigger: all` 全量回复）；
群聊问答只检索本群记录；群内/私聊可用 `/qa`、`/persona <key>`、`/cite`、`/help` 切换。

> **读取与机器人分离部署**（Windows 导出 + Linux 主机跑机器人）见 [`deploy/README.md`](deploy/README.md)。

> persona 是「模仿口吻」，不是登录对方账号；冒充他人账号违反 QQ 协议，请用机器人账号承载。

### 获取数据库口令（NapCat 插件）

导出聊天记录需要数据库口令。内置了一个 NapCat 小插件，登录后自动打印并保存该口令：

```powershell
# 安装（把插件目录复制到 NapCat 插件目录）
robocopy tools\napcat-plugin-db-passphrase "<NapCat插件目录>\db-passphrase" /E
# 启动 NapCat，用「导出账号」扫码登录 → 控制台打印
#   [db-passphrase] 数据库口令 passphrase = XXXXXXXXXXXXXXXX
# 也会保存到 plugins\db-passphrase\passphrase.json
```

拿到口令后解密导出：

```powershell
.\.venv\Scripts\python.exe -m qq_exporter all --qq <导出账号> --passphrase "<16字符口令>" --out out
```

> **导出账号与机器人账号可以不同**：解密时必须使用与目标数据库匹配的口令，并检查解密结果；不要假定口令永久不变或一次获取后一直有效。机器人运行阶段只需索引和相应服务配置。

### 增量、评测、长期记忆与管理

当前实现提供时间戳增量同步、可持久化语义向量、离线检索评测、带来源的增量时间线、可选长期记忆、管理 API 以及群成员检查。长期记忆、追踪日志和群成员检查均默认关闭；HTTP `/chat` 没有可信用户身份，因此不启用长期记忆。

具体配置、`chat --session`、`/forget`、评测基线、导出 `--resume` 和管理 API 示例见 [Roadmap 使用指南](<docs/roadmap-usage.md>)。R6 已实现会话级导出恢复；真实 QQ WAL 兼容性仍待验收，解密续传与并发导出未实现。

---

## 风险与合规

* 解密的是**你自己账号、你自己电脑上**的数据；请勿用于他人设备或数据。
* 密钥、数据库、导出的聊天内容都属敏感信息，注意不要提交或外传。
  `.gitignore` 已排除 `_work/` 与 `out/`。
* 读取进程内存属于操作系统公开 API 的只读操作（`OpenProcess` + `ReadProcessMemory`），
  不注入 DLL、不 Hook；NapCat 属于第三方协议框架，**均可能违反 QQ 用户协议**，
  请自行判断并承担风险。
* 建议先复制一份 `nt_db` 目录再操作；导出结果请自行加密与备份。
