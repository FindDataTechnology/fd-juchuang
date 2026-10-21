/**
 * 质量门 · 六维评估（change juchuang-continuity-layer task 4.1-4.4）。
 *
 * 视图与容器同文件：视图（QualityView）可独立 server-render 测试降级态与就绪态。
 * 关键约束：六个维度分列展示，任一维分数都可展开到「规则项 + 问题清单 + 依据」，
 * 不允许只显示一个总分；命中合规红线时置顶红色阻断横幅并逐条列出命中项。
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  type Project,
  type QualityAssessment,
  type QualityDimensionScore,
  type QualityLocation,
  type QualityProblem,
  type QualityViolationDetail,
} from "../../api/client";
import { Loadable, mapApiError, noteFromError, type Note } from "../../api/state";
import {
  btnPrimary,
  EmptyLine,
  inputText,
  LoadBox,
  NoteBox,
  Picker,
  SectionTitle,
} from "../../components/ui";

type Translate = (key: string, options?: Record<string, unknown>) => string;

const SEVERITY_BADGE: Record<string, string> = {
  warning: "border-amber-300 bg-amber-50 text-amber-800",
  error: "border-red-300 bg-red-50 text-red-800",
  critical: "border-red-600 bg-red-600 text-white",
};

const SEVERITY_BOX: Record<string, string> = {
  warning: "border-amber-200 bg-amber-50/50",
  error: "border-red-200 bg-red-50/50",
  critical: "border-red-300 bg-red-50",
};

const RULE_BADGE: Record<string, string> = {
  ok: "border-green-200 bg-green-50 text-green-700",
  violated: "border-red-200 bg-red-50 text-red-700",
  skipped: "border-neutral-200 bg-neutral-50 text-neutral-500",
};

function scoreText(score: number | null | undefined): string {
  return score === null || score === undefined ? "—" : score.toFixed(1);
}

function locationText(location: QualityLocation | undefined, t: Translate): string {
  if (!location) return "";
  const scene = location.scene_id != null ? `#${location.scene_id}` : "—";
  const line = location.line_idx != null ? `#${location.line_idx}` : "—";
  return t("workbench.quality.location", {
    scene,
    line,
    character: location.character || "—",
  });
}

// ---- 视图 ----

export interface QualityViewProps {
  projects: Loadable<Project[]>;
  selectedProjectId: string;
  onSelectProject: (id: string) => void;
  episode: string;
  onEpisodeChange: (value: string) => void;
  onAssess: () => void;
  assessBusy: boolean;
  assessNote: Note;
  assessment: Loadable<QualityAssessment>;
}

export function QualityView({
  projects,
  selectedProjectId,
  onSelectProject,
  episode,
  onEpisodeChange,
  onAssess,
  assessBusy,
  assessNote,
  assessment,
}: QualityViewProps) {
  const { t } = useTranslation();
  const translate = t as unknown as Translate;
  return (
    <section className="space-y-8">
      <div>
        <SectionTitle>{t("workbench.quality.heading")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.quality.hint")}</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-[1fr,180px]">
        <Picker
          label={t("workbench.quality.project")}
          state={projects}
          selectedId={selectedProjectId}
          onSelect={onSelectProject}
          labelOf={(p) => p.title || t("workbench.untitled")}
        />
        <label className="block text-sm">
          <span className="text-neutral-500">{t("workbench.quality.episode")}</span>
          <input
            className={`mt-1 ${inputText}`}
            type="number"
            min={1}
            value={episode}
            onChange={(e) => onEpisodeChange(e.target.value)}
            aria-label={t("workbench.quality.episode")}
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className={btnPrimary}
          disabled={assessBusy || !selectedProjectId}
          onClick={onAssess}
        >
          {assessBusy ? t("workbench.quality.assessing") : t("workbench.quality.assess")}
        </button>
        <NoteBox note={assessNote} />
      </div>

      {!selectedProjectId ? (
        <EmptyLine>{t("workbench.quality.noProject")}</EmptyLine>
      ) : null}

      <LoadBox state={assessment}>
        {(data) => <AssessmentReport assessment={data} translate={translate} />}
      </LoadBox>
    </section>
  );
}

/** 评估报告：阻断横幅 → 总分 → 六维卡片（每维可展开问题清单）。 */
export function AssessmentReport({
  assessment,
  translate,
}: {
  assessment: QualityAssessment;
  translate: Translate;
}) {
  const { t } = useTranslation();
  const order =
    assessment.dimension_order && assessment.dimension_order.length > 0
      ? assessment.dimension_order
      : Object.keys(assessment.dimensions);
  const violations = assessment.violation_details ?? [];

  return (
    <div className="space-y-5" data-testid="quality-report">
      {assessment.blocked ? (
        <div
          role="alert"
          data-testid="quality-blocked"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800"
        >
          <p className="font-medium">{t("workbench.quality.blockedTitle")}</p>
          <ul className="mt-2 list-disc space-y-2 pl-5">
            {violations.map((v, index) => (
              <ViolationItem key={`${v.rule_id}-${index}`} violation={v} translate={translate} />
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-sm text-green-700">{t("workbench.quality.notBlocked")}</p>
      )}

      <div className="flex flex-wrap items-baseline justify-between gap-2 rounded-xl border border-neutral-200 bg-white px-4 py-3">
        <span className="text-sm text-neutral-500">{t("workbench.quality.overall")}</span>
        <span className="text-2xl font-semibold tabular-nums text-neutral-900">
          {scoreText(assessment.overall_score)}
          <span className="ml-1 text-xs font-normal text-neutral-400">/ 10</span>
        </span>
        <span className="w-full text-xs text-neutral-400">
          {assessment.episode_id} · {assessment.overall_formula}
        </span>
      </div>

      <div className="space-y-4" data-testid="quality-dimensions">
        {order.map((key) => {
          const dimension = assessment.dimensions[key];
          return dimension ? (
            <DimensionCard key={key} dimension={dimension} translate={translate} />
          ) : null;
        })}
      </div>
    </div>
  );
}

function ViolationItem({
  violation,
  translate,
}: {
  violation: QualityViolationDetail;
  translate: Translate;
}) {
  const { t } = useTranslation();
  return (
    <li>
      <span className="font-mono text-xs">{violation.rule_id}</span>
      <span className="ml-2">{violation.message}</span>
      <span className="ml-2 text-xs text-red-700">
        {locationText(violation.location, translate)}
      </span>
      <span className="mt-0.5 block text-xs">
        {t("workbench.quality.evidence")}: {violation.evidence}
      </span>
    </li>
  );
}

/** 单维卡片：分数 + 规则执行情况 + 可展开问题清单（含依据与台账引用）。 */
export function DimensionCard({
  dimension,
  translate,
}: {
  dimension: QualityDimensionScore;
  translate: Translate;
}) {
  const { t } = useTranslation();
  const problems = dimension.problems ?? [];
  const rules = dimension.rule_results ?? [];
  const weight = Math.round((dimension.weight ?? 0) * 100);
  return (
    <article
      className="rounded-xl border border-neutral-200 bg-white"
      data-testid={`quality-dim-${dimension.dimension}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
        <div>
          <h3 className="font-medium text-neutral-900">{dimension.label}</h3>
          <p className="mt-0.5 text-xs text-neutral-500">
            {t("workbench.quality.weight", { value: weight })}
            {" · "}
            {dimension.problem_count} {t("workbench.quality.problemCount")}
          </p>
        </div>
        <div className="text-right">
          <span className="text-xl font-semibold tabular-nums text-neutral-900">
            {dimension.available ? scoreText(dimension.score) : t("workbench.quality.na")}
          </span>
          <span className="ml-1 text-xs text-neutral-400">/ 10</span>
        </div>
      </div>

      {!dimension.available ? (
        <p className="px-4 pb-3 text-xs text-amber-700">
          {t("workbench.quality.unavailableReason")}
          {dimension.reason ? `：${dimension.reason}` : ""}
        </p>
      ) : null}

      <details
        className="border-t border-neutral-100 px-4 py-3"
        data-testid={`quality-problems-${dimension.dimension}`}
      >
        <summary className="cursor-pointer text-sm text-neutral-700">
          {t("workbench.quality.expand", { count: problems.length })}
        </summary>

        <div className="mt-3 space-y-4">
          <div>
            <p className="text-xs font-medium text-neutral-500">
              {t("workbench.quality.rules")}
            </p>
            <ul className="mt-1 space-y-1">
              {rules.map((rule) => (
                <li
                  key={rule.rule_id}
                  className="flex flex-wrap items-center gap-1.5 text-xs text-neutral-600"
                >
                  <span className="font-mono text-[11px] text-neutral-500">{rule.rule_id}</span>
                  <span>{rule.title}</span>
                  <span
                    className={`rounded border px-1.5 py-0.5 text-[11px] ${
                      RULE_BADGE[rule.status] ?? RULE_BADGE.skipped
                    }`}
                  >
                    {t(`workbench.quality.ruleStatus.${rule.status}`, {
                      defaultValue: rule.status,
                    })}
                  </span>
                  <span>{t("workbench.quality.checked", { count: rule.checked })}</span>
                  {rule.problem_count > 0 ? (
                    <span className="text-red-700">· {rule.problem_count}</span>
                  ) : null}
                  {rule.reason ? (
                    <span className="text-neutral-400">· {rule.reason}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>

          {problems.length === 0 ? (
            <p className="text-sm text-neutral-400">{t("workbench.quality.noProblems")}</p>
          ) : (
            <ul
              className="space-y-3"
              data-testid={`quality-problem-list-${dimension.dimension}`}
            >
              {problems.map((problem, index) => (
                <ProblemItem
                  key={`${problem.rule_id}-${index}`}
                  problem={problem}
                  translate={translate}
                />
              ))}
            </ul>
          )}
        </div>
      </details>
    </article>
  );
}

function ProblemItem({
  problem,
  translate,
}: {
  problem: QualityProblem;
  translate: Translate;
}) {
  const { t } = useTranslation();
  const refs = problem.ledger_refs ?? [];
  return (
    <li
      className={`rounded-lg border px-3 py-2 text-sm ${
        SEVERITY_BOX[problem.severity] ?? "border-neutral-200 bg-neutral-50"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded border px-1.5 py-0.5 text-[11px] ${
            SEVERITY_BADGE[problem.severity] ?? SEVERITY_BADGE.warning
          }`}
        >
          {t(`workbench.quality.severity.${problem.severity}`, {
            defaultValue: problem.severity,
          })}
        </span>
        <span className="font-mono text-[11px] text-neutral-500">{problem.rule_id}</span>
        <span className="text-xs text-neutral-500">
          {locationText(problem.location, translate)}
        </span>
      </div>
      <p className="mt-1 text-neutral-900">{problem.message}</p>
      <p className="mt-1 text-xs text-neutral-600">
        {t("workbench.quality.evidence")}: {problem.evidence}
      </p>
      {refs.length > 0 ? (
        <ul className="mt-1 space-y-0.5 text-xs text-indigo-700">
          {refs.map((ref, index) => (
            <li key={`${ref.kind}-${index}`}>
              {ref.kind === "foreshadow"
                ? t("workbench.quality.ledgerForeshadow", {
                    id: ref.foreshadow_id ?? "—",
                    hint: ref.signature ?? "—",
                  })
                : t("workbench.quality.ledgerFact", {
                    id: ref.fact_id ?? "—",
                    signature: ref.signature ?? "—",
                  })}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

// ---- 容器：接 API ----

export default function QualityPanel() {
  const { t } = useTranslation();
  const [projects, setProjects] = useState<Loadable<Project[]>>({ status: "idle" });
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [episode, setEpisode] = useState("1");
  const [assessment, setAssessment] = useState<Loadable<QualityAssessment>>({ status: "idle" });
  const [assessBusy, setAssessBusy] = useState(false);
  const [assessNote, setAssessNote] = useState<Note>(null);

  const loadProjects = async () => {
    setProjects({ status: "loading" });
    try {
      setProjects({ status: "ready", data: await api.listProjects() });
    } catch (e) {
      setProjects(mapApiError(e));
    }
  };

  useEffect(() => {
    void loadProjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectProject = (id: string) => {
    setSelectedProjectId(id);
    setAssessment({ status: "idle" });
    setAssessNote(null);
  };

  const assess = async () => {
    if (!selectedProjectId) return;
    const value = Math.max(1, Math.round(Number(episode) || 1));
    setAssessBusy(true);
    setAssessNote(null);
    setAssessment({ status: "loading" });
    try {
      const data = await api.assessQuality(selectedProjectId, { episode: value });
      setAssessment({ status: "ready", data });
      setAssessNote(
        data.blocked
          ? { err: t("workbench.quality.blockedNote") }
          : { ok: t("workbench.quality.assessed") },
      );
    } catch (e) {
      setAssessment(mapApiError(e));
      setAssessNote(noteFromError(e));
    } finally {
      setAssessBusy(false);
    }
  };

  return (
    <QualityView
      projects={projects}
      selectedProjectId={selectedProjectId}
      onSelectProject={selectProject}
      episode={episode}
      onEpisodeChange={setEpisode}
      onAssess={() => void assess()}
      assessBusy={assessBusy}
      assessNote={assessNote}
      assessment={assessment}
    />
  );
}
