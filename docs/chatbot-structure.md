# 基于聊天记录提炼聊天机器人（qq-chatbot）结构

本文描述在 `qq-chat-exporter`（导出插件）之上新增的 `qq_chatbot` 包的结构与设计。
聊天机器人「提炼」自导出的聊天记录，支持两种可切换模式：

1. **数字分身（persona）**——模仿某个指定人的说话风格、口头禅、观点与事实，让机器人像 TA 一样聊天。
2. **历史问答（qa）**——面向聊天记录做检索式问答，回答「我们当时聊过什么」类问题，忠于原文并给出来源。

两种模式共享同一套「导入 → 归一化 → 索引 → 检索 → 生成」底座，只在提示词与人格配置层区分。

---

## 1. 总体架构与数据流

```
加密 nt_msg.db ──(复用 qq_exporter.sqlcipher.decrypt_database)──▶ plain.db
plain.db ──(复用 qq_exporter.collect.build_conversations)────────┐
                                                                 ▼
已有 JSON 导出 out/json/*.json ──(直接解析)─────────────────▶ Corpus（Speaker + Turn）
                                                                 │
                    ┌────────────────────────────┬───────────────┘
                    ▼                            ▼
            turns.py 归一化/分轮/过滤      profile.py 人格提炼
            （去非文本、合并连续同人、    （风格/口头禅/事实/关系）
              解析说话人、切 Turn）              │
                    │                            │
                    ▼                            ▼
            index/ 向量化 + BM25 + 元数据 ──▶ 持久化到 .chatbot/
                    │
        query ──▶ retriever（混合检索 + 过滤）──▶ top-K 证据轮
                    │
                    ▼
        prompt.py（persona / qa 两套模板）──▶ llm.generate ──▶ 回复(+出处)
```

---

## 2. 目录结构

聊天机器人是独立于 `qq_exporter` 的新包，避免给导出器引入 LLM 依赖：

```
qq_chatbot/
├── __init__.py          # __version__
├── __main__.py          # python -m qq_chatbot 入口
├── cli.py               # 子命令：build / chat / ask / profile / serve
├── config.py            # 配置 dataclass + 文件加载 + 环境变量覆盖
├── schema.py            # 规范数据模型：Speaker / Turn / Corpus / PersonaProfile / Answer
├── ingest.py            # 从 plain.db 或 JSON 目录装载成 Corpus
├── turns.py             # 归一化、说话人解析、连续合并、分轮/切块、过滤
├── profile.py           # 人格档案提炼（离线统计 + 可选 LLM 摘要）
├── prompt.py            # persona / qa 两套提示词模板与拼装
├── session.py           # 对话记忆（短期环形缓冲 + 长期摘要）
├── chat.py              # 交互式 REPL 与 Chatbot 编排
├── serve.py             # 可选 HTTP API（FastAPI）
├── llm/
│   ├── base.py          # LLMProvider / EmbeddingProvider 抽象
│   ├── openai.py        # OpenAI 兼容客户端
│   ├── ollama.py        # 本地 ollama 客户端
│   ├── mock.py          # 确定性 mock（自测）
│   └── registry.py      # 由配置字符串解析后端
├── index/
│   ├── base.py          # Retriever / VectorStore 抽象
│   ├── embeddings.py    # 向量化后端（hash / ollama / openai）
│   ├── store.py         # 内存向量 + BM25 倒排索引 + 持久化
│   └── hybrid.py        # 混合检索 + 过滤（人/会话/时间）
└── eval/
    ├── __init__.py
    └── metrics.py       # 召回/忠实度/风格相似度评测
```

---

## 3. 规范数据模型（schema.py）

```python
@dataclass
class Speaker:
    key: str            # "c2c:10001" 或 "group:123456/u_alice"；本人为 "me"
    name: str           # 解析后的显示名
    is_self: bool

@dataclass
class Turn:
    id: str
    speaker: Speaker
    conversation_key: str   # "c2c:10001" / "group:123456"
    kind: str               # "c2c" | "group"
    time: int
    time_str: str
    text: str
    msg_ids: list           # 该 Turn 覆盖的原始消息 ID（用于出处）

@dataclass
class Corpus:
    speakers: dict[str, Speaker]
    turns: list[Turn]
    meta: dict

@dataclass
class PersonaProfile:
    speaker: str
    style: dict        # tone / catchphrases / emojis / avg_sentence_len
    facts: list[dict]  # {topic, fact, evidence}
    opinions: list[dict]
    relationships: list[dict]
    topics: list[str]
```

---

## 4. 模块职责与关键接口

### 4.1 ingest.py —— 输入装载（复用导出插件）

```python
def load_corpus(*, db=None, json_dir=None, names=None,
                limit=None, since=None, people=None, progress=None) -> Corpus
```
- `db` 路径走 `qq_exporter.collect.build_conversations`（内部复用 `db.iter_messages` + `wire.parse_body`）。
- `json_dir` 路径读 `index.json` + 各会话文件，把 `messages[]` 还原成同一套消息字典。
- 两条路径都产出统一 `Corpus`。

### 4.2 turns.py —— 归一化与分轮

```python
def build_turns(conversations, *, merge_gap_s=300, max_turn_chars=1200) -> list[Turn]
```
- 过滤：`text` 为空的非文本消息（图片/文件/语音）默认不入索引，仅保留 `（非文本）` 占位供统计。
- 说话人解析：群聊用 `sender_uid/sender_qq`，私聊用方向+`peer`；`is_self` 用 `direction == "out"`。
- 合并：同一会话同一人、间隔 ≤ `merge_gap_s` 的连续消息合并为一个 `Turn`；超长 Turn 按 `max_turn_chars` 切块并带 overlap。

### 4.3 profile.py —— 人格提炼（persona 模式用）

```python
def build_profile(corpus, speaker_key, llm=None, progress=None) -> PersonaProfile
```
- 离线统计：词频/口头禅/表情/平均句长/高频话题（不依赖 LLM，永远可用）。
- 可选 LLM 摘要：对采样轮次做「风格 + 事实 + 关系 + 观点」结构化摘要。

### 4.4 index/ —— 索引与检索

```python
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class Retriever(Protocol):
    def retrieve(self, query, *, top_k=8, speaker=None, conversation=None,
                 kind=None, time_range=None) -> list[RetrievedTurn]
```
- `store.py`：内存向量矩阵（余弦）+ BM25 倒排索引（纯 stdlib），JSON 持久化到 `.chatbot/`。
- `hybrid.py`：向量分数与 BM25 分数加权融合（默认 0.7/0.3），叠加元数据过滤。
- 低于 `min_score` 的结果在 qa 模式视为「无证据」。

### 4.5 llm/ —— 可插拔后端

```python
class LLMProvider(Protocol):
    def chat(self, messages: list[dict], *, temperature=0.4, max_tokens=1024) -> str
```
- `openai.py`：OpenAI 兼容（`base_url` + `api_key`，可接通义/DeepSeek 等）。
- `ollama.py`：本机 `http://localhost:11434`，零 SDK 依赖（`urllib`）。
- `mock.py`：确定性回复，供离线自测。
- `registry.py`：`resolve(config)` 由 provider 字符串选择后端。

### 4.6 prompt.py —— 两套提示词

- **qa 模式**：系统提示「只依据给定证据回答，无法回答就说不知道」；`user` 拼入 top-K 证据（每条带出处）+ 当前问题。
- **persona 模式**：系统提示注入 `PersonaProfile`（风格+口头禅+事实+关系）+ 该人真实发言样例，要求以该人口吻回答；仍拼入证据以避免凭空编造。

### 4.7 session.py / chat.py / serve.py

- `session.py`：短期窗口保留最近 N 轮；超窗后做 LLM 摘要并入长期记忆。
- `chat.py`：`Chatbot` 编排（检索 → 拼提示 → 生成 → 返回带出处的 `Answer`），REPL 支持 `/persona X`、`/qa`、`/cite`。
- `serve.py`：可选 FastAPI，`POST /chat`、`GET /health`。

---

## 5. CLI 与配置

```powershell
python -m qq_chatbot build --db plain.db | --json out/json [--config chatbot.yaml]
python -m qq_chatbot chat --mode persona --person c2c:10001
python -m qq_chatbot chat --mode qa
python -m qq_chatbot ask "我们上次约饭是什么时候"
python -m qq_chatbot profile --person c2c:10001
python -m qq_chatbot serve --port 8000
```

`chatbot.yaml`（含默认值）：
```yaml
llm: { provider: mock, model: "", base_url: "", api_key: "", temperature: 0.4, max_tokens: 1024 }
embedding: { provider: hash, model: "", base_url: "", api_key: "", dim: 256 }
store: { type: memory, dir: .chatbot }
retrieval: { top_k: 8, vector_weight: 0.7, min_score: 0.35 }
session: { max_turns: 12 }
```
环境变量可覆盖（如 `QQ_CHATBOT_API_KEY`、`QQ_CHATBOT_LLM_PROVIDER`）。

---

## 6. 边界情况与失败模式

| 情况 | 处理 |
|---|---|
| 非文本消息（图片/文件/语音） | 按 `segments` 类型识别，默认不入索引、不计入分轮，仅留占位统计 |
| 群聊多说话人 | 说话人用 `sender_uid/sender_qq` 区分；persona 必须显式 `--person` |
| 连续同人刷屏 / 超长消息 | 合并与切块，避免单 Turn 撑爆上下文 |
| 时间戳毫秒/秒 | 沿用 `db._fmt_ts` 判据（>1e12 当毫秒） |
| 检索无结果 / 低分 | qa 如实回答「记录里没找到」，不编造 |
| 云端外传风险 | cloud 后端显式提醒数据将发送到远程 API；本地模式离线；`.chatbot/` 加入 `.gitignore` |
| 超大队列（大量消息） | `build` 流式 ingest，支持 `--limit`/`--since`/`--people` 减量；索引带 manifest |
| 依赖缺失 | 默认 hash 向量 + mock LLM 零依赖可跑通；启用 ollama/openai 时才需对应服务 |

---

## 7. 测试与验收

复用 `tools/make_fixture.py` 生成的明文样本库作为固定语料。

- **单元**：`turns` 分轮/合并/说话人解析/过滤；`prompt` 两套模板；`hybrid` 用确定性假向量验证过滤与阈值。
- **单元（mock LLM/Embedding）**：`profile` 结构化摘要、`qa`/`persona` 完整问答链路，全程不联网。
- **E2E**：`build --db _work/fixture_plain.db` → mock LLM 跑 `ask`/`chat`，断言检索命中预期原文、qa 无证据时拒绝回答。
- **验收**：`build` 后 `chat --mode qa` 回答带出处；`chat --mode persona --person c2c:10001` 风格接近样例且事实真实；本地 ollama 模式离线可用，OpenAI 兼容模式可连通。
