# qq-chatbot 结构与边界

`qq_chatbot` 在导出记录上提供数字分身（persona）与历史问答（qa）。persona 模仿发言风格，qa 基于检索证据回答；两者都不认证原始陈述或生成答案的现实真实性。本文示例身份均为虚构。

命令与配置见 [Roadmap 使用指南](<roadmap-usage.md>)；实施状态及未完成验收见 [ROADMAP](<ROADMAP.md>)。

## 1. 数据流

```text
一致的加密数据库快照 → 解密/检查 → 明文数据库 ─┐
已有 JSON 导出 ──────────────────────────────┤
                                             ↓
                         ingest / turns：规范化、说话人、分轮
                                             ↓
                     index：实际 embedding 向量 + BM25 + 元数据
                                             ↓
                        检索过滤 → prompt → LLM → Answer/引用
                                             ↑
          profile / timeline：来源校验、增量指纹、档案范围
          session / memory：短期历史、可选隔离长期摘录
                                             ↓
                  可选 observability：追踪元数据或显式内容
```

时间戳增量同步位于 [同步工具](<../tools/sync_index.py>)：累计快照与水位线只在成功后提交，仍需全量解密主库与重建索引。WAL 真实环境兼容性另行验收。

## 2. 模块目录

```text
qq_chatbot/
├── cli.py               # build / chat / ask / profile / eval / serve / qq
├── config.py            # dataclass、JSON/YAML、环境变量覆盖
├── schema.py            # Speaker / Turn / Corpus / PersonaProfile / Answer
├── ingest.py            # 明文库或 JSON 装载
├── turns.py             # 归一化、分轮、过滤
├── profile.py           # 风格、增量档案与备份
├── timeline.py          # 来源验证、显式事件时间、冲突状态
├── prompt.py            # QA/persona 提示词与事实边界
├── session.py           # 短期窗口、可选长期摘录注入
├── memory.py            # SQLite 摘录、衰减、过期与删除
├── observability.py     # 可选 JSONL 追踪与轮换
├── chat.py              # Chatbot 编排与 CLI REPL
├── serve.py             # stdlib HTTP 与令牌保护的管理 API
├── bridge/
│   ├── onebot.py        # WebSocket 传输、成员列表请求与事件
│   └── policy.py        # 身份/范围、触发、命令、限流
├── llm/                 # mock / Ollama / OpenAI 兼容生成后端
├── index/
│   ├── embeddings.py    # hash / Ollama / OpenAI 向量
│   ├── store.py         # 向量/BM25、持久化与向量空间校验
│   └── hybrid.py        # 混合检索、范围过滤、可选 hash 回退
└── eval/
    ├── metrics.py       # recall / MRR 等指标
    └── runner.py        # 样本校验、演示语料、基线及人工评审汇总
```

## 3. 数据模型与来源

- `Speaker.key` 区分私聊/群聊身份；跨会话同人匹配使用解析出的 QQ 标识。
- `Turn` 保留会话 key、kind、说话人、文本、来源时间和 `msg_ids`；这些来源属性用于过滤与引用。
- `Corpus` 包含说话人、Turn 和元数据；当前索引及累计 JSON 会驻留内存，不承诺全链路流式或固定内存。
- `PersonaProfile` 保存风格与时间线，缓存按 speaker/kind/conversation 分隔；旧版 facts 等字段保持读取兼容，不能绕过新来源校验作为事实依据。
- `Answer` 包含正文、引用、grounded 与可选 trace_id。grounded 只标识是否有达阈值的检索依据，分数不是事实概率。

## 4. 索引与检索

[索引存储](<../qq_chatbot/index/store.py>) 保存 corpus、embeddings 与 manifest。元数据记录实际 provider/model/dim；加载时校验向量数量、有限值和空间匹配，避免将不同模型或 hash 与语义空间混用。

`HybridRetriever.retrieve()` 支持 speaker、person_qq、conversation、kind、time_range 和 top_k 过滤，融合向量与 BM25 分数。qa 没有超过阈值的证据时返回未找到记录；长期对话摘录不会代替语料证据使其自动 grounded。

默认 hash 与 mock 可离线运行。Ollama / OpenAI 的服务必须可用；`embedding.fallback_to_hash` 默认 false，显式开启时可整体切换到独立 hash 空间。回退改变检索效果，需要同一标注集对比。

## 5. 档案与时间线

[profile 模块](<../qq_chatbot/profile.py>) 离线统计风格与话题，可选 LLM 丰富风格；[timeline 模块](<../qq_chatbot/timeline.py>) 对来源 Turn、逐字引用、消息 ID 与事件日期进行校验，按指纹增量处理变化来源。

来源时间表示消息发送时间；事件时间只有原文明示日期时才保存。不确定、转述、假设、历史、被替代、冲突、来源缺失等条目保留状态，不把最新陈述自动认证成当前事实。来源可追溯不代表内容真实。

`profile --kind c2c|group` 选择风格类型，`--conversation` 才是精确来源会话边界。档案更新采用临时文件和原子替换，旧档案保留在 `profiles/history/`。备份不自动删除，用户需管理其保留与权限。

## 6. Session 与长期记忆

```python
store = MemoryStore(".chatbot/memory.sqlite3")
session = Session(12, memory_store=store, scope="trusted-unambiguous-scope", memory_top_k=4)
session.add("虚构用户陈述", "虚构 bot 输出")
messages = session.as_messages(query="虚构查询")
store.delete("trusted-unambiguous-scope")
```

存储的用户陈述与 bot 输出分字段保存，仅作未经事实认证的摘录。检索按字面相关性乘半衰期衰减，过期时物理删除，scope 内有数量上限。SQLite 每次操作独立事务，支持重启与并发写入；长期摘录以 user 优先级注入，不提升为 system 指令。

`Session()` 不传 store 时保持原短期接口。应用层 `memory.enabled` 默认 false；CLI 使用显式 `chat --session`，QQ 由可信事件身份构造 scope。`/forget` 清理当前身份/模式长期记忆和短期历史。HTTP `/chat` 与单次 `ask` 不建立长期 Session；请求自报身份不构成授权。

## 7. QQ 桥接与范围

[OneBot 传输](<../qq_chatbot/bridge/onebot.py>) 通过 `websocket-client` 接收事件，[策略模块](<../qq_chatbot/bridge/policy.py>) 决定是否回复及可检索范围。

- 私聊直接响应；群聊默认 `@机器人` 触发。
- `restrict_group_scope: true` 对群聊 qa、persona、档案来源、`/search` 和显式参数均限制本群；`/kind` 或提及不能绕过范围。
- 会话按机器人、群/私聊和提问者隔离。切换 mode/person/kind 清理当前引用和短期上下文，长期 scope 随模式区分。
- `group_member_check` 默认 false；开启后成员未知或缓存过期拒绝受保护请求，并请求成员列表供稍后重试。
- 成员缓存支持 TTL、请求超时和成员变更失效；真实服务端的事件兼容性待部署验收。

```yaml
bridge:
  ws_url: ws://127.0.0.1:3001
  mode: qa
  group_trigger: at
  restrict_group_scope: true
  rate_limit_per_min: 30
  group_member_check: false
  group_member_cache_ttl: 300
  group_member_request_timeout: 10
```

## 8. HTTP、追踪与管理

HTTP 使用 `ThreadingHTTPServer`，提供 `GET /health`、`POST /chat`。管理接口为令牌认证的 `GET /admin/traces`、`GET /admin/retrieval`、`POST /admin/retrieval`；无管理令牌拒绝请求。管理令牌不为 `/chat` 建立终端用户身份。

追踪默认关闭；启用后默认仍不记录问题/答案/证据正文。显式 `include_content: true` 才存内容。HTTP 问答与检索参数在线更新使用锁保持一致，更新仅在进程中生效，不写回文件。追踪日志通过 SQLite 跨进程锁协调读写与轮换，支持跳过损坏记录及隔开不完整尾行；损坏内容不会被还原。没有 Web 管理界面。

## 9. 配置默认值与验证

```yaml
llm: {provider: mock, model: "", base_url: "", api_key: "", temperature: 0.4, max_tokens: 1024}
embedding: {provider: hash, model: "", base_url: "", api_key: "", dim: 256, fallback_to_hash: false}
store: {type: memory, dir: .chatbot}
retrieval: {top_k: 8, vector_weight: 0.7, min_score: 0.35}
session: {max_turns: 12}
memory: {enabled: false, half_life_days: 30, ttl_days: 180, max_entries: 500, top_k: 4}
observability: {enabled: false, include_content: false, max_bytes: 5000000, admin_token: ""}
```

通过 `--config` 指定配置。环境变量可覆盖模型配置与管理令牌，例如 `QQ_CHATBOT_EMBEDDING_MODEL`、`QQ_CHATBOT_ADMIN_TOKEN`。YAML 需要 PyYAML，JSON 不需要该依赖。

```powershell
python tests/run_tests.py
python -m unittest discover -s tests -p "test_*.py"
python -m qq_chatbot eval --demo --dataset tests/fixtures/retrieval_cases.json --output _work/eval.json
```

测试使用虚构数据与 mock 后端，不联网。它们验证处理逻辑和接口边界；真实 QQ WAL、模型的语义效果、人格真实性和部署端兼容性必须单独验收。`eval --reviews` 仅汇总人工评分，不能代替事实认证。
