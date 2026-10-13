# 部署：Windows 读取机与 Linux 机器人主机

读取机负责取得一致数据库快照、解密、导出和构建索引；机器人主机运行模型调用与 QQ 桥接。以下账号、主机、路径和对话均为虚构示例或占位符，需替换为自己的授权环境，不包含实际聊天或本机信息。

功能命令见 [Roadmap 使用指南](<../docs/roadmap-usage.md>)，验收边界见 [ROADMAP](<../docs/ROADMAP.md>)。

## 1. 读取机：导出与构建

先用授权的导出账号登录 NapCat 获取匹配数据库的口令，参见 [主 README](<../README.md>)。不要假定口令跨登录或版本永久不变。

```powershell
python -m qq_exporter all --qq <导出账号> --passphrase "<数据库口令>" --out out
python -m qq_chatbot build --json out/json --config chatbot.yaml
```

源文件须是一致快照。WAL 合成测试不等于真实 QQ 持续写入场景已验证；时间戳同步目前跳过 WAL，尚未进入主库的消息可能暂时不可见。

索引核心产物为 `corpus.json`、`embeddings.json`、`manifest.json`。人格档案、记忆和日志按需生成，分别属于运行状态，不应在每次索引更新时被旧副本覆盖。

## 2. 搬运索引与增量同步

首次部署可将索引核心文件复制到主机的索引目录：

```powershell
scp .chatbot/corpus.json .chatbot/embeddings.json .chatbot/manifest.json "<SSH用户>@<主机>:<部署目录>/.chatbot/"
```

首次复制前在主机创建目标目录；运行中的多文件替换不是整体原子发布，安排停机或完成复制后重启以重新加载索引。

后续可使用 [同步工具](<../tools/sync_index.py>) 的 `--push --host`。它保留读取端 `out/.sync` 中的累计原始数据；远端只接收 corpus、embeddings 和剔除本地 sync 状态的 manifest，不覆盖/删除远端 profiles 或 memory。工具的远端路径与重启方式有约定（用户主目录下的部署目录、user service），不同部署布局需先核对，不能直接套用下文 system service 模板。

时间戳增量在未启用差分页缓存时仍全量解密；启用缓存后仍扫描全库，索引构建仍为全量。重读窗口之外的迟到消息用 `--full` 补回。不要把读取端同步累计快照当作机器人必须接收的文件。

## 3. 主机环境与配置

在自己的部署目录中放入 `qq_chatbot/` 包及 [配置示例](<chatbot.yaml>)，使用虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install websocket-client PyYAML
cp deploy/chatbot.yaml chatbot.yaml
```

示例配置文件默认使用云端生成与本地 hash 检索；库本身的默认值是 mock/hash。调整 `store.dir` 或使用 `--store` 指向已搬运的索引。不要把密钥提交到配置示例中。

```bash
export QQ_CHATBOT_API_KEY='<自己的模型服务密钥>'
.venv/bin/python -m qq_chatbot qq --config chatbot.yaml --store .chatbot
```

环境变量只作用于继承它的进程；systemd 服务不会自动加载当前 shell 的变量或项目 `.env`。如需服务读取密钥，使用自己配置的 `EnvironmentFile` 或等价凭据机制并限制权限。[服务模板](<qq-chatbot.service>) 本身不声明 EnvironmentFile。

## 4. 模型与向量空间

[部署配置](<chatbot.yaml>) 提供 OpenAI 兼容生成端点示例；可更换为自己的兼容端点和模型，或改为本地 Ollama。

- hash 向量化不发送全量文本到远端，检索可离线；云端 LLM 会收到实际生成提示，其中可能包含检索片段、对话历史、人格档案和启用的长期摘录，并非只有几条检索正文。
- Ollama / OpenAI 语义向量已持久化，但查询仍需兼容 provider/model/dim。主机配置不匹配时应修正配置或重建索引。
- `embedding.fallback_to_hash` 默认 false。显式启用后，后端故障可切换到独立 hash 空间；它会影响检索质量，不能保证语义召回不变。
- 本地 Ollama 需预先安装模型并可访问服务；本轮离线测试不证明具体在线模型端点已连通。

## 5. NapCat 与常驻服务

在机器人主机运行 NapCat，登录机器人账号并开启 OneBot11 正向 WebSocket。机器人与 NapCat 同机时，可在服务端监听本机接口；容器部署需根据容器内部监听地址及端口映射核对连接，不要把容器内 loopback 与主机 loopback 混为一谈。

```bash
.venv/bin/python -m qq_chatbot qq --ws ws://127.0.0.1:3001 --config chatbot.yaml --store .chatbot
```

需要常驻时，以 [systemd 模板](<qq-chatbot.service>) 为基础修改用户、工作目录、Python 路径与配置路径，再安装到自己的服务环境。模板是 system service；同步脚本自动重启使用 user service，两者必须自行对齐。

## 6. 增量人格与长期记忆

```bash
.venv/bin/python -m qq_chatbot profile --person group:100:10001 --kind group --conversation group:100 --config chatbot.yaml
```

以上身份是虚构示例。`--kind` 选择风格样本，`--conversation` 限制事实来源会话。再次运行按来源指纹处理变化；原子保存前备份旧档案到 `profiles/history/`。来源消息时间与事件时间不同，来源可追溯也不证明陈述真实。历史备份不会自动清理。

长期记忆默认 `memory.enabled: false`。启用后，QQ 根据可信事件身份区分机器人、群/私聊、提问者和模式；`/forget` 删除当前身份/模式的长期记忆和短期历史。CLI 还需 `chat --session <本地身份>`；HTTP `/chat` 没有可信身份，不使用长期 Session。记忆是明文 SQLite，应保留在机器人主机，索引推送时不要覆盖。

## 7. 管理与群成员检查

- 追踪默认关闭，启用后 `include_content` 仍默认 false；显式开启才保存问题、答案和证据正文。
- HTTP 管理端点为 `/admin/traces` 与 `/admin/retrieval`，使用 `QQ_CHATBOT_ADMIN_TOKEN` 或配置令牌认证。在线调整只在进程中生效，不写回配置。命令示例见 [使用指南](<../docs/roadmap-usage.md>)。
- `restrict_group_scope: true` 对群聊 QA、persona、档案、`/search` 及显式参数均限制本群；persona 也不能绕过。
- `group_member_check` 默认 false；开启后，未知或过期成员状态拒绝受保护请求并刷新名单，用户需稍后重试。实际 OneBot 成员事件兼容性需部署验收。

## 8. 验证与数据保留

先用虚构样本验证：私聊回复、群内 @ 触发、跨群拒绝、模式切换清理引用、`/forget` 仅影响当前 scope、重启后记忆可读取。检索指标可运行 `eval --demo`；模型事实正确性和人格质量需另行人工评审。

索引、档案及其历史备份、长期记忆、可选内容日志均属于敏感资料。只搬运必要文件并限制权限；更新索引不等于清除旧记忆或历史档案。NapCat 的使用和真实 QQ 数据库兼容性按所用环境独立验证。
