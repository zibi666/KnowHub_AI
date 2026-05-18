# KnowHub AI

KnowHub AI 是一个面向个人或小团队使用的私有化 GPT 风格 Web 应用。项目包含 Vue 聊天前端、FastAPI 后端、用户级模型 API Key 加密存储、OpenAI 兼容流式输出、对话历史、附件解析、后台管理、用量统计，以及 Docker Compose 一键部署。

## 功能特性

- GPT 风格聊天界面，支持流式回复、Markdown、代码高亮、数学公式渲染，以及长用户提问自动折叠。
- 用户登录、CSRF 防护、Session Cookie、首次登录强制修改临时密码。
- 用户 API Key 加密存储，支持模型发现和当前 Key 切换。
- 对话历史、对话列表软删除、上下文统计、手动/自动上下文压缩。
- 文件附件上传，支持本地存储、文本/PDF/DOCX 解析、图片缩略图和清理任务。
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
```

请不要提交 `.env`、`data/`、`logs/` 或数据库文件。

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
