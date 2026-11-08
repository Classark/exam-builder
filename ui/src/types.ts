export interface BBox {
  page: number
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface QuestionPart {
  id: string
  question_id: string
  path: string
  depth: number
  text: string
  image_path: string
  marks: number
  // what the paper prints for this part itself (0: none - the mark is its group's)
  marks_printed?: number | null
  topic_ids: string[]
  segments: BBox[]
  // withdrawn by the board ("...has been removed from the question paper"): never checked or set
  removed?: boolean
}

export interface Question {
  paper_key: string
  board: string
  subject: string
  question_number: number
  stem_text: string
  stem_image_path: string
  total_marks: number
  topic_ids: string[]
  parts: QuestionPart[]
  stem_segments: BBox[]
}

export interface MarkSchemeEntry {
  id: string
  question_number: number
  part_path: string
  answer_text: string
  marks: number
  guidance_text?: string
  // linked by hand in the Reviewer
  manual?: boolean
}

export interface Join {
  part_id: string
  mark_scheme_entry_id: string
}

export interface Paper {
  paper_key: string
  board: string
  level: string
  subject: string
  paper: string
  variant: string
  session: string
  year: number
  cover_total_marks: number | null
  source_qp_path: string
  source_ms_path: string
  subject_name: string
  paper_title: string
  questions: Question[]
  mark_scheme: MarkSchemeEntry[]
  joins: Join[]
}

export interface Issue {
  check: string
  question_number: number | null
  part_path: string | null
  detail: string
  key: string
  checked: boolean
}

export interface Report {
  paper_key: string
  passed: boolean
  issues: Issue[]
}

export interface PaperSummary {
  paper_key: string
  passed: boolean
  n_issues: number
  // issues not yet marked checked
  n_open: number
  n_questions: number
  // "Cambridge" or "Edexcel": an exam draws on one board's papers only
  board?: string
  subject: string
  subject_name: string
  paper_title: string
  level: string
  paper: string
  variant: string
  session: string
  year: number
  total_marks: number
  cover_total_marks: number | null
}

export interface BankQuestionBrief {
  n: number
  marks: number
  stem: string
  n_parts: number
  topics: string[] // syllabus topic ids across its parts (tags.json), [] until tagged
}

export interface BankPaper extends PaperSummary {
  questions: BankQuestionBrief[]
}

export interface QuestionDetail {
  paper_key: string
  question_number: number
  stem_text: string
  total_marks: number
  parts: { path: string; marks: number; text: string; tags: PartTags | null }[]
}

/** A part's topics and the earlier parts it can't be set without (engine/qbx/tagging.py). */
export interface PartTags {
  topic_ids: string[]
  depends_on: string[]
  reason: string
  source: 'claude' | 'manual'
}

export interface Topic {
  id: string
  name: string
  parent: string | null
  /** condensed learning outcomes, read from the syllabus - what tagging matches against */
  outcomes?: string[]
}

export interface SubjectInfo {
  subject: string
  name: string
  n_papers: number
  n_parts: number
  n_tagged: number
  n_topics: number
}

/** What the UI may know about the Anthropic API key: never the key itself. */
export interface ApiKeyStatus {
  set: boolean
  source: 'saved' | 'environment' | null
  last4: string | null
}

export interface Settings {
  enabled: boolean // "Use Claude"
  // "api": the Anthropic API with the user's key; "claude_code": Claude Code on this computer (testing)
  provider: 'api' | 'claude_code'
  providers: string[]
  api_key: ApiKeyStatus
  tag_model: string
  request_model: string
  // Claude reviewing papers' issues: "auto" applies the fixes the checks confirm
  fix_model: string
  fix_mode: 'auto' | 'review'
  fix_workers: number
  max_fix_workers: number
  models: string[]
}

export interface FixStep {
  op: string
  part_id?: string | null
  part_ids?: string[]
  entry_id?: string | null
  question_number?: number | null
  value?: number | null
  path?: string | null
  fields?: Record<string, unknown>
  at?: Record<string, number>
}

/** A change to an extracted paper (engine qbx/fixes.py), by Claude or the teacher. */
export interface Fix {
  id: string
  steps: FixStep[]
  issues: string[] // the issue keys it's meant to clear
  reason: string
  source: 'ai' | 'manual'
  status: 'applied' | 'suggested' | 'rejected' | 'undone'
  clears: string[]
  raises: string[]
  created_at: number
}

export interface FixingState {
  running: boolean
  stop?: boolean
  stage?: string
  mode?: 'auto' | 'review'
  paper_key?: string | null
  issue_key?: string | null
  papers_total?: number
  papers_done?: number
  applied?: number
  suggested?: number
  notes?: number
  current?: string | null
  failed?: { paper_key: string; error: string }[]
  paused?: string | null
  resets_at?: number | null
  error?: string
  finished_at?: number
}

export interface TaggingState {
  running: boolean
  stop?: boolean
  subject?: string | null
  stage?: string
  total?: number
  done?: number
  current?: string | null
  failed?: { paper_key: string; error: string }[]
  paused?: string | null // set when the usage limit stopped it
  resets_at?: number | null
  papers_total?: number
  papers_done?: number
  no_topics?: string[]
  error?: string
  finished_at?: number
  batch?: { id: string; done: number; total: number } // the API provider tags by Message Batch
}

/** What building an exam from a typed request made. */
export interface ExamOutcome {
  name: string
  understood: string
  total_marks: number
  target_marks: number
  exact: boolean
  n_items: number
  missing_topics: string[]
}

// what an instruction to change an exam did (/api/quizzes/{name}/adjust)
export interface AdjustOutcome extends ExamOutcome {
  removed: string[] // printed labels of the questions taken out, e.g. "Q3"
  added: number
}

export interface QuizRequest {
  text: string
  understood: string
  unmatched: string[]
  spec: { total_marks: number; subject: string | null; topic_ids: string[] }
}

export interface ScanState {
  running: boolean
  reprocess?: boolean
  stage?: string
  folder?: string
  total?: number
  done?: number
  current?: string | null
  processed?: { paper_key: string; n_questions: number; passed: boolean; n_issues: number }[]
  skipped?: string[]
  failed?: { paper_key: string; files: string[]; error: string }[]
  unpaired?: { file: string; reason: string }[]
  error?: string
  finished_at?: number
}

export interface LiveState {
  version: number
  n_papers: number
  scan: ScanState
  tagging: TaggingState
  fixing?: FixingState
  classified?: ClassifiedState
  /** debug (Claude Code + Settings, for testing) or release (for teachers) - qbx/edition.py */
  edition?: 'debug' | 'release'
  /** whether this build can reach Claude at all; false hides everything that needs it */
  ai?: boolean
  /** the Classark account this computer is signed in to (qbx/account.py) */
  account?: AccountState
  app_version?: string
}

export interface AccountState {
  status: 'signed_out' | 'waiting' | 'signed_in'
  error?: string | null
  /** the sign-in page, while waiting, for when the browser didn't open */
  url?: string | null
  email?: string | null
  name?: string | null
}

/** Classification mode: topic booklets being made (engine/main.py, /api/classified/*). */
export interface ClassifiedState {
  running: boolean
  id?: number
  ready?: boolean
  error?: string | null
  done?: number
  total?: number
  n_topics?: number
  current?: string | null
  file_name?: string
}

export interface ClassifiedItem {
  paper_key: string
  question_number: number
  part_paths: string[]
  marks: number
  mcq: boolean
  paper: string // "0620/42"
  sitting: string // "May/June 2024"
  parts: string // "All" or "(a)(i), (b)"
  also_in: string[] // reprints: "0620/32 May/June 2023"
}

export interface ClassifiedTopic {
  topic_id: string
  name: string
  n_questions: number
  total_marks: number
  items: ClassifiedItem[]
}

export interface ClassifiedPreview {
  topics: ClassifiedTopic[]
  untagged_parts: number
  n_parts: number
}

export interface ClassifiedRequest {
  board: string
  sources: { subject: string; papers: string[] }[]
  topic_ids: string[]
  year_from?: number | null
  year_to?: number | null
  question_style: 'any' | 'mcq' | 'structured'
  excluded?: { topic_id: string; paper_key: string; question_number: number }[]
}

export interface BankQuestionPart {
  path: string
  marks: number
  text: string
  image_path: string
}

export interface BankQuestion {
  paper_key: string
  board: string
  subject: string
  // as the paper's cover prints them - "Chemistry", "Paper 4 Theory (Extended)"
  subject_name: string
  paper_title: string
  level: string
  year: number // two digits, as the paper code prints it: 24
  session: string // "F/M", "M/J", "O/N" or "SP" (specimen); Edexcel "JAN", "JUN", "NOV"
  paper: string
  variant: string
  question_number: number
  stem_text: string
  stem_image_path: string
  total_marks: number
  parts: BankQuestionPart[]
}

export interface QuizSummary {
  name: string
  n_items: number
  from_request: boolean
  has_chat: boolean
  // the questions it sets, as questionKey()s
  questions: string[]
}

// One source question plus the subset of its parts to include, in the order they'll
// appear - the composed paper relabels them a, b, c... regardless of their original
// letters, so `part_paths` order is the *output* order, not necessarily source order.
export interface QuizItemRef {
  paper_key: string
  question_number: number
  part_paths: string[]
}

export interface QuizResolvedPart {
  part_path: string
  marks: number | null
  text: string | null
  image_path: string | null
  missing: boolean
}

export interface QuizItemResolved {
  paper_key: string
  question_number: number
  stem_text: string | null
  missing: boolean
  // multiple choice: printed in its own section, after the written questions
  mcq: boolean
  parts: QuizResolvedPart[]
}

export interface QuizDetail {
  name: string
  items: QuizItemResolved[]
  total_marks: number
  request: QuizRequest | null
  // the time allowance saved with the exam, printed on the cover
  duration_minutes: number | null
  // the conversation with Claude that made or changed it
  chat: ChatMessage[]
}

// one message in the Claude tab (/api/chat)
export interface ChatMessage {
  role: 'teacher' | 'claude'
  text: string
  // a Claude message that acted: what the exam came out as (a change also lists removed/added)
  outcome?: (ExamOutcome & Partial<Pick<AdjustOutcome, 'removed' | 'added'>>) | null
  // why the action couldn't run
  error?: string | null
}

export interface ChatReply {
  message: ChatMessage
  action: 'none' | 'make' | 'change' | 'reroll'
  // the exam open after this turn
  quiz: string | null
}
