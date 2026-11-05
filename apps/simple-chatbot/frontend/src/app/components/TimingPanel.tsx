import { timingStatuses, type TurnTiming } from "@/timings";

const FIRST_AUDIO_GOAL_MS = 1000;
const TTS_FIRST_AUDIO_GOAL_MS = 800;
const STT_WORDS_PER_SECOND_TARGET = 10;
const STT_AUDIO_SPEED_TARGET = 4;

function duration(ms: number | undefined): string {
  return ms === undefined ? "—" : `${(ms / 1000).toFixed(2)} s`;
}

function rate(count: number | undefined, ms: number | undefined): number | undefined {
  return count === undefined || !ms ? undefined : count / (ms / 1000);
}

function firstAudio(timing: TurnTiming): number | undefined {
  if (
    timing.kind !== "reply" || timing.stt_ms === undefined ||
    timing.llm_first_text_ms === undefined || timing.text_to_audio_ms === undefined
  ) return undefined;
  return timing.stt_ms + (timing.diarization_wait_ms ?? 0) +
    timing.llm_first_text_ms + timing.text_to_audio_ms;
}

function color(severity: "yellow" | "orange" | "red" | undefined): string | undefined {
  if (severity === "red") return "text-red-700 dark:text-red-400";
  if (severity === "orange") return "text-orange-700 dark:text-orange-400";
  if (severity === "yellow") return "text-amber-700 dark:text-amber-400";
}

function delay(ms: number | undefined, goalMs?: number) {
  if (ms === undefined) return "—";
  if (goalMs === undefined) return duration(ms);
  const percent = (ms / goalMs) * 100;
  const severity = percent >= 200 ? "red"
    : percent >= 150 ? "orange" : percent > 100 ? "yellow" : undefined;
  return (
    <span className={color(severity)}>
      {duration(ms)}
      {severity && <span className="sr-only">, {severity} delay</span>}
    </span>
  );
}

function throughput(value: number | undefined, target: number, enoughAudio: boolean) {
  if (value === undefined) return "—";
  if (!enoughAudio) {
    return (
      <span title="Short sample: throughput is not graded">
        {value.toFixed(1)}<span className="sr-only">, short sample: rate not graded</span>
      </span>
    );
  }
  const percent = (value / target) * 100;
  const severity = percent < 50 ? "red"
    : percent < 75 ? "orange" : percent < 100 ? "yellow" : undefined;
  return (
    <span className={color(severity)}>
      {value.toFixed(1)}
      {severity && <span className="sr-only">, {severity} throughput</span>}
    </span>
  );
}

export function TimingPanel({
  timings,
}: {
  timings: TurnTiming[];
}) {
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Response timings</summary>
      {timings.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">Timings appear when a conversation starts.</p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-sm tabular-nums">
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="p-2">Response</th>
                <th scope="col" className="p-2">First audio estimate</th>
                <th scope="col" className="p-2">STT request</th>
                <th scope="col" className="p-2">STT words/s</th>
                <th scope="col" className="p-2">STT audio s/s</th>
                <th scope="col" className="p-2">Speaker finalization</th>
                <th scope="col" className="p-2">Speaker wait</th>
                <th scope="col" className="p-2">LLM first text</th>
                <th scope="col" className="p-2">Text buffering</th>
                <th scope="col" className="p-2">TTS first audio</th>
                <th scope="col" className="p-2">Text → audio</th>
              </tr>
            </thead>
            <tbody>
              {timings.map((timing) => (
                <tr key={timing.turn} className="border-t">
                  <th scope="row" className="p-2 font-normal">
                    {timing.kind === "greeting" ? "Greeting" : `Reply ${timing.turn}`}
                    <span className="block text-xs text-muted-foreground">{timingStatuses[timing.status]}</span>
                  </th>
                  <td className="whitespace-nowrap p-2 font-medium">{delay(firstAudio(timing), FIRST_AUDIO_GOAL_MS)}</td>
                  <td className="whitespace-nowrap p-2">{delay(timing.stt_ms)}</td>
                  <td className="p-2">{throughput(
                    rate(timing.words, timing.stt_ms),
                    STT_WORDS_PER_SECOND_TARGET,
                    (timing.words ?? 0) >= 8 && (timing.audio_seconds ?? 0) >= 3,
                  )}</td>
                  <td className="p-2">{throughput(
                    rate(timing.audio_seconds, timing.stt_ms),
                    STT_AUDIO_SPEED_TARGET,
                    (timing.words ?? 0) >= 8 && (timing.audio_seconds ?? 0) >= 3,
                  )}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.diarization_finalize_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.diarization_wait_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.llm_first_text_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.text_buffer_ms)}</td>
                  <td className="whitespace-nowrap p-2">{delay(timing.tts_first_audio_ms, TTS_FIRST_AUDIO_GOAL_MS)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.text_to_audio_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}
