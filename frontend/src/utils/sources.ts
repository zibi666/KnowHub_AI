import type { Message, WebSearchSource } from '../types'

const SOURCE_LIST_RE = /(?:^|\n)#{3,}\s*(?:来源|Sources?)\s*\n([\s\S]*)$/i
const SOURCE_ITEM_RE = /^\s*(?:\d+[\.)]\s*)?(?:\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/\S+))/i

function normalizeUrl(value: string) {
  try {
    const url = new URL(value.trim())
    url.hash = ''
    return url.toString()
  } catch {
    return ''
  }
}

function readableUrlText(value: string) {
  try {
    const url = new URL(value)
    const host = url.hostname.replace(/^www\./, '')
    const path = decodeURIComponent(url.pathname || '')
      .split('/')
      .filter(Boolean)
      .join('/')
    return path ? `${host}/${path}` : host
  } catch {
    return value
  }
}

function fallbackTitleFromUrl(value: string) {
  try {
    const url = new URL(value)
    const host = url.hostname.replace(/^www\./, '')
    const parts = decodeURIComponent(url.pathname || '')
      .split('/')
      .filter(Boolean)
    const lastPart = parts[parts.length - 1]?.replace(/\.[a-z0-9]{1,8}$/i, '').replace(/[-_]+/g, ' ').trim()
    if (lastPart && lastPart.length >= 3 && !/^(index|home|default|search)$/i.test(lastPart)) {
      return `${host} / ${lastPart}`
    }
    return host || value
  } catch {
    return value
  }
}

function textLooksLikeUrl(value: string) {
  return Boolean(normalizeUrl(value)) || /^https?:\/\//i.test(value.trim())
}

function sourceSnippet(raw: WebSearchSource, title: string, canonicalUrl: string) {
  const snippet = String(raw.snippet || '').trim().replace(/\s+/g, ' ')
  if (!snippet) return ''
  if (textLooksLikeUrl(snippet)) return ''
  if (snippet === title || snippet === canonicalUrl || snippet === readableUrlText(canonicalUrl)) return ''
  return snippet
}

export function canonicalSourceUrl(value: string) {
  const normalized = normalizeUrl(value)
  if (!normalized) return ''
  const url = new URL(normalized)
  const host = url.hostname.toLowerCase()
  const parts = url.pathname.split('/').filter(Boolean)

  if (host === 'raw.githubusercontent.com' && parts.length >= 2) {
    return `https://github.com/${parts[0]}/${parts[1]}`
  }
  if ((host === 'github.com' || host === 'www.github.com') && parts.length >= 2) {
    if (parts[2]?.toLowerCase() === 'raw') return `https://github.com/${parts[0]}/${parts[1]}`
    if (parts[2]?.toLowerCase() === 'blob' && parts.length >= 5) {
      const filename = parts[parts.length - 1]?.toLowerCase()
      if (['readme.md', 'readme.mdx', 'readme.markdown', 'readme'].includes(filename)) {
        return `https://github.com/${parts[0]}/${parts[1]}`
      }
    }
  }
  return normalized
}

function githubRepoFromUrl(value: string) {
  const normalized = normalizeUrl(value)
  if (!normalized) return null
  const url = new URL(normalized)
  const host = url.hostname.toLowerCase()
  const parts = url.pathname.split('/').filter(Boolean)
  if (host === 'raw.githubusercontent.com' && parts.length >= 2) return { owner: parts[0], repo: parts[1] }
  if ((host === 'github.com' || host === 'www.github.com') && parts.length >= 2) return { owner: parts[0], repo: parts[1] }
  return null
}

function githubRepoSource(raw: WebSearchSource, canonicalUrl: string) {
  return githubRepoFromUrl(raw.url || '') || githubRepoFromUrl(raw.displayUrl || raw.display_url || '') || githubRepoFromUrl(canonicalUrl)
}

function sourceTitle(raw: WebSearchSource, canonicalUrl: string) {
  const title = String(raw.title || '').trim().replace(/\s+/g, ' ')
  const repo = githubRepoSource(raw, canonicalUrl)
  if (repo) {
    const lowerTitle = title.toLowerCase()
    if (!title || title.startsWith('http://') || title.startsWith('https://') || lowerTitle.includes('readme')) {
      return `${repo.owner}/${repo.repo}`
    }
  }
  if (!title || textLooksLikeUrl(title)) return fallbackTitleFromUrl(canonicalUrl)
  return title
}

export function sourceSiteName(source: WebSearchSource) {
  if (githubRepoFromUrl(source.url || '') || githubRepoFromUrl(source.displayUrl || source.display_url || '')) return 'GitHub'
  const explicit = source.siteName || source.site_name
  if (explicit) return explicit
  try {
    const host = new URL(source.url).hostname.replace(/^www\./, '')
    return host || source.url
  } catch {
    return source.url
  }
}

export function sourceOpenUrl(source: WebSearchSource) {
  return source.displayUrl || source.display_url || canonicalSourceUrl(source.url || '') || source.url
}

export function sourceDisplayUrl(source: WebSearchSource) {
  return readableUrlText(sourceOpenUrl(source))
}

function sourceFaviconUrl(source: WebSearchSource) {
  const explicit = source.faviconUrl || source.favicon_url
  if (explicit && !/raw\.githubusercontent\.com/i.test(explicit)) return explicit
  try {
    const url = new URL(sourceOpenUrl(source))
    return `${url.origin}/favicon.ico`
  } catch {
    return ''
  }
}

function faviconProxyUrl(source: WebSearchSource) {
  const url = sourceOpenUrl(source)
  return url ? `/api/web-search/favicon?url=${encodeURIComponent(url)}` : ''
}

function normalizeSource(raw: WebSearchSource, index: number): WebSearchSource | null {
  const url = canonicalSourceUrl(raw.displayUrl || raw.display_url || raw.url || '')
  if (!url) return null
  const title = sourceTitle(raw, url)
  const source: WebSearchSource = {
    index,
    title,
    url,
    displayUrl: url,
    snippet: sourceSnippet(raw, title, url),
    siteName: raw.siteName || raw.site_name || undefined,
    publishedAt: raw.publishedAt || raw.published_at || undefined,
    faviconUrl: raw.faviconUrl || raw.favicon_url || undefined,
    provider: raw.provider || undefined,
    confidence: typeof raw.confidence === 'number' ? raw.confidence : undefined,
    rerankStatus: raw.rerankStatus || raw.rerank_status || undefined,
    sourceTier: raw.sourceTier || raw.source_tier || undefined,
    matchedTerms: Array.isArray(raw.matchedTerms || raw.matched_terms) ? raw.matchedTerms || raw.matched_terms : undefined,
    supportLevel: raw.supportLevel || raw.support_level || undefined,
    searchDepth: raw.searchDepth || raw.search_depth || undefined,
    degraded: Boolean(raw.degraded) || undefined,
    filterReason: raw.filterReason || raw.filter_reason || undefined
  }
  const repo = githubRepoSource(raw, url)
  source.siteName = source.siteName || (repo ? 'GitHub' : sourceSiteName(source))
  source.faviconUrl = source.faviconUrl || sourceFaviconUrl(source)
  return source
}

export function stripLegacySourcesMarkdown(content: string) {
  return content.replace(SOURCE_LIST_RE, '').trimEnd()
}

export function parseLegacySourcesMarkdown(content: string): WebSearchSource[] {
  const match = content.match(SOURCE_LIST_RE)
  if (!match) return []
  const sources: WebSearchSource[] = []
  const seen = new Set<string>()
  for (const line of match[1].split('\n')) {
    const item = line.match(SOURCE_ITEM_RE)
    if (!item) continue
    const title = item[1] || item[3] || ''
    const href = item[2] || item[3] || ''
    const url = canonicalSourceUrl(href)
    if (!url || seen.has(url)) continue
    const normalized = normalizeSource({ index: sources.length + 1, title, url: href }, sources.length + 1)
    if (normalized) {
      seen.add(normalized.url)
      sources.push(normalized)
    }
    if (sources.length >= 10) break
  }
  return sources
}

export function messageWebSearchSources(message: Message): WebSearchSource[] {
  const rawSources = message.webSearchSources || message.web_search_sources || []
  const normalized: WebSearchSource[] = []
  const seen = new Set<string>()
  for (const raw of rawSources) {
    const source = normalizeSource(raw, normalized.length + 1)
    if (!source || seen.has(source.url)) continue
    seen.add(source.url)
    normalized.push(source)
    if (normalized.length >= 10) break
  }
  return normalized.length ? normalized : parseLegacySourcesMarkdown(message.content || '')
}

export function sourceLabel(source: WebSearchSource) {
  const site = sourceSiteName(source)
  return site ? site.charAt(0).toUpperCase() : String(source.index)
}

export function sourceIconUrls(source: WebSearchSource) {
  const urls: string[] = []
  const add = (value?: string | null) => {
    if (!value) return
    try {
      const url = new URL(value)
      const normalized = url.toString()
      if (!urls.includes(normalized)) urls.push(normalized)
    } catch {
      // Ignore invalid icon candidates.
    }
  }
  const proxyUrl = faviconProxyUrl(source)
  if (proxyUrl) urls.push(proxyUrl)
  add(source.faviconUrl || source.favicon_url)
  try {
    const openUrl = new URL(sourceOpenUrl(source))
    add(`${openUrl.origin}/favicon.ico`)
    add(`https://www.google.com/s2/favicons?domain=${encodeURIComponent(openUrl.hostname)}&sz=64`)
  } catch {
    // Fallback to the text badge.
  }
  return urls
}
