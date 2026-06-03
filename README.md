# KnowHub AI

KnowHub AI 是一个面向个人和小团队的私有化 GPT 风格 Web 应用。项目当前维护在 `design/lm` 分支，当前版本为 `V4.3.10`，支持聊天、图像生成、附件解析、会话级文件树、账号管理、用量统计和 Docker Compose 一键部署。

## 功能特性

- GPT 风格聊天界面：支持流式回复、Markdown、代码高亮、数学公式渲染、长问题折叠和自动滚动。
- 多模型与 API Key：用户可配置自己的 OpenAI-compatible API Key，支持模型发现、当前 Key 切换、管理员分组、自定义 BaseURL 和 `/v1` 兜底探测。
- 图像生成：支持 `image-2`、`image-1.5`、`image-1` 等图像模型，生成过程可展示阶段进度和 partial images。
- 附件与视觉理解：支持文本、PDF、DOCX、代码文件解析，图片会生成缩略图，并可在视觉模型下参与多模态对话；同一对话后续追问可通过会话文件树继续勾选引用历史文档和图片。
- 对话管理：支持历史会话、搜索聊天、对话软删除、删除确认、上下文统计、手动/自动上下文压缩、流式思考计时和右侧悬浮文件树。
- 后台管理：支持用户、API Key 分组、配额、运行时配置、清理任务、失败消息和用量统计。
- 版本控制页面：应用内可查看版本更新记录，便于回顾每次聊天、图像和部署能力的迭代。
- 私有化部署：提供 FastAPI 后端、ARQ Worker、Redis、Nginx 前端网关和 Docker Compose 部署方案。

## 技术栈

- 前端：Vue 3、Vite、Pinia、Tailwind CSS、markdown-it、KaTeX、highlight.js。
- 后端：FastAPI、SQLAlchemy async、SQLite/MySQL、Redis、ARQ、httpx。
- 部署：Docker Compose、Nginx、Redis。

## Docker 快速启动

### 环境要求

- 已安装 Docker 和 Docker Compose v2。
- 准备一个兼容 OpenAI 接口格式的模型 API Key。

### 1. 克隆项目

```bash
git clone -b design/lm git@github.com:zibi666/KnowHub_AI.git
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

把生成结果填入 `.env`：

```text
APP_ENCRYPTION_KEY=你的生成结果
```

这个密钥用于加密存储用户的模型 API Key。请妥善保存，不要提交到 Git。

### 4. 配置模型服务

按你的上游服务修改 `.env` 中的模型配置：

```text
MODEL_BASE_URL=https://你的模型服务地址/v1
MODEL_API_MODE=responses
```

如果上游只支持 Chat Completions，把 `MODEL_API_MODE` 改成：

```text
MODEL_API_MODE=chat_completions
```

### 5. 启动服务

```bash
docker compose up -d --build
```

打开：

```text
http://localhost:8080
```

默认首次登录账号：

- 用户名：`admin`
- 密码：`ChangeMe123!`

登录后请先修改临时密码，然后在设置页或 API 管理页添加你的模型 API Key。

## 重要环境变量

`.env` 中常用配置如下：

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
VISION_IMAGE_MAX_EDGE=1024
VISION_IMAGE_JPEG_QUALITY=82
VISION_IMAGE_MAX_COUNT=8
UPLOAD_RATE_LIMIT_PER_HOUR=0
```

请不要提交 `.env`、`data/`、`logs/` 或数据库文件。

## 图片与文件机制

KnowHub AI 采用“先上传、再引用”的附件模式：

- 前端先上传附件并拿到 `attachment_id`，发送消息时只提交附件 ID，不把完整文件内容塞进普通请求体。
- 文本、PDF、DOCX 和代码文件会在后端解析为 `context_text`，聊天时按 token 预算包进 `<untrusted_data>`。
- 图片文件会保存原图并生成缩略图；当当前模型命中 `VISION_MODEL_PATTERNS` 时，后端会把图片压缩后追加到多模态消息里。
- 如果当前模型不支持图片理解，前后端都会阻止发送，并提示切换视觉模型。
- 图像生成模型会进入图片生成流程，生成结果保存为附件，刷新页面后仍可查看。

## 公网服务器部署建议

5 人小团队使用时，建议云服务器规格为 2 核 4G 起步，4 核 8G 更稳，系统盘至少 40GB；如果会频繁上传文档或图片，建议 80GB 以上。

数据库可以继续使用默认 SQLite，也可以切换到云 MySQL。生产环境建议使用云 MySQL，并把应用附件保存在服务器本地持久化目录，后续文件量大再迁移到对象存储。

### 1. 准备服务器

在服务器上安装 Docker 和 Docker Compose v2，只开放公网 `80/443`。后端、Redis 和数据库不要直接暴露公网。

```bash
mkdir -p /opt/knowhub
cd /opt/knowhub
git clone -b design/lm git@github.com:zibi666/KnowHub_AI.git .
cp .env.example .env
mkdir -p data/local-storage data/cache logs
```

### 2. 配置生产环境

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

`.env` 只放在服务器本地，不要提交到 GitHub 或 Gitee。以后云数据库地址变更，只需要修改 `DATABASE_URL` 并重启容器。

### 3. 初始化 MySQL

如果使用 MySQL，先创建数据库：

```sql
CREATE DATABASE IF NOT EXISTS `knowhub_ai`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;
```

也可以直接执行仓库里的 `scripts/sql/init_mysql.sql`。

### 4. 启动与检查

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8080/readyz
```

确认返回类似：

```json
{"ok":true,"db":true}
```

如果有域名，推荐在宿主机 Nginx/Caddy 上做 HTTPS 反代到 `127.0.0.1:8080`。如果只用公网 IP，也可以先开放 `8080` 测试，但正式使用建议走 HTTPS。

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

Windows PowerShell：

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

这些内容已经被 `.gitignore` 忽略，不会上传到 GitHub 或 Gitee。

## 安全建议

- 对外开放服务前，请修改 `ADMIN_INITIAL_PASSWORD`。
- 添加 API Key 前，请生成唯一的 `APP_ENCRYPTION_KEY`。
- 妥善保存 `.env`。如果丢失或更换 `APP_ENCRYPTION_KEY`，已加密保存的 API Key 将无法解密。
- 生产环境建议放在 HTTPS 后面。
- 使用 `MODEL_BASE_URL_ALLOWED_HOSTS` 限制允许访问的上游模型域名。
- 定期备份数据库、`.env` 和 `data/local-storage`。

## 项目结构

```text
backend/   FastAPI 后端、数据模型、业务服务和异步任务
frontend/  Vue 3 单页前端应用
nginx/     生产环境 Nginx 网关和前端构建镜像
scripts/   初始化脚本和辅助工具
data/      运行数据，已被 Git 忽略
logs/      运行日志，已被 Git 忽略
```

## 许可说明

当前默认为私有项目。如需开源，请补充对应的 LICENSE 文件。
