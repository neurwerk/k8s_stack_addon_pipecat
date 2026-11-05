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
      {duration(ms)} <span className="text-xs">({Math.round(percent)}% of goal)</span>
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
      {value.toFixed(1)} <span className="text-xs">({Math.round(percent)}% of target)</span>
      {severity && <span className="sr-only">, {severity} throughput</span>}
    </span>
  );
}

export function TimingPanel({
  timings,
  speaking,
}: {
  timings: TurnTiming[];
  speaking: boolean;
}) {
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Response timings</summary>
      <p className="mt-2 text-xs text-muted-foreground">
          Speaking signal: {speaking ? "active" : "inactive"} (server output, not speaker playback)
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        First-audio estimate goal: 1 s; TTS first-audio goal: 0.8 s. These are local targets, not service guarantees. {" "}
        <span className="text-amber-700 dark:text-amber-400">Yellow &gt; 100%</span>,{" "}
        <span className="text-orange-700 dark:text-orange-400">orange ≥ 150%</span>,{" "}
        <span className="text-red-700 dark:text-red-400">red ≥ 200%</span> of each goal.
        STT rate targets: 10 words/s and 4 audio s/s; below target is yellow,
        below 75% orange, below 50% red. Samples under 8 words or 3 s of audio are not graded for rate.
      </p>
      {timings.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">Timings appear when a conversation starts.</p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-sm tabular-nums">
            <caption className="sr-only">Last eight responses, newest first. Missing measurements are shown as a dash.</caption>
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
      <details className="mt-3 text-xs text-muted-foreground">
        <summary className="cursor-pointer">How timings are measured</summary>
        <p className="mt-2">
        STT includes the transcription request and response; words are counted by whitespace.
        Audio s/s above 1 means faster than real time, including segment padding.
        Speaker finalization measures the end-of-segment flush request to finalized speaker results,
        including queued audio and network time; it overlaps STT and is not total model compute time.
        Speaker wait is only the extra wait after transcription, zero when results are already ready.
        STT and speaker measurements are summed across segments in each reply; missing measurements show a dash.
        LLM measures request to first usable text. Text buffering measures first text to the first speech request.
        TTS measures that request to first valid PCM audio. Text → audio includes buffering and TTS;
        it is not an extra stage. Network time is included; end-of-speech detection and browser playback are not.
        The first-audio estimate adds STT, extra speaker wait, LLM first text, and text → audio;
        it does not measure pauses between sentences.
        </p>
      </details>
    </details>
  );
}
