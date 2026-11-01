# 钉钉群知识机器人

把钉钉群里的讨论沉淀成可检索的内部知识，用机器人以问答方式还回去，并保证"什么群的知识只能给什么人看"。

- **零依赖**：核心只用 Python 标准库（3.9+），服务器上不装任何包也能跑起来
- **按群隔离**：一个群 = 一个知识域，A 群的知识不会漏到 B 群
- **可审计**：每次提问、每次越权拦截都留痕
- **可降级**：没配大模型也能用（返回原文片段），配了就变成 RAG 问答

---

## 一、先说一个必须知道的限制：全量群消息

钉钉对「读取群内全部消息」是受限开放的。普通企业内部机器人（Stream / HTTP 回调）**只能收到 @ 机器人 的消息**，收不到群里的普通对话。这是平台侧的合规设计，不是本项目的能力缺失。

所以本项目把「消息来源」做成可插拔的三条通道，你可以按企业实际能拿到的权限组合使用：

| 来源 | 能拿到什么 | 前提条件 | 现状 |
|---|---|---|---|
| **DWS 命令行** | **群内全部消息（含非 @）**、历史消息 | 装 `dws` + `dws auth login`，管理员开启「CLI 访问管理」 | ✅ 已实现，`dws-listen` / `dws-sync` |
| **Stream 机器人** | 群里 @机器人 的消息、单聊 | 创建企业内部应用 + 机器人能力 | ✅ 已实现，`bot` 命令 |
| **聊天记录导入** | 任意历史消息（JSON / CSV / 文本） | 管理员从群里导出 | ✅ 已实现，`import` 命令 |
| **会话内容存档** | 群内全部消息 | 企业开通增值能力，主管理员签协议 | 🔌 适配位已留好 |

**要"自动收全部群消息"，走 DWS**（第六节有完整步骤）。它把钉钉能力封装成命令行，
其中 `event consume user_im_message_receive_group_all` 能订阅到群内所有消息，
思路见 `app/ingest/dws.py`。会话存档作为备用通道保留，适合企业已经开通的情况。

如果两条都走不通，还有两个退路：

1. 机器人进群后，靠 `@机器人 /沉淀` 让成员主动把结论沉淀进来（最轻，也最准）；
2. 让管理员定期把群记录导出，用 `import` 命令批量灌进来（可回溯历史）。

---

## 二、权限模型

权限的最小单位是**知识域（scope）**，不是群、不是人。

```
钉钉群 ──绑定──> 知识域 ──包含──> 知识文档 ──切分──> 检索片段
   │                                              ↑
   └──成员身份──> 用户可读知识域 ──────────────强制过滤──┘
```

用户能读哪些知识域，来自三个来源的并集：

| 来源 | 说明 |
|---|---|
| 群成员身份 | 收到某人在群 G 的消息就把 `(G, 该人)` 记进成员表（机器人、DWS、导入三种来源都算）；G 绑定的知识域对该人可见 |
| 跨群授权 | 管理员在后台单独给某人开某个知识域（`grants` 表） |
| 管理员角色 | `role=admin` 的用户可读全部知识域；`role=superadmin` 另外还能看到待审核内容 |

四种角色的差别：

| 角色 | 可读范围 | 待审核内容 | 典型用途 |
|---|---|---|---|
| `member` | 只限自己所在群 + 单独授权的知识域 | ✗ | 普通员工 |
| `guest` | 同上，但按"外部联系人"标记 | ✗ | 外部协作方 |
| `admin` | 全部知识域 | ✗ | 知识库运营 |
| `superadmin` | 全部知识域，且群内提问不受群隔离收窄 | ✓ | 老板 / 系统 owner |

判定规则（`app/acl.py`）：

- **默认拒绝**：解析不出任何知识域就返回空集，绝不回退到全库检索；
- **群内提问强制收窄**：在群 G 里提问，只检索 G 绑定的那一个知识域，即使用户还有别的权限（`superadmin` 例外）；
- **越权即拦截**：在无权限的群里提问，直接拒绝并写审计日志，不做"模糊命中"；
- **待审核内容默认不参与检索**：检索 SQL 里带 `AND d.status = 'approved'`，只有 `superadmin` 会去掉这个条件，并且回答里给来源打上「待审核」标记；
- **不能把自己锁在门外**：后台不允许把最后一个 `superadmin` 降级。

两个开关（`.env`）：

```bash
ACL_REQUIRE_GROUP_MEMBERSHIP=true   # 不在任何群的人直接拒绝（默认开）
ACL_DENY_UNKNOWN_USER=false         # 未登记用户一律拒绝（更严，按需开）
```

---

## 三、快速开始

```bash
cd dingtalk-kb-bot
cp .env.example .env          # 改掉 KB_ADMIN_TOKEN

python -m app.main demo       # 写入演示数据：4 个群、16 条知识、18 条群消息
python -m app.main serve      # 打开 http://127.0.0.1:8000 ，填 token 连接
```

演示数据埋好了四种权限形态，任选一行都能一眼看出区别：

| 身份 | 角色 | 能读的知识域 | 用来演示什么 |
|---|---|---|---|
| `root` 超级管理员 | superadmin | 全部 4 个 | 跨全部知识域 + 能看到待审核内容（如果开着审核） |
| `admin001` 知识库管理员 | admin | 全部 4 个 | 看得到全部**已生效**知识，看不到待审核 |
| `u1001` 张三 | member | 仅 `chat_support` | 群成员默认范围 |
| `u4001` 运营-周敏 | member | `chat_support` + `chat_product` | 不在群里，靠**单独授权**拿到两个域 |
| `client001` 陈经理 | guest | 仅 `chat_ext` | 外部客户只能看到外部协作群的边界说明 |
| `u9999` 新同事-未入群 | member | 无 | 默认拒绝：什么都不返回 |

同一个问题换个身份问，答案范围立刻不一样：

| 问什么 | 谁问 | 预期 |
|---|---|---|
| 签收 20 天还能补差价吗 | `root` / `u1001` | ✅ 都命中《价保与降价补差规则》 |
| 什么时候交付 | `client001` | ✅ 只命中《外部协作群沟通边界》 |
| 签收 20 天还能补差价吗 | `u9999` | 🚫 拒绝：没有可访问的知识域 |
| 发票怎么开（在 `cidSupportAAAA` 里问） | `u2001` 李四 | 🚫 拒绝：不是该群成员 |

跑测试：

```bash
python -m unittest discover -s tests -v     # 46 项，覆盖隔离、越权、审计、接口鉴权、向量检索、DWS 解析
```

### 只要真实群数据，把演示数据清掉

```bash
python -m app.main prune --keep chat_demand        # 预演：只告诉你删什么
python -m app.main prune --keep chat_demand --yes  # 真删
```

`--keep` 指定要保留的知识域（多个用逗号分隔），其余知识域连同群、文档、切片、消息、授权、审计、演示用户一起删；
保留群里的真实成员会自动补登记成用户，问答页的身份下拉里能看到他们。

对应地，`demo --force` 现在会先检查库里有没有真实数据，有就拒绝执行——要连真实数据一起删必须显式加 `--force-real`。

### 两个页面

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8000/` | 管理后台：群与知识域绑定、待审核、知识库、授权、用户、审计 |
| `http://127.0.0.1:8000/ask` | 问答页：左侧选身份，右侧提问，看到的就是这个身份能看到的 |

问答页支持直达链接，方便演示和排查：

```
/ask?staff=root&q=慢查询怎么排查      # 以超级管理员身份问
/ask?staff=u1001&q=发票能不能重开     # 以张三身份问
```

同一个问题换个身份问一遍，左边权限面板和右边引用的来源会立刻跟着变——
这是验证权限配置对不对最快的办法，比看代码和日志直观。

---

## 四、接入钉钉（七步）

1. **创建应用**：钉钉开放平台 → 应用开发 → 企业内部应用 → 创建应用。
2. **记下凭证**：应用详情 → 凭证与基础信息，拿到 `Client ID` / `Client Secret`，填进 `.env` 的 `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`。
3. **加机器人能力**：应用 → 添加应用能力 → 机器人。
4. **配消息接收**：机器人 → 消息接收模式 → 选 **Stream 模式**（不需要公网地址、不需要验签，服务器只要能出网）。
5. **申请权限**：权限管理里至少勾选「企业内机器人发送消息权限」。
6. **发布版本**：版本管理与发布 → 创建版本 → 可见范围选内部成员 → 发布（不发布手机端搜不到）。
7. **拉进群**：群设置 → 机器人 → 添加机器人 → 选中你刚建的应用机器人。

然后启动机器人进程：

```bash
pip install dingtalk-stream
python -m app.main bot
```

群里 `@机器人 你好`，能收到回复就通了。

### 群里可用的指令

| 指令 | 作用 |
|---|---|
| `@机器人 退货要多久` | 直接提问，走权限过滤后的 RAG |
| `@机器人 /沉淀` | 把本群最近的消息提炼成知识（默认直接生效，开了审核则进队列） |
| `@机器人 /权限` | 看自己能访问哪些知识域 |
| `@机器人 /帮助` | 指令说明 |

---

## 五、知识是怎么来的

```
群消息 ──去噪──> 会话窗口 ──提炼──> 知识文档 ──切分+索引──> 可检索
                                    │
                       免审核（当前）→ 直接生效
                       或 待审核 → 管理员通过 → 生效
```

- **提炼**（`app/distiller.py`）：一次把窗口内的消息交给大模型，抽成「一条一个知识点」的自包含文档；过滤寒暄、图片占位符、无结论讨论。
- **没大模型时**：退化为「原始记录归档」，按时间窗整理成文档，一样能检索到原文。
- **审核**：`KNOWLEDGE_AUTO_APPROVE=true`（当前配置）时**不审核**，提炼出来立刻建索引、立刻能被检索；改成 `false` 就走「待审核」页，管理员点了通过才生效。
- **索引**：中文按字二元组切分（`app/text.py`），标题 + 标签 + 正文一起进 SQLite FTS5 做 BM25；配了 `EMBED_MODEL` 再叠加向量检索，两路结果用 RRF 融合。
- **向量检索的两个坑**：① 单次 input 条数有上限（百炼 `text-embedding-v3` 是 10 条），代码按 `EMBED_BATCH` 分批；② 向量模型的相似度基线很高，不相关的中文也能到 0.5，所以关键词零命中的片段还要过一道门槛——既要比 `RAG_VEC_MIN_SIM`（默认 0.55）高，也不能比本问题的最好结果低太多（相对 85%）。不过这道门的片段会污染上下文、让模型把无关文档当依据。
- **定时沉淀**：`serve --with-scheduler` 会按 `DISTILL_WINDOW_MINUTES` 周期检查并提炼。

### 提炼有门槛，所以检索要留一条兜底

提炼要求一个窗口里**至少 `DISTILL_MIN_MESSAGES` 条消息**（默认 3）。这个门槛有它的道理——
一句话的寒暄不值得占一张卡片——但它有个副作用：**一问一答就说清楚的答案，永远变不成卡片。**

真实踩到的情况：

```
A：配对码要在白名单里吗
B：不用，配对码的地址是 10.2.3.4，端口 9527
```

只有 2 条，不够提炼门槛 → 没有卡片 → 之后谁问「配对码走哪个端口」都是 0 结果。
可这两句话明明就在 `messages` 表里。

所以问答分两步走（`app/rag.py`）：

```
问题 ──> 检索知识卡片 ──命中──> 正常作答
              │
            零命中
              ↓
        检索原始消息 ──命中──> 作答，并标注「未经整理」
              │
            零命中
              ↓
          老实说没有
```

兜底实现要点：

- `store.search_messages()` 只在该知识域内找（权限一点没松），先用 `LIKE` 把候选压到几百行，再用和卡片检索同一套 bigram 打分排序，两边相关性口径一致。
- 命中数低于门槛的直接丢掉（查询有 3 个以上 token 时至少要命中 2 个），避免「端口」两个字把整库捞出来。
- 答案前面强制加一句「以下没有对应的知识卡片，是从群聊原始记录里找的，未经整理，可能有出入」，来源标成 `群聊原文 · 张三 2026-10-20 10:13`，前端显示「未整理」标签。用户一眼能分清这是整理过的结论还是随口一说。
- 兜底**不回写知识库**，只是回答时临时用一下；要真变成卡片，仍走 `/沉淀`。
- 不想要就 `RAG_RAW_FALLBACK=false` 关掉。

导入历史记录：

```bash
python -m app.main import examples/chat_export.json --distill
# 支持 .json / .csv / .txt，字段兼容常见导出工具，见 app/ingest/file_import.py
```

---

## 六、用 DWS 接管群消息（推荐）

DWS（DingTalk Workspace CLI）是钉钉官方开源的命令行工具，把钉钉能力封装成命令，
以「登录用户」的身份工作。它能订阅群内全部消息，正好补上机器人只能收 @ 的缺口。

### 1. 安装与登录

```bash
npm install -g dingtalk-workspace-cli
# 或 brew tap DingTalk-Real-AI/dingtalk-workspace-cli https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli.git && brew install dingtalk-workspace-cli

dws auth login          # 会唤起浏览器授权
dws auth status         # 确认已登录
```

> 需要企业管理员在开发者后台 →「CLI 访问管理」里开启，否则登录时会提示申请。

### 2. 自检

```bash
python -m app.main dws-check
```

它会检查：命令是否装好、版本、登录状态、能搜到哪些群（顺带把 `openConversationId` 打出来）。

### 3. 先核对字段，再正式跑

不同版本的 `--flatten` 字段可能微调。先抓几条真实事件看解析对不对：

```bash
python -m app.main dws-dump --n 5 --seconds 120
```

去钉钉群里发几句话，它会打印每条事件的解析结果（群、发送人、内容），并把原始 NDJSON
存到 `data/dws_sample.ndjson`。如果显示"解析不出消息"，把原始字段名发我，改
`app/ingest/dws.py` 里的 `_ALIASES` 就行——映射表是集中的，不用动逻辑。

### 4. 跑起来

```bash
python -m app.main dws-listen                    # 实时监听所有群消息并入库
python -m app.main dws-sync --days 30 --distill  # 补历史消息，并立即提炼
```

`DWS_EVENT_KIND` 控制订阅范围：

| 值 | 订阅什么 |
|---|---|
| `group_all` | 所有群的全部消息（默认） |
| `group` | 指定群（配合 `--chat <openConversationId>`） |
| `at` | 只收 @ 当前登录用户的消息 |
| `o2o_all` | 所有单聊消息 |

### 5. 和机器人的分工

DWS 负责**进**，机器人负责**出**：

- `dws-listen` 常驻收集群消息 → 沉淀成知识
- 群成员 `@机器人 问题` → 机器人在权限范围内作答

机器人没配也能用，直接到后台「提问测试」页问。机器人是"让人在群里顺手问"的体验层，
不是必需品。

### 6. 两个必须知道的细节

**身份标识不一样。** DWS 拿到的发送人是 `openDingTalkId`（形如 `DLii5ckLaaX...`），
而出机器人通道拿到的 `senderStaffId` 是 userId。同一个人在两个通道里会是两个身份。
**建议只用一个通道入库**，否则同一个人会被登记两次，权限判定会错乱。

**密钥会被自动脱敏。** 群里贴 token / API Key 是常见事故，这些东西一旦进知识库
就能被同群任何人问出来。所以入库前会把 GitHub token、`sk-` 开头的 Key、AWS/Slack
令牌、`password: xxx`、`密钥 xxx` 这类内容替换成 `[已脱敏]`。
开关是 `REDACT_SECRETS`（默认开）。

> 已经贴出去过的密钥，脱敏只是止损——**该吊销的还是要吊销**。聊天记录本身在钉钉侧
> 仍然存在，脱敏只管知识库这一份。

### 7. 注意

- DWS 用的是**某个人的登录态**，能看到的就是这个人所在群的消息。建议用一个专门的
  「收集账号」，群成员对这件事应当知情。
- 登录态有有效期，失效时 `dws-listen` 会在日志里明确提示重新 `dws auth login`。
  长期跑建议在服务器上用 `dws auth login --device` 或 `dws auth export/import` 迁移登录态。
- 群成员身份仍然自动积累：谁说过话，谁就是这个知识域的成员。没发过言的人需要管理员
  在后台「授权」里单独开，或是等他在群里说话。

---

## 七、部署到服务器

### 方式一：systemd（推荐，最简单）

```bash
sudo cp -r dingtalk-kb-bot /opt/ && cd /opt/dingtalk-kb-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && vi .env      # 改 token、填凭证

sudo tee /etc/systemd/system/kb-admin.service >/dev/null <<'EOF'
[Unit]
Description=KB Admin
After=network.target
[Service]
WorkingDirectory=/opt/dingtalk-kb-bot
EnvironmentFile=/opt/dingtalk-kb-bot/.env
ExecStart=/opt/dingtalk-kb-bot/.venv/bin/python -m app.main serve --host 127.0.0.1 --port 8000 --with-scheduler
Restart=always
[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/kb-bot.service >/dev/null <<'EOF'
[Unit]
Description=KB DingTalk Bot
After=network.target
[Service]
WorkingDirectory=/opt/dingtalk-kb-bot
EnvironmentFile=/opt/dingtalk-kb-bot/.env
ExecStart=/opt/dingtalk-kb-bot/.venv/bin/python -m app.main bot
Restart=always
[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now kb-admin kb-bot
```

两个进程共用同一个 SQLite 文件（WAL 模式，读写并发没问题）。换成 MySQL 后端时，
两个进程就是共用同一个库，各自连各自的连接池。后台服务建议只绑 `127.0.0.1`，再用 Nginx/Caddy 反代加 HTTPS 和 Basic Auth。

### 方式二：Docker

```bash
cp .env.example .env && vi .env
docker compose up -d --build
```

`docker-compose.yml` 里已经拆成 `admin`（后台 + 定时沉淀）和 `bot`（Stream 长连接）两个容器，共享 `kbdata` 卷。

### 方式三：数据放远端 MySQL

单机跑着玩用 SQLite 就够，多人用、要备份和扩容时换 MySQL。代码默认按 SQLite 的 SQL 写，
换库靠 `app/db_mysql.py` 一层方言适配（`?`→`%s`、`INSERT OR IGNORE`→`INSERT IGNORE`、
`ON CONFLICT`→`ON DUPLICATE KEY UPDATE`、FTS5 虚拟表→ngram 全文索引），业务代码不用改。

```bash
# 1) .env 里配好
KB_BACKEND=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=...
MYSQL_DATABASE=dingtalk_kb

# 2) 先预演，看清要写哪个库
python3 -m app.main migrate-mysql

# 3) 真迁（会先清空目标库里的同名表，别的库不受影响）
python3 -m app.main migrate-mysql --yes

# 4) 重启服务
python3 -m app.main serve --with-scheduler
```

`migrate-mysql` 干的事：库不存在就 `CREATE DATABASE`（utf8mb4），建 9 张表，然后把本地
SQLite 的 `scopes / groups / users / group_members / grants / messages / docs / chunks / audit_log`
整库搬过去，回填 `AUTO_INCREMENT`，最后按「标题 + 标签 + 正文」重建 `chunks.fts_body`。

**MySQL 侧三个坑，已经处理掉了，改代码时别踩回去：**

1. **`groups` 和 `key` 是 MySQL 8.0 保留字**。代码里 `FROM groups g`、`SELECT key FROM scopes`
   在 SQLite 里合法，在 MySQL 里直接语法错误。`_translate()` 里统一给这两个词补反引号，
   并且只改字符串字面量之外的部分（不会把 `WHERE msg_type = 'groups'` 这种取值改坏）。
   补引号必须在生成 `ON DUPLICATE KEY UPDATE` **之前**，否则 `KEY` 也会被加上反引号。
2. **ngram 索引不会自动覆盖 docs 表**。FTS5 那边是把 `head_tokens`（标题 + 标签）塞进虚拟表，
   MySQL 的全文索引只能建在 `chunks.fts_body` 上，所以 `index_doc()` 里额外把标题和标签
   拼到正文前面——否则「官网产品卡片命名与图标规范」这种结论只在标题里的文档搜不到。
3. **ngram 的相关度分数会退化**。`MATCH ... AGAINST` 对「登录页」这种多字中文查询会命中若干条
   但分数全是 0，`ORDER BY score` 等于随机排序。所以 `_lexical_mysql()` 只用 MATCH 负责召回，
   排序交给和 SQLite 侧同一套 bigram 打分，两边检索结果才对得上。

**连不上 3306 的时候**（比如腾讯云安全组没开、MySQL 只监听内网）：

```bash
# 本机开发：走 SSH 隧道，把远端 3306 映射到本地 13306
ssh -N -L 13306:127.0.0.1:3306 root@<你的服务器>
# .env 里 MYSQL_PORT=13306
```

隧道是临时通道，`ssh` 一断服务就报错。**正式部署请把服务装在服务器上**，直连
`127.0.0.1:3306`，别让生产服务依赖一条手工隧道。

### 已上线：kb.smartwork-inc.me

当前生产形态（`$KB_HOST`，默认见 `deploy.sh`）：

| 组件 | 位置 | 说明 |
|---|---|---|
| 问答服务 | 服务器 `/opt/dingtalk-kb-bot`，systemd `kb-admin` | 127.0.0.1:8017，MySQL 走本机 3306 |
| 群消息采集 | 服务器，systemd `kb-listen` | DWS 设备流登录（`dws auth login --device`），订阅 `group_all` |
| 反向代理 | nginx `conf.d/kb.smartwork-inc.me.conf` | 80 跳 443，443 反代 8017 |
| 证书 | `/etc/nginx/ssl/kb.smartwork-inc.me/` | acme.sh webroot 签发，自动续期 |

页面：
- 手机版 `https://kb.smartwork-inc.me/m`（单栏对话、底部安全区输入、引用折叠）
- 桌面版 `https://kb.smartwork-inc.me/ask`
- 管理后台 `https://kb.smartwork-inc.me/`

```bash
systemctl restart kb-admin     # 重启问答
systemctl restart kb-listen    # 重启采集
journalctl -u kb-admin -n 50   # 看日志（同理 kb-listen）
```

**发布只需一条命令**（在本地项目根目录）：

```bash
./deploy.sh              # 备份 → 同步 → 装依赖 → 装单元 → 重启 → 健康检查（失败自动回滚）
./deploy.sh --dry-run    # 只看会改什么
```

脚本只同步代码和 systemd/nginx 配置，**不会覆盖服务器上的 `.env` 和数据库**。
`deploy/kb-*.service`、`deploy/kb-sync.timer`、`deploy/nginx-kb.conf` 是配置的唯一来源，
改完跟着代码一起发布，不用手改服务器。

两个容易踩的地方：

1. **`kb-listen` 必须设 `Environment=HOME=/root`**。systemd 服务默认没有 `HOME`，
   dws 会退回安装目录 `vendor/.dws` 找凭证，报 `open .../vendor/.dws/.data: no such file`。
2. **凭证是机器绑定的**，把 `~/.dws` 拷到另一台机器不会生效（`auth status` 显示「未登录」）。
   换机器必须重新 `dws auth login --device`，拿授权码到钉钉里确认。

### 数据怎么自动更新

三个组件各管一段，谁挂了都不影响别人：

| 组件 | 周期 | 作用 |
|---|---|---|
| `kb-listen` | 常驻 | 事件流实时接住新消息，秒级入库 |
| `kb-sync.timer` | 10 分钟 | 拉最近 7 天历史做补漏——服务重启、断线、事件丢失都能靠它兜回来 |
| `kb-admin` 内置调度 | 10 分钟 | 扫待处理消息（`doc_id IS NULL`），够 3 条就提炼成知识并建索引 |

`DISTILL_INTERVAL_MINUTES` 控制提炼间隔（默认 10），`DISTILL_MIN_MESSAGES` 控制门槛（默认 3）。
调度器启动时会先跑一轮，重启服务不用干等一个周期。

实测：往一个新群写 3 条消息后，5 秒内自动产出 3 篇已生效知识，无需任何人工操作。

> 本机（Mac）的 `.env` 现在指回 SQLite，只做开发测试，不要再连生产库。
> DWS 的登录态在服务器上，本机已不再参与采集。

---

## 八、目录结构

```
app/
  config.py          环境变量集中读取
  db.py              SQLite 表结构（scopes/groups/users/messages/docs/chunks/fts/grants/audit）
  acl.py             权限判定：身份 → 可读知识域（唯一的判定入口）
  store.py           知识读写 + 混合检索
  text.py            中文 bigram 分词、切块、去噪
  rag.py             权限 → 检索 → 作答 → 附来源
  llm.py             OpenAI 兼容模型调用（标准库 urllib）
  embedder.py        可选向量化
  distiller.py       消息 → 知识
  api.py             管理后台 HTTP 接口 + 静态页
  main.py            命令行入口
  scripts_seed.py    演示数据
  ingest/
    normalizer.py    统一入库入口
    dws.py           DWS 命令行接入（群全量消息 + 历史）
    stream_bot.py    钉钉 Stream 机器人
    file_import.py   聊天记录文件导入
    archive_poller.py 会话存档适配位
web/index.html       管理后台前端（单文件，无构建）
web/ask.html         问答页前端：可选身份的聊天界面（单文件，无构建）
tests/test_acl.py    权限/检索/接口测试
```

---

## 九、常见问题

**Q：机器人进群了但收不到消息？**
检查三点：应用版本是否已发布；是否选了 Stream 模式；是不是没 @ 机器人（这是平台限制，见第一节）。

**Q：知识库搜不到中文？**
本项目用字二元组切分，标题、标签、正文都进索引，正常中文都能命中。如果搜索词只有 1 个字，命中会差——加长关键词即可。
调整过分词或索引策略后，跑 `python -m app.main reindex` 重建切片索引。

**Q：没有大模型 Key 能用吗？**
能。检索、权限、后台、导入全部可用，回答会返回原文片段而不是生成式答案。

**Q：能接入通义/智谱/自建模型吗？**
只要提供 OpenAI 兼容的 `/chat/completions` 接口就行，改 `LLM_BASE_URL` / `LLM_MODEL`。阿里云百炼的现成配置：

```bash
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus
EMBED_MODEL=text-embedding-v3      # 不想要向量检索就留空
```

换模型只改 `LLM_MODEL`（`qwen-turbo` 更快更便宜，`qwen-max` 更强）；换别家只改这三行。

**Q：配了向量模型，为什么有的问题还是搜不到？**
先看关键词有没有命中。向量检索只补语义相近的，且要过相似度门槛（见第五节），过不了宁可返回“没有”。想放宽就调低 `RAG_VEC_MIN_SIM`，但别低于 0.45，否则会把无关文档塞进上下文。

**Q：改了索引策略/换了向量模型，老数据怎么办？**
跑 `python -m app.main reindex`，会重建全部切片并重新向量化（换模型必须跑，维度不一样会直接算不出相似度）。

**Q：数据存在哪？**
只有一个 SQLite 文件（默认 `data/kb.sqlite3`），备份就是复制这个文件（建议先 `sqlite3 kb.sqlite3 ".backup"`）。

---

## 十、上线前安全清单

- [ ] `KB_ADMIN_TOKEN` 换成 32 位以上随机串，别用默认值
- [ ] `.env` 里的大模型 Key 别提交到 git（`.gitignore` 已含 `.env`），也别写进前端
- [ ] 后台服务绑 `127.0.0.1`，通过反代暴露，并开 HTTPS
- [ ] 把 `role=superadmin` / `role=admin` 限制到 2-3 人，并确认「最后一个超管不能降级」这条保护还在
- [ ] 明确告知群成员：消息会被沉淀进知识库（合规要求）
- [ ] 决定要不要审核：当前 `KNOWLEDGE_AUTO_APPROVE=true`（不审核）。要人工把关就改成 `false`，敏感场景建议打开
- [ ] 定期看「审计」页的拦截记录，异常越权往往是权限配错了
- [ ] 若要接会话内容存档，先完成法务与员工告知流程
