# KnowHub AI

KnowHub AI 是一个面向个人或小团队使用的私有化 GPT 风格 Web 应用。项目包含 Vue 聊天前端、FastAPI 后端、用户级模型 API Key 加密存储、OpenAI 兼容流式输出、对话历史、附件解析、后台管理、用量统计，以及 Docker Compose 一键部署。

## 功能特性

- GPT 风格聊天界面，支持流式回复、Markdown、代码高亮、数学公式渲染，以及长用户提问自动折叠。
- 用户登录、CSRF 防护、Session Cookie、首次登录强制修改临时密码。
- 用户 API Key 加密存储，支持模型发现和当前 Key 切换。
- 对话历史、对话列表软删除、上下文统计、手动/自动上下文压缩。
- 文件附件上传，支持本地存储、文本/PDF/DOCX 解析、图片缩略图、视觉模型图片理解和清理任务。
- 管理后台支持用户、API Key 分组、配额、统计、运行时配置、清理任务和失败消息查看。
- Docker 一键部署，包含 Redis、后端 Worker、FastAPI 后端和 Nginx 前端网关。

## 技术栈

- 前端：Vue 3、Vite、Pinia、Tailwind CSS、markdown-it、KaTeX、highlight.js。
- 后端：FastAPI、SQLAlchemy async、默认 SQLite、Redis、ARQ、httpx。
- 部署：Docker Compose、Nginx、Redis。

## Docker 快速启动

### 环境要求

- 已安装 Docker 和 Docker Compose v2
- 一个兼容 OpenAI 接口格式的模型 API Key

### 1. 克隆项目

```bash
git clone git@github.com:zibi666/KnowHub_AI.git
cd KnowHub_AI
```

### 2. 创建环境变量文件

Linux/macOS：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

### 3. 生成加密密钥

运行下面的命令生成一个新的加密密钥：

```bash
python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

然后把生成结果填入 `.env` 文件中的：

```text
APP_ENCRYPTION_KEY=你的生成结果
```

这个密钥用于加密存储用户的模型 API Key。请妥善保存，不要提交到 Git。

### 4. 启动服务

```bash
docker compose up -d --build
```

### 5. 打开网页

```text
http://localhost:8080
```

默认首次登录账号：

- 用户名：`admin`
- 密码：`ChangeMe123!`

登录后请先修改临时密码，然后在设置页或 API 管理页添加你的模型 API Key。

## 重要环境变量

`.env` 中最常用的配置如下：

```text
APP_ENCRYPTION_KEY=...
ADMIN_INITIAL_USERNAME=admin
ADMIN_INITIAL_PASSWORD=ChangeMe123!
MODEL_BASE_URL=https://nexor.nexoraivision.com/v1
MODEL_API_MODE=responses
DATABASE_URL=sqlite+aiosqlite:///./data/app.db
REDIS_URL=redis://redis:6379/0
REDIS_SESSION_URL=redis://redis:6379/1
VISION_MODEL_PATTERNS=gpt-4o,gpt-4.1,gpt-5,o3,o4,vision,vl,gemini,claude
```

请不要提交 `.env`、`data/`、`logs/` 或数据库文件。

## 图片与文件发送机制

KnowHub AI 采用和 Open WebUI、LibreChat 类似的“先上传、再引用”模式：

- 前端先上传附件并拿到 `attachment_id`，发送消息时只提交附件 ID，不把完整文件内容塞进普通请求体。
- 文本、PDF、DOCX 和代码文件会在后端解析为 `context_text`，聊天时按 token 预算包进 `<untrusted_data>`，供模型参考。
- 图片文件会保存原图并生成缩略图；当当前模型命中 `VISION_MODEL_PATTERNS` 时，后端会把本轮图片压缩为最长边 `VISION_IMAGE_MAX_EDGE` 的 JPEG data URL，再追加到 OpenAI-compatible 多模态消息里。
- 如果当前模型不支持图片理解，前后端都会阻止发送并提示切换视觉模型。

图片相关环境变量：

```text
VISION_MODEL_PATTERNS=gpt-4o,gpt-4.1,gpt-5,o3,o4,vision,vl,gemini,claude
VISION_IMAGE_MAX_EDGE=1024
VISION_IMAGE_JPEG_QUALITY=82
VISION_IMAGE_MAX_COUNT=4
```

如果你的上游模型命名不同，把支持图片的模型关键词追加到 `VISION_MODEL_PATTERNS` 即可。

## 公网服务器部署建议

5 人小团队使用时，建议云服务器规格为 2 核 4G 起步，4 核 8G 更稳，系统盘至少 40GB；如果会频繁上传文档或图片，建议 80GB 以上。数据库推荐继续使用云 MySQL，应用附件先保存在服务器本地持久化目录，后续文件量大再迁移到腾讯 COS。

### 1. 准备服务器

在服务器上安装 Docker 和 Docker Compose v2，只开放公网 `80/443`。后端、Redis 和数据库不要直接暴露公网。

```bash
mkdir -p /opt/knowhub
cd /opt/knowhub
git clone -b design/lm git@github.com:zibi666/KnowHub_AI.git .
cp .env.example .env
mkdir -p data/local-storage data/cache logs
```

### 2. 配置 `.env`

生产环境至少修改这些字段：

```text
APP_ENV=production
APP_BASE_DOMAIN=你的域名或公网IP
APP_ENCRYPTION_KEY=使用 README 上方命令生成的 32 字节 base64
ADMIN_INITIAL_USERNAME=admin
ADMIN_INITIAL_PASSWORD=请换成强密码
DATABASE_URL=mysql+aiomysql://数据库用户:数据库密码@数据库地址:端口/knowhub_ai?charset=utf8mb4
MODEL_BASE_URL=你的 OpenAI-compatible 上游地址
MODEL_API_MODE=chat_completions
LOCAL_STORAGE_ROOT=./data/local-storage
LOCAL_CACHE_ROOT=./data/cache
```

`.env` 只放在服务器本地，不要提交到 GitHub。以后云数据库地址变更，只需要修改 `DATABASE_URL` 并重启容器。

### 3. 初始化数据库

连接云 MySQL 后执行：

```sql
CREATE DATABASE IF NOT EXISTS `knowhub_ai`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
```

也可以直接执行仓库里的 `scripts/sql/init_mysql.sql`。

### 4. 启动服务

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8080/readyz
```

确认返回类似：

```json
{"ok":true,"db":true}
```

### 5. 配置 HTTPS

如果有域名，推荐在宿主机 Nginx/Caddy 上做 HTTPS 反代到 `127.0.0.1:8080`；如果只用公网 IP，也可以先开放 `8080` 测试，但正式给 5 人使用建议走 HTTPS。

### 6. 5 人使用与备份

- 用管理员账号创建或启用 5 个用户，关闭不必要的公开注册入口。
- 每个用户绑定自己的模型 API Key，或由管理员统一分配。
- 每天备份云 MySQL；同时备份 `/opt/knowhub/data/local-storage` 和 `.env`。
- 服务器迁移时，复制 `.env` 和 `data/local-storage`，再连接同一个云 MySQL 即可恢复主要数据。

## 本地开发

### 后端

```bash
cd backend
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

Linux/macOS：

```bash
source .venv/bin/activate
```

安装依赖并启动后端：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端

```bash
cd frontend
npm ci
npm run dev
```

打开：

```text
http://localhost:5173
```

如果不使用 Docker 进行本地开发，请准备本地 Redis，或者修改 `.env` 中的 `REDIS_URL` 和 `REDIS_SESSION_URL`。

## 常用命令

构建前端：

```bash
cd frontend
npm run build
```

检查后端语法：

```bash
cd backend
python -m compileall app
```

查看 Docker 日志：

```bash
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f nginx
```

停止服务：

```bash
docker compose down
```

删除本地运行数据：

```bash
docker compose down -v
rm -rf data logs
```

Windows PowerShell 可使用：

```powershell
docker compose down -v
Remove-Item -Recurse -Force data, logs
```

## 数据存储

默认运行数据会保存在：

- `data/app.db`
- `data/local-storage`
- `data/cache`
- `logs/`
- Docker volume：`redis-data`

这些内容已经被 `.gitignore` 忽略，不会上传到 GitHub。

## 模型服务配置

后端默认调用兼容 OpenAI 的模型接口：

```text
MODEL_BASE_URL=https://nexor.nexoraivision.com/v1
MODEL_API_MODE=responses
```

如果你的模型服务只支持 Chat Completions，把 `.env` 改成：

```text
MODEL_API_MODE=chat_completions
```

修改后重新启动：

```bash
docker compose up -d --build
```

## 安全建议

- 对外开放服务前，请修改 `ADMIN_INITIAL_PASSWORD`。
- 添加 API Key 前，请生成唯一的 `APP_ENCRYPTION_KEY`。
- 请保管好 `.env`。如果丢失或更换 `APP_ENCRYPTION_KEY`，已加密保存的 API Key 将无法解密。
- 生产环境建议放在 HTTPS 后面。
- 使用 `MODEL_BASE_URL_ALLOWED_HOSTS` 限制允许访问的上游模型域名。

## 项目结构

```text
backend/   FastAPI 后端、数据模型、业务服务、异步任务
frontend/  Vue 3 单页前端应用
nginx/     生产环境 Nginx 网关和前端构建镜像
data/      运行数据，已被 Git 忽略
logs/      运行日志，已被 Git 忽略
```

## 许可证

当前默认为私有项目。如需开源，请补充对应的 LICENSE 文件。
