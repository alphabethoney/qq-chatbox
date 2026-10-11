# qq-chatbot 部署（Windows 读取机 + Linux 机器人主机）

「读取/导出」与「机器人」分离：读取是一次性批任务（在持有导出账号数据的机器上做），
机器人是常驻服务（在 Linux 主机上跑）。两者之间只搬运一个目录 `.chatbot/`。

```
┌─ 读取机（Windows，导出账号）───────────────────┐
│ ① NapCat(导出账号) → db-passphrase 插件 → 口令             │
│ ② qq_exporter all --qq <导出账号> --passphrase <口令>     │
│ ③ qq_chatbot build --json out/json  →  产出 .chatbot/     │
└──────────────────────────┬───────────────────────────────┘
                           │  scp -r .chatbot  → 主机
┌──────────────────────────▼───────────────────────────────┐
│ 主机（Linux，机器人账号）                       │
│ ④ NapCat(机器人账号) → OneBot11 ws://127.0.0.1:3001        │
│ ⑤ qq_chatbot qq（systemd 常驻）                           │
└───────────────────────────────────────────────────────────┘
```

---

## 阶段一：读取机（Windows）—— 导出 + 建索引

```powershell
# 1) 用「导出账号」登录 NapCat，db-passphrase 插件打印口令（见 README「获取数据库口令」）
D:\NapCat\napcat.bat

# 2) 解密导出
.\.venv\Scripts\python.exe -m qq_exporter all --qq <导出账号> --passphrase "<16字符口令>" --out out

# 3) 建索引（向量化用 hash 则零依赖；用 ollama/openai 见下方注意）
.\.venv\Scripts\python.exe -m qq_chatbot build --json out/json
# 产出 .chatbot/（含 corpus.json、embeddings.json、profiles/）
```

> 向量化注意：`build` 时若用默认 `hash`，主机端也用 `hash` 即可（查询时重算，无需网络）。
> 若用 `ollama`/`openai`，索引里已存好向量，但主机端仍需同一 provider 做「查询向量化」。

---

## 阶段二：把索引传到 Linux 主机

```bash
# 在 Windows 上执行（或其它方式）
scp -r .chatbot  qqbot@<主机IP>:/opt/qq-chatbot/
```

---

## 阶段三：Linux 主机部署机器人

### 3.1 安装运行环境

```bash
sudo useradd -r -m -s /usr/sbin/nologin qqbot || true
sudo mkdir -p /opt/qq-chatbot && sudo chown qqbot:qqbot /opt/qq-chatbot

# 拷贝代码（只需 qq_chatbot 包，不需要 qq_exporter / 数据库 / cryptography）
scp -r qq_chatbot/  qqbot@<主机IP>:/opt/qq-chatbot/

# Python 环境（机器人运行时唯一必需第三方依赖是 websocket-client）
sudo -u qqbot python3 -m venv /opt/qq-chatbot/.venv
sudo -u qqbot /opt/qq-chatbot/.venv/bin/pip install websocket-client
# 可选：用 YAML 配置时装 PyYAML；用 JSON 配置则不需要
# sudo -u qqbot /opt/qq-chatbot/.venv/bin/pip install PyYAML

# 配置
sudo -u qqbot cp deploy/chatbot.yaml.example /opt/qq-chatbot/chatbot.yaml
sudo -u qqbot vim /opt/qq-chatbot/chatbot.yaml
```

### 3.2 LLM / 向量化

**默认方案：云端 API（LLM 生成）+ 本机 hash（向量化）** —— 检索全离线，只有检索到的几条片段上云。

```bash
# 1) 配置已内置在 deploy/chatbot.yaml（provider: openai，默认 DeepSeek）
#    要换通义/OpenAI，改 chatbot.yaml 里 llm.base_url / model 即可

# 2) 填 API key（不写进 chatbot.yaml，走环境变量）
cp deploy/.env.example /home/<用户>/qq-chatbot/.env
vim /home/<用户>/qq-chatbot/.env   # 把 QQ_CHATBOT_API_KEY=sk-... 填成你的 key
chmod 600 /home/<用户>/qq-chatbot/.env
```

- DeepSeek key 在 <https://platform.deepseek.com/api_keys> 创建，`base_url=https://api.deepseek.com/v1`。
- 通义千问 key 在 <https://platform.qianwenai.com/home/api-keys> 创建，`base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`。

> 向量化固定用 `hash`（零依赖、离线），所以**不需要**装 ollama，也不需要给云端发全量历史。

**备选（要完全离线时）**：本地 ollama —— `curl -fsSL https://ollama.com/install.sh | sh`，再 `ollama pull qwen2.5:7b`，`chatbot.yaml` 里 `llm.provider: ollama`。

### 3.3 NapCat（机器人账号）

在 Linux 主机上跑 NapCat 并登录机器人账号，开启 OneBot11 正向 WebSocket `127.0.0.1:3001`
（Docker 推荐，端口映射 `-p 3001:3001`）：

```bash
docker run -d --name napcat --restart=always \
  -p 6099:6099 -p 3001:3001 \
  -v napcat_data:/app/napcat/config \
  napneko/napcat
# 首次用 docker logs napcat 查看登录二维码，手机扫码登录 <机器人账号>
# 登录后到 WebUI http://<主机>:6099 配置网络 → WebSocket 服务端 127.0.0.1:3001
```

> NapCat 与机器人同机时，`ws://127.0.0.1:3001` 走容器端口映射即可；不同机则填主机 IP。

### 3.4 常驻服务（systemd）

```bash
sudo cp deploy/qq-chatbot.service /etc/systemd/system/qq-chatbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-chatbot
sudo systemctl status qq-chatbot
journalctl -u qq-chatbot -f
```

---

## 验证

1. 给机器人账号私聊发一句话 → 应回复（qa 模式带出处）。
2. 群里 `@机器人 问题` → 应回复；不 @ 不回复（`group_trigger: at`）。
3. `/qa`、`/persona <key>`、`/cite`、`/help` 命令可用。

## 注意

- `.chatbot/` 与 `chatbot.yaml`（若含 api_key）都是敏感数据，`UMask=0077`、权限收紧。
- 群聊检索范围 `restrict_group_scope` 依赖「机器人账号与导出账号在同一个群」——群号不同则查不到本群记录；数字分身（persona）不受此限制。
- NapCat 属第三方协议框架，可能违反 QQ 用户协议，仅用于自己账号、自己设备。
