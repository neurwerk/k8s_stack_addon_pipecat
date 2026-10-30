import {
  ConnectionTimeoutError,
  PipecatClient,
  TransportStartError,
} from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

function element<T extends HTMLElement>(id: string, type: { new (): T }): T {
  const node = document.getElementById(id);
  if (!(node instanceof type)) throw new Error(`Missing element: ${id}`);
  return node;
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function iceList(value: unknown): RTCIceServer[] {
  if (!Array.isArray(value)) throw new Error("Invalid ICE configuration");
  return value.map((entry: unknown) => {
    if (
      !object(entry) ||
      !(
        typeof entry.urls === "string" ||
        (Array.isArray(entry.urls) &&
          entry.urls.every((url: unknown) => typeof url === "string"))
      )
    ) {
      throw new Error("Invalid ICE server");
    }
    if (entry.username !== undefined && typeof entry.username !== "string")
      throw new Error("Invalid ICE username");
    if (entry.credential !== undefined && typeof entry.credential !== "string")
      throw new Error("Invalid ICE credential");
    return {
      urls: entry.urls,
      username: entry.username,
      credential: entry.credential,
    };
  });
}

export function initializeSession(
  onClient: (client: PipecatClient) => void,
): () => void {
  const status = element("status", HTMLParagraphElement);
  const start = element("connect", HTMLButtonElement);
  const end = element("disconnect", HTMLButtonElement);
  const mute = element("mute", HTMLButtonElement);
  const logout = element("logout", HTMLButtonElement);
  const audio = element("audio", HTMLAudioElement);
  const base = location.pathname.replace(/\/$/, "");
  let csrf = "";
  let active: Attempt | undefined;
  let cleanup: Promise<void> = Promise.resolve();
  let sessionsEnabled = false;

  interface Attempt {
    client?: PipecatClient;
    cancelled: boolean;
    connecting: boolean;
    mediaConnected: boolean;
    serviceError: boolean;
    transport: number;
    polling?: ReturnType<typeof setInterval>;
    serverCancel?: Promise<Response>;
    stopped?: Promise<void>;
  }
  class Cancelled extends Error {}
  class MediaTimeout extends Error {}
  function owns(attempt: Attempt): boolean {
    return active === attempt && !attempt.cancelled;
  }
  function check(attempt: Attempt): void {
    if (!owns(attempt)) throw new Cancelled();
  }
  function mutation(path: string): Promise<Response> {
    return fetch(`${base}${path}`, {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
    });
  }
  async function disconnectOwn(attempt: Attempt): Promise<void> {
    const previous = attempt.client;
    attempt.client = undefined;
    attempt.transport++; // Retire callbacks before awaiting the old SDK's disconnect.
    await previous?.disconnect();
  }
  function stop(attempt: Attempt): Promise<void> {
    if (attempt.stopped) return attempt.stopped;
    attempt.cancelled = true;
    if (attempt.polling) clearInterval(attempt.polling);
    if (active === attempt) {
      active = undefined;
      audio.srcObject = null;
      start.disabled = true;
      end.disabled = true;
      mute.disabled = true;
      status.textContent = "Ending conversation…";
    }
    // A new Start waits for this fence: stale server cancellation cannot end its call.
    const before = cleanup;
    attempt.stopped = (async () => {
      await before;
      await disconnectOwn(attempt);
      // Do not abort a cancellation already sent during fallback; wait for its acknowledgement.
      if (attempt.serverCancel) await attempt.serverCancel;
      const response = await mutation("/api/cancel");
      if (!response.ok) throw new Error("Could not confirm server cleanup");
      if (!active) {
        start.disabled = !sessionsEnabled;
        mute.textContent = "Mute microphone";
        status.textContent = "Conversation ended.";
      }
    })();
    cleanup = attempt.stopped;
    return attempt.stopped;
  }
  async function stopCurrent(): Promise<void> {
    const attempt = active;
    if (!attempt) return;
    try {
      await stop(attempt);
    } catch {
      if (!active)
        status.textContent =
          "Unable to confirm call cleanup. Reload before retrying.";
    }
  }
  async function deadline<T>(
    attempt: Attempt,
    operation: Promise<T>,
  ): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        operation,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new MediaTimeout()), 15000);
        }),
      ]);
      check(attempt);
      return result;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  function callbacks(attempt: Attempt) {
    const transport = ++attempt.transport;
    const current = () => owns(attempt) && attempt.transport === transport;
    return {
      onConnected: () => {
        if (current()) attempt.mediaConnected = true;
      },
      onTrackStarted: (
        track: MediaStreamTrack,
        participant?: { local?: boolean },
      ) => {
        if (current() && !participant?.local && track.kind === "audio") {
          audio.srcObject = new MediaStream([track]);
          void audio.play().catch(() => {
            if (current())
              status.textContent = "Press play to hear the assistant.";
          });
        }
      },
      onDisconnected: () => {
        if (current() && !attempt.connecting) void stopCurrent();
      },
      onError: () => {
        if (current()) {
          attempt.serviceError = true;
          status.textContent = "Call failed. End the call and retry.";
        }
      },
    };
  }
  async function pollNotices(attempt: Attempt): Promise<void> {
    try {
      const response = await fetch(`${base}/api/notices`);
      if (!owns(attempt)) return;
      if (!response.ok) {
        await stop(attempt);
        return;
      }
      const data: unknown = await response.json();
      if (!owns(attempt) || !object(data) || !Array.isArray(data.notices))
        return;
      for (const entry of data.notices) {
        if (
          !object(entry) ||
          entry.type !== "pii-notice" ||
          typeof entry.text !== "string"
        )
          continue;
        const report = document.createElement("pre");
        report.textContent = entry.text;
        const container = element("notices", HTMLDivElement);
        container.append(report);
        while (container.children.length > 8)
          container.firstElementChild?.remove();
      }
    } catch {
      if (owns(attempt)) status.textContent = "Connection interrupted.";
    }
  }
  async function connect(servers: RTCIceServer[]): Promise<void> {
    if (active || !sessionsEnabled) return;
    const attempt: Attempt = {
      cancelled: false,
      connecting: true,
      mediaConnected: false,
      serviceError: false,
      transport: 0,
    };
    active = attempt;
    start.disabled = true;
    end.disabled = false;
    status.textContent = "Connecting…";
    try {
      await cleanup;
      check(attempt);
      // Permission is outside the networking timeout and can never trigger fallback.
      const permission = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: false,
      });
      permission.getTracks().forEach((track) => track.stop());
      check(attempt);
      attempt.client = new PipecatClient({
        transport: new SmallWebRTCTransport({ iceServers: servers }),
        enableMic: true,
        enableCam: false,
        callbacks: callbacks(attempt),
      });
      onClient(attempt.client);
      try {
        await deadline(
          attempt,
          attempt.client.connect({
            webrtcRequestParams: {
              endpoint: `${base}/api/offer`,
              headers: { "X-CSRF-Token": csrf },
            },
          }),
        );
      } catch (error: unknown) {
        check(attempt);
        const refused = object(error) && typeof error.status === "number";
        const network =
          error instanceof MediaTimeout ||
          error instanceof ConnectionTimeoutError ||
          (error instanceof TransportStartError && !refused);
        if (attempt.mediaConnected || attempt.serviceError || !network)
          throw error;
        await disconnectOwn(attempt);
        check(attempt);
        attempt.serverCancel = mutation("/api/cancel");
        const cancelled = await attempt.serverCancel;
        check(attempt);
        if (!cancelled.ok) throw new Error("Cleanup denied");
        const ticketResponse = await mutation("/api/websocket-ticket");
        check(attempt);
        if (!ticketResponse.ok) throw new Error("Fallback admission denied");
        const ticket: unknown = await ticketResponse.json();
        check(attempt);
        if (!object(ticket) || typeof ticket.ticket !== "string")
          throw new Error("Invalid ticket");
        const { WebSocketTransport, ProtobufFrameSerializer } = await import(
          "@pipecat-ai/websocket-transport"
        );
        check(attempt);
        status.textContent =
          "WebRTC unavailable. Connecting using HTTPS WebSocket audio…";
        attempt.client = new PipecatClient({
          transport: new WebSocketTransport({
            serializer: new ProtobufFrameSerializer(),
            recorderSampleRate: 16000,
            playerSampleRate: 24000,
          }),
          enableMic: true,
          enableCam: false,
          callbacks: callbacks(attempt),
        });
        onClient(attempt.client);
        const url = new URL(`${base}/api/websocket`, location.origin);
        url.protocol = "wss:";
        url.searchParams.set("ticket", ticket.ticket);
        check(attempt);
        await deadline(
          attempt,
          attempt.client.connect({ wsUrl: url.toString() }),
        );
      }
      check(attempt);
      attempt.connecting = false;
      mute.disabled = false;
      status.textContent = "Connected. You can speak now.";
      attempt.polling = setInterval(() => {
        void pollNotices(attempt);
      }, 1500);
    } catch (error: unknown) {
      if (!owns(attempt)) {
        await attempt.stopped?.catch(() => undefined);
        return;
      }
      try {
        await stop(attempt);
      } catch {
        /* Keep Start disabled until cleanup is acknowledged. */
      }
      if (!active && !(error instanceof Cancelled))
        status.textContent =
          "Could not connect. Check microphone permission or sign in again.";
    }
  }
  async function initialize(): Promise<void> {
    const response = await fetch(`${base}/api/config`);
    if (!response.ok) throw new Error("Configuration unavailable");
    const data: unknown = await response.json();
    if (
      !object(data) ||
      typeof data.title !== "string" ||
      typeof data.authenticated !== "boolean"
    )
      throw new Error("Invalid configuration");
    element("title", HTMLHeadingElement).textContent = data.title;
    document.title = data.title;
    if (data.loginAvailable === true && data.signedIn !== true) {
      const link = element("login", HTMLAnchorElement);
      link.href = `${base}/auth/login`;
      link.hidden = false;
    }
    if (!data.authenticated) {
      const link = element("login", HTMLAnchorElement);
      link.href = `${base}/auth/login`;
      link.hidden = false;
      status.textContent =
        data.sessionsEnabled === true
          ? "Sign in to use this bot."
          : "Voice sessions are not configured. You can still sign in.";
      return;
    }
    if (typeof data.csrf !== "string") throw new Error("Invalid session");
    csrf = data.csrf;
    logout.hidden = false;
    sessionsEnabled = data.sessionsEnabled === true;
    logout.textContent = data.signedIn === true ? "Sign out" : "Reset session";
    if (!sessionsEnabled) {
      status.textContent =
        typeof data.unavailableReason === "string"
          ? data.unavailableReason
          : "Voice sessions are unavailable.";
      return;
    }
    const servers = iceList(data.iceServers);
    start.disabled = false;
    status.textContent = data.anonymous
      ? "Public bot using its scoped bot credential. Start when ready."
      : "Signed in. Start when ready.";
    start.onclick = () => {
      void connect(servers);
    };
  }
  end.onclick = () => {
    void stopCurrent();
  };
  mute.onclick = () => {
    const client = active?.client;
    if (!client) return;
    const enabled = !client.isMicEnabled;
    client.enableMic(enabled);
    mute.textContent = enabled ? "Mute microphone" : "Unmute microphone";
  };
  logout.onclick = async () => {
    logout.disabled = true;
    try {
      await stopCurrent();
      const response = await mutation("/auth/logout");
      if (!response.ok) throw new Error("Sign-out failed");
      location.replace(`${base}/auth/signed-out`);
    } catch {
      logout.disabled = false;
      status.textContent = "Could not sign out. Please try again.";
    }
  };
  void initialize().catch(() => {
    status.textContent = "Unable to load this bot. Reload to retry.";
  });
  return () => {
    void stopCurrent();
  };
}
