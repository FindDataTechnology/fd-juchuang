/** 底料 · AI 生成管线：选场次 → 生成分镜表（可编辑镜头表）→ 三形态提示词预览 + JSON 下载。 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  downloadJson,
  parseRefs,
  PROMPT_FORMS,
  shotPatch,
  type Project,
  type PromptForm,
  type Scene,
  type Storyboard,
  type StoryboardShot,
  type GenerateCapabilities,
  type GenerationResult,
  type EditResult,
  type VideoCapabilities,
  type VideoEstimate,
  type VideoSubmitResult,
  type VideoRenderResult,
} from "../../api/client";
import { Loadable, mapApiError, noteFromError, type Note } from "../../api/state";
import {
  btnPrimary,
  btnSecondary,
  DegradeBox,
  EmptyLine,
  ErrorBox,
  inputText,
  LoadBox,
  Loading,
  NoteBox,
  Picker,
  SectionTitle,
} from "../../components/ui";

/** 防御式镜头行：兼容常见键名（shot_size/scale/景别 等），统一为规范字段。 */
export function normalizeShot(raw: unknown, i: number): StoryboardShot {
  if (!raw || typeof raw !== "object")
    return {
      idx: i,
      shot_size: "",
      visual: "",
      action: "",
      line: "",
      duration: "",
      staging: null,
      keyframe: null,
      model_sheet_refs: [],
      art_bible_refs: [],
    };
  const o = raw as Record<string, unknown>;
  const pick = (...keys: string[]): string => {
    for (const k of keys) {
      const v = o[k];
      if (typeof v === "string" && v) return v;
      if (typeof v === "number") return String(v);
    }
    return "";
  };
  const refs = (v: unknown): string[] =>
    Array.isArray(v) ? v.map((x) => String(x)).filter(Boolean) : [];
  const dur = o.duration ?? o.seconds ?? o.length;
  return {
    idx: typeof o.idx === "number" ? o.idx : i,
    shot_size: pick("shot_size", "shot", "scale", "景别"),
    visual: pick("visual", "frame", "image", "picture", "画面"),
    action: pick("action", "动作"),
    line: pick("line", "dialogue", "text", "台词"),
    duration: typeof dur === "number" || typeof dur === "string" ? dur : "",
    staging: pick("staging", "走位") || null,
    keyframe: pick("keyframe", "关键帧") || null,
    model_sheet_refs: refs(o.model_sheet_refs),
    art_bible_refs: refs(o.art_bible_refs),
  };
}

function normalizeStoryboard(sb: Storyboard): Storyboard {
  const shots = (sb.shots ?? []).map(normalizeShot);
  return { ...sb, id: String(sb.id ?? ""), shots };
}

// ---- 视图（显式状态，可独立渲染降级态） ----

export function PipelineView({
  projects,
  selectedProjectId,
  onSelectProject,
  scenes,
  selectedSceneId,
  onSelectScene,
  storyboard,
  onGenerateStoryboard,
  sbBusy,
  sbNote,
  onUpdateShot,
  shotBusyIdx,
  prompts,
  onGenPrompts,
  promptBusy,
  promptNote,
  form,
  onForm,
  promptData,
  onDownload,
  downloadBusy,
  downloadNote,
  caps,
  onGenerate,
  genBusy,
  genNote,
  genResult,
  onEditShot,
  editBusyIdx,
  editNote,
  editResult,
  videoCaps,
  onVideoI2v,
  onVideoRender,
  videoBusy,
  videoNote,
  videoEstimate,
  videoSubmit,
  videoRender,
  onConfirmI2v,
  onCancelI2v,
}: {
  projects: Loadable<Project[]>;
  selectedProjectId: string;
  onSelectProject: (id: string) => void;
  scenes: Loadable<Scene[]>;
  selectedSceneId: string;
  onSelectScene: (id: string) => void;
  storyboard: Loadable<Storyboard>;
  onGenerateStoryboard: () => void;
  sbBusy: boolean;
  sbNote: Note;
  onUpdateShot: (shot: StoryboardShot) => void;
  shotBusyIdx: number | null;
  prompts: { id: string } | null;
  onGenPrompts: () => void;
  promptBusy: boolean;
  promptNote: Note;
  form: PromptForm;
  onForm: (f: PromptForm) => void;
  promptData: Loadable<unknown>;
  onDownload: () => void;
  downloadBusy: boolean;
  downloadNote: Note;
  caps: Loadable<GenerateCapabilities>;
  onGenerate: () => void;
  genBusy: boolean;
  genNote: Note;
  genResult: GenerationResult | null;
  onEditShot: (shotIdx: number, instruction: string) => void;
  editBusyIdx: number | null;
  editNote: Note;
  editResult: EditResult | null;
  videoCaps: Loadable<VideoCapabilities>;
  onVideoI2v: () => void;
  onVideoRender: () => void;
  videoBusy: "i2v" | "render" | null;
  videoNote: Note;
  videoEstimate: VideoEstimate | null;
  videoSubmit: VideoSubmitResult | null;
  videoRender: VideoRenderResult | null;
  onConfirmI2v: () => void;
  onCancelI2v: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="space-y-8">
      <div>
        <SectionTitle>{t("workbench.pipeline.heading")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.pipeline.hint")}</p>
      </div>

      {/* 第一步：选场次 → 生成分镜 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.pipeline.step1")}</SectionTitle>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <Picker
            label={t("workbench.pipeline.project")}
            state={projects}
            selectedId={selectedProjectId}
            onSelect={onSelectProject}
            labelOf={(p: Project) => p.title || p.id}
          />
          <Picker
            label={t("workbench.pipeline.scene")}
            state={scenes}
            selectedId={selectedSceneId}
            onSelect={onSelectScene}
            labelOf={(s: Scene) => s.title || s.id.slice(0, 8)}
          />
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={sbBusy || !selectedSceneId}
            onClick={onGenerateStoryboard}
            className={btnPrimary}
          >
            {sbBusy ? t("workbench.pipeline.generating") : t("workbench.pipeline.genStoryboard")}
          </button>
          {!selectedSceneId ? (
            <span className="text-xs text-neutral-400">{t("workbench.pipeline.pickScene")}</span>
          ) : null}
        </div>
        <div className="mt-2">
          <NoteBox note={sbNote} />
        </div>

        {/* 镜头表（可编辑） */}
        <div className="mt-5">
          <LoadBox state={storyboard}>
            {(sb) =>
              (sb.shots ?? []).length === 0 ? (
                <EmptyLine>{t("workbench.pipeline.storyboardEmpty")}</EmptyLine>
              ) : (
                <div className="overflow-x-auto" data-testid="storyboard-table">
                  <table className="w-full min-w-3xl border-collapse text-sm">
                    <thead>
                      <tr className="border-b border-neutral-200 text-left text-xs text-neutral-400">
                        <th className="py-2 pr-2 font-normal">#</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.shot")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.visual")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.action")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.line")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.duration")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.staging")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.keyframe")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.modelSheetRefs")}</th>
                        <th className="py-2 pr-2 font-normal">{t("workbench.pipeline.artBibleRefs")}</th>
                        <th className="py-2 font-normal" />
                      </tr>
                    </thead>
                    <tbody>
                      {(sb.shots ?? []).map((shot) => (
                        <ShotRow
                          key={`${shot.idx}-${sb.id}`}
                          shot={shot}
                          busy={shotBusyIdx === shot.idx}
                          onSave={onUpdateShot}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
          </LoadBox>
        </div>
      </div>

      {/* 第二步：分镜 → 三形态提示词 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.pipeline.step2")}</SectionTitle>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={promptBusy || storyboard.status !== "ready"}
            onClick={onGenPrompts}
            className={btnPrimary}
          >
            {promptBusy ? t("workbench.pipeline.genPromptsBusy") : t("workbench.pipeline.genPrompts")}
          </button>
          {prompts ? (
            <button type="button" disabled={downloadBusy} onClick={onDownload} className={btnSecondary}>
              {downloadBusy ? t("workbench.pipeline.downloading") : t("workbench.pipeline.download")}
            </button>
          ) : null}
        </div>
        <div className="mt-2 space-y-2">
          <NoteBox note={promptNote} />
          <NoteBox note={downloadNote} />
        </div>

        {prompts ? (
          <div className="mt-5">
            <div className="flex gap-1 border-b border-neutral-200" role="tablist">
              {PROMPT_FORMS.map((f) => (
                <button
                  key={f}
                  type="button"
                  role="tab"
                  aria-selected={form === f}
                  onClick={() => onForm(f)}
                  className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
                    form === f
                      ? "border-neutral-900 font-medium text-neutral-900"
                      : "border-transparent text-neutral-500 hover:text-neutral-900"
                  }`}
                >
                  {t(`workbench.pipeline.forms.${f}`)}
                </button>
              ))}
            </div>
            <div className="mt-3">
              {promptData.status === "loading" ? <Loading /> : null}
              {promptData.status === "unavailable" ? <DegradeBox /> : null}
              {promptData.status === "error" ? (
                <ErrorBox message={promptData.message} code={promptData.code} />
              ) : null}
              {promptData.status === "ready" ? (
                <pre className="max-h-96 overflow-auto rounded-lg bg-neutral-900 p-4 text-xs leading-relaxed text-neutral-100">
                  {JSON.stringify(promptData.data, null, 2)}
                </pre>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="mt-4">
            <EmptyLine>{t("workbench.pipeline.promptsEmpty")}</EmptyLine>
          </div>
        )}
      </div>

      {/* 第三步：出图与镜级修改（change juchuang-image-iteration） */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.pipeline.step3")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.pipeline.step3Hint")}</p>

        {caps.status === "ready" ? (
          <div className="mt-3 space-y-1 text-sm">
            <CapLine
              label={t("workbench.pipeline.capGenerate")}
              state={caps.data.generate}
            />
            <CapLine
              label={t("workbench.pipeline.capEdit")}
              state={caps.data.edit}
            />
          </div>
        ) : null}
        {caps.status === "unavailable" ? <DegradeBox /> : null}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={
              genBusy ||
              storyboard.status !== "ready" ||
              caps.status !== "ready" ||
              !caps.data.generate.available
            }
            onClick={onGenerate}
            className={btnPrimary}
            title={
              caps.status === "ready" && !caps.data.generate.available
                ? caps.data.generate.message ?? caps.data.generate.reason ?? ""
                : undefined
            }
          >
            {genBusy ? t("workbench.pipeline.generating") : t("workbench.pipeline.generate")}
          </button>
        </div>

        <div className="mt-2 space-y-2">
          <NoteBox note={genNote} />
          <NoteBox note={editNote} />
        </div>

        {genResult ? (
          <div className="mt-3 text-sm text-neutral-600">
            <div>
              {t("workbench.pipeline.genSummary", {
                ok: genResult.generated.length,
                total: genResult.requested,
              })}
            </div>
            {genResult.failures.map((f) => (
              <div key={f.shot_idx} className="mt-1 text-red-600">
                {t("workbench.pipeline.shotFailed", { idx: f.shot_idx })} — {f.category}
              </div>
            ))}
          </div>
        ) : null}

        {editResult ? (
          <div className="mt-2 text-sm text-neutral-600">
            {t("workbench.pipeline.editDone", { idx: editResult.shot_idx })}
          </div>
        ) : null}

        {storyboard.status === "ready" ? (
          <div className="mt-5 space-y-2">
            {(storyboard.data.shots ?? []).map((shot) => (
              <EditShotRow
                key={shot.idx}
                shotIdx={shot.idx}
                disabled={caps.status !== "ready" || !caps.data.edit.available}
                disabledReason={
                  caps.status === "ready" && !caps.data.edit.available
                    ? caps.data.edit.message ?? caps.data.edit.reason ?? ""
                    : undefined
                }
                busy={editBusyIdx === shot.idx}
                onSubmit={onEditShot}
              />
            ))}
          </div>
        ) : null}
      </div>

      {/* 第四步：成片（两条通路，成本口径分开） */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.pipeline.step4")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.pipeline.step4Hint")}</p>

        {videoCaps.status === "ready" ? (
          <div className="mt-3 space-y-1 text-sm">
            <CapLine label={t("workbench.pipeline.capI2v")} state={videoCaps.data.i2v} />
            <CapLine label={t("workbench.pipeline.capRender")} state={videoCaps.data.render} />
          </div>
        ) : null}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={videoBusy !== null || storyboard.status !== "ready" ||
              videoCaps.status !== "ready" || !videoCaps.data.render.available}
            onClick={onVideoRender}
            className={btnPrimary}
            title={videoCaps.status === "ready" && !videoCaps.data.render.available
              ? videoCaps.data.render.message ?? videoCaps.data.render.reason : undefined}
          >
            {videoBusy === "render"
              ? t("workbench.pipeline.videoRendering")
              : t("workbench.pipeline.videoRender")}
          </button>
          <button
            type="button"
            disabled={videoBusy !== null || storyboard.status !== "ready" ||
              videoCaps.status !== "ready" || !videoCaps.data.i2v.available}
            onClick={onVideoI2v}
            className={btnSecondary}
            title={videoCaps.status === "ready" && !videoCaps.data.i2v.available
              ? videoCaps.data.i2v.message ?? videoCaps.data.i2v.reason : undefined}
          >
            {videoBusy === "i2v"
              ? t("workbench.pipeline.videoI2vBusy")
              : t("workbench.pipeline.videoI2v")}
          </button>
        </div>
        <p className="mt-1 text-xs text-neutral-400">
          {t("workbench.pipeline.videoCostNote")}
        </p>

        <div className="mt-2 space-y-2">
          <NoteBox note={videoNote} />
        </div>

        {videoEstimate ? (
          <div className="mt-3 rounded border border-amber-200 bg-amber-50 p-3 text-sm">
            <div>
              {t("workbench.pipeline.i2vEstimate", {
                calls: videoEstimate.estimated_calls,
                secs: videoEstimate.estimated_seconds,
              })}
            </div>
            <div className="mt-2 flex gap-2">
              <button type="button" onClick={onConfirmI2v} className={btnPrimary}>
                {t("workbench.pipeline.i2vConfirm")}
              </button>
              <button type="button" onClick={onCancelI2v} className={btnSecondary}>
                {t("workbench.pipeline.i2vCancel")}
              </button>
            </div>
          </div>
        ) : null}

        {videoSubmit ? (
          <div className="mt-3 text-sm text-neutral-600">
            {t("workbench.pipeline.i2vSubmitted", { n: videoSubmit.submitted?.length ?? 0 })}
            {(videoSubmit.failures ?? []).map((f) => (
              <div key={f.shot_idx} className="mt-1 text-red-600">
                {t("workbench.pipeline.shotFailed", { idx: f.shot_idx })} — {f.category}
              </div>
            ))}
          </div>
        ) : null}

        {videoRender ? (
          <div className="mt-3 text-sm text-neutral-600">
            <div>{t("workbench.pipeline.videoRendered")}</div>
            <div className="mt-1 text-xs text-neutral-500">
              {(videoRender.file_paths ?? []).join(", ")}
            </div>
            <div className="mt-1 text-xs text-neutral-400">
              {t("workbench.pipeline.durationByAudio")}
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function CapLine({ label, state }: { label: string; state: { available: boolean; reason?: string; message?: string } }) {
  return (
    <div className="flex items-center gap-2">
      <span className={state.available ? "text-emerald-600" : "text-neutral-400"}>
        {state.available ? "●" : "○"}
      </span>
      <span className="font-medium">{label}</span>
      <span className="text-neutral-500">
        {state.available ? "" : (state.message ?? state.reason ?? "")}
      </span>
    </div>
  );
}

function EditShotRow({
  shotIdx,
  disabled,
  disabledReason,
  busy,
  onSubmit,
}: {
  shotIdx: number;
  disabled: boolean;
  disabledReason?: string;
  busy: boolean;
  onSubmit: (shotIdx: number, instruction: string) => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-neutral-100 bg-neutral-50 p-2">
      <span className="w-16 text-sm text-neutral-500">
        {t("workbench.pipeline.shotLabel", { idx: shotIdx })}
      </span>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("workbench.pipeline.editPlaceholder")}
        aria-label={t("workbench.pipeline.editInstruction")}
        className="min-w-0 flex-1 rounded border border-neutral-300 px-2 py-1 text-sm"
      />
      <button
        type="button"
        disabled={disabled || busy || !text.trim()}
        onClick={() => onSubmit(shotIdx, text.trim())}
        className={btnSecondary}
        title={disabled ? disabledReason : undefined}
      >
        {busy ? t("workbench.pipeline.editing") : t("workbench.pipeline.editThisShot")}
      </button>
    </div>
  );
}

function ShotRow({
  shot,
  busy,
  onSave,
}: {
  shot: StoryboardShot;
  busy: boolean;
  onSave: (shot: StoryboardShot) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<StoryboardShot>(shot);
  // 引用框保留原始输入文本（否则输入逗号会被 split/join 吞掉）
  const [modelSheetText, setModelSheetText] = useState(
    (shot.model_sheet_refs ?? []).join(", "),
  );
  const [artBibleText, setArtBibleText] = useState((shot.art_bible_refs ?? []).join(", "));
  const set = (patch: Partial<StoryboardShot>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <tr className="border-b border-neutral-100 align-top">
      <td className="py-2 pr-2 text-xs tabular-nums text-neutral-400">{draft.idx + 1}</td>
      <td className="py-2 pr-2">
        <input
          value={draft.shot_size ?? ""}
          onChange={(e) => set({ shot_size: e.target.value })}
          className={`${inputText} w-24`}
          aria-label={t("workbench.pipeline.shot")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={draft.visual ?? ""}
          onChange={(e) => set({ visual: e.target.value })}
          className={`${inputText} min-w-48`}
          aria-label={t("workbench.pipeline.visual")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={draft.action ?? ""}
          onChange={(e) => set({ action: e.target.value })}
          className={`${inputText} min-w-36`}
          aria-label={t("workbench.pipeline.action")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={draft.line ?? ""}
          onChange={(e) => set({ line: e.target.value })}
          className={`${inputText} min-w-36`}
          aria-label={t("workbench.pipeline.line")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={String(draft.duration ?? "")}
          onChange={(e) => set({ duration: e.target.value })}
          className={`${inputText} w-16`}
          aria-label={t("workbench.pipeline.duration")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={draft.staging ?? ""}
          onChange={(e) => set({ staging: e.target.value })}
          className={`${inputText} min-w-56`}
          aria-label={t("workbench.pipeline.staging")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={draft.keyframe ?? ""}
          onChange={(e) => set({ keyframe: e.target.value })}
          className={`${inputText} min-w-56`}
          aria-label={t("workbench.pipeline.keyframe")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={modelSheetText}
          onChange={(e) => {
            setModelSheetText(e.target.value);
            set({ model_sheet_refs: parseRefs(e.target.value) });
          }}
          className={`${inputText} min-w-28`}
          placeholder={t("workbench.pipeline.refsHint")}
          aria-label={t("workbench.pipeline.modelSheetRefs")}
        />
      </td>
      <td className="py-2 pr-2">
        <input
          value={artBibleText}
          onChange={(e) => {
            setArtBibleText(e.target.value);
            set({ art_bible_refs: parseRefs(e.target.value) });
          }}
          className={`${inputText} min-w-28`}
          placeholder={t("workbench.pipeline.refsHint")}
          aria-label={t("workbench.pipeline.artBibleRefs")}
        />
      </td>
      <td className="py-2">
        <button type="button" disabled={busy} onClick={() => onSave(draft)} className={btnSecondary}>
          {busy ? t("workbench.saving") : t("workbench.pipeline.saveShot")}
        </button>
      </td>
    </tr>
  );
}

// ---- 容器：接 API ----

export default function PipelinePanel() {
  const { t } = useTranslation();

  const [projects, setProjects] = useState<Loadable<Project[]>>({ status: "idle" });
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [scenes, setScenes] = useState<Loadable<Scene[]>>({ status: "idle" });
  const [selectedSceneId, setSelectedSceneId] = useState("");

  const [storyboard, setStoryboard] = useState<Loadable<Storyboard>>({ status: "idle" });
  const [sbBusy, setSbBusy] = useState(false);
  const [sbNote, setSbNote] = useState<Note>(null);
  const [shotBusyIdx, setShotBusyIdx] = useState<number | null>(null);

  const [prompts, setPrompts] = useState<{ id: string } | null>(null);
  const [promptBusy, setPromptBusy] = useState(false);
  const [promptNote, setPromptNote] = useState<Note>(null);
  const [form, setForm] = useState<PromptForm>("image");
  const [promptData, setPromptData] = useState<Loadable<unknown>>({ status: "idle" });
  const [caps, setCaps] = useState<Loadable<GenerateCapabilities>>({ status: "idle" });
  const [genBusy, setGenBusy] = useState(false);
  const [genNote, setGenNote] = useState<Note>(null);
  const [genResult, setGenResult] = useState<GenerationResult | null>(null);
  const [editBusyIdx, setEditBusyIdx] = useState<number | null>(null);
  const [editNote, setEditNote] = useState<Note>(null);
  const [editResult, setEditResult] = useState<EditResult | null>(null);
  const [videoCaps, setVideoCaps] = useState<Loadable<VideoCapabilities>>({ status: "idle" });
  const [videoBusy, setVideoBusy] = useState<"i2v" | "render" | null>(null);
  const [videoNote, setVideoNote] = useState<Note>(null);
  const [videoEstimate, setVideoEstimate] = useState<VideoEstimate | null>(null);
  const [videoSubmit, setVideoSubmit] = useState<VideoSubmitResult | null>(null);
  const [videoRender, setVideoRender] = useState<VideoRenderResult | null>(null);
  const [promptCache, setPromptCache] = useState<Partial<Record<PromptForm, unknown>>>({});
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [downloadNote, setDownloadNote] = useState<Note>(null);

  useEffect(() => {
    setProjects({ status: "loading" });
    api
      .listProjects()
      .then((r) => setProjects({ status: "ready", data: r }))
      .catch((e: unknown) => setProjects(mapApiError(e)));
  }, []);

  useEffect(() => {
    if (!selectedProjectId) {
      setScenes({ status: "idle" });
      return;
    }
    setScenes({ status: "loading" });
    api
      .listScenes(selectedProjectId)
      .then((r) => setScenes({ status: "ready", data: r }))
      .catch((e: unknown) => setScenes(mapApiError(e)));
  }, [selectedProjectId]);

  const loadForm = async (psId: string, f: PromptForm) => {
    if (promptCache[f] !== undefined) {
      setPromptData({ status: "ready", data: promptCache[f] });
      return;
    }
    setPromptData({ status: "loading" });
    try {
      const data = await api.getPrompt(psId, f);
      setPromptCache((c) => ({ ...c, [f]: data }));
      setPromptData({ status: "ready", data });
    } catch (e) {
      setPromptData(mapApiError(e));
    }
  };

  // 能力探测：进面板即查一次。失败不影响分镜/提示词（降级是分类不是异常）。
  useEffect(() => {
    let alive = true;
    setCaps({ status: "loading" });
    api
      .getGenerateCapabilities()
      .then((c) => alive && setCaps({ status: "ready", data: c }))
      .catch((e) => alive && setCaps(mapApiError(e)));
    return () => {
      alive = false;
    };
  }, []);

  const generateShots = async () => {
    if (storyboard.status !== "ready") return;
    if (caps.status !== "ready" || !caps.data.generate.available) {
      // 不可用时必须有可见文案，不能静默无响应（spec 5.3）
      setGenNote({
        err: caps.status === "ready"
          ? caps.data.generate.message ?? caps.data.generate.reason ?? ""
          : "",
      });
      return;
    }
    setGenBusy(true);
    setGenNote(null);
    setGenResult(null);
    try {
      const r = await api.generateShots(storyboard.data.id);
      setGenResult(r);
      if (r.failures.length === 0) {
        setGenNote({ ok: t("workbench.pipeline.generateDone") });
      } else {
        setGenNote({
          err: t("workbench.pipeline.generatePartial", { n: r.failures.length }),
        });
      }
    } catch (e) {
      setGenNote(noteFromError(e));
    } finally {
      setGenBusy(false);
    }
  };

  const editShot = async (shotIdx: number, instruction: string) => {
    if (storyboard.status !== "ready") return;
    setEditBusyIdx(shotIdx);
    setEditNote(null);
    setEditResult(null);
    try {
      const r = await api.editShot(storyboard.data.id, shotIdx, { instruction });
      setEditResult(r);
      setEditNote({ ok: t("workbench.pipeline.editDone", { idx: shotIdx }) });
      // 改完后计划会翻成 edit 模式 —— 重取一次，让用户看到状态已变
      if (prompts) await loadForm(prompts.id, "generation");
    } catch (e) {
      setEditNote(noteFromError(e));
    } finally {
      setEditBusyIdx(null);
    }
  };

  useEffect(() => {
    let alive = true;
    setVideoCaps({ status: "loading" });
    api
      .getVideoCapabilities()
      .then((c) => alive && setVideoCaps({ status: "ready", data: c }))
      .catch((e) => alive && setVideoCaps(mapApiError(e)));
    return () => {
      alive = false;
    };
  }, []);

  const videoRenderAction = async () => {
    if (storyboard.status !== "ready") return;
    setVideoBusy("render");
    setVideoNote(null);
    setVideoRender(null);
    setVideoEstimate(null);
    try {
      const r = (await api.createVideo(storyboard.data.id, { route: "render" })) as VideoRenderResult;
      setVideoRender(r);
      setVideoNote({ ok: t("workbench.pipeline.videoRendered") });
    } catch (e) {
      setVideoNote(noteFromError(e));
    } finally {
      setVideoBusy(null);
    }
  };

  const videoI2vAction = async () => {
    if (storyboard.status !== "ready") return;
    setVideoBusy("i2v");
    setVideoNote(null);
    setVideoSubmit(null);
    setVideoRender(null);
    try {
      const r = await api.createVideo(storyboard.data.id, { route: "i2v" });
      // 未确认时后端只返预估 —— 不发起任何付费调用
      if ("estimated_calls" in r) {
        setVideoEstimate(r as VideoEstimate);
      } else {
        setVideoSubmit(r as VideoSubmitResult);
      }
    } catch (e) {
      setVideoNote(noteFromError(e));
    } finally {
      setVideoBusy(null);
    }
  };

  const videoI2vConfirm = async () => {
    if (storyboard.status !== "ready") return;
    setVideoBusy("i2v");
    setVideoNote(null);
    try {
      const r = (await api.createVideo(storyboard.data.id,
        { route: "i2v", confirm: true })) as VideoSubmitResult;
      setVideoSubmit(r);
      setVideoEstimate(null);
      setVideoNote({ ok: t("workbench.pipeline.i2vSubmitted", { n: r.submitted?.length ?? 0 }) });
    } catch (e) {
      setVideoNote(noteFromError(e));
    } finally {
      setVideoBusy(null);
    }
  };

  const generateStoryboard = async () => {
    if (!selectedSceneId) return;
    setSbBusy(true);
    setSbNote(null);
    setPrompts(null);
    setPromptData({ status: "idle" });
    setPromptCache({});
    try {
      const sb = normalizeStoryboard(await api.genStoryboard(selectedSceneId));
      setStoryboard({ status: "ready", data: sb });
    } catch (e) {
      setStoryboard(mapApiError(e));
      setSbNote(noteFromError(e));
    } finally {
      setSbBusy(false);
    }
  };

  const updateShot = async (shot: StoryboardShot) => {
    if (storyboard.status !== "ready") return;
    const sb = storyboard.data;
    setShotBusyIdx(shot.idx);
    setSbNote(null);
    try {
      // 视图字段（line/duration）→ 后端契约字段（line_text/duration_sec），
      // 同时带上 staging/keyframe/model_sheet_refs/art_bible_refs。
      await api.updateShot(sb.id, shot.idx, shotPatch(shot));
      setStoryboard({
        status: "ready",
        data: { ...sb, shots: (sb.shots ?? []).map((s) => (s.idx === shot.idx ? shot : s)) },
      });
      setSbNote({ ok: t("workbench.pipeline.shotSaved") });
    } catch (e) {
      setSbNote(noteFromError(e));
    } finally {
      setShotBusyIdx(null);
    }
  };

  const generatePrompts = async () => {
    if (storyboard.status !== "ready") return;
    setPromptBusy(true);
    setPromptNote(null);
    try {
      const ref = await api.genPrompts(storyboard.data.id);
      const ps = { id: String(ref.id) };
      setPrompts(ps);
      await loadForm(ps.id, form);
    } catch (e) {
      setPromptNote(noteFromError(e));
    } finally {
      setPromptBusy(false);
    }
  };

  const switchForm = (f: PromptForm) => {
    setForm(f);
    if (prompts) void loadForm(prompts.id, f);
  };

  const download = async () => {
    if (!prompts) return;
    setDownloadBusy(true);
    setDownloadNote(null);
    try {
      const entries = await Promise.all(
        PROMPT_FORMS.map(async (f) => [f, await api.getPrompt(prompts.id, f)] as const),
      );
      downloadJson(`juchuang-prompts-${prompts.id}.json`, {
        id: prompts.id,
        forms: Object.fromEntries(entries),
      });
      setDownloadNote({ ok: t("workbench.pipeline.downloaded") });
    } catch (e) {
      setDownloadNote(noteFromError(e));
    } finally {
      setDownloadBusy(false);
    }
  };

  return (
    <PipelineView
      projects={projects}
      selectedProjectId={selectedProjectId}
      onSelectProject={setSelectedProjectId}
      scenes={scenes}
      selectedSceneId={selectedSceneId}
      onSelectScene={setSelectedSceneId}
      storyboard={storyboard}
      onGenerateStoryboard={() => void generateStoryboard()}
      sbBusy={sbBusy}
      sbNote={sbNote}
      onUpdateShot={(shot) => void updateShot(shot)}
      shotBusyIdx={shotBusyIdx}
      prompts={prompts}
      onGenPrompts={() => void generatePrompts()}
      promptBusy={promptBusy}
      promptNote={promptNote}
      form={form}
      onForm={switchForm}
      promptData={promptData}
      onDownload={() => void download()}
      downloadBusy={downloadBusy}
      downloadNote={downloadNote}
      caps={caps}
      onGenerate={() => void generateShots()}
      genBusy={genBusy}
      genNote={genNote}
      genResult={genResult}
      onEditShot={(idx, instruction) => void editShot(idx, instruction)}
      editBusyIdx={editBusyIdx}
      editNote={editNote}
      editResult={editResult}
      videoCaps={videoCaps}
      onVideoI2v={() => void videoI2vAction()}
      onVideoRender={() => void videoRenderAction()}
      videoBusy={videoBusy}
      videoNote={videoNote}
      videoEstimate={videoEstimate}
      videoSubmit={videoSubmit}
      videoRender={videoRender}
      onConfirmI2v={() => void videoI2vConfirm()}
      onCancelI2v={() => setVideoEstimate(null)}
    />
  );
}
