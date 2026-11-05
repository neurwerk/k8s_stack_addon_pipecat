import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { PipecatClient } from "@pipecat-ai/client-js";
import {
  PipecatClientProvider,
  usePipecatClientTransportState,
  usePipecatClientMicControl,
} from "@pipecat-ai/client-react";
import {
  SmallWebRTCTransport,
  WavMediaManager,
} from "@pipecat-ai/small-webrtc-transport";
import { BotIcon } from "lucide-react";

import { UserAudioControl } from "@/components/pipecat/user-audio-control";
import { ConnectButtonView } from "@/components/pipecat/connect-button";
import { Button } from "@/components/ui/button";
import { ConversationPanel } from "@/app/components/ConversationPanel";
import { TimingPanel } from "@/app/components/TimingPanel";
import { ActivityTimeline, ModelLocations, PipelinePanel } from "@/app/components/PipelinePanel";
import type { PipelineActivity } from "./activity";
import { version } from "../package.json";
import type { TurnTiming } from "./timings";
import { initializeSession, type ChatError } from "./session";
import "./app/globals.css";
import "./style.css";

function MicrophoneControl({ paused }: { paused: boolean }) {
  const state = usePipecatClientTransportState();
  const connected = state === "connected" || state === "ready";
  return (
    <UserAudioControl
      size="lg"
      unavailableText={
        paused ? "Conversation paused" : connected ? undefined : "Microphone off"
      }
    />
  );
}

function App() {
  const [model, setModel] = useState("");
  const [timings, setTimings] = useState<TurnTiming[]>([]);
  const [speaking, setSpeaking] = useState(false);
  const [paused, setPaused] = useState(false);
  const [notices, setNotices] = useState<string[]>([]);
  const [errors, setErrors] = useState<ChatError[]>([]);
  const [activity, setActivity] = useState<PipelineActivity>();
  // No device initialization before an explicit Start click and admission check.
  const [client, setClient] = useState(
    () =>
      new PipecatClient({
        transport: new SmallWebRTCTransport({
          mediaManager: new WavMediaManager(),
        }),
        enableMic: false,
        enableCam: false,
      }),
  );
  useEffect(
    () => initializeSession(setClient, setModel, setTimings, setSpeaking, setNotices, setErrors, setPaused, setActivity),
    [],
  );

  return (
    <PipecatClientProvider client={client}>
      <main className="mx-auto flex min-h-dvh max-w-6xl flex-col gap-6 p-4 md:p-8">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-primary p-3 text-primary-foreground">
              <BotIcon aria-hidden="true" />
            </div>
            <div>
              <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
                Pipecat · Simple chatbot
              </p>
              <h1 id="title" className="text-2xl font-semibold tracking-tight">
                Demo Assistant
              </h1>
            </div>
          </div>
          <nav aria-label="Session" className="flex items-center gap-3">
            <a
              id="login"
              hidden
              className="rounded-lg border px-4 py-2 text-sm font-medium focus-visible:outline-2"
            >
              Sign in
            </a>
            <Button id="logout" hidden variant="outline">
              Sign out
            </Button>
          </nav>
        </header>
        <section
          aria-label="Voice session"
          className="rounded-xl border bg-card p-5 shadow-sm"
        >
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <h2 className="font-medium">Talk to your assistant</h2>
              <p
                id="status"
                role="status"
                className="mt-1 max-w-xl text-sm text-muted-foreground"
              >
                Loading configuration…
              </p>
              <p
                id="call-error"
                role="alert"
                hidden
                className="mt-2 max-w-xl text-sm text-destructive"
              />
            </div>
            <div className="flex flex-wrap items-center gap-3 lg:flex-nowrap">
              <label className="flex items-center gap-2 whitespace-nowrap text-sm has-[:disabled]:text-muted-foreground">
                Reasoning
                <select
                  id="reasoning"
                  defaultValue=""
                  disabled
                  className="rounded-md border border-input bg-background px-3 py-2 focus-visible:outline-2 disabled:opacity-50"
                >
                  <option value="">Model default</option>
                  <option value="none">Off</option>
                  <option value="low">Low</option>
                  <option value="high">High</option>
                  <option value="max">Max</option>
                </select>
              </label>
              <label className="flex items-center gap-2 whitespace-nowrap text-sm has-[:disabled]:text-muted-foreground">
                Connection
                <select
                  id="transport"
                  defaultValue="webrtc"
                  disabled
                  className="rounded-md border border-input bg-background px-3 py-2 focus-visible:outline-2 disabled:opacity-50"
                >
                  <option value="webrtc">WebRTC</option>
                  <option value="websocket">HTTPS WebSocket</option>
                </select>
              </label>
              <MicrophoneControl paused={paused} />
              <ConnectButtonView id="connect" disabled size="lg">
                Start conversation
              </ConnectButtonView>
              <ConnectButtonView
                id="disconnect"
                disabled
                size="lg"
                transportState="ready"
              >
                End
              </ConnectButtonView>
              <Button id="pause" disabled variant="outline">
                Pause
              </Button>
              <button id="mute" hidden type="button">
                Mute microphone
              </button>
              <Button id="enable-audio" hidden variant="outline">
                Enable audio
              </Button>
            </div>
          </div>
          <audio id="audio" hidden />
        </section>
        <ConversationPanel model={model} notices={notices} errors={errors} className="min-h-96 flex-1" />
        <LivePipeline activity={activity} paused={paused} />
        <TimingPanel timings={timings} speaking={speaking} />
        <ActivityTimeline activity={activity} />
        <ModelLocations activity={activity} />
        <footer className="text-center text-xs text-muted-foreground">
          Demo Assistant v{version} · Built with Pipecat and Pipecat UI
        </footer>
      </main>
    </PipecatClientProvider>
  );
}

function LivePipeline({ activity, paused }: { activity?: PipelineActivity; paused: boolean }) {
  const { isMicEnabled } = usePipecatClientMicControl();
  return <PipelinePanel activity={activity} micEnabled={isMicEnabled} paused={paused} />;
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing application root");
createRoot(root).render(<App />);
