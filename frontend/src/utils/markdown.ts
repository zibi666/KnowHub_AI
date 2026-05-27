import MarkdownIt from 'markdown-it'
import texmath from 'markdown-it-texmath'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'
import katex from 'katex'

const escapeHtml = MarkdownIt().utils.escapeHtml

function languageLabel(language: string) {
  const normalized = language.trim().toLowerCase()
  const labels: Record<string, string> = {
    js: 'JavaScript',
    javascript: 'JavaScript',
    ts: 'TypeScript',
    typescript: 'TypeScript',
    py: 'Python',
    python: 'Python',
    shell: 'Shell',
    bash: 'Shell',
    sh: 'Shell',
    zsh: 'Shell',
    powershell: 'PowerShell',
    ps1: 'PowerShell',
    html: 'HTML',
    css: 'CSS',
    json: 'JSON',
    yaml: 'YAML',
    yml: 'YAML',
    sql: 'SQL',
    vue: 'Vue',
    md: 'Markdown',
    markdown: 'Markdown',
    text: '文本',
    txt: '文本'
  }
  return labels[normalized] || language.trim() || '代码'
}

function codeBlock(code: string, language: string) {
  const normalizedLanguage = language.trim().toLowerCase()
  const label = languageLabel(normalizedLanguage)
  const highlighted =
    normalizedLanguage && hljs.getLanguage(normalizedLanguage)
      ? hljs.highlight(code, { language: normalizedLanguage }).value
      : escapeHtml(code)

  return `<div class="code-block" data-code="${encodeURIComponent(code)}">
    <div class="code-block-header">
      <span class="code-language"><span class="code-language-icon">&lt;/&gt;</span>${escapeHtml(label)}</span>
      <button class="code-copy-button" type="button" aria-label="复制代码" title="复制代码">复制</button>
    </div>
    <pre><code class="hljs language-${escapeHtml(normalizedLanguage || 'text')}">${highlighted}</code></pre>
  </div>`
}

const md: MarkdownIt = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: false
}).use(texmath, {
  engine: katex,
  delimiters: ['dollars', 'brackets', 'beg_end', 'gitlab'],
  katexOptions: {
    throwOnError: false,
    strict: 'ignore',
    trust: false,
    output: 'htmlAndMathml',
    displayMode: false
  }
})

md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx]
  const language = token.info.trim().split(/\s+/)[0] || ''
  return codeBlock(token.content, language)
}

md.renderer.rules.image = () => ''

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const href = tokens[idx].attrGet('href') || ''
  if (/^https?:\/\//i.test(href)) {
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
  }
  return self.renderToken(tokens, idx, options)
}

export function renderMarkdown(source: string): string {
  const html = md.render(source)
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true, mathMl: true },
    ADD_TAGS: [
      'eq',
      'eqn',
      'math',
      'semantics',
      'annotation',
      'mrow',
      'mi',
      'mn',
      'mo',
      'msup',
      'msub',
      'msubsup',
      'mfrac',
      'msqrt',
      'mroot',
      'mtext',
      'mspace',
      'mtable',
      'mtr',
      'mtd',
      'munderover',
      'munder',
      'mover',
      'mpadded',
      'menclose',
      'svg',
      'path'
    ],
    ADD_ATTR: [
      'target',
      'rel',
      'data-code',
      'title',
      'aria-label',
      'aria-hidden',
      'encoding',
      'xmlns',
      'display',
      'alttext',
      'stretchy',
      'accent',
      'width',
      'height',
      'viewBox',
      'preserveAspectRatio',
      'd',
      'fill-rule',
      'fill-opacity',
      'stroke-width',
      'stroke-linecap',
      'stroke-linejoin',
      'stroke-miterlimit',
      'stroke-dasharray',
      'stroke-dashoffset',
      'stroke-opacity'
    ]
  })
}

const markdownCache = new Map<string, string>()
const MAX_MARKDOWN_CACHE_ENTRIES = 120
const MAX_CACHED_MARKDOWN_LENGTH = 200_000

export function renderMarkdownCached(source: string): string {
  if (source.length > MAX_CACHED_MARKDOWN_LENGTH) return renderMarkdown(source)

  const cached = markdownCache.get(source)
  if (cached !== undefined) return cached

  const rendered = renderMarkdown(source)
  markdownCache.set(source, rendered)

  if (markdownCache.size > MAX_MARKDOWN_CACHE_ENTRIES) {
    const oldest = markdownCache.keys().next().value
    if (oldest !== undefined) markdownCache.delete(oldest)
  }

  return rendered
}
