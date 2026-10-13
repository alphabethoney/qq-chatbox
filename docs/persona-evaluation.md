# Persona 评测

这套评测提供固定的 16 条 dev + 8 条 holdout，默认离线运行实际 `Chatbot.answer` 编排，对照 `persona_inject_retrieval=False/True`。自动分只衡量策略契约，不能证明回复像真人。五个人工维度默认全部 `null`，报告明确显示 `UNSCORED`，没有总体验通过结论。

所有人物、地点、经历都是虚构。dev 的岚舟和 holdout 的禾汀使用独立档案与不同问题。每条固定 profile/history/retrieved/query/rubric，不抽样真实聊天记录，不读取真实索引；直接实例化 `Chatbot(corpus=None)`，使用匹配 `profile_cache_key(person, kind, conversation)` 的内存档案，以及内存 session/retriever。禁用 memory 和 observability，不调用 `load_chatbot`，不读写 `.chatbot`，不修改 `prompt.py`。

## 一键离线对照

从仓库根目录运行，输出目录必须由使用者显式指定，为仓库 `tools/` 或 `tests/` 下的**新目录**，禁止仓库外、UNC 和 `.chatbot` 路径。推荐统一使用已忽略的 `tools/persona_eval_results/`。工具拒绝覆盖已有目录；重复运行请换名称。使用 `-B` 避免 Python 导入产生其他目录的缓存文件。

```powershell
.venv\Scripts\python.exe -B tools/persona_eval.py run --output tools/persona_eval_results/dev-v1 --label baseline
```

默认只打开 dev 文件。输出 `run.json`（固定输入、实际发送模型的 messages、回复、citation、grounded、逐项检查、未评分人工维度、输入 SHA256）和 `report.md`。每场景每策略 7 项检查：单次生成、问题与历史、缓存档案、注入与阈值、出处、grounded、检索 scope。dev 共 `16 × 2 × 7 = 224` 项；契约失败返回退出码 1，并列出失败项。

开关关闭时所有 retrieved 都不注入，出处为空且 `grounded=False`；开启时仅分数 `>= .35` 的片段进入模型，最多五条出处，grounded 只表达存在合格证据，不认证事实。候选检索仍可执行，不能把“关闭注入”误称“未检索”。

冻结实现和提示词后，可显式运行独立留出集或一次运行全部：

```powershell
.venv\Scripts\python.exe -B tools/persona_eval.py run --split holdout --output tools/persona_eval_results/holdout-frozen-v1
.venv\Scripts\python.exe -B tools/persona_eval.py run --split all --output tools/persona_eval_results/all-frozen-v1
```

`all` 的报告分别列出 dev 224 项、holdout 112 项，总计 336 项。两套原始结果分别写到 `dev/run.json` 和 `holdout/run.json`。holdout 应只在候选方案冻结后使用，不用于 dev 运行、改词或调参；查看留出集后继续调参，应记录污染并为下一轮另建留出集。该工具不负责自动阻止人查看文件，默认路径隔离负责防止意外加载。

离线回复是明确标注的契约占位字符串。即使所有自动检查通过，也不意味着贴题、像本人、自然长度、事实归属或连贯通过。

## 导入实际回复

无需联网也可对已有模型结果做盲评。创建 UTF-8 JSON，键为所选 split 每个 `case_id:policy`，值为非空回复，必须完整包含 off/on 两份：

```json
{
  "dev-01:off": "在呢，怎么啦？",
  "dev-01:on": "在呀。"
}
```

上面仅示意两项，实际 dev 文件需要 32 项，holdout 需要 16 项。文件应保存在本地指定的评测目录，不放真实个人聊天内容。

```powershell
.venv\Scripts\python.exe -B tools/persona_eval.py run --mode import --replies tools/persona_eval_results/import-input/replies.json --label model-A --output tools/persona_eval_results/import-A
```

导入仍运行本地 Chatbot 的离线编排契约；导入文字替换展示回复。**这验证不了导入回复确实由该编排生成**，报告的 `contract_source` 明确区分这一点。导入不支持 `--split all`，请分别导入两套，保持边界。

## 显式真实 LLM（可选）

只有 `--mode llm` 才创建真实 provider。必须显式给 provider、model、base URL；密钥仅从指定环境变量读取，不写入结果，不加载生产配置。工具本身不自动联网、不执行服务器运维操作；真实运行会向指定模型端点发送本套虚构输入。此次开发验证没有运行此模式。

```powershell
$env:PERSONA_EVAL_API_KEY = 'your-key'
.venv\Scripts\python.exe -B tools/persona_eval.py run --mode llm --provider openai --model your-model --base-url https://your-endpoint/v1 --temperature 0.4 --max-tokens 1024 --label model-A --output tools/persona_eval_results/llm-A
```

支持 `openai` 与 `ollama` 后端；使用仓库现有 provider 实现，不自动安装依赖。结果记录模型端点、模型名、temperature/max_tokens 与输入 hash；随机模型即使输入相同也可能产生波动，比较时应固定模型参数并保留多次独立结果。

## 人工盲评分与比较

可将一个运行中的 off/on 对照，或多个同 split、同输入 hash 的运行一起打乱。输出 `blind.json` 和 `private-key.json`。只把前者交给评分者；后者包含运行名称与策略映射，应由组织者保管。固定 seed 确保顺序可复现；真实盲评不应把 seed、来源报告或映射交给评分者。

```powershell
.venv\Scripts\python.exe -B tools/persona_eval.py blind --runs tools/persona_eval_results/import-A/run.json --output tools/persona_eval_results/blind-A
```

也可用 `--runs run-A.json run-B.json` 比较多组候选。盲评卡保留 profile、query、history、**实际可用 evidence** 和场景 rubric，隐藏运行名、模型名与策略标签。证据可用性或回复措辞可能使评分者推测策略，这属于此盲法限制；不应为了隐藏条件剥夺事实归属判断所需的输入。关闭策略没有的证据，不应被要求当作已知事实回答。

只编辑 `blind.json` 每卡的 `scores` 和 `notes`，保持内容与顺序。每维用整数 0/1/2，未评保持 `null`：

| 维度 | 0 | 1 | 2 |
|---|---|---|---|
| 贴题 | 未回应或被无关检索带跑 | 部分回应、有明显绕行 | 直接回应当前意图 |
| 像本人 | 与档案风格明显不符、客服腔 | 风格部分符合、口头禅生硬 | 与该虚构档案风格一致，表达自然 |
| 自然长度 | 明显啰嗦或短到无法回应 | 略长或略短 | 适合当前请求，详细请求可展开 |
| 事实归属 | 把用户/第三人经历当本人，或编造事实 | 有含糊归属或过度断言 | 忠于可用档案/历史/证据，未知自然说明 |
| 连贯 | 遗忘指代、纠正或当前话题 | 承接不完整 | 连贯承接历史、纠正与话题变化 |

“像本人”只表示**虚构档案的风格一致性**，不能直接认定真人分身改善。历史 assistant 回复是生成内容，不能自动升级为被证实的本人经历。建议至少两位评分者独立填写并讨论分歧；工具一次汇总一个评分者的表，不自动计算评分者间一致性。

```powershell
.venv\Scripts\python.exe -B tools/persona_eval.py score --sheet tools/persona_eval_results/blind-A/blind.json --key tools/persona_eval_results/blind-A/private-key.json --output tools/persona_eval_results/scored-A
```

报告逐运行/策略/维度显示均分（满分 2）、已评分数量/总数；空维度明确 `UNSCORED`。并以第一个运行的 off 为基准，计算只在双方共同评分场景上的配对差值与样本数。没有共同分数就不生成改善结论，不把缺失项当 0 或通过。不同 split 或输入 hash 的运行不能直接混入一份盲评报告。自动契约分与人工分分开保存，不能互相代替。

## 工具测试

```powershell
.venv\Scripts\python.exe -B tests/test_persona_eval.py
```

5 项小型集成测试覆盖独立 split 加载、真实 Chatbot 阈值/缓存/历史/生成参数、盲评未评分统计与内容篡改拒绝，以及输出目录保护。测试只在仓库 tests 下创建并清理临时评测目录，不接触真实数据与索引。
