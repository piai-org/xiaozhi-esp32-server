# 老人陪伴后端：源码复现

本目录提供构建、私有配置生成、角色和模型注册、验证入口。研究报告、人工评审材料、用户对话、记忆数据库、真实设备标识、API Key 和 AppSecret 不进入 Git。

## 代码与部署范围

基线为 `4576e391bb1f45be514a3bdf358d7dbfac37643e`。后续提交按原实施阶段与依赖顺序重建，提交时间为本次整理时间，正文标注原实施阶段。已放弃的 D1/D2 实验不重新引入。可运行 `git log --reverse --oneline 4576e391..HEAD` 查看顺序。

默认部署采用**本地配置的语音服务 + 浏览器测试端**，完整使用工具、记忆和 Q1–Q6，不依赖旧机器的数据库。可选 `manager` profile 构建管理端、小程序 API、MySQL 和 Redis。管理账号与小程序微信身份彼此隔离。

| 配置项 | 样例值 |
| --- | --- |
| 角色模型 | `qwen-plus-character` |
| 意图、后台观察、资料整理 | `qwen3.7-flash-2026-07-15` |
| 普通聊天 | Character 单模型，本轮温度 0.4 |
| 明确记忆读取、联网查询 | Character 草稿 0.7，资料整理 0.4 |
| ASR | `qwen-audio-3.1-asr-flash-message`，16 kHz，保留方言 |
| TTS | `qwen-audio-3.1-tts-flash`，`longanlingxin_v3.1`，PCM 24 kHz |
| 记忆 | PowerMem 1.0.2 + SQLite 事实账本/向量索引，另存近期对话、情绪、经历 |
| Q2–Q6 | 对明确作用域启用，情绪 `feedback`，朗读计划 `rules` |
| 主动说话 | 至少静默 20 秒、冷却 60 秒；静默断线仍为 120 秒，还需满足输入/播放等条件 |

仓库同时保存了两项**尚未上线的工作区候选**：模型朗读计划默认关闭；稀疏称呼协议单独记为 `experiment(dialogue)` 提交，尚未证明改善。要对齐该实验之前的线上称呼协议，可从该实验提交的父提交取回 `main/xiaozhi-server/core/utils/companion_expression.py`。这些候选不应被描述为已上线修复。通用 `config.yaml` 默认关闭灰度功能，只有本目录生成的明确作用域配置开启。

复现指相同代码和配置结构，不包含旧用户数据，也不保证随机模型逐字相同。Character 使用可更新别名；真实调用仍取决于账号权限、地域、模型/API 可用性。

## 环境与首次启动

推荐 Linux x86_64、Docker Engine/Compose v2 以上、Python 3.10 以上。Java 构建用 JDK 21/Maven 3.9.9，管理前端用 Node 18.20.8 和仓库锁文件 `npm ci`。需要访问镜像、依赖源及云模型/信息服务。

语音依赖镜像固定 SHA256；应用代码由本次检出覆盖，并安装本仓库 requirements。可用 `VOICE_DEPENDENCIES` build arg 换为同等依赖基底。构建需要较多磁盘空间：该语音依赖镜像展开约 10 GB，构建缓存可能额外占用同量空间，建议至少留 20 GB 空闲。不要在空间不足的线上机器直接全量构建。MySQL/Redis 标签固定主次版本，所有传递依赖并未锁至字节级；长期归档应保留构建镜像、依赖缓存和 Git bundle。

在仓库根目录执行：

```bash
cp deploy/companion/.env.example deploy/companion/.env
chmod 600 deploy/companion/.env
```

编辑 `.env`，填自己的 Key 和可达的 `PUBLIC_WS_URL`、`PUBLIC_HTTP_URL`。需要业务空间专属入口时设置自己的 `DASHSCOPE_WS_URL`，`DASHSCOPE_BASE_URL` 与账号/地域一致。不要复用旧部署的 Key 或空间 ID。

`COMPANION_DEVICE_ID` 必须等于浏览器/设备的 ID；`COMPANION_SUBJECT_SCOPE` 标识一个人。同一个人的多终端共享此作用域，并在生成配置中更新记忆 `allowed_device_ids`、Q 功能白名单和 `server.auth.allowed_devices`。不同人不能照搬单人共享配置。

默认使用 18000/18003/18006/18002 端口并仅绑定本机。远程直连须设置 `BIND_ADDRESS` 和可达 URL，或配置转发/HTTPS。浏览器麦克风需要安全上下文：本机 localhost 页面可测试，远端应使用 HTTPS。测试端会校验 WS 主机名，使用实际域名或浏览器电脑 hosts 中映射的域名，不直接填被校验器拒绝的回环地址。

```bash
python3 deploy/companion/prepare.py
docker compose --env-file deploy/companion/.env \
  -f deploy/companion/compose.yaml -p companion-repro up -d --build voice browser
```

生成配置在 `deploy/companion/runtime/voice/data/.config.yaml`，权限 0600，含密钥且被忽略。已存在时拒绝覆盖；确认后使用 `--overwrite` 仅重写配置，不删除记忆。

打开 `http://localhost:18006/index.html`，设置设备 ID，OTA 地址填 `http://实际可达主机:18003/xiaozhi/ota/`，或填配置中的 WS URL 后拨号。设备 ID 不能遗漏，否则单人记忆和 Q 开关不会命中。WS 端口不是管理网页；管理 API 根路径返回 401 不等于服务未启动。

真实验收：首次通话介绍测试姓名/偏好，结束后等待抽取，再拨号核对；测试新闻、指定城市天气、更正、删除、打断与主动说话。没有真实模型凭据时只能验证启动和离线协议，不能声称云模型/声音已验证。

## 可选管理端与模型注册

```bash
docker compose --env-file deploy/companion/.env \
  -f deploy/companion/compose.yaml -p companion-repro --profile manager \
  up -d --build db redis manager-api manager-web
```

数据库/Redis 不发布宿主机端口，使用该项目独立卷。Java 启动时 Liquibase 自动建表并执行 `202610211200.sql` 小程序账号/身份/会话迁移，不需线上数据。网页 `http://localhost:18002/`，API `/xiaozhi/`，管理账号按上游首次注册流程创建。

从私有配置生成只新增模型、不覆盖同名记录的 SQL：

```bash
python3 deploy/companion/seed-manager.py
docker compose --env-file deploy/companion/.env \
  -f deploy/companion/compose.yaml -p companion-repro --profile manager \
  exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot xiaozhi_esp32_server' \
  < deploy/companion/runtime/manager-models.sql
```

脚本不连接数据库；生成文件 0600、含凭据，不提交。重复执行 SQL 不覆盖已有模型，脚本也拒绝覆盖输出文件。改 Key 请在管理端编辑，不要期待重复 seed 自动轮换。脚本不创建账号、不改角色、不绑定设备。

在管理端新建小圆，角色设定粘贴 `role.txt`，选择 `Companion_LLM`、`Companion_Intent`、`Companion_ASR`、`Companion_TTS`、`Companion_Memory`、`Companion_VAD`，绑定自己的设备。意图引用 `Companion_Auxiliary`。Q4 按原角色散列匹配，改角色文本可能使表达叠加不启用。

默认 voice 为独立模式，管理端改动不会自动影响它。要切到管理端供给模式，以 `main/xiaozhi-server/config_from_api.yaml` 为基础生成新的私有配置：配置 `manager-api.url=http://manager-api:8002/xiaozhi` 和管理端服务间密钥；保留样例的 `dialogue_quality`、所有 `companion_*` 本地开关、server 端口与公共模板路径；去除本地 `selected_module`、模型定义和角色 prompt。在管理端配置实际 OTA/WS 地址后重建此项目的 voice 容器。不要同时混用两个来源。

微信登录默认关闭。自己的小程序准备好后，在私有 `.env` 配置 `MINIAPP_AUTH_ENABLED=true`、`MINIAPP_WECHAT_APP_ID`、`MINIAPP_WECHAT_SECRET`，重建该项目 manager-api 容器。真实微信联调还需要自己的 HTTPS 入口和合法域名。模拟集成测试不能替代真实 code 交换；AppSecret 不写客户端、角色卡或 Git。

## 把当前本地提交交给另一台机器

本次提交没有推送远程，因此仅从原 GitHub 地址 clone 还拿不到这些提交。可以复制整理时导出的 `.artifacts/xiaozhi-backend-20261114.bundle`，然后在另一台机器执行：

```bash
git clone --branch main /path/to/xiaozhi-backend-20261114.bundle xiaozhi-esp32-server
cd xiaozhi-esp32-server
git log --oneline -5
```

bundle 只带 main 可达的源码历史，不含私有配置、业务数据库或本地研究文件。随后按本说明重新填写自己的配置。已有仓库也可从 bundle fetch，再检查并合并 main。

## 验证与数据迁移

在干净检出运行：

```bash
bash scripts/test-companion-backend.sh
bash main/manager-api/scripts/test-miniapp-backend.sh
```

语音测试在断网、只读挂载、临时数据目录的容器中运行，不读运营配置/记忆、不调用模型；宿主有 Node 时再检查浏览器语法。可用 `COMPANION_TEST_IMAGE` 指定自建依赖镜像。Java 脚本创建自己的 MySQL/Redis 网络/卷，运行全部 Maven、真实迁移和 HTTP/Redis 集成检查，结束后只清理自己创建的资源。Maven 缓存保留。报告在忽略的 `main/manager-api/.test-reports/`，也可用 `MINIAPP_TEST_REPORT_DIR` 指定外部目录。

本次整理的回归：语音 455 项通过；Java 210 项通过（小程序相关 60 项，0 跳过）；管理前端按锁文件完成安装与生产构建。语音源码镜像构建完成，独立配置启动后 OTA 返回 200、WS 握手成功；模型注册 SQL 在隔离 MySQL 重复导入保持 7 条模型及正确引用。这是代码回归，不是人物情感质量评分或新一次人工盲评。

新实例没有历史用户数据。若需迁移，另行受控备份管理 MySQL、voice data 中的事实账本/向量索引/近期对话/情绪/经历、上传文件和私有配置。SQLite 使用在线备份 API 或停止待迁移实例写入后完整拷贝，不能漏掉正在使用的 WAL。恢复时保留 subject_scope 和设备映射，先用独立实例验证。私有备份不进入 Git。

本次整理不调整运行中的角色/数据库/容器，也不推送远程。使用独立项目名，勿对现有线上项目执行 `down -v`。
