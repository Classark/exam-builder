import type {
  ChatMessage,
  ChatReply,
  BankPaper,
  BankQuestion,
  BBox,
  AdjustOutcome,
  ApiKeyStatus,
  ClassifiedPreview,
  ClassifiedRequest,
  ClassifiedState,
  ExamOutcome,
  Fix,
  FixStep,
  LiveState,
  AccountState,
  Balance,
  SavedFile,
  SaveResult,
  SavingState,
  PartTags,
  Settings,
  SubjectInfo,
  TaggingState,
  Topic,
  Paper,
  PaperSummary,
  QuestionDetail,
  ScanState,
  QuizDetail,
  QuizItemRef,
  QuizSummary,
  Report,
} from './types'

// Same origin as the page: the packaged app serves the built UI from the backend itself,
// and in development Vite forwards /api to it (vite.config.ts).
const API_BASE = ''

const STATE_TIMEOUT_MS = 10_000

/** An error worth showing as it is: a sentence for the teacher, never "GET /api/x -> 500",
 * "Failed to fetch" or a Python exception's name (user-asked: errors should read like a
 * finished app's). What went wrong technically goes to the console and, from the server,
 * to app.log. */
export class AppError extends Error {
  readonly status?: number

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'AppError'
    this.status = status
  }
}

const UNREACHABLE = "Couldn't reach the app's engine. Check it's still running, then try again."
const TOO_SLOW = 'That took too long to answer. Please try again.'

/** The sentence for a response that failed: the server's own when it wrote one for people
 * (every 4xx it raises, and its catch-all for 500s), else one by status. */
function messageFor(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail && !/^[A-Za-z]+Error\b|Traceback|Internal Server Error/.test(detail)) {
    return detail
  }
  if (status === 404) return "That isn't there any more - it may have been deleted or moved."
  if (status === 409) return 'Something else is using that right now. Wait a moment and try again.'
  if (status === 413) return 'That file is too large.'
  if (status >= 500) return 'Something went wrong on our side. Please try again.'
  return "That didn't work. Please try again."
}

async function send(path: string, init: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, init)
  } catch (e) {
    console.error(`${init.method ?? 'GET'} ${path}`, e)
    const name = e instanceof Error ? e.name : ''
    throw new AppError(name === 'TimeoutError' || name === 'AbortError' ? TOO_SLOW : UNREACHABLE)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    console.error(`${init.method ?? 'GET'} ${path} -> ${response.status}`, body)
    throw new AppError(messageFor(response.status, body.detail), response.status)
  }
  return response
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(path, { headers: { 'Content-Type': 'application/json' }, ...init })
  return response.json() as Promise<T>
}

async function uploadRequest<T>(path: string, form: FormData): Promise<T> {
  // no Content-Type header here - the browser sets the multipart boundary itself
  const response = await send(path, { method: 'POST', body: form })
  return response.json() as Promise<T>
}

/** What to show for anything caught: an AppError's sentence, or a plain one for the rest
 * (a bug in the UI itself - its details go to the console). */
export function errorMessage(e: unknown): string {
  if (e instanceof AppError) return e.message
  console.error(e)
  return 'Something went wrong. Please try again.'
}

export interface PreviewRequest {
  items: QuizItemRef[]
  title?: string
  duration_minutes?: number
  // printed at the right of every page's foot
  footer_note?: string
  // false renders the questions alone, without the paper's cover page
  cover?: boolean
  // the question number the first item gets - for previewing one question in place
  first_number?: number
}

export interface UploadResult {
  paper_key: string
  passed: boolean
  n_issues: number
  n_questions: number
}

export const api = {
  listPapers: () => request<PaperSummary[]>('/api/papers'),
  uploadPaper: (qpFile: File, msFile: File, insertFile?: File | null) => {
    const form = new FormData()
    form.append('qp_file', qpFile)
    form.append('ms_file', msFile)
    if (insertFile) form.append('insert_file', insertFile)
    return uploadRequest<UploadResult>('/api/papers/upload', form)
  },
  // Opens the papers folder in Explorer / Finder, where PDFs go before a scan
  openPapersFolder: () => request<{ folder: string }>('/api/papers/open_folder', { method: 'POST' }),
  // Starts a background scan; progress arrives through getState. reprocess: also extract
  // again every pair whose report has issues - after the parser improves.
  scanPapers: (reprocess = false) =>
    request<ScanState>(`/api/papers/scan${reprocess ? '?reprocess=true' : ''}`, { method: 'POST' }),
  // Never left hanging: a poll that doesn't answer within STATE_TIMEOUT_MS is given up
  // and the next one tried, or one stuck request would stop the app updating for good.
  getState: () => request<LiveState>('/api/state', { signal: AbortSignal.timeout(STATE_TIMEOUT_MS) }),
  /** The last scan's report, kept on disk - "Last scan's results" in the scan menu. */
  lastScan: () => request<ScanState>('/api/papers/scan/last'),
  getBank: () => request<{ version: number; papers: BankPaper[] }>('/api/bank'),
  search: (q: string) =>
    request<{ paper_key: string; question_number: number }[]>(`/api/search?q=${encodeURIComponent(q)}`),
  getQuestion: (paperKey: string, questionNumber: number) =>
    request<QuestionDetail>(`/api/papers/${paperKey}/questions/${questionNumber}`),
  msPageImageUrl: (paperKey: string, page: number) => `${API_BASE}/api/papers/${paperKey}/ms_page/${page}`,
  getMsRows: (paperKey: string) =>
    request<{ rows: Record<string, BBox[]>; page_count: number }>(`/api/papers/${paperKey}/ms_rows`),
  /** Link a part to the mark scheme region drawn round its answer (qbx/ms_links.py). */
  linkMarkScheme: (paperKey: string, partId: string, segments: BBox[]) =>
    request(`/api/papers/${paperKey}/parts/${encodeURIComponent(partId)}/ms_link`, {
      method: 'PUT',
      body: JSON.stringify({ segments }),
    }),
  unlinkMarkScheme: (paperKey: string, partId: string) =>
    request(`/api/papers/${paperKey}/parts/${encodeURIComponent(partId)}/ms_link`, { method: 'DELETE' }),
  reviewIssues: (paperKey: string, keys: string[], checked: boolean) =>
    request<{ checked: string[] }>(`/api/papers/${paperKey}/review`, {
      method: 'POST',
      body: JSON.stringify({ keys, checked }),
    }),
  getPaper: (paperKey: string) => request<Paper>(`/api/papers/${paperKey}`),
  getReport: (paperKey: string) => request<Report>(`/api/papers/${paperKey}/report`),
  pageImageUrl: (paperKey: string, page: number) =>
    `${API_BASE}/api/papers/${paperKey}/page/${page}`,
  assetUrl: (paperKey: string, assetPath: string) =>
    `${API_BASE}/api/papers/${paperKey}/assets/${assetPath.replace(/^assets\//, '')}`,
  updatePart: (paperKey: string, partId: string, updates: Record<string, unknown>) =>
    request<{ fix_id: string }>(`/api/papers/${paperKey}/parts/${encodeURIComponent(partId)}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    }),
  /** A region drawn on the question paper, moved to the question's own content. */
  snapRegion: (paperKey: string, segments: BBox[]) =>
    request<{ segments: BBox[] }>(`/api/papers/${paperKey}/snap`, {
      method: 'POST',
      body: JSON.stringify({ segments }),
    }),
  setQuestionTotal: (paperKey: string, questionNumber: number, total: number) =>
    request<{ fix_id: string }>(`/api/papers/${paperKey}/questions/${questionNumber}/total`, {
      method: 'PUT',
      body: JSON.stringify({ total }),
    }),
  mergePart: (paperKey: string, partId: string) =>
    request<{ fix_id: string }>(`/api/papers/${paperKey}/parts/${encodeURIComponent(partId)}/merge`, {
      method: 'POST',
    }),
  splitPart: (paperKey: string, partId: string, at: { page: number; y: number }) =>
    request<{ fix_id: string }>(`/api/papers/${paperKey}/parts/${encodeURIComponent(partId)}/split`, {
      method: 'POST',
      body: JSON.stringify(at),
    }),
  /** Any Reviewer edit as fix steps, applied together as one fix - the id is for Undo. */
  editPaper: (paperKey: string, steps: FixStep[]) =>
    request<{ fix_id: string }>(`/api/papers/${paperKey}/edit`, {
      method: 'POST',
      body: JSON.stringify({ steps }),
    }),

  listQuestions: (params: { q?: string; subject?: string } = {}) => {
    const search = new URLSearchParams()
    if (params.q) search.set('q', params.q)
    if (params.subject) search.set('subject', params.subject)
    const qs = search.toString()
    return request<BankQuestion[]>(`/api/questions${qs ? `?${qs}` : ''}`)
  },
  listQuizzes: () => request<QuizSummary[]>('/api/quizzes'),
  renameQuiz: (name: string, newName: string) =>
    request<{ name: string }>(`/api/quizzes/${encodeURIComponent(name)}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name: newName }),
    }),
  createQuiz: (name: string) =>
    request<{ name: string; items: QuizItemRef[] }>('/api/quizzes', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  getQuiz: (name: string) => request<QuizDetail>(`/api/quizzes/${encodeURIComponent(name)}`),
  updateQuiz: (name: string, items: QuizItemRef[]) =>
    request(`/api/quizzes/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify({ items }),
    }),
  setQuizDuration: (name: string, minutes: number | null) =>
    request<{ duration_minutes: number | null }>(`/api/quizzes/${encodeURIComponent(name)}/duration`, {
      method: 'PUT',
      body: JSON.stringify({ minutes }),
    }),
  paperUsedIn: (paperKey: string) =>
    request<{ exams: string[] }>(`/api/papers/${encodeURIComponent(paperKey)}/used_in`),
  reprocessPaper: (paperKey: string) =>
    request<{ paper_key: string; passed: boolean; n_issues: number }>(
      `/api/papers/${encodeURIComponent(paperKey)}/reprocess`,
      { method: 'POST' },
    ),
  deletePaper: (paperKey: string, deletePdfs: boolean) =>
    request<{ deleted: string; removed_pdfs: string[]; kept_pdfs: string[] }>(
      `/api/papers/${encodeURIComponent(paperKey)}?delete_pdfs=${deletePdfs}`,
      { method: 'DELETE' },
    ),
  deleteQuiz: (name: string) =>
    request(`/api/quizzes/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  exportQuizQpPdfUrl: (name: string, durationMinutes?: string, footerNote?: string) => {
    const params = new URLSearchParams()
    const minutes = Number(durationMinutes)
    if (durationMinutes?.trim() && Number.isFinite(minutes) && minutes > 0) {
      params.set('duration_minutes', String(Math.round(minutes)))
    }
    if (footerNote?.trim()) params.set('footer_note', footerNote.trim())
    const query = params.toString()
    return `${API_BASE}/api/quizzes/${encodeURIComponent(name)}/export_qp.pdf${query ? `?${query}` : ''}`
  },
  // Renders an unsaved selection through the same composer as Export QP, as page images.
  previewQp: (body: PreviewRequest) =>
    request<{ pages: string[] }>('/api/preview/qp', { method: 'POST', body: JSON.stringify(body) }),
  exportQuizSourcesPdfUrl: (name: string, footerNote?: string) =>
    `${API_BASE}/api/quizzes/${encodeURIComponent(name)}/export_sources.pdf` +
    (footerNote?.trim() ? `?footer_note=${encodeURIComponent(footerNote.trim())}` : ''),
  exportQuizMsPdfUrl: (name: string, footerNote?: string) =>
    `${API_BASE}/api/quizzes/${encodeURIComponent(name)}/export_ms.pdf` +
    (footerNote?.trim() ? `?footer_note=${encodeURIComponent(footerNote.trim())}` : ''),

  // -- AI features (opt-in: Claude on the user's own subscription) --
  getSettings: () => request<Settings>('/api/settings'),
  saveSettings: (body: Partial<Omit<Settings, 'models' | 'providers' | 'api_key'>>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(body) }),
  saveApiKey: (key: string) =>
    request<ApiKeyStatus>('/api/settings/api_key', { method: 'PUT', body: JSON.stringify({ key }) }),
  removeApiKey: () => request<ApiKeyStatus>('/api/settings/api_key', { method: 'DELETE' }),
  checkClaude: () => request<{ ok: boolean; seconds: number }>('/api/settings/check', { method: 'POST' }),
  listSubjects: () => request<SubjectInfo[]>('/api/subjects'),
  getTopics: (subject: string) => request<{ subject: string; topics: Topic[] }>(`/api/topics/${subject}`),
  saveTopics: (subject: string, topics: Topic[]) =>
    request<{ subject: string; topics: Topic[] }>(`/api/topics/${subject}`, {
      method: 'PUT',
      body: JSON.stringify({ topics }),
    }),
  getAbout: () =>
    request<{ product: string; version: string; edition: string; copyright: string; license: string; notice: string }>(
      '/api/about',
    ),
  getLicense: () => fetch(`${API_BASE}/api/about/license`).then((r) => r.text()),
  getThirdParty: () => fetch(`${API_BASE}/api/about/third_party`).then((r) => r.text()),
  signIn: () => request<AccountState>('/api/account/sign_in', { method: 'POST' }),
  cancelSignIn: () => request<AccountState>('/api/account/cancel', { method: 'POST' }),
  signOut: () => request<AccountState>('/api/account/sign_out', { method: 'POST' }),
  getBalance: () => request<Balance>('/api/account/balance'),
  getTerms: () => request<{ version: string; accepted: boolean }>('/api/terms'),
  acceptTerms: (version: string) =>
    request<{ version: string; accepted: boolean }>('/api/terms/accept', { method: 'POST', body: JSON.stringify({ version }) }),
  openUpdate: () => request<{ url: string }>('/api/update/open', { method: 'POST' }),
  /** Mac: close, swap in the downloaded update, open again (engine/qbx/mac_update.py) */
  /** Settings' "Delete all my data": deletes everything, then the app closes (engine/qbx/wipe.py) */
  deleteAllData: (confirm: string) =>
    request<{ ok: boolean }>('/api/data/delete_all', { method: 'POST', body: JSON.stringify({ confirm }) }),
  installUpdate: () => request<{ ok: boolean }>('/api/update/install', { method: 'POST' }),
  openAccountPage: (page: 'subscription' | 'credit' | 'terms' | 'privacy' | 'refunds' | 'licence') =>
    request<{ url: string }>(`/api/account/open/${page}`, { method: 'POST' }),
  topicsFromSyllabus: (subject: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return uploadRequest<{ subject: string; topics: Topic[] }>(`/api/topics/${subject}/from_syllabus`, form)
  },
  startTagging: (subject?: string) =>
    request<TaggingState>(`/api/tagging/start${subject ? `?subject=${subject}` : ''}`, { method: 'POST' }),
  stopTagging: () => request<TaggingState>('/api/tagging/stop', { method: 'POST' }),
  /** Every edit made to a paper in the Reviewer, newest first. */
  listFixes: (paperKey: string) => request<Fix[]>(`/api/papers/${paperKey}/fixes`),
  fixAction: (paperKey: string, fixId: string, action: 'undo') =>
    request<Fix>(`/api/papers/${paperKey}/fixes/${fixId}/${action}`, { method: 'POST' }),
  savePartTags: (paperKey: string, body: { question_number: number; path: string; topic_ids: string[]; depends_on: string[] }) =>
    request<PartTags>(`/api/papers/${paperKey}/tags`, { method: 'PUT', body: JSON.stringify(body) }),
  // `sources`: the subject codes picked in the builder, each with its ticked papers (none:
  // any) - only these are drawn on. Empty: the request says which subject.
  examFromRequest: (text: string, sources: { subject: string; papers: string[] }[] = [], board = '') =>
    request<ExamOutcome>('/api/exam/from_request', {
      method: 'POST',
      body: JSON.stringify({ text, sources, board }),
    }),
  rerollQuiz: (name: string) =>
    request<ExamOutcome>(`/api/quizzes/${encodeURIComponent(name)}/reroll`, { method: 'POST' }),
  /** One turn of the Claude tab: the conversation so far (the teacher's message last), the
   * open exam, and the board/subjects picked for a new one. */
  chat: (body: { messages: ChatMessage[]; quiz: string | null; board: string; sources: { subject: string; papers: string[] }[] }) =>
    request<ChatReply>('/api/chat', { method: 'POST', body: JSON.stringify(body) }),
  // -- Classification mode: topic booklets from the tags questions already carry (no Claude) --
  classifiedPreview: (body: ClassifiedRequest) =>
    request<ClassifiedPreview>('/api/classified/preview', { method: 'POST', body: JSON.stringify(body) }),
  /** Starts making the booklets in the background; progress arrives in getState's `classified`. */
  classifiedExport: (body: ClassifiedRequest) =>
    request<ClassifiedState>('/api/classified/export', { method: 'POST', body: JSON.stringify(body) }),
  classifiedDownloadUrl: (id: number) => `${API_BASE}/api/classified/download/${id}`,

  // -- saving into the teacher's folder (qbx/saving.py), not the browser's downloads --
  getSaving: () => request<SavingState>('/api/saving'),
  setSaveFolder: (folder: string) =>
    request<SavingState>('/api/saving', { method: 'PUT', body: JSON.stringify({ folder }) }),
  pickSaveFolder: () => request<{ folder: string | null }>('/api/saving/pick', { method: 'POST' }),
  saveQuiz: (name: string, kind: 'qp' | 'ms', durationMinutes?: number | string | null, footerNote?: string) =>
    request<SaveResult>(`/api/quizzes/${encodeURIComponent(name)}/save`, {
      method: 'POST',
      body: JSON.stringify({ kind, duration_minutes: Number(durationMinutes) || null, footer_note: footerNote?.trim() ?? '' }),
    }),
  saveClassified: (id: number) => request<SaveResult>(`/api/classified/save/${id}`, { method: 'POST' }),
  savedFiles: () => request<SavedFile[]>('/api/saved'),
  openSaved: (path: string) => request<{ ok: true }>('/api/saved/open', { method: 'POST', body: JSON.stringify({ path }) }),
  showSaved: (path: string) => request<{ ok: true }>('/api/saved/show', { method: 'POST', body: JSON.stringify({ path }) }),
  /** Changes an exam from a typed instruction ("replace Q3 with...", "make it 30 marks"). */
  adjustQuiz: (name: string, text: string) =>
    request<AdjustOutcome>(`/api/quizzes/${encodeURIComponent(name)}/adjust`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
}

export type { BBox }
