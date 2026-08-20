/**
 * Данные для аудита вёрстки. Только для разработки: каталог `audit/` не входит
 * в сборку (входная точка Vite одна — index.html), в образ не попадает и на
 * боевом домене не существует.
 *
 * Строки нарочно недобрые: длинные заголовки без пробелов, кириллица вперемешку
 * с латиницей, имена в две строки — на них и ломается вёрстка.
 */
export const PROJECT = { id: 1, name: 'Линия Жизни Битрикс24', client: 'Линия Жизни', b24_group_id: 11 }

const LONG = 'Не отправляется отчёт по контрагентам из раздела ЭДО — воспроизводится у всех операторов смены'

export const TASKS = [
  {
    id: 233, title: LONG, status: 3, status_title: 'Выполняется', status_emoji: '',
    stage_id: 335, priority: 2, deadline: '2026-08-19T18:00:00+03:00', overdue: true,
    created_date: '2026-08-14T09:12:00+03:00', closed_date: null, parent_id: null, depth: 0,
    responsible: { id: 7, name: 'Константин Стародубцев-Оболенский' },
    creator: { id: 1, name: 'Ирина П.' }, project: PROJECT,
  },
  {
    id: 241, title: 'Подзадача: собрать логи шлюза', status: 2, status_title: 'Ждёт выполнения',
    status_emoji: '', stage_id: 335, priority: 1, deadline: '2026-08-20T12:00:00+03:00',
    overdue: false, created_date: '2026-08-15T10:00:00+03:00', closed_date: null,
    parent_id: 233, depth: 1, responsible: { id: 8, name: 'Пётр' },
    creator: { id: 1, name: 'Ирина П.' }, project: PROJECT,
  },
  {
    id: 250, title: 'СверхдлинноеСловоБезПробеловКотороеДолжноОбрезатьсяМноготочиемАНеРазорватьСетку',
    status: 4, status_title: 'Ожидает контроля', status_emoji: '', stage_id: 333, priority: 0,
    deadline: null, overdue: false, created_date: '2026-08-16T11:00:00+03:00', closed_date: null,
    parent_id: null, depth: 0, responsible: { id: 0, name: '' },
    creator: { id: 1, name: 'Ирина П.' }, project: PROJECT,
  },
  {
    id: 252, title: 'Обновить сертификат на шлюзе оплаты', status: 5, status_title: 'Завершена',
    status_emoji: '', stage_id: 337, priority: 1, deadline: '2026-08-25T18:00:00+03:00',
    overdue: false, created_date: '2026-08-17T15:30:00+03:00',
    closed_date: '2026-08-18T12:00:00+03:00', parent_id: null, depth: 0,
    responsible: { id: 9, name: 'Анна Кузнецова' }, creator: { id: 1, name: 'Ирина П.' },
    project: PROJECT,
  },
  {
    id: 258, title: 'Отложено до решения вендора', status: 6, status_title: 'Отложена',
    status_emoji: '', stage_id: 0, priority: 1, deadline: '2026-09-01T10:00:00+03:00',
    overdue: false, created_date: '2026-08-18T09:00:00+03:00', closed_date: null,
    parent_id: null, depth: 0, responsible: { id: 9, name: 'Анна Кузнецова' },
    creator: { id: 1, name: 'Ирина П.' }, project: PROJECT,
  },
]

export const CARD = {
  ...TASKS[0],
  description:
    'Воспроизводится с 14 августа у всех операторов дневной смены.\n\nШаги: ЭДО → Отчёты → Контрагенты → Выгрузить. Крутится и падает по таймауту.',
  accomplices: [], auditors: [], tags: ['эдо', 'срочно'],
  allowed: ['complete', 'pause', 'defer', 'edit'],
  changed_date: '2026-08-18T10:00:00+03:00', stage_title: 'Выполняются',
  time_spent: 19800, portal_url: 'https://devondev.bitrix24.ru/company/personal/user/1/tasks/task/view/233/',
}

const ROUTES: Record<string, unknown> = {
  '/api/miniapp/bootstrap': {
    state: 'ok',
    me: { tg_user_id: 1, name: 'Ирина П.', username: 'irina', b24_user_id: 1 },
    portal: 'devondev.bitrix24.ru',
    context: {
      chat_ref: 11, title: 'Линия Жизни — поддержка', pinned: false, task_id: null,
      projects: [PROJECT],
    },
  },
  '/api/miniapp/tasks': {
    items: TASKS, total: 5, truncated: false,
    stages: [
      { project_id: 1, id: 333, title: 'Новые' },
      { project_id: 1, id: 335, title: 'Выполняются' },
      { project_id: 1, id: 337, title: 'Сделаны' },
    ],
    context: {
      chat_ref: 11, title: 'Линия Жизни — поддержка', pinned: false, task_id: null,
      projects: [PROJECT],
    },
  },
  '/api/miniapp/tasks/233': CARD,
  '/api/miniapp/tasks/233/comments': {
    items: [
      { id: 1, author: 'Ирина Полякова', text: 'Логи приложила, смотрите вложение.', date: '2026-08-18T09:30:00+03:00' },
      { id: 2, author: 'Пётр', text: 'Вижу таймаут на стороне вендора. Написал им, жду ответа до вечера.\n\nПараллельно проверяю обходной путь через выгрузку по частям.', date: '2026-08-18T10:15:00+03:00' },
    ],
  },
  '/api/miniapp/approvals': {
    items: [
      { id: 1, task_id: 261, title: 'Заменить принтер в 4 кабинете — заявка от бухгалтерии', project: { id: 1, name: 'Линия Жизни Битрикс24', client: 'Линия Жизни' }, requested_at: '2026-08-19T14:00:00+03:00' },
      { id: 2, task_id: 262, title: 'Доступ к CRM для нового сотрудника', project: { id: 1, name: 'Линия Жизни Битрикс24', client: 'Линия Жизни' }, requested_at: '2026-08-19T16:20:00+03:00' },
    ],
    total: 2,
  },
  '/api/miniapp/contexts': {
    items: [
      { chat_ref: 11, title: 'Линия Жизни — поддержка', projects: [PROJECT] },
      { chat_ref: 12, title: 'DEVON внутренний', projects: [{ id: 2, name: 'Devon SD BOT', client: 'DEVON', b24_group_id: 1 }] },
    ],
    filtered: false,
  },
  '/api/miniapp/projects/1/members': {
    items: [
      { id: 7, name: 'Константин Стародубцев-Оболенский', position: 'Инженер поддержки', role: 'user' },
      { id: 8, name: 'Пётр', position: '', role: 'user' },
      { id: 9, name: 'Анна Кузнецова', position: 'Руководитель', role: 'tenant_admin' },
    ],
  },
  '/api/miniapp/summary': {
    projects: [{
      id: 1, name: 'Линия Жизни Битрикс24', client: 'Линия Жизни', open: 14,
      stages: [
        { id: 333, title: 'Новые', count: 5 },
        { id: 335, title: 'Выполняются', count: 6 },
        { id: 337, title: 'Сделаны', count: 1 },
      ],
      outside: 1, outside_title: 'Вне канбана',
      unresolved: 1, unresolved_title: 'Стадия не опознана',
      overdue: 3, mine: 4,
    }],
    open: 14, overdue: 3, mine: 4,
  },
  '/api/miniapp/timesheet/months': {
    items: [
      { value: '2026-08', title: 'Август 2026' },
      { value: '2026-07', title: 'Июль 2026' },
      { value: '2026-06', title: 'Июнь 2026' },
    ],
    current: '2026-08',
  },
  '/api/miniapp/timesheet': {
    month: '2026-08', title: 'Август 2026',
    by_status: [
      { title: 'Выполняется', seconds: 43200, tasks: 4 },
      { title: 'Завершена', seconds: 28800, tasks: 3 },
    ],
    by_stage: [
      { title: 'Выполняются', seconds: 43200, tasks: 4 },
      { title: 'Сделаны', seconds: 25200, tasks: 2 },
      { title: 'Вне канбана', seconds: 3600, tasks: 1 },
    ],
    total_seconds: 72000, task_count: 7, entry_count: 79,
    complete: false, seen: 60, total_on_portal: 79,
    projects: [{ id: 1, name: 'Линия Жизни Битрикс24', client: 'Линия Жизни' }],
  },
  '/api/miniapp/surveys': {
    items: [
      { id: 1, title: 'Не работает' },
      { id: 2, title: 'Нужен доступ' },
    ],
  },
  '/api/miniapp/surveys/1': {
    items: [
      { code: 'what', text: 'Что именно не работает?', required: true, kind: 'text',
        options: [], field: '' },
      { code: 'urgency', text: 'Насколько срочно?', required: true, kind: 'choice',
        options: [
          { value: '2', label: 'Горит' },
          { value: '1', label: 'Сегодня' },
          { value: '0', label: 'Может подождать' },
        ],
        field: 'PRIORITY' },
      { code: 'when', text: 'К какому сроку нужно?', required: false, kind: 'text',
        options: [], field: 'DEADLINE' },
    ],
  },
  '/api/miniapp/me': {
    tenant: { id: 1, name: 'DEVON' },
    portal: 'devondev.bitrix24.ru',
    b24_user_id: 7, tg_user_id: 42,
    name: 'Ирина Полякова', username: 'irina', role: 'tenant_admin',
  },
  '/api/miniapp/projects/1/stages': {
    items: [
      { id: 333, title: 'Новые', sort: 100 },
      { id: 335, title: 'Выполняются', sort: 200 },
      { id: 337, title: 'Сделаны', sort: 300 },
    ],
  },
}

/** Подменяет fetch: сервера при аудите нет, а экранам нужны данные. */
export function stubFetch(): void {
  window.fetch = async (input: RequestInfo | URL) => {
    const path = new URL(String(input), window.location.origin).pathname
    const body = ROUTES[path] ?? { error: { code: 'not_found', message: 'Нет фикстуры.' } }
    return new Response(JSON.stringify(body), {
      status: ROUTES[path] ? 200 : 404,
      headers: { 'Content-Type': 'application/json' },
    })
  }
}
