/**
 * 极简 API client（原生 fetch，不引第三方库）。
 *
 * - 契约：同源 /api/ 前缀（部署态 nginx 反代 fd-juchuang-api）。
 *   本地联调可设 VITE_JUCHUANG_API_BASE 指到后端（如 http://127.0.0.1:8010）。
 * - token 存 localStorage["juchuang_token"]（顶栏输入框 / Logto 登录写入）。
 * - 后端与界面并行开发：所有端点视为 wired——网络错误、超时、5xx、404/501
 *   统一抛 ApiUnavailableError，界面据此渲染「该能力接线中」降级态，不白屏。
 */

export const API_BASE: string = (() => {
  const raw = import.meta.env?.VITE_JUCHUANG_API_BASE;
  return typeof raw === "string" && raw.trim() ? raw.trim().replace(/\/+$/, "") : "";
})();

export const TOKEN_KEY = "juchuang_token";
const TIMEOUT_MS = 30_000;

// ---- token 存取（非浏览器环境退化为内存 shim，测试可用） ----

const memoryStore = new Map<string, string>();
const shim: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
  getItem: (k) => memoryStore.get(k) ?? null,
  setItem: (k, v) => void memoryStore.set(k, v),
  removeItem: (k) => void memoryStore.delete(k),
};
function store(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* 隐私模式等场景落到内存 shim */
  }
  return shim;
}
export function getToken(): string {
  return store().getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string): void {
  if (token) store().setItem(TOKEN_KEY, token);
  else store().removeItem(TOKEN_KEY);
}

// ---- 错误类型 ----

/** 「该能力接线中」：网络失败/超时/5xx，或并行接线期端点 404/501。 */
export class ApiUnavailableError extends Error {
  constructor(message = "capability not wired yet") {
    super(message);
    this.name = "ApiUnavailableError";
  }
}

/** 业务错误（4xx 等），message 携带后端 detail。 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// ---- 请求 ----

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, string>;
  /** 接线期端点：404/501 也视为「未接线」而非业务错误（默认开） */
  wired?: boolean;
}

/** 同源相对路径在浏览器取当前 origin；非浏览器（测试）落到 http://localhost。 */
function toUrl(path: string, query?: Record<string, string>): URL {
  const qs = new URLSearchParams(query ?? {});
  const full = `${API_BASE}${path}${[...qs.keys()].length ? `?${qs.toString()}` : ""}`;
  if (/^https?:\/\//i.test(full)) return new URL(full);
  const origin =
    typeof location !== "undefined" && /^https?:/i.test(String(location.origin))
      ? String(location.origin)
      : "http://localhost";
  return new URL(full, origin);
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = toUrl(path, opts.query);

  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const hasBody = opts.body !== undefined;
  if (hasBody) headers["Content-Type"] = "application/json";

  let resp: Response;
  try {
    resp = await fetch(url, {
      method: opts.method ?? (hasBody ? "POST" : "GET"),
      headers,
      body: hasBody ? JSON.stringify(opts.body) : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // 网络拒绝 / DNS / CORS / 超时（AbortError/TimeoutError）→ 接线中降级
    throw new ApiUnavailableError();
  }

  const wired = opts.wired !== false;
  if (!resp.ok) {
    const unwired = resp.status >= 500 || resp.status === 501 || (wired && resp.status === 404);
    if (unwired) throw new ApiUnavailableError();
    let detail: unknown = "";
    try {
      detail = (await resp.json())?.detail ?? "";
    } catch {
      /* 无 JSON body */
    }
    const message = typeof detail === "string" && detail ? detail : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, message);
  }
  if (resp.status === 204) return undefined as T;
  try {
    return (await resp.json()) as T;
  } catch {
    // 200 但 body 不是 JSON（如本地 preview 把 /api/ 落到 SPA 回退页）→ 视为未接线
    if (wired) throw new ApiUnavailableError();
    throw new ApiError(resp.status, "invalid JSON response");
  }
}

const enc = encodeURIComponent;

// ---- 契约类型（后端并行开发，字段防御式可选） ----

export interface Project {
  id: string;
  title?: string | null;
  logline?: string | null;
  created_at?: string;
  updated_at?: string;
}

/** 立场/冲突可能是纯文本，也可能是哲思注入的结构化元数据（D2：注入落元数据）。 */
export interface StanceMeta {
  text?: string;
  position?: string;
  reason?: string | null;
  source?: string;
  origin?: string;
  card_ids?: string[];
  source_cards?: string[];
}

export interface Character {
  id: string;
  name: string;
  appearance?: Record<string, string> | null;
  stance?: string | StanceMeta | null;
  created_at?: string;
}

export interface ConflictMeta {
  description?: string;
  a?: string;
  b?: string;
  source?: string;
  origin?: string;
  card_ids?: string[];
  source_cards?: string[];
  tension_pair?: unknown;
}

export interface Scene {
  id: string;
  project_id?: string;
  title?: string | null;
  conflict?: string | ConflictMeta | null;
  order?: number;
  created_at?: string;
}

export interface Line {
  id: string;
  character_id?: string | null;
  character_name?: string | null;
  text?: string | null;
  order?: number;
}

export interface VersionEntry {
  id: string;
  label?: string | null;
  created_at?: string;
  version?: number;
}

export interface StoryboardShot {
  idx: number;
  shot_size?: string | null;
  visual?: string | null;
  action?: string | null;
  line?: string | null;
  duration?: number | string | null;
  /** 走位/俯视图描述（task 2.1） */
  staging?: string | null;
  /** 关键帧描述 */
  keyframe?: string | null;
  /** 角色 model sheet 引用（character_id 列表） */
  model_sheet_refs?: string[];
  /** 场景/道具 art bible 引用（scene_id / prop_id 列表） */
  art_bible_refs?: string[];
}

/** 后端 ShotPatch 契约（PUT /pipeline/storyboard/{sb}/{idx}，extra=forbid）。 */
export interface ShotPatch {
  shot_size?: string;
  visual?: string;
  action?: string;
  line_text?: string;
  duration_sec?: number;
  staging?: string | null;
  keyframe?: string | null;
  model_sheet_refs?: string[];
  art_bible_refs?: string[];
}

/** 引用输入框解析：逗号/中文逗号/空白分隔，去空去重（保持顺序）。 */
export function parseRefs(raw: string): string[] {
  const out: string[] = [];
  for (const part of raw.split(/[,，\s]+/)) {
    const value = part.trim();
    if (value && !out.includes(value)) out.push(value);
  }
  return out;
}

/**
 * 视图行 → 后端 patch：别名归一（line/duration → line_text/duration_sec）。
 * 扩展字段允许清空（空串 → null，空列表 → []）；旧字段为空时不下发，
 * 避免撞上后端 min_length / 景别 pattern 校验。
 */
export function shotPatch(shot: StoryboardShot): ShotPatch {
  const patch: ShotPatch = {
    staging: shot.staging && shot.staging.trim() ? shot.staging : null,
    keyframe: shot.keyframe && shot.keyframe.trim() ? shot.keyframe : null,
    model_sheet_refs: [...(shot.model_sheet_refs ?? [])],
    art_bible_refs: [...(shot.art_bible_refs ?? [])],
  };
  if (shot.shot_size) patch.shot_size = shot.shot_size;
  if (shot.visual) patch.visual = shot.visual;
  if (shot.action) patch.action = shot.action;
  if (typeof shot.line === "string" && shot.line.trim()) patch.line_text = shot.line;
  const duration = Number(shot.duration);
  if (String(shot.duration ?? "").trim() && Number.isFinite(duration) && duration > 0) {
    patch.duration_sec = duration;
  }
  return patch;
}

export interface Storyboard {
  id: string;
  scene_id?: string;
  shots?: StoryboardShot[];
}

// change juchuang-image-iteration：新增第四形态 `generation`（可执行生成计划）。
// 三种提示词形态仍是「说什么」；generation 是「怎么生成」，带幂等键与模式。
export type PromptForm = "image" | "video" | "narrative" | "generation";
export const PROMPT_FORMS: readonly PromptForm[] = [
  "image",
  "video",
  "narrative",
  "generation",
];

/** 出图能力：generate 与 edit **各自独立**（spec「降级独立模式」）。 */
export interface CapabilityState {
  available: boolean;
  reason?: string;
  message?: string;
  generator?: string;
  lane?: { health?: string; reason_class?: string | null };
}
export interface GenerateCapabilities {
  configured: boolean;
  generate: CapabilityState;
  edit: CapabilityState;
}

export interface ShotGeneration {
  shot_idx: number;
  asset_id: string;
  file_paths?: string[];
  download_url?: string | null;
}
export interface GenerationFailure {
  shot_idx: number;
  category: string;
  message: string;
}
export interface GenerationResult {
  storyboard_id: number | string;
  generated: ShotGeneration[];
  failures: GenerationFailure[];
  requested: number;
}
/** 两条成片通路各自的能力（spec：互不代表）。 */
export interface VideoCapabilities {
  i2v: { available: boolean; reason?: string; message?: string;
         first_frame_supported?: boolean | null; generator?: string };
  render: { available: boolean; reason?: string; message?: string; cost?: string };
}
export interface VideoEstimate {
  confirmed: false;
  estimated_calls: number;
  estimated_seconds: number;
  cost: string;
  note?: string;
}
export interface VideoSubmitResult {
  confirmed: true;
  route: "i2v" | "render";
  submitted?: { shot_idx: number; job_id?: string; provider_task_id?: string }[];
  failures?: { shot_idx: number; category: string; message: string }[];
  requested?: number;
  note?: string;
}
export interface VideoRenderResult {
  route: "render";
  cost: string;
  asset_id?: string;
  file_paths?: string[];
  download_url?: string | null;
  segments?: { shot_idx: number; text: string; text_source: string; image?: string | null }[];
  duration_source?: string;
  note?: string;
}

export interface EditResult {
  storyboard_id: number | string;
  shot_idx: number;
  asset_id: string;
  source_asset_id: string;
  file_paths?: string[];
  download_url?: string | null;
  warnings?: string[];
}

export interface KernelCard {
  id: string;
  question?: string | null;
  position?: string | null;
  title?: string | null;
}

export interface KernelStatus {
  mode: string; // "linked" | "standalone"（防御式 string）
  card_count?: number;
  reason?: string | null;
  cards?: KernelCard[] | null;
  linked_at?: string | null;
}

// ---- 质量门（六维 rubric / 问题清单 / 红线阻断） ----

export interface QualityLocation {
  scene_id?: number | string | null;
  line_id?: number | string | null;
  line_idx?: number | null;
  character?: string | null;
  scope?: "episode" | "scene" | "line" | string;
  offset?: number | null;
  excerpt?: string | null;
}

/** 台账冲突事实/伏笔引用（连续性问题必须带）。 */
export interface QualityLedgerRef {
  kind?: "fact" | "foreshadow" | string;
  fact_id?: number | null;
  foreshadow_id?: number | null;
  signature?: string | null;
  violation_type?: string | null;
  detail?: Record<string, unknown>;
}

export interface QualityProblem {
  rule_id: string;
  dimension: string;
  severity: "warning" | "error" | "critical" | string;
  message: string;
  location: QualityLocation;
  evidence: string;
  ledger_refs?: QualityLedgerRef[];
  penalty?: number;
}

export interface QualityRuleResult {
  rule_id: string;
  title: string;
  status: "ok" | "violated" | "skipped" | string;
  checked: number;
  problem_count: number;
  reason?: string | null;
}

export interface QualityDimensionScore {
  dimension: string;
  label: string;
  score: number | null;
  available: boolean;
  reason?: string | null;
  weight: number;
  rules_applied: string[];
  rules_disabled?: string[];
  rule_results: QualityRuleResult[];
  problems: QualityProblem[];
  problem_count: number;
  severity_counts: Record<string, number>;
}

export interface QualityViolationDetail {
  rule_id: string;
  severity: "warning" | "error" | "critical" | string;
  message: string;
  location: QualityLocation;
  evidence: string;
  red_line: boolean;
}

export interface QualityAssessment {
  id?: number | string | null;
  project_id: string;
  episode_id: string;
  episode: number;
  dimensions: Record<string, QualityDimensionScore>;
  dimension_order?: string[];
  overall_score: number | null;
  overall_formula?: string;
  blocked: boolean;
  redline_hit?: boolean;
  block_reasons?: string[];
  violation_details?: QualityViolationDetail[];
  policy?: Record<string, unknown>;
  stats?: Record<string, unknown>;
  created_at?: string | null;
}

export interface QualityAssessmentSummary {
  id: number | string;
  project_id: string;
  episode_id: string;
  episode: number;
  overall_score: number | null;
  blocked: boolean;
  redline_hit: boolean;
  created_at: string;
}

/** 提示词集合引用：POST /pipeline/prompts/{sb} 的返回（至少带 id）。 */
export interface PromptSetRef {
  id: string;
  [k: string]: unknown;
}

// ---- 哲思来源标记（注入后 conflict/stance 显示 zhesi 来源） ----

export interface ZhesiSource {
  source: string;
  cardIds: string[];
}

/** 从 conflict/stance 值里识别哲思来源：对象带 zhesi/哲思 source 或卡 ID 列表即视为已注入。 */
export function zhesiSource(value: unknown): ZhesiSource | null {
  if (!value || typeof value !== "object") return null;
  const o = value as Record<string, unknown>;
  const src = `${String(o.source ?? "")} ${String(o.origin ?? "")}`;
  const ids = [o.card_ids, o.source_cards, (o.tension_pair as Record<string, unknown> | undefined)?.card_ids]
    .filter(Array.isArray)
    .flat()
    .filter((v): v is string => typeof v === "string" && v.length > 0);
  const marked = /zhesi|哲思/i.test(src) || ids.length > 0;
  if (!marked) return null;
  return { source: /zhesi|哲思/i.test(src) ? src.trim().split(/\s+/)[0] : "zhesi", cardIds: ids };
}

/** 立场文本：string 直出；对象按 text/position/reason 拼 Defensive。 */
export function stanceText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    const o = value as Record<string, unknown>;
    const text = [o.text, o.position, o.reason].find((v) => typeof v === "string" && v);
    return typeof text === "string" ? text : "";
  }
  return String(value);
}

/** 冲突文本：string 直出；对象按 description / a↔b 拼。 */
export function conflictText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    const o = value as Record<string, unknown>;
    if (typeof o.description === "string" && o.description) return o.description;
    const a = typeof o.a === "string" ? o.a : "";
    const b = typeof o.b === "string" ? o.b : "";
    if (a || b) return [a, b].filter(Boolean).join(" ↔ ");
    return "";
  }
  return String(value);
}

/** 列表响应防御式取数：{results:[...]} / [...] / 空值。 */
export function asList<T>(payload: unknown): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === "object") {
    const r = (payload as Record<string, unknown>).results;
    if (Array.isArray(r)) return r as T[];
  }
  return [];
}

/** JSON 下载（Blob），导出底料用。 */
export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ---- API 面（对齐 ../server 契约，全部 wired：接线期 404/5xx → 接线中） ----

export const api = {
  // 剧本：项目 / 角色卡 / 场次 / 台词 / 版本 / 导出
  listProjects: () =>
    request<unknown>("/api/v1/screenplay/projects").then((r) => asList<Project>(r)),
  createProject: (b: { title: string; logline?: string }) =>
    request<Project>("/api/v1/screenplay/projects", { method: "POST", body: b }),

  listCharacters: (projectId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/characters`).then((r) =>
      asList<Character>(r),
    ),
  createCharacter: (
    projectId: string,
    b: { name: string; appearance: Record<string, string>; stance?: string },
  ) =>
    request<Character>(`/api/v1/screenplay/projects/${enc(projectId)}/characters`, {
      method: "POST",
      body: b,
    }),
  updateCharacter: (
    projectId: string,
    characterId: string,
    b: { name: string; appearance: Record<string, string>; stance?: string },
  ) =>
    request<Character>(`/api/v1/screenplay/projects/${enc(projectId)}/characters/${enc(characterId)}`, {
      method: "PUT",
      body: b,
    }),
  deleteCharacter: (projectId: string, characterId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/characters/${enc(characterId)}`, {
      method: "DELETE",
    }),

  listScenes: (projectId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/scenes`).then((r) =>
      asList<Scene>(r),
    ),
  createScene: (projectId: string, b: { title: string; conflict?: string }) =>
    request<Scene>(`/api/v1/screenplay/projects/${enc(projectId)}/scenes`, {
      method: "POST",
      body: b,
    }),
  updateScene: (projectId: string, sceneId: string, b: { title?: string; conflict?: string }) =>
    request<Scene>(`/api/v1/screenplay/projects/${enc(projectId)}/scenes/${enc(sceneId)}`, {
      method: "PUT",
      body: b,
    }),
  deleteScene: (projectId: string, sceneId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/scenes/${enc(sceneId)}`, {
      method: "DELETE",
    }),

  listLines: (sceneId: string) =>
    request<unknown>(`/api/v1/screenplay/scenes/${enc(sceneId)}/lines`).then((r) => asList<Line>(r)),
  createLine: (sceneId: string, b: { character_id?: string; text: string }) =>
    request<Line>(`/api/v1/screenplay/scenes/${enc(sceneId)}/lines`, { method: "POST", body: b }),
  updateLine: (sceneId: string, lineId: string, b: { character_id?: string; text: string }) =>
    request<Line>(`/api/v1/screenplay/scenes/${enc(sceneId)}/lines/${enc(lineId)}`, { method: "PUT", body: b }),
  deleteLine: (sceneId: string, lineId: string) =>
    request<unknown>(`/api/v1/screenplay/scenes/${enc(sceneId)}/lines/${enc(lineId)}`, { method: "DELETE" }),

  listVersions: (projectId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/versions`).then((r) =>
      asList<VersionEntry>(r),
    ),
  createVersion: (projectId: string, label?: string) =>
    request<VersionEntry>(`/api/v1/screenplay/projects/${enc(projectId)}/versions`, {
      method: "POST",
      body: label ? { label } : {},
    }),
  restoreVersion: (projectId: string, versionId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/restore/${enc(versionId)}`, {
      method: "POST",
      body: {},
    }),
  exportProject: (projectId: string) =>
    request<unknown>(`/api/v1/screenplay/projects/${enc(projectId)}/export`),

  // 哲思内核：接入 / 状态 / 注入
  kernelLink: () => request<KernelStatus>("/api/v1/kernel/link", { method: "POST", body: {} }),
  kernelStatus: () => request<KernelStatus>("/api/v1/kernel/status"),
  kernelInject: (projectId: string, b: { scene_id?: string; character_id?: string }) =>
    request<unknown>(`/api/v1/kernel/inject/${enc(projectId)}`, { method: "POST", body: b }),

  // 质量门：六维评估 / 结果 / rubric
  getQualityRubric: () => request<unknown>("/api/v1/quality/rubric"),
  assessQuality: (
    projectId: string,
    b: { episode?: number; episode_id?: string; scene_ids?: number[]; persist?: boolean },
  ) =>
    request<QualityAssessment>(`/api/v1/quality/projects/${enc(projectId)}/assess`, {
      method: "POST",
      body: b,
    }),
  listQualityAssessments: (projectId: string) =>
    request<unknown>(`/api/v1/quality/projects/${enc(projectId)}/assessments`).then((r) =>
      asList<QualityAssessmentSummary>(r),
    ),
  getQualityAssessment: (projectId: string, assessmentId: string) =>
    request<QualityAssessment>(
      `/api/v1/quality/projects/${enc(projectId)}/assessments/${enc(assessmentId)}`,
    ),

  // 底料：分镜表 / 镜头编辑 / 三形态提示词
  genStoryboard: (sceneId: string) =>
    request<Storyboard>(`/api/v1/pipeline/storyboard/${enc(sceneId)}`, { method: "POST", body: {} }),
  updateShot: (storyboardId: string, idx: number, patch: ShotPatch) =>
    request<unknown>(`/api/v1/pipeline/storyboard/${enc(storyboardId)}/${enc(String(idx))}`, {
      method: "PUT",
      body: patch,
    }),
  genPrompts: (storyboardId: string) =>
    request<PromptSetRef>(`/api/v1/pipeline/prompts/${enc(storyboardId)}`, { method: "POST", body: {} }),
  getPrompt: (promptSetId: string, form: PromptForm) =>
    request<unknown>(`/api/v1/pipeline/prompts/${enc(promptSetId)}`, { query: { form } }),

  // 出图：能力探测 / 按计划生成 / 改这一镜（change juchuang-image-iteration）
  getGenerateCapabilities: () => request<GenerateCapabilities>("/api/v1/pipeline/generate/capabilities"),
  generateShots: (storyboardId: string, b: { shot_idxs?: number[]; max_calls?: number } = {}) =>
    request<GenerationResult>(`/api/v1/pipeline/generate/${enc(storyboardId)}`, {
      method: "POST",
      body: b,
    }),
  getVideoCapabilities: () => request<VideoCapabilities>("/api/v1/pipeline/video/capabilities"),
  createVideo: (
    storyboardId: string,
    b: { route: "i2v" | "render"; shot_idxs?: number[]; max_calls?: number; confirm?: boolean },
  ) =>
    request<VideoEstimate | VideoSubmitResult | VideoRenderResult>(
      `/api/v1/pipeline/video/${enc(storyboardId)}`,
      { method: "POST", body: b },
    ),
  editShot: (storyboardId: string, shotIdx: number, b: { instruction: string; mask?: string }) =>
    request<EditResult>(
      `/api/v1/pipeline/shots/${enc(storyboardId)}/${enc(String(shotIdx))}/edit`,
      { method: "POST", body: b },
    ),
};
