import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { PipecatClient } from "@pipecat-ai/client-js";
import {
  PipecatClientProvider,
  usePipecatClientTransportState,
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
import type { TurnTiming } from "./timings";
import { initializeSession } from "./session";
import "./app/globals.css";
import "./style.css";

function MicrophoneControl() {
  const state = usePipecatClientTransportState();
  const connected = state === "connected" || state === "ready";
  return (
    <UserAudioControl
      size="lg"
      unavailableText={connected ? undefined : "Microphone off"}
    />
  );
}

function App() {
  const [model, setModel] = useState("");
  const [timings, setTimings] = useState<TurnTiming[]>([]);
  const [speaking, setSpeaking] = useState(false);
  const [notices, setNotices] = useState<string[]>([]);
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
  useEffect(() => initializeSession(setClient, setModel, setTimings, setSpeaking, setNotices), []);

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
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex flex-col gap-1 text-sm">
                Reasoning
                <select
                  id="reasoning"
                  defaultValue=""
                  disabled
                  aria-describedby="reasoning-help"
                  className="rounded-md border border-input bg-background px-3 py-2 focus-visible:outline-2 disabled:opacity-50"
                >
                  <option value="">Model default</option>
                  <option value="none">Off</option>
                  <option value="low">Low</option>
                  <option value="high">High</option>
                  <option value="max">Max</option>
                </select>
              </label>
              <MicrophoneControl />
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
              <button id="mute" hidden type="button">
                Mute microphone
              </button>
              <Button id="enable-audio" hidden variant="outline">
                Enable audio
              </Button>
            </div>
          </div>
          <p id="reasoning-help" className="mt-3 text-sm text-muted-foreground">
            Applies to a new conversation. Off usually gives faster replies.
          </p>
          <audio id="audio" hidden />
        </section>
        <ConversationPanel model={model} notices={notices} className="min-h-96 flex-1" />
        <TimingPanel timings={timings} speaking={speaking} />
        <footer className="text-center text-xs text-muted-foreground">
          Built with Pipecat and Pipecat UI
        </footer>
      </main>
    </PipecatClientProvider>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing application root");
createRoot(root).render(<App />);
