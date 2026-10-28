import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { PipecatClient } from "@pipecat-ai/client-js";
import {
  PipecatClientProvider,
  usePipecatClientTransportState,
} from "@pipecat-ai/client-react";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { BotIcon, ShieldCheckIcon } from "lucide-react";

import { UserAudioControl } from "@/components/pipecat/user-audio-control";
import { ConnectButtonView } from "@/components/pipecat/connect-button";
import { Button } from "@/components/ui/button";
import { ConversationPanel } from "@/app/components/ConversationPanel";
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
  // No device initialization before an explicit Start click and admission check.
  const [client, setClient] = useState(
    () =>
      new PipecatClient({
        transport: new SmallWebRTCTransport(),
        enableMic: false,
        enableCam: false,
      }),
  );
  useEffect(() => initializeSession(setClient), []);

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
                Voice assistant
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
            </div>
            <div className="flex flex-wrap items-center gap-3">
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
            </div>
          </div>
          <audio
            id="audio"
            controls
            className="mt-4 w-full"
            aria-label="Assistant audio"
          />
        </section>
        <ConversationPanel className="min-h-96 flex-1" />
        <aside
          aria-labelledby="privacy-heading"
          className="rounded-xl border bg-muted/40 p-5"
        >
          <h2
            id="privacy-heading"
            className="flex items-center gap-2 text-sm font-medium"
          >
            <ShieldCheckIcon className="size-4" aria-hidden="true" />
            Privacy notices
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            Requests use your approved AI gateway access. Any personal-data
            processing notices appear here.
          </p>
          <div
            id="notices"
            role="log"
            aria-live="polite"
            className="mt-3 space-y-3 whitespace-pre-wrap text-sm"
          />
        </aside>
        <footer className="text-center text-xs text-muted-foreground">
          Built with Pipecat and Pipecat UI · Camera and text commands are not
          enabled
        </footer>
      </main>
    </PipecatClientProvider>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing application root");
createRoot(root).render(<App />);
