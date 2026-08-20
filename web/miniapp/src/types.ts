export type Person = { id: number | null; name: string }

export type ProjectRef = {
  id: number
  name: string
  client: string
  b24_group_id: number
}

export type Task = {
  id: number
  title: string
  status: number | null
  status_title: string
  status_emoji: string
  stage_id: number | null
  priority: number
  deadline: string | null
  overdue: boolean
  created_date: string | null
  closed_date: string | null
  parent_id: number | null
  depth: number
  responsible: Person
  creator: Person
  project: ProjectRef | null
}

export type TaskCard = Task & {
  description: string
  accomplices: number[]
  auditors: number[]
  tags: string[]
  allowed: string[]
  changed_date: string | null
  /** Название колонки канбана: своего названия портал в задаче не отдаёт, только id. */
  stage_title: string
  /** Сумма списаний времени по задаче, секунды. */
  time_spent: number
  portal_url: string
  not_applied?: string[]
  created?: boolean
}

export type Stage = { project_id: number; id: number; title: string }

export type Context = {
  chat_ref: number
  title: string
  pinned: boolean
  task_id: number | null
  projects: ProjectRef[]
}

export type Bootstrap =
  | {
      state: 'ok'
      me: { tg_user_id: number; name: string; username: string; b24_user_id: number }
      portal: string
      context: Context | null
    }
  | { state: 'not_linked'; bot: string; message: string }

export type ContextListItem = {
  chat_ref: number
  title: string
  projects: ProjectRef[]
}

export type Member = { id: number; name: string; position: string; role: string }

export type Approval = {
  id: number
  task_id: number
  title: string
  project: { id: number; name: string; client: string }
  requested_at: string
}

export type Comment = { id: number | string; author: string; text: string; date: string }

/* ------------------------------------------------- паритет с ботом */

/** Сводка по стадиям канбана — «📊 Сводка» бота. */
export type SummaryProject = {
  id: number
  name: string
  client: string
  open: number
  stages: { id: number; title: string; count: number }[]
  outside: number
  outside_title: string
  unresolved: number
  unresolved_title: string
  overdue: number
  mine: number
}

export type Summary = {
  projects: SummaryProject[]
  open: number
  overdue: number
  mine: number
}

export type Bucket = { title: string; seconds: number; tasks: number }

/** Свод трудозатрат за месяц — «⏱ Трудозатраты» бота. */
export type Timesheet = {
  month: string
  title: string
  by_status: Bucket[]
  by_stage: Bucket[]
  total_seconds: number
  task_count: number
  entry_count: number
  /** Видели ли мы все записи портала. false — сумма снизу, а не точная. */
  complete: boolean
  seen: number
  total_on_portal: number
  projects: { id: number; name: string; client: string }[]
}

export type SurveyTemplate = { id: number; title: string }

export type SurveyQuestion = {
  code: string
  text: string
  required: boolean
  kind: 'text' | 'choice'
  options: { value: string; label: string }[]
  /** Поле задачи, куда уедет ответ. Пусто — ответ уйдёт в описание. */
  field: string
}

export type Me = {
  tenant: { id: number; name: string }
  portal: string
  b24_user_id: number
  tg_user_id: number
  name: string
  username: string
  role: string
}

export const ROLE_TITLES: Record<string, string> = {
  superadmin: 'Суперадминистратор DEVON',
  tenant_admin: 'Администратор теннанта',
  user: 'Пользователь',
}

export type TaskFilter = 'all' | 'mine' | 'overdue' | 'closed'

/** Полное название — для подписи скринридеру и для объяснения в пустом экране. */
export const FILTER_TITLES: Record<TaskFilter, string> = {
  all: 'Все открытые',
  mine: 'Мои',
  overdue: 'Просрочены',
  closed: 'Завершённые',
}

/**
 * Короткое — для самой кнопки. Четыре полных названия не помещаются в 375px
 * и уезжают за край, унося с собой выбранный фильтр.
 */
export const FILTER_SHORT: Record<TaskFilter, string> = {
  all: 'Все',
  mine: 'Мои',
  overdue: 'Просрочены',
  closed: 'Готовые',
}

export const PRIORITY_TITLES: Record<number, string> = {
  0: 'Низкий',
  1: 'Средний',
  2: 'Высокий',
}
