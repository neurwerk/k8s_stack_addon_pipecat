export const stages = ["input", "vad", "turn", "stt", "diarization", "attribution", "ready", "llm", "tool", "buffer", "tts", "output"] as const;
export type Stage = (typeof stages)[number];
export const statuses = ["active", "waiting", "complete", "interrupted", "failed", "off"] as const;
export type Status = (typeof statuses)[number];
export interface ActivityEvent { stage: Stage; status: Status; label: string; at_ms: number; seq: number }
export interface ModelInfo { model: string; hosting: string; host: string; compute: string; evidence: string; via?: string; upstream_provider?: string; checked_at?: number }
export interface PipelineActivity {
  at_ms: number;
  seq: number;
  live: boolean;
  paused: boolean;
  receivedAt: number;
  states: Record<Stage, ActivityEvent>;
  events: ActivityEvent[];
  models: Partial<Record<Stage, ModelInfo>>;
  interruption?: { at_ms: number; output_was_active: boolean; output_stopped_ms?: number };
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function number(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}
function text(value: unknown, max = 253): value is string { return typeof value === "string" && value.length <= max; }
function stage(value: unknown): value is Stage { return stages.some((s) => s === value); }
function status(value: unknown): value is Status { return statuses.some((s) => s === value); }
function event(value: unknown): ActivityEvent | undefined {
  if (!object(value) || !stage(value.stage) || !status(value.status) || !text(value.label, 100) || !number(value.at_ms) || !number(value.seq)) return;
  return { stage: value.stage, status: value.status, label: value.label, at_ms: value.at_ms, seq: value.seq };
}
export function parseActivity(value: unknown): PipelineActivity | undefined {
  if (!object(value) || value.type !== "pipeline-activity" || !number(value.at_ms) || !number(value.seq) || !object(value.states) || !object(value.models) || !Array.isArray(value.events) || value.events.length > 160) return;
  if (value.paused !== undefined && typeof value.paused !== "boolean") return;
  const pairs: [Stage, ActivityEvent][] = [];
  for (const key of stages) {
    const state = event(value.states[key]);
    if (!state || state.stage !== key || state.at_ms > value.at_ms || state.seq > value.seq) return;
    pairs.push([key, state]);
  }
  const events: ActivityEvent[] = [];
  for (const raw of value.events) {
    const parsed = event(raw);
    if (!parsed || parsed.at_ms > value.at_ms || parsed.seq > value.seq) return;
    const previous = events.at(-1);
    if (previous && (parsed.seq <= previous.seq || parsed.at_ms < previous.at_ms)) return;
    events.push(parsed);
  }
  const models: Partial<Record<Stage, ModelInfo>> = {};
  for (const key of stages) {
    const m = value.models[key];
    if (m === undefined) continue;
    if (!object(m) || !text(m.model) || !text(m.hosting) || !text(m.host) || !text(m.compute) || !text(m.evidence)) return;
    const info: ModelInfo = { model: m.model, hosting: m.hosting, host: m.host, compute: m.compute, evidence: m.evidence };
    for (const optional of ["via", "upstream_provider"] as const) {
      if (m[optional] !== undefined) {
        if (!text(m[optional])) return;
        info[optional] = m[optional];
      }
    }
    if (m.checked_at !== undefined) {
      if (!number(m.checked_at)) return;
      info.checked_at = m.checked_at;
    }
    models[key] = info;
  }
  let interruption: PipelineActivity["interruption"];
  if (value.interruption !== null) {
    const i = value.interruption;
    if (!object(i) || !number(i.at_ms) || typeof i.output_was_active !== "boolean") return;
    interruption = { at_ms: i.at_ms, output_was_active: i.output_was_active };
    if (i.output_stopped_ms !== undefined) {
      if (!number(i.output_stopped_ms) || i.output_stopped_ms < i.at_ms) return;
      interruption.output_stopped_ms = i.output_stopped_ms;
    }
  }
  return { at_ms: value.at_ms, seq: value.seq, live: true, paused: value.paused === true, receivedAt: performance.now(),
    states: Object.fromEntries(pairs) as Record<Stage, ActivityEvent>, events, models, interruption };
}

export function endActivity(activity: PipelineActivity | undefined): PipelineActivity | undefined {
  if (!activity) return;
  return { ...activity, live: false };
}
