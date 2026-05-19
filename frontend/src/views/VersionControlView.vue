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
