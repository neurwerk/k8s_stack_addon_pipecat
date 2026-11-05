import { useEffect, useState } from "react";
import { stages, type PipelineActivity, type Stage, type Status } from "@/activity";

const nodes: Record<Stage, { title: string; x: number; y: number }> = {
  input: { title: "Microphone input", x: 0, y: 0 },
  vad: { title: "Voice activity", x: 220, y: 0 },
  turn: { title: "End of turn", x: 440, y: 0 },
  diarization: { title: "Speaker recognition", x: 0, y: 190 },
  stt: { title: "Transcription", x: 220, y: 190 },
  attribution: { title: "Speaker attribution", x: 440, y: 190 },
  ready: { title: "Ready to answer", x: 660, y: 85 },
  llm: { title: "Language model", x: 880, y: 85 },
  tool: { title: "Speaker tools", x: 880, y: 330 },
  buffer: { title: "Sentence buffer", x: 660, y: 400 },
  tts: { title: "Speech synthesis", x: 440, y: 400 },
  output: { title: "Audio output", x: 220, y: 400 },
};
const colors: Record<Status, string> = {
  active: "bg-primary text-primary-foreground border-primary",
  waiting: "bg-muted text-muted-foreground border-border",
  complete: "bg-card text-foreground border-border",
  interrupted: "bg-amber-50 text-amber-900 border-amber-300",
  failed: "bg-destructive/10 text-destructive border-destructive",
  off: "bg-muted/40 text-muted-foreground border-border",
};
const symbols: Record<Status, string> = { active: "●", waiting: "○", complete: "✓", interrupted: "↯", failed: "!", off: "—" };
const paths = [
  "M190 65 H220", "M410 65 H440", "M95 160 V190",
  "M190 110 H205 V175 H315 V190", "M315 160 V190",
  "M410 255 H440", "M190 285 H210 V370 H535 V350",
  "M630 65 H650 V125 H660", "M630 255 H650 V180 H660",
  "M850 155 H880", "M975 245 V280 H755 V400",
  "M660 470 H630", "M440 470 H410", "M965 245 V330",
  "M1000 330 V265 H1025 V245",
];

export function PipelinePanel({ activity, micEnabled }: { activity?: PipelineActivity; micEnabled: boolean }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), 500);
    return () => clearInterval(timer);
  }, []);
  const stale = !!activity?.live && now - activity.receivedAt > 2500;
  const live = !!activity?.live && !stale;
  const interruption = activity?.interruption;
  return (
    <details className="rounded-xl border bg-card p-5">
      <summary className="cursor-pointer font-medium">Live voice pipeline</summary>
      <p className="mt-2 text-xs text-muted-foreground">{stale ? "Activity connection delayed — last known state" : live ? "Live · microphone " + (micEnabled ? "enabled" : "muted") : activity ? "Conversation ended · last activity" : "Start a conversation to see activity"}</p>
      <p className="mt-2 text-xs text-muted-foreground">● Dark: active · ○ Grey: waiting · ✓ Complete · ↯ Interrupted. Arrows show dependencies; streaming stages can overlap.</p>
      {interruption && <p role="status" className="mt-3 rounded-md bg-amber-50 p-2 text-sm text-amber-900">
        ↯ Barge-in detected at {(interruption.at_ms / 1000).toFixed(2)} s.
        {interruption.output_was_active
          ? interruption.output_stopped_ms === undefined
            ? " Waiting for server output to stop."
            : ` Server output stopped ${Math.round(interruption.output_stopped_ms - interruption.at_ms)} ms later.`
          : " Interrupted response generation; server output was not active."}
      </p>}
      <div className="mt-4 overflow-x-auto" tabIndex={0} aria-label="Pipeline dependency graph; scroll horizontally on small screens">
        <div className="relative" style={{ width: 1070, height: 570 }}>
          <svg aria-hidden="true" className="absolute inset-0 h-full w-full text-muted-foreground" viewBox="0 0 1070 570">
            <defs><marker id="pipeline-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10Z" fill="currentColor" /></marker></defs>
            {paths.map((path) => <path key={path} d={path} fill="none" stroke="currentColor" strokeWidth="1.5" markerEnd="url(#pipeline-arrow)" />)}
            <text x="325" y="166" fontSize="11" fill="currentColor">pause</text>
            <text x="770" y="262" fontSize="11" fill="currentColor">streamed text</text>
            <text x="886" y="300" fontSize="11" fill="currentColor">optional tool ↔ follow-up</text>
            <text x="20" y="390" fontSize="11" fill="currentColor">VAD stays enabled during replies.</text>
          </svg>
          {stages.map((stage) => {
            const node = nodes[stage];
            const state = activity?.states[stage];
            const status = !live && state?.status === "active" ? "off" : state?.status ?? "off";
            const label = stage === "input" && live && !micEnabled ? "Microphone muted in browser" : state?.label ?? "Awaiting conversation";
            return <div key={stage} className={`absolute rounded-lg border p-3 shadow-sm ${colors[status]}`} style={{ left: node.x, top: node.y, width: 190, height: 160 }}>
              <h3 className="text-sm font-semibold">{node.title}</h3>
              <p className="mt-1 text-xs" aria-label={`${status}: ${label}`}>{symbols[status]} {label}</p>
              <p className="mt-2 text-[11px] opacity-70">{stage === "input" ? "Browser → app" : stage === "output" ? "App → browser · playback unmeasured" : activity?.models[stage] ? "See model placement below" : "App process"}</p>
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
            <th scope="col" className="p-2">Source</th>
          </tr></thead>
          <tbody>{stages.map((stage) => {
            const model = activity.models[stage];
            if (!model) return null;
            return <tr key={stage} className="border-t align-top">
              <th scope="row" className="p-2 font-medium">{nodes[stage].title}</th>
              <td className="p-2">{model.model}</td>
              <td className="p-2">{stage === "llm" ? `Configured route: ${model.host}` : `${model.hosting} (${model.host})`}
                {model.via && <span className="block text-xs text-muted-foreground">Via gateway: {model.via}</span>}
                {model.upstream_provider && <span className="block text-xs text-muted-foreground">Last response reported provider: {model.upstream_provider}</span>}
              </td>
              <td className="p-2">{model.compute === "Unknown" ? "Not reported" : model.compute}</td>
              <td className="p-2">{model.evidence}{model.checked_at && <span className="block text-xs text-muted-foreground">Checked {new Date(model.checked_at * 1000).toLocaleTimeString()}</span>}</td>
            </tr>;
          })}</tbody>
        </table>
        <p className="mt-3 text-xs text-muted-foreground">Routes do not prove the inference provider or its hardware. Local compute reflects loaded ONNX session providers; remote hardware is shown only when reported by runtime health, checked every minute. Activity describes app-observed work, not GPU utilization.</p>
      </div> : <p className="mt-3 text-sm text-muted-foreground">Start a conversation to see model placement.</p>}
    </details>
  );
}
