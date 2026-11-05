import { useEffect, useState, type CSSProperties } from "react";
import { stages, type PipelineActivity, type Stage, type Status } from "@/activity";

const nodes: Record<Stage, { title: string; x: number; y: number }> = {
  input: { title: "Microphone input", x: 0, y: 0 },
  vad: { title: "Voice activity", x: 220, y: 0 },
  turn: { title: "End of turn", x: 440, y: 0 },
  diarization: { title: "Speaker recognition", x: 0, y: 160 },
  stt: { title: "Transcription", x: 220, y: 160 },
  attribution: { title: "Speaker attribution", x: 440, y: 160 },
  ready: { title: "Ready to answer", x: 660, y: 70 },
  llm: { title: "Language model", x: 880, y: 70 },
  tool: { title: "Speaker tools", x: 880, y: 280 },
  buffer: { title: "Sentence buffer", x: 660, y: 340 },
  tts: { title: "Speech synthesis", x: 440, y: 340 },
  output: { title: "Audio output", x: 220, y: 340 },
};
const colors: Record<Status, string> = {
  active: "bg-primary text-primary-foreground border-primary",
  waiting: "bg-muted text-muted-foreground border-border",
  complete: "bg-card text-foreground border-border",
  interrupted: "bg-amber-50 text-amber-900 border-amber-300",
  failed: "bg-destructive/10 text-destructive border-destructive",
  off: "bg-muted/40 text-muted-foreground border-border",
};
const symbols: Record<Status, string> = { active: "●", waiting: "○", complete: "✓", interrupted: "↯", failed: "!", off: "" };
const paths = [
  "M190 55 H220", "M410 55 H440", "M95 130 V160",
  "M190 90 H205 V145 H315 V160", "M315 130 V160",
  "M410 215 H440", "M190 245 H210 V310 H535 V290",
  "M630 55 H650 V105 H660", "M630 215 H650 V150 H660",
  "M850 125 H880", "M975 200 V235 H755 V340",
  "M660 395 H630", "M440 395 H410", "M965 200 V280",
  "M1000 280 V220 H1025 V200",
];

export function PipelinePanel({ activity, micEnabled, paused }: { activity?: PipelineActivity; micEnabled: boolean; paused: boolean }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), 500);
    return () => clearInterval(timer);
  }, []);
  const stale = !!activity?.live && now - activity.receivedAt > 2500;
  const live = !!activity?.live && !stale;
  const conversationPaused = paused || (live && activity?.paused);
  const interruption = conversationPaused ? undefined : activity?.interruption;
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Live voice pipeline</summary>
      <p className="mt-2 text-xs text-muted-foreground">{conversationPaused ? "Conversation paused" : stale ? "Activity connection delayed — last known state" : live ? "Live · microphone " + (micEnabled ? "enabled" : "muted") : activity ? "Conversation ended · last activity" : "Start a conversation to see activity"}</p>
      <p className="mt-2 text-xs text-muted-foreground">● Dark: active · ○ Grey: waiting · ✓ Complete · ↯ Interrupted. Arrows show dependencies; streaming stages can overlap.</p>
      {interruption && <div role="status" className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
        <h3 className="font-semibold">↯ Barge-in</h3>
        <p className="mt-1">Detected at {(interruption.at_ms / 1000).toFixed(2)} s.</p>
        <p className="mt-1">{interruption.output_was_active
          ? interruption.output_stopped_ms === undefined
            ? " Waiting for server output to stop."
            : ` Server output stopped ${Math.round(interruption.output_stopped_ms - interruption.at_ms)} ms later.`
          : " Interrupted response generation; server output was not active."}</p>
      </div>}
      <div className="pipeline-layout mt-4" aria-label="Pipeline dependency graph">
        <div className="pipeline-graph">
          <svg aria-hidden="true" className="pipeline-edges text-muted-foreground" viewBox="0 0 1070 480">
            <defs><marker id="pipeline-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10Z" fill="currentColor" /></marker></defs>
            {paths.map((path) => <path key={path} d={path} fill="none" stroke="currentColor" strokeWidth="1.5" markerEnd="url(#pipeline-arrow)" />)}
          </svg>
          {stages.map((stage) => {
            const node = nodes[stage];
            const state = activity?.states[stage];
            const status = conversationPaused || (!live && state?.status === "active") ? "off" : state?.status ?? "off";
            const label = conversationPaused ? "Conversation paused" : stage === "input" && live && !micEnabled ? "Microphone muted in browser" : state?.label ?? "Awaiting conversation";
            return <div key={stage} className={`pipeline-node rounded-lg border px-3 py-2 shadow-sm ${colors[status]}`} style={{ "--node-x": `${node.x / 1070 * 100}%`, "--node-y": `${node.y / 480 * 100}%` } as CSSProperties}>
              <h3 className="text-sm font-semibold">{node.title}</h3>
              <p className="mt-1 text-xs" aria-label={`${status}: ${label}`}>{symbols[status]} {label}</p>
              {stage === "input" || stage === "output" || !activity?.models[stage] ? <p className="mt-2 text-[11px] opacity-70">{stage === "input" ? "Browser → app" : stage === "output" ? "App → browser · playback unmeasured" : "App process"}</p> : null}
            </div>;
          })}
        </div>
      </div>
    </details>
  );
}

export function ActivityTimeline({ activity }: { activity?: PipelineActivity }) {
  const end = activity?.at_ms ?? 0;
  const start = Math.max(0, end - 20000);
  const width = Math.max(1, end - start);
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Recent activity timeline</summary>
      {activity ? <>
        <p className="mt-2 text-xs text-muted-foreground">Shared 20-second time axis; blank areas have no retained measurement.</p>
        <div className="mt-3 space-y-1" aria-label="Recent pipeline activity timeline">
          <div className="ml-36 flex justify-between text-[10px] text-muted-foreground"><span>{(start / 1000).toFixed(1)} s</span><span>{(end / 1000).toFixed(1)} s since connection</span></div>
          {stages.map((stage) => {
            const events = activity.events.filter((event) => event.stage === stage);
            return <div key={stage} className="flex items-center gap-2">
              <span className="w-34 shrink-0 text-xs">{nodes[stage].title}</span>
              <div className="relative h-4 flex-1 rounded bg-muted/30">
                {events.map((event, i) => {
                  const left = Math.max(start, event.at_ms);
                  const right = Math.min(end, events[i + 1]?.at_ms ?? end);
                  if (right <= left) return null;
                  return <div key={event.seq} className={`absolute h-full min-w-px rounded-sm border ${colors[event.status]}`}
                    style={{ left: `${(left - start) / width * 100}%`, width: `${(right - left) / width * 100}%` }}
                    title={`${event.label}: ${(left / 1000).toFixed(2)}–${(right / 1000).toFixed(2)} s`} />;
                })}
              </div>
            </div>;
          })}
        </div>
      </> : <p className="mt-3 text-sm text-muted-foreground">Start a conversation to see activity.</p>}
    </details>
  );
}

export function ModelLocations({ activity }: { activity?: PipelineActivity }) {
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Model and runtime placement</summary>
      {activity ? <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Configured model routes and reported runtime details</caption>
          <thead className="text-xs text-muted-foreground"><tr>
            <th scope="col" className="p-2">Stage</th><th scope="col" className="p-2">Model</th>
            <th scope="col" className="p-2">Location / route</th><th scope="col" className="p-2">Compute</th>
          </tr></thead>
          <tbody>{stages.map((stage) => {
            const model = activity.models[stage];
            if (!model) return null;
            return <tr key={stage} className="border-t align-top">
              <th scope="row" className="p-2 font-medium">{nodes[stage].title}</th>
              <td className="break-words p-2">{model.model}
                {model.revision && <span className="block break-all text-xs text-muted-foreground">Revision: {model.revision}</span>}
              </td>
              <td className="p-2">{stage === "llm" ? `Configured route: ${model.host}` : model.hosting === "pod" || model.hosting === "node" ? `${model.hosting}: ${model.host}` : `${model.hosting} (${model.host})`}
                {model.via && <span className="block text-xs text-muted-foreground">Via gateway: {model.via}</span>}
                {model.upstream_provider && <span className="block text-xs text-muted-foreground">Last response reported provider: {model.upstream_provider}</span>}
              </td>
              <td className="p-2">{stage === "llm" && model.hosting === "Third-party inference" ? "GPU" : model.compute === "Unknown" ? "Not reported" : model.compute}</td>
            </tr>;
          })}</tbody>
        </table>
      </div> : <p className="mt-3 text-sm text-muted-foreground">Start a conversation to see model placement.</p>}
    </details>
  );
}
