<script setup lang="ts">
import { ArrowLeft, GitBranch } from 'lucide-vue-next'
import { useRouter } from 'vue-router'

type VersionEntry = {
  version: string
  title: string
  date: string
  summary: string
  changes: string[]
}

const router = useRouter()

const versions: VersionEntry[] = [
  {
    version: 'V2.0',
    title: '聊天体验大版本升级',
    date: '2026-05-20',
    summary: '围绕新建对话、顶部控制、输入框、消息展示和图片预览完成一次整体体验升级。',
    changes: [
      '恢复新建对话的欢迎语和输入框展示，左上角模型与推理强度选择更清晰，右上角继续保留 Token、请求数和对话名称。',
      '对话名称支持悬停快速修改，鼠标移到右上角对话信息时会出现编辑按钮，可直接重命名当前对话。',
      '放大模型选择、推理强度、输入框和消息内容字体，并压缩输入框上下留白，让对话区域更紧凑易读。',
      '优化发送按钮位置和输入框布局，减少按钮贴边感，整体操作更顺手。',
      '图片消息按原图大小自动使用小、中、大不同气泡，小图不再被撑大，大图也能保留更合适的预览比例。',
      '保持 8080 端口部署流程，方便在本地直接访问和验证最新版本。'
    ]
  },
  {
    version: 'v1.5',
    title: '搜索聊天与云数据库',
    date: '2026-05-19',
    summary: '补齐历史会话搜索、折叠侧边栏和云 MySQL 数据库存储能力。',
    changes: [
      '新增左侧“搜索聊天”入口，支持关键词检索历史消息，点击结果后自动跳转并高亮命中的对话消息。',
      '新增折叠侧边栏能力，折叠后保留新对话、搜索聊天和展开侧栏按钮，对话页面自适应宽度。',
      '优化全局下拉框选中态间距，思考深度选项里的“低 / 中 / 高 / 极致”和说明文字区分更清楚。',
      '支持通过 .env 切换云 MySQL 数据库，Docker Compose 不再硬编码 SQLite；新增无密码的 MySQL 初始化 SQL 脚本。',
      'MySQL 下长消息、上下文摘要和附件解析文本使用 MEDIUMTEXT / LONGTEXT，存储配额和流量字段改为 BigInteger。'
    ]
  },
  {
    version: 'v1.4',
    title: '统一下拉与设计分支',
    date: '2026-05-19',
    summary: '统一所有下拉框交互和视觉，并将后续设计修改迁移到 design/lm 分支。',
    changes: [
      '新增通用 AppSelect 组件，替换模型、思考深度、个性化设置、API 分组和后台管理里的原生下拉框。',
      '重做下拉选中态，使用柔和左侧高光、玻璃弹层、内阴影和低饱和强调色。',
      '修复“极致”思考深度显示不全的问题。',
      '从 main 创建 design/lm 分支，后续 UI 设计改动集中在该分支维护。'
    ]
  },
  {
    version: 'v1.3',
    title: '聊天体验与流式输出',
    date: '2026-05-18',
    summary: '优化聊天主界面、输入框、消息排序和流式输出节奏。',
    changes: [
      '重设对话流和输入框居中对齐，用户消息、模型回复和输入区域保持同一视觉轴线。',
      '输入框改为更轻量的底部浮层，移除输出 token 控件，保留模型和思考深度选择。',
      '优化流式输出节奏，让模型回复按到达内容逐步渲染，减少整段突然出现的感觉。',
      '修复重新打开对话时长文本用户消息跑到模型回复下方的问题。'
    ]
  },
  {
    version: 'v1.2',
    title: '上下文管理与长文本处理',
    date: '2026-05-18',
    summary: '借鉴 LibreChat 与 Open WebUI 的做法，改善长对话和分支对话的上下文管理。',
    changes: [
      '上下文构建改为分支感知，只沿当前 parent_message_id 链路回溯当前分支消息。',
      '当前用户问题始终完整保留，历史长消息按预算进行 head/tail 压缩。',
      '移除前端上下文压缩圆环和相关视觉负担，但保留后台上下文历史管理逻辑。',
      '附件和长文本按不可信资料处理，降低附件内容伪装指令影响主回答的风险。'
    ]
  },
  {
    version: 'v1.1',
    title: '消息操作与界面细节',
    date: '2026-05-17',
    summary: '补齐对话删除、长文本折叠、返回底部和侧边栏细节。',
    changes: [
      '新增用户侧软删除对话显示，不删除云端已有存储。',
      '用户长文本提问支持默认折叠和点击展开，AI 回复保持完整展开。',
      '新增毛玻璃返回底部按钮，长对话滚动时可以快速回到最新消息。',
      '优化左侧侧边栏账号区域、设置入口和新对话按钮的视觉层级。'
    ]
  },
  {
    version: 'v1.0',
    title: 'KnowHub AI 初始版本',
    date: '2026-05-16',
    summary: '完成项目基础聊天、用户登录、API Key 管理和 Docker 部署。',
    changes: [
      '提供前后端 Docker Compose 一键启动能力。',
      '支持多模型选择、用户登录、会话历史和基础消息流式回复。',
      '支持用户 API Key 管理、管理员分组管理和密钥切换。',
      '完善中文 README，方便下载后直接部署使用。'
    ]
  }
]
</script>

<template>
  <main class="version-page app-page">
    <header class="app-header version-header">
      <button class="app-secondary-button version-back-button" type="button" @click="router.push('/')">
        <ArrowLeft :size="16" />
        <span>返回聊天</span>
      </button>
      <div class="version-header-title">
        <GitBranch :size="18" />
        <span>版本控制</span>
      </div>
    </header>

    <section class="version-shell">
      <div class="version-hero">
        <p class="version-eyebrow">Release Notes</p>
        <h1>版本更新记录</h1>
        <p>按版本整理 KnowHub AI 的主要功能变化，方便回看每次设计、上下文、聊天体验和部署能力的迭代。</p>
      </div>

      <div class="version-timeline">
        <article v-for="entry in versions" :key="entry.version" class="version-card">
          <div class="version-card-marker" aria-hidden="true" />
          <div class="version-card-main">
            <div class="version-card-top">
              <span class="version-badge">{{ entry.version }}</span>
              <time>{{ entry.date }}</time>
            </div>
            <h2>{{ entry.title }}</h2>
            <p>{{ entry.summary }}</p>
            <ul>
              <li v-for="change in entry.changes" :key="change">{{ change }}</li>
            </ul>
          </div>
        </article>
      </div>
    </section>
  </main>
</template>
