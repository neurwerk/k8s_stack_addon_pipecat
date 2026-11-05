import { timingStatuses, type TurnTiming } from "@/timings";

function duration(ms: number | undefined): string {
  return ms === undefined ? "—" : `${(ms / 1000).toFixed(2)} s`;
}

function rate(count: number | undefined, ms: number | undefined): string {
  return count === undefined || !ms ? "—" : (count / (ms / 1000)).toFixed(1);
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
      {timings.length === 0 ? (
        <p className="mt-3 text-sm text-muted-foreground">Timings appear when a conversation starts.</p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-sm tabular-nums">
            <caption className="sr-only">Last eight responses, newest first. Missing measurements are shown as a dash.</caption>
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="p-2">Response</th>
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
                  <td className="whitespace-nowrap p-2">{duration(timing.stt_ms)}</td>
                  <td className="p-2">{rate(timing.words, timing.stt_ms)}</td>
                  <td className="p-2">{rate(timing.audio_seconds, timing.stt_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.diarization_finalize_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.diarization_wait_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.llm_first_text_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.text_buffer_ms)}</td>
                  <td className="whitespace-nowrap p-2">{duration(timing.tts_first_audio_ms)}</td>
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
        </p>
      </details>
    </details>
  );
}
