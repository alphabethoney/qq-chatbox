# qq-chatbox

把 Windows 版 **NTQQ** 的本地聊天记录导出为 **JSON / 纯 TXT**，支持**媒体下载**与**定时增量导出**。

纯 Python 实现，不需要编译 SQLCipher 扩展。解密密钥通过 **NapCat**（开源的 NTQQ 协议框架）在登录时自动截获，本工具负责「解密 → 解析 → 导出」。

---

## 为什么需要这个工具

新版 NTQQ 已经移除了旧版「消息管理器 → 导出消息记录」功能，设置里只剩「聊天记录备份与迁移」，那只是把数据在 QQ 内部搬运，产不出可读文件。

聊天记录本身也不是明文：

| 项目 | 说明 |
|---|---|
| 主库位置 | `<QQ数据目录>/<账号>/nt_qq/nt_db/nt_msg.db` |
| 加密方式 | SQLCipher（AES-256-CBC + HMAC-SHA1，PBKDF2-HMAC-SHA512/4000） |
| 文件结构 | 1024 字节明文头部 + 4096 字节页，每页尾部保留 48 字节 |
| 密钥 | 16 字符 ASCII 口令，**不落盘**，且**无法本地推导** |

关键结论（来自 [QQDecrypt](https://github.com/QQBackup/QQDecrypt) 的逆向研究与 NapCat 源码）：

> 数据库密钥是登录后，用数据库头里的 `key_meta` 发 OIDB `0xcde` 指令从服务器**换回来**的，账号固定、多次登录不变。因此单纯拿到数据库文件无法离线解密，必须借助一个已登录的 QQ 会话。

**NapCat 正好在登录时自动截获了这个口令**，这就是本项目推荐它的原因。

---

## 快速开始

### 第 1 步：用 NapCat 拿到密钥

安装 [NapCat](https://github.com/NapNeko/NapCatQQ)（Shell 模式即可，自带 QQNT 核心），
用目标账号扫码 / 快速登录。登录成功后 NapCat 会缓存数据库口令：

```js
const dbApi = core.apis.DatabaseApi;
const passphrase = dbApi.getPassphraseBuffer()?.toString('ascii'); // 16 字符 ASCII 口令
```

> 或者让 NapCat 直接解密成明文库，跳过本工具的 `decrypt`：
> ```js
> const plain = dbApi.decryptDatabase('nt_msg.db', 'D:/out/nt_msg_plain.db');
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
| `export` | 从明文库导出 JSON / TXT（可加 `--media` / `--incremental`） |
| `watch` | 定时增量导出（每 N 秒一轮，自动只导出新消息） |
| `all` | 取密钥 → 解密 → 导出，一步到位 |

常用参数：

* `--passphrase <口令>`：直接给 16 字符口令（NapCat 给出的就是它）；
* `--key <64位hex>`：直接给 32 字节 AES 密钥；
* `--names names.json`：会话名称映射，如 `{"group:123456": "项目群", "c2c:10001": "张三"}`；
* `--limit N`：只导前 N 条（调试用）；
* `--single-json`：JSON 合并为单文件；
* `--include-content`：JSON 里附带完整的 Protobuf 字段树（很大）；
* `--media`：下载图片/文件/语音/视频/表情到 `out/media/`；
* `--incremental`：增量导出（用 `out/.export_state.json` 记录进度）；
* `--since <消息ID>`：只导出消息 ID 大于该值的消息；
* `--media-dir <nt_data目录>`：QQ 媒体缓存目录（默认从消息里的本地路径自动推导）。

---

## 媒体下载

`export --media`（或 `watch --media`）会把消息里引用的图片/文件/语音/视频/表情存到
`out/media/<会话>/`，查找顺序：

1. 消息里记录的本地缓存路径（原图）；
2. 本地缓存按 **MD5 兜底**（原图被 QQ 清理后，退而求其次拿缩略图）；
3. HTTP 下载（URL 里的 `rkey` 有时效，旧消息可能已过期）。

> 说明：QQ 会定期清理本地媒体缓存、下载 token 也会过期。对于已被清理的旧媒体，
> 完整恢复需要 NapCat 的**富媒体 API**（重新换 rkey），这超出了本工具的范围。
> 日常场景里，近期媒体 + 缩略图基本都能覆盖到。

## 定时 / 增量导出

```powershell
# 一次性增量：只导出上次之后的新消息
.\.venv\Scripts\python.exe -m qq_exporter export --db plain.db --incremental

# 定时增量：每 1 小时自动跑一轮，只导出新消息
.\.venv\Scripts\python.exe -m qq_exporter watch --db plain.db --interval 3600 --media
```

增量进度记录在 `out/.export_state.json`；删除它即可重新全量导出。
配合 QQ 持续写入时，每轮只需重新解密一次（密钥已缓存）即可拿到新消息。

---

## 原理

### SQLCipher 解密（`qq_exporter/sqlcipher.py`）

* 去掉开头 1024 字节的 NTQQ 自定义头部；
* 口令经 `PBKDF2-HMAC-SHA512(口令, 盐, 4000)` 派生为 32 字节 AES 密钥，盐值明文存放在第 1 页开头；
* 保留区大小**自动探测**（拿第 1 页试几个候选值，看哪个能还原出合法 SQLite 页头）；
* `-wal` 里尚未落盘的页面也会被解密合并，不必为了最新消息先退出 QQ；
* 输出前用 `PRAGMA quick_check` 自检。

一个实测出的坑：SQLite 头部的「保留区字节」（偏移 20）**不等于** SQLCipher 的保留区大小
（实测分别是 `80` 和 `48`）。若强行把两者改成相等，SQLite 算出的 `usableSize` 会偏大，
读取溢出页时就会读坏。因此本工具保留解密出来的原值。

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

`python tests/run_tests.py`（46 项，全部通过）覆盖：

* Protobuf wire 解析边界（varint 极值、深层嵌套、repeated、二进制过滤）；
* SQLCipher 往返：自造真实格式的加密文件 → 解密 → 逐字节比对，
  含保留区探测、HMAC 校验、错误密钥必须失败；
* 导出链路：会话归类（私聊/群聊、收/发方向）、JSON/TXT 产物、名称映射、异常输入不崩溃。

真实数据验证细节与私人语料规模不公开；使用授权数据自行验证。`global` 目录下的 `login.db` / `sign.db`
用硬编码口令 `BD156D6710D54D8782F4` 即可离线解开（该口令公开在 `wrapper.node` 中）。

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

## 风险与合规

* 解密的是**你自己账号、你自己电脑上**的数据；请勿用于他人设备或数据。
* 密钥、数据库、导出的聊天内容都属敏感信息，注意不要提交或外传。
  `.gitignore` 已排除 `_work/` 与 `out/`。
* 读取进程内存属于操作系统公开 API 的只读操作（`OpenProcess` + `ReadProcessMemory`），
  不注入 DLL、不 Hook；NapCat 属于第三方协议框架，**均可能违反 QQ 用户协议**，
  请自行判断并承担风险。
* 建议先复制一份 `nt_db` 目录再操作；导出结果请自行加密与备份。
