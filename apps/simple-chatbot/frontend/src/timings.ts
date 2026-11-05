export const timingStatuses: Record<string, string> = {
  transcribing: "Transcribing",
  transcribed: "Transcript ready",
  generating: "Generating reply",
  synthesizing: "Preparing audio",
  "audio-ready": "Audio streaming",
  complete: "Audio generated",
  "no-audio": "No spoken content",
  failed: "Failed",
  interrupted: "Interrupted",
};

const metricKeys = [
  "stt_ms",
  "diarization_finalize_ms",
  "diarization_wait_ms",
  "words",
  "audio_seconds",
  "llm_first_text_ms",
  "text_buffer_ms",
  "tts_first_audio_ms",
  "text_to_audio_ms",
] as const;

export type TurnTiming = {
  turn: number;
  kind: "greeting" | "reply";
  status: string;
} & Partial<Record<(typeof metricKeys)[number], number>>;

export function parseTiming(value: unknown): TurnTiming | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return;
  const data: Record<string, unknown> = Object.fromEntries(Object.entries(value));
  if (
    data.type !== "turn-timing" ||
    typeof data.turn !== "number" ||
    !Number.isSafeInteger(data.turn) ||
    data.turn < 1 ||
    (data.kind !== "greeting" && data.kind !== "reply") ||
    typeof data.status !== "string" ||
    !Object.hasOwn(timingStatuses, data.status)
  ) return;
  const result: TurnTiming = {
    turn: data.turn,
    kind: data.kind,
    status: data.status,
  };
  for (const key of metricKeys) {
    const metric = data[key];
    if (metric === undefined) continue;
    if (typeof metric !== "number" || !Number.isFinite(metric) || metric < 0) return;
    result[key] = metric;
  }
  return result;
}
