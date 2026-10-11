# Roadmap 功能使用指南

本文所有账号、姓名、对话和路径均为虚构示例或占位符；从项目根目录执行命令。尖括号内容需替换，不是可直接运行的实际账号或密钥。实现状态与验收限制见 [ROADMAP](<ROADMAP.md>)。

YAML 配置需要 PyYAML；也可使用等价 JSON。CLI 不会自动读取当前目录配置，请显式传 `--config chatbot.yaml`。默认配置使用 mock LLM 与 hash 向量；设置模型相关环境变量会覆盖文件配置，离线演示前应检查自己的环境。

## R1：时间戳增量同步

对已解密的一致快照，仅导出某 Unix 秒时间戳之后的消息：

```powershell
python tools/export_large.py --db _work/fictional_plain.db --out _work/incremental --format json --since 1700000000 --strict
```

注意参数语义：[大库导出工具](<../tools/export_large.py>) 的 `--since` 和 `qq_chatbot build --since` 都是 Unix **秒时间戳**；`qq_exporter export --since` 是**消息 ID**，不能互换。

首次与后续同步使用相同命令：

```powershell
python tools/sync_index.py --live-db "<一致快照中的nt_msg.db>" --passphrase-file "<口令文件>" --base-json out/json --work _work/sync --out .chatbot --overlap-seconds 300 --config chatbot.yaml
```

可加 `--person <虚构示例对应的目标QQ号>` 只为一个人构建索引。默认重读上次水位之前 300 秒，按会话与消息 ID 去重；窗口最少 1 秒。状态与累计快照保留在索引目录，不能只保留水位线而删除其引用的快照。数据源或关键参数变化会触发全量读取。

补回窗口之外的迟到消息：

```powershell
python tools/sync_index.py --live-db "<一致快照中的nt_msg.db>" --passphrase-file "<口令文件>" --base-json out/json --work _work/sync --out .chatbot --config chatbot.yaml --full
```

当前仍全量解密主库、合并累计数据和重建索引。`--since` 只减少消息读取/导出范围，不减少加密主库解密量；需预留明文库、累计快照和新索引空间。同步跳过 WAL，不能保证看到尚未写入主库的消息。失败不推进本地水位线；若使用可选 `--push --host <SSH目标>`，远端传输/重启需独立检查。

## R2：语义向量与可选 hash 回退

配置示例：

```yaml
embedding:
  provider: ollama
  model: nomic-embed-text
  base_url: http://127.0.0.1:11434
  dim: 768
  fallback_to_hash: false
store:
  dir: .chatbot
```

```powershell
python -m qq_chatbot build --json out/json --config chatbot.yaml
python -m qq_chatbot ask "虚构项目的午餐聚会安排" --config chatbot.yaml
```

模型须已在所选服务中可用。OpenAI 兼容 embedding 可改为 `provider: openai`，设置该服务的 `model`、`base_url` 和 `QQ_CHATBOT_EMBEDDING_API_KEY`；云端向量化会发送所索引的文本。

索引保存**实际使用**的 provider/model/dim 与向量。查询机需使用兼容配置；仅修改维度或模型名不会转换已有向量，出现向量空间不匹配应重建索引。Ollama 后端根据返回向量确定实际维度，索引元数据以实际值为准。

显式设置 `fallback_to_hash: true` 后，后端故障可退回完整 hash 向量空间。语义向量与 hash 备用向量分别保存/检索，不会直接交叉比较。构建期间退回 hash 的索引应按实际元数据理解；回退不代表语义质量保持不变。默认 hash 模式零网络，默认回退开关为 false。

## R3：离线评测与基线

使用内置虚构语料和 [标注集](<../tests/fixtures/retrieval_cases.json>)：

```powershell
python -m qq_chatbot eval --demo --dataset tests/fixtures/retrieval_cases.json --output _work/eval.json
python -m qq_chatbot eval --demo --dataset tests/fixtures/retrieval_cases.json --output _work/eval-next.json --baseline _work/eval.json
```

报告包含 hit@k、recall@k、MRR、逐样本结果、数据集及语料指纹、实际 embedding provider/model/dim，并记录每个查询实际使用的 embedding 以识别回退。对比必须使用相同标注集、k 和样本数量；同时核对语料与向量空间是否符合本次比较目标。配置变化可通过 `--config` 传入。省略 `--demo` 会加载自己的持久化索引，应使用与该索引 Turn ID 对应的自建标注集，不能直接套用演示 ID。

标注 JSON 结构示例：

```json
[
  {
    "query": "虚构项目何时开会",
    "gold_turn_ids": ["fictional-meeting-turn"],
    "filters": {"conversation": "group:100", "kind": "group"}
  }
]
```

人工人格评审另存为 JSON 数组，例如 `_work/reviews.json`：

```json
[
  {"case": "fictional-review-1", "style": 0.8, "factuality": 1.0, "timeline": 0.9},
  {"case": "fictional-review-2", "style": 0.6, "factuality": 0.5, "timeline": 0.7}
]
```

```powershell
python -m qq_chatbot eval --demo --dataset tests/fixtures/retrieval_cases.json --reviews _work/reviews.json --output _work/eval-reviewed.json
```

`case` 必须是唯一字符串，评分为 0..1。评分示例仅说明格式，不代表任何模型测得的效果。`--reviews` 只汇总人工评分；检索指标和人工均分都不能当成自动事实认证。

## R4：增量人格档案与来源时间

```powershell
python -m qq_chatbot profile --person c2c:10001 --kind c2c --conversation c2c:10001 --config chatbot.yaml
python -m qq_chatbot profile --person group:100:10001 --kind group --conversation group:100 --config chatbot.yaml
```

以上账号均为虚构；请换成索引中的说话人和会话 key。`--kind` 选择风格样本类型；若只允许某个会话作为知识来源，还必须提供 `--conversation`。不指定会话时可能使用该人在多个私聊/群聊中的发言。

再次运行会复用已有档案并按来源指纹处理新增/变化条目。时间线包含来源 Turn/消息 ID 与逐字引用；**来源消息时间**表示何时说过，**事件时间**只在原文明示日期并通过校验时填写。缺少明示日期时保留空值，不把「某天发消息说以前搬家」转换成「那天搬家」。抽取置信度、来源可追溯性与现实真实性是不同概念。

旧条目可能成为历史、被替代、冲突、不确定或来源变更状态；不能只取最新一条就断言当前事实。档案按 person/kind/conversation 分开保存；覆盖前在 `profiles/history/` 留存旧版本，历史备份不自动清理。备份同样含敏感资料，应纳入自己的保留策略。离线统计可用；有内容的时间线抽取需要所配置 LLM，模型效果另行验收。

## R5：长期记忆（默认关闭）

默认值：

```yaml
session:
  max_turns: 12
memory:
  enabled: false
  half_life_days: 30
  ttl_days: 180
  max_entries: 500
  top_k: 4
```

需要时将 `enabled` 改为 true。CLI 还必须显式指定本地身份：

```powershell
python -m qq_chatbot chat --mode persona --person c2c:10001 --session fictional-local-user --config chatbot.yaml
```

同一配置、身份和模式下重启可读取长期摘录；不同身份或模式隔离。不传 `--session` 时 CLI 只用短期历史。这个字符串是本地操作人员选择的隔离键，不是公共服务的登录认证。

QQ 桥接自动按机器人、群/私聊、提问者及当前模式等构造 scope，不需手写 `--session`。CLI 或 QQ 输入：

```text
/forget
```

它清除**当前身份和模式**的长期记录及当前短期上下文，不删除原始聊天索引、其它模式/群的记忆或档案备份。HTTP `/chat` 与单次 `ask` 没有长期 Session；HTTP 没有可信用户身份，不会因请求中自报一个用户名而共享长期记忆。

记忆每轮立即事务保存，用户陈述和 bot 输出分别存储，并明确声明未经核实、bot 内容不是事实证据。字面相关性乘时间半衰期权重，过期条目在读写时淘汰；存储 API 另有 `prune()` 和 `delete(scope, memory_id=None)`。SQLite 文件位于索引目录，属于本地明文数据；删除不承诺磁盘介质安全擦除。

## R6：WAL 与恢复验收边界

流式解密和容错导出已存在。WAL 修复通过标准 SQLite 与合成加密样本测试，真实 QQ 版本及持续写入场景尚未验收，不能据此宣称运行中 QQ 无需一致快照。

首次导出就加 `--resume`；中断后原样重跑：

```powershell
python tools/export_large.py --db _work/fictional_plain.db --out _work/resumable --format json,txt --strict --resume
```

checkpoint 保存在输出目录的 `.export-checkpoint.json`。恢复校验源 DB/WAL/journal 指纹、导出参数和完成会话产物 digest；源/参数变化会拒绝恢复，产物损坏或缺失会重写。换源或改变参数时使用新的输出目录，坏页残缺导出不登记成功。

它只跳过已完成且校验有效会话的序列化与写盘，仍会全源哈希，扫描、解析、分桶和排序所有匹配消息，不保证更快或节省内存。解密断点续传与并发导出未实现。`--strict` 遇读取失败即终止，适用于不允许残缺数据推进同步水位线的场景。

## R7：追踪日志与管理 API

```yaml
observability:
  enabled: true
  include_content: false
  max_bytes: 5000000
  admin_token: ""
```

默认 `enabled: false`。启用后默认只记录元数据、证据引用摘要和分数，不记录问题、回答与证据正文。只有明确设置 `include_content: true` 才记录内容。日志使用跨进程 SQLite 锁协调写入与大小轮换，读取时跳过损坏记录、追加时隔开不完整尾行。HTTP 问答与管理更新通过锁保持配置一致。不要将检索分数或 `grounded` 当成事实正确概率。

启动服务并通过环境变量提供管理令牌：

```powershell
$env:QQ_CHATBOT_ADMIN_TOKEN = "<自行设置的管理令牌>"
python -m qq_chatbot serve --host 127.0.0.1 --port 8000 --config chatbot.yaml
```

在另一个终端发请求（该终端也需有相同令牌）：

```powershell
$headers = @{ Authorization = "Bearer $env:QQ_CHATBOT_ADMIN_TOKEN" }
Invoke-RestMethod -Uri http://127.0.0.1:8000/admin/traces -Headers $headers
Invoke-RestMethod -Uri http://127.0.0.1:8000/admin/retrieval -Headers $headers
Invoke-RestMethod -Uri http://127.0.0.1:8000/admin/retrieval -Method Post -Headers $headers -ContentType application/json -Body '{"top_k":6,"vector_weight":0.6,"min_score":0.3}'
```

未配置令牌或认证失败时管理接口返回 403。可调整字段仅 `top_k`（1..100 整数）、`vector_weight` 和 `min_score`（0..1 有限数值）。调整只影响当前进程，响应包含 `persisted: false`，重启恢复配置文件值；需要持久化时手动修改配置。管理令牌不为公共 `/chat` 提供用户身份或长期记忆授权。当前没有 Web 管理界面。

## R8：群成员检查与作用域

```yaml
bridge:
  restrict_group_scope: true
  group_member_check: false
  group_member_cache_ttl: 300
  group_member_request_timeout: 10
```

成员检查默认关闭。启用后，桥接请求 OneBot 群成员列表并缓存；缓存过期、成员变化或请求超时会影响可用状态。成员身份未知或不在名单时拒绝受保护的群聊请求，不能用失败回退放宽范围。

`restrict_group_scope: true` 对群聊 QA、persona、档案来源、`/search`、`/kind` 和显式检索参数均限制在本群。提及某成员只缩小检索范围，不允许越权扩展到其它群。虚构命令示例：

```text
/search 虚构项目会议 --conversation group:100 --kind group
/persona group:100:10001
/kind group
```

群号需与索引中的会话标识匹配；配置不能产生原本不存在的本群记录。私聊不是管理员认证入口，HTTP/CLI 也没有复用 QQ 的成员授权；这些部署边界需分别处理。
