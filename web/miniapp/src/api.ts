/**
 * Клиент нашего API.
 *
 * Заголовок `Authorization: tma <initData>` идёт на каждом запросе: своей сессии
 * у мини-аппа нет, подпись Telegram и есть удостоверение. Ошибки приходят в одном
 * формате {error:{code,message}} и здесь превращаются в ApiError с кодом — экраны
 * различают «просрочена подпись» и «нет прав», а не показывают одно «что-то пошло не так».
 */
import { tg } from './telegram'
import type {
  Bootstrap,
  Comment,
  Context,
  ContextListItem,
  Member,
  Stage,
  Task,
  TaskCard,
  TaskFilter,
} from './types'

export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown>

  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message)
    this.status = status
    this.code = code
    this.details = details ?? {}
  }
}

const BASE = '/api/miniapp'

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; query?: Record<string, string | number | undefined> } = {},
): Promise<T> {
  const url = new URL(BASE + path, window.location.origin)
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== '') url.searchParams.set(key, String(value))
  }

  let response: Response
  try {
    response = await fetch(url.toString(), {
      method: options.method ?? 'GET',
      headers: {
        Authorization: `tma ${tg.initData()}`,
        ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    })
  } catch {
    throw new ApiError(0, 'network', 'Нет связи с сервером. Проверьте интернет.')
  }

  if (response.status === 204) return undefined as T

  let payload: unknown = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    const error = (payload as { error?: { code?: string; message?: string; details?: Record<string, unknown> } })?.error
    throw new ApiError(
      response.status,
      error?.code ?? 'unknown',
      error?.message ?? 'Запрос не выполнен.',
      error?.details,
    )
  }
  return payload as T
}

export type TaskListResponse = {
  items: Task[]
  total: number
  truncated: boolean
  stages: Stage[]
  context: Context
}

/**
 * Контекст чата из адреса страницы.
 *
 * Из группы он приезжает в `startapp` внутри initData, но кнопка `web_app` в личке
 * `startapp` нести не умеет — там контекст кладётся в query. Строка подписана нашим
 * ключом, проверяет её сервер; здесь она просто передаётся дальше.
 */
export function ctxFromUrl(): string {
  return new URLSearchParams(window.location.search).get('ctx') ?? ''
}

export const api = {
  bootstrap: () => request<Bootstrap>('/bootstrap', { query: { ctx: ctxFromUrl() } }),

  contexts: () => request<{ items: ContextListItem[]; filtered: boolean }>('/contexts'),

  tasks: (chatRef: number, filter: TaskFilter, q: string, projectId?: number) =>
    request<TaskListResponse>('/tasks', {
      query: { chat_ref: chatRef, filter, q, project_id: projectId },
    }),

  task: (chatRef: number, id: number) =>
    request<TaskCard>(`/tasks/${id}`, { query: { chat_ref: chatRef } }),

  action: (chatRef: number, id: number, act: string) =>
    request<TaskCard>(`/tasks/${id}/action`, {
      method: 'POST',
      query: { chat_ref: chatRef },
      body: { act },
    }),

  patch: (chatRef: number, id: number, fields: Record<string, unknown>) =>
    request<TaskCard>(`/tasks/${id}`, {
      method: 'PATCH',
      query: { chat_ref: chatRef },
      body: fields,
    }),

  comments: (chatRef: number, id: number) =>
    request<{ items: Comment[] }>(`/tasks/${id}/comments`, { query: { chat_ref: chatRef } }),

  addComment: (chatRef: number, id: number, text: string) =>
    request<{ items: Comment[] }>(`/tasks/${id}/comments`, {
      method: 'POST',
      query: { chat_ref: chatRef },
      body: { text },
    }),

  create: (chatRef: number, body: Record<string, unknown>) =>
    request<TaskCard>('/tasks', { method: 'POST', query: { chat_ref: chatRef }, body }),

  members: (chatRef: number, projectId: number) =>
    request<{ items: Member[] }>(`/projects/${projectId}/members`, {
      query: { chat_ref: chatRef },
    }),

  stages: (chatRef: number, projectId: number) =>
    request<{ items: { id: number; title: string; sort: number }[] }>(
      `/projects/${projectId}/stages`,
      { query: { chat_ref: chatRef } },
    ),
}
