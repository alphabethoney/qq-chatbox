# 选择性导出

选择性导出在数据库查询和正文解析前筛选会话、类型与时间，减少无关消息的读取、Protobuf 解析、媒体处理和输出。它不改变加密数据库的解密方式。以下账号、群号和路径均为虚构示例。

## 发现与选择会话

先查看明文数据库的会话清单：

```powershell
python -m qq_exporter conversations --db plain.db
python -m qq_exporter conversations --db plain.db --kind group --json
```

清单提供会话键、名称、消息数量和时间范围。会话键区分 `group:123456`（群聊）和 `c2c:10001`（私聊）；UID 回退会话应使用清单中的完整键。名称映射只影响显示，不用于匹配目标，以免重名选错。

```powershell
# 单个群
python -m qq_exporter export --db plain.db --conversation group:123456 --out out/selected-group

# 单个私聊，必要时明确本账号
python -m qq_exporter export --db plain.db --conversation c2c:10001 --self-qq 20002 --out out/selected-friend

# 多个会话，重复参数取并集
python -m qq_exporter export --db plain.db --conversation group:123456 --conversation c2c:10001 --self-qq 20002 --out out/selected-multiple

# 只导出群聊
python -m qq_exporter export --db plain.db --kind group --out out/groups
```

私聊必须同时收录对方发来的消息与本账号发给对方的消息。工具尝试从同一快照中的发送记录确定本账号；选择性私聊无法可靠确定身份时，要求 `--self-qq`，不会以猜测代替身份。仅筛选群聊不需要确定私聊身份。

## 时间区间

`--after` 为包含的下界，`--before` 为不包含的上界，即 `[after, before)`。参数支持 Unix 秒和 ISO 8601 日期时间；没有时区的日期时间按运行机器的本地时区解释。需要可复现边界时明确提供时区：

```powershell
python -m qq_exporter export --db plain.db --conversation group:123456 --after "2025-01-01T00:00:00+08:00" --before "2026-01-01T00:00:00+08:00" --out out/group-2025
```

源消息中秒与毫秒时间戳会按现有格式约定处理。非法时间、倒置区间和互相矛盾的类型/会话选择会报错。

注意旧参数 `--since` 的语义没有统一改名：

| 入口 | `--since` 含义 |
|---|---|
| `python -m qq_exporter export` / `watch` / `all` | 消息 ID 严格大于给定值 |
| `python tools/export_large.py` | 消息时间严格晚于给定 Unix 秒 |

这些起点与新增选择条件取交集。`--limit` 是匹配消息的调试上限，不是“每个会话最近 N 条”。

## 大库、媒体与增量

大库导出采用相同的会话和区间参数：

```powershell
python tools/export_large.py --db plain.db --conversation group:123456 --after "2025-01-01T00:00:00+08:00" --before "2026-01-01T00:00:00+08:00" --out out/large-selected --format json,txt --strict
```

普通 `export --media` 仅处理选中消息的媒体；大库工具仍只提供其既有 JSON/TXT 能力，不新增媒体下载。

```powershell
python -m qq_exporter export --db plain.db --conversation group:123456 --media --incremental --out out/group-incremental
python -m qq_exporter watch --db plain.db --conversation group:123456 --interval 3600 --out out/group-watch
```

`watch` 读取已有明文库，不负责刷新加密快照或解密缓存。`all` 可传递选择参数到导出步骤，但前面的密钥获取与解密仍按原逻辑运行。

输出目录保存选择范围标记，增量状态记录规范化的筛选条件及输出格式。切换会话、时间边界、来源、身份、JSON/TXT 格式或单文件 JSON 模式后，应使用新的输出目录；不能借用原目录的水位线跳过另一个会话或新格式所需的历史。旧版无范围状态只允许在兼容的无筛选模式使用。失败不能推进水位；媒体下载有失败时，本轮返回失败并保留旧水位供重试，已写出的文本产物可以保留。空结果会更新本次格式的索引为零消息，旧的未引用文件不会自动删除，应以当前索引为准。

`--limit` 仅供调试，不能与 `--incremental` 同用，以免截断结果后提交过高水位而永久跳过尚未导出的消息。

普通消息 ID 增量导出沿用原来的批次输出语义，不等同于机器人同步工具的累计历史合并。若需要累计索引，应使用相应同步流程；本轮不为机器人同步添加会话筛选参数。

大库 `--resume` 绑定源快照与全部选择参数；恢复时必须保持一致。它仍会计算全源指纹，并重新扫描匹配消息，仅跳过有效会话产物的序列化与写盘。

## 检查查询计划与实际成本

```powershell
python -m qq_exporter export --db plain.db --conversation group:123456 --explain-query
python tools/export_large.py --db plain.db --conversation group:123456 --explain-query

# 仅在已确认标准字段布局时启用，先检查查询计划
python -m qq_exporter export --db plain.db --conversation group:123456 --standard-layout --explain-query

# 标准布局 + 来源已有适用索引，可直接查询目标会话
python -m qq_exporter export --db plain.db --conversation group:123456 --standard-layout --out out/group-fast
```

查询计划检查不会解析正文、下载媒体、创建导出目录或更新状态。应针对已有明文库运行；`all` 涉及密钥获取与解密，不能作为只读查询计划入口。

- 默认兼容模式：为识别历史文本/字节标识，会先检查标识元数据，检查本身可能扫描整个表或索引。查询计划同时展示这些预检查，不会只展示最后的索引搜索。
- 确认来源使用标准整数/文本标识时，可显式加 `--standard-layout` 跳过标识兼容预扫描；私聊还应明确 `--self-qq`，避免本账号自动推断的元数据扫描。此模式依赖调用方确认布局，不用于未知字节编码、非规范数字文本等历史格式。切换模式使用新的输出目录。
- 兼容布局：无法直接可靠匹配标识时，先读取标识元数据定位行，再批量读取匹配正文。可能扫描元数据，但不把无关正文全部读入 Python。
- 查询计划出现 `SEARCH` 表示相应步骤使用索引搜索；出现 `SCAN` 仍可能扫描表或索引，不能仅凭指定了会话就认定 I/O 按目标消息数增长。会话查询会兼顾 QQ 与 UID，私聊还涉及发送者和对端；只有 QQ 列有索引时，UID 候选分支仍可能使 SQLite 选择扫描。以实际计划为准。
- 工具不向来源数据库或差分页明文缓存添加索引，也不承诺固定提速倍数。SQLite 读取页面时可能连带读取同页其它数据，元数据查询也不等于物理磁盘只读取几个字段。
- 首次全量解密、差分页缓存的全库指纹扫描、完整性检查，以及 resume 的源文件哈希，属于独立成本。会话列表统计本身也可能扫描元数据。
- 当前仍在内存中聚合和排序匹配消息。筛选可降低数据量，但选择一个很大的群仍可能需要较多内存。
