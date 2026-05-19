export function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : null
}

export class ApiError extends Error {
  code: string
  status: number

  constructor(code: string, message: string, status: number) {
    super(localizeApiMessage(code, message))
    this.code = code
    this.status = status
  }
}

const ERROR_MESSAGES: Record<string, string> = {
  KEY_REQUIRED: '请先绑定模型 API Key',
  PASSWORD_CHANGE_REQUIRED: '请先修改临时密码',
  INVALID_CREDENTIALS: '账号或密码错误，或登录已失效',
  API_KEY_INVALID: '模型 API Key 无效，请在设置中更新',
  MODEL_NOT_AVAILABLE: '当前模型不可用，请切换模型或联系管理员',
  ATTACHMENT_NOT_READY: '附件仍在解析中，请稍后再试',
  VISION_MODEL_REQUIRED: '当前模型不支持图片理解，请切换到支持视觉的模型',
  QUOTA_EXCEEDED: '已达到额度上限',
  RATE_LIMITED: '操作太频繁，请稍后再试',
  CONTEXT_TOO_LARGE: '上下文过长，请精简消息或拆分附件',
  CSRF_INVALID: '安全校验失败，请刷新页面后重试',
  FORBIDDEN: '没有权限执行此操作',
  SAVE_FAILED: '回复已生成但保存失败，请重试',
  UPSTREAM_ERROR: '上游模型服务异常',
  PARSE_FAILED: '附件解析失败',
  COMPACTION_FAILED: '上下文压缩失败',
  VALIDATION_ERROR: '请求内容格式不正确',
  HTTP_ERROR: '请求失败'
}

export function localizeApiMessage(code: string, fallback = '请求失败') {
  return ERROR_MESSAGES[code] || fallback || '请求失败'
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const csrf = readCookie('csrf_token')
  const headers = new Headers(init.headers)
  if (!headers.has('Content-Type') && init.body && !(init.body instanceof Blob)) {
    headers.set('Content-Type', 'application/json')
  }
  if (csrf) headers.set('X-CSRF-Token', csrf)
  const response = await fetch(`/api${path}`, { ...init, headers, credentials: 'include' })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail || {}
    throw new ApiError(detail.code || 'HTTP_ERROR', detail.message || response.statusText, response.status)
  }
  return response.json() as Promise<T>
}

type StreamJsonLineOptions = {
  splitLargeDeltas?: boolean
}

const STREAM_SPLIT_MIN_CHARS = 5
const STREAM_SPLIT_DELAY_MS = 5

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

function randomChunkSize(remainingChars: number): number {
  if (remainingChars > 1200) return Math.min(Math.floor(Math.random() * 6) + 5, remainingChars)
  if (remainingChars > 240) return Math.min(Math.floor(Math.random() * 4) + 3, remainingChars)
  return Math.min(Math.floor(Math.random() * 3) + 1, remainingChars)
}

async function emitStreamEvent(
  event: string,
  data: any,
  onEvent: (event: string, data: any) => void | Promise<void>,
  options: StreamJsonLineOptions
) {
  const text = data?.text
  if (!options.splitLargeDeltas || event !== 'token' || typeof text !== 'string') {
    await onEvent(event, data)
    return
  }

  const chars = Array.from(text)
  if (chars.length < STREAM_SPLIT_MIN_CHARS) {
    await onEvent(event, data)
    return
  }

  let offset = 0
  while (offset < chars.length) {
    const size = randomChunkSize(chars.length - offset)
    const chunk = chars.slice(offset, offset + size).join('')
    offset += size
    await onEvent(event, { ...data, text: chunk })
    if (offset < chars.length && document.visibilityState !== 'hidden') {
      await sleep(STREAM_SPLIT_DELAY_MS)
    }
  }
}

export async function streamJsonLines(
  path: string,
  body: unknown,
  onEvent: (event: string, data: any) => void | Promise<void>,
  options: StreamJsonLineOptions = { splitLargeDeltas: true }
): Promise<void> {
  const csrf = readCookie('csrf_token')
  const headers = new Headers({ 'Content-Type': 'application/json' })
  if (csrf) headers.set('X-CSRF-Token', csrf)
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
    headers,
    credentials: 'include'
  })
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail || {}
    throw new ApiError(detail.code || 'HTTP_ERROR', detail.message || response.statusText, response.status)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (!line.trim()) continue
      const payload = JSON.parse(line)
      await emitStreamEvent(payload.event, payload.data, onEvent, options)
    }
  }
}
