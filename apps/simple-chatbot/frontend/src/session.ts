import {
  ConnectionTimeoutError,
  PipecatClient,
  TransportStartError,
} from "@pipecat-ai/client-js";
import {
  SmallWebRTCTransport,
  WavMediaManager,
} from "@pipecat-ai/small-webrtc-transport";
import { parseTiming, type TurnTiming } from "./timings";
import { endActivity, parseActivity, type PipelineActivity } from "./activity";

export interface ChatError {
  text: string;
  createdAt: string;
}

// The pinned WebSocket transport sends audio without a track ID. Its player
// permanently ignores a track once interrupted, and its retiring worklet can
// discard audio sent before it stops. Give each playback a new ID and hold
// incoming chunks until the old worklet has been detached.
function websocketAudioManager(isPaused: () => boolean) {
  const manager = new WavMediaManager(512, 16000);
  const buffer = manager.bufferBotAudio.bind(manager);
  const interrupt = manager.interruptBotAudio.bind(manager);
  // The pinned player's offset poll has no timeout. Use its worklet protocol
  // directly so a missing acknowledgement fails the call, not the next reply.
  const player = Reflect.get(manager, "_wavStreamPlayer") as {
    stream: AudioWorkletNode | null;
    context: AudioContext;
    analyser: AnalyserNode;
    interruptedTrackIds: Record<string, boolean>;
    trackSampleOffsets: Record<string, unknown>;
    getTrackSampleOffset: (interrupt?: boolean) => Promise<unknown>;
  };
  const getOffset = player.getTrackSampleOffset.bind(player);
  player.getTrackSampleOffset = (stop = false) => {
    if (!stop || !player.stream) return getOffset(stop);
    const stream = player.stream;
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const finish = () => {
        clearTimeout(timer);
        stream.port.removeEventListener("message", onMessage);
        delete player.trackSampleOffsets[requestId];
      };
      const onMessage = (event: MessageEvent) => {
        const data: unknown = event.data;
        if (!object(data) || data.event !== "offset" || data.requestId !== requestId) return;
        finish();
        if (typeof data.trackId === "string") player.interruptedTrackIds[data.trackId] = true;
        resolve(data);
      };
      const timer = setTimeout(() => {
        finish();
        reject(new Error("Bot audio interruption timed out"));
      }, 1500);
      stream.port.addEventListener("message", onMessage);
      stream.port.postMessage({ event: "interrupt", requestId });
    });
  };
  let gain: GainNode | undefined;
  let routed: AudioWorkletNode | null = null;
  function routeAudio(): void {
    const stream = player.stream;
    if (!stream) return;
    if (!gain) {
      gain = player.context.createGain();
      gain.connect(player.context.destination);
    }
    if (routed !== stream) {
      stream.disconnect();
      stream.connect(gain);
      stream.connect(player.analyser);
      routed = stream;
    }
    gain.gain.value = isPaused() ? 0 : 1;
  }
  let playback = 0;
  let interrupting: Promise<void> | undefined;
  let pending: Int16Array[] = [];

  manager.bufferBotAudio = (data: Int16Array, id?: string) => {
    if (isPaused()) return;
    if (interrupting) {
      if (pending.length >= 128) throw new Error("Bot audio queue full");
      pending.push(data);
      return;
    }
    const result = buffer(data, id ?? `reply-${playback}`);
    routeAudio();
    return result;
  };
  manager.interruptBotAudio = () => {
    if (isPaused()) {
      // Mute synchronously, keeping the worklet alive for its interrupt reply.
      pending = [];
      if (gain) gain.gain.value = 0;
    }
    if (interrupting) return interrupting;
    playback++;
    interrupting = (async () => {
      try {
        await interrupt();
      } finally {
        // The pinned player does not expose a reset method. Its worklet sends
        // "stop" asynchronously; detach it before feeding the next reply.
        player.stream?.disconnect();
        player.stream = null;
        routed = null;
        const queued = pending;
        pending = [];
        interrupting = undefined;
        if (!isPaused()) for (const chunk of queued) manager.bufferBotAudio(chunk);
      }
    })();
    return interrupting;
  };
  return manager;
}

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
  onModel: (model: string) => void,
  onTimings: (timings: TurnTiming[]) => void,
  onSpeaking: (speaking: boolean) => void,
  onNotices: (notices: string[]) => void,
  onErrors: (errors: ChatError[]) => void,
  onPaused: (paused: boolean) => void,
  onActivity: (activity: PipelineActivity | undefined) => void,
): () => void {
  const status = element("status", HTMLParagraphElement);
  const callError = element("call-error", HTMLParagraphElement);
  const start = element("connect", HTMLButtonElement);
  const end = element("disconnect", HTMLButtonElement);
  const mute = element("mute", HTMLButtonElement);
  const pause = element("pause", HTMLButtonElement);
  const logout = element("logout", HTMLButtonElement);
  const audio = element("audio", HTMLAudioElement);
  const enableAudio = element("enable-audio", HTMLButtonElement);
  const reasoning = element("reasoning", HTMLSelectElement);
  const transportChoice = element("transport", HTMLSelectElement);
  const base = location.pathname.replace(/\/$/, "");
  let csrf = "";
  let active: Attempt | undefined;
  let cleanup: Promise<void> = Promise.resolve();
  let sessionsEnabled = false;
  let startConversation: (() => void) | undefined;

  interface Attempt {
    client?: PipecatClient;
    cancelled: boolean;
    connecting: boolean;
    mediaConnected: boolean;
    serviceError: boolean;
    recoverableError?: boolean;
    transport: number;
    polling?: ReturnType<typeof setInterval>;
    serverCancel?: Promise<Response>;
    stopped?: Promise<void>;
    timings: TurnTiming[];
    notices: string[];
    errors: ChatError[];
    activity?: PipelineActivity;
    pollingNotices?: boolean;
    paused: boolean;
    controlPending?: {
      sequence: number;
      paused: boolean;
      timer: ReturnType<typeof setTimeout>;
    };
    controlSequence: number;
    websocketAudio?: ReturnType<typeof websocketAudioManager>;
    micWasEnabled: boolean;
  }
  class Cancelled extends Error {}
  class MediaTimeout extends Error {}
  function owns(attempt: Attempt): boolean {
    return active === attempt && !attempt.cancelled;
  }
  function check(attempt: Attempt): void {
    if (!owns(attempt)) throw new Cancelled();
  }
  async function playAudio(attempt: Attempt): Promise<void> {
    if (attempt.paused) return;
    const transport = attempt.transport;
    try {
      await audio.play();
      if (owns(attempt) && !attempt.paused && attempt.transport === transport)
        enableAudio.hidden = true;
    } catch {
      if (owns(attempt) && !attempt.paused && attempt.transport === transport)
        enableAudio.hidden = false;
    }
  }
  function showError(attempt: Attempt, message: string): void {
    attempt.serviceError = true;
    // Keep the cause when closing the transport reports a second error.
    if (!callError.hidden) return;
    callError.textContent = message.slice(0, 512);
    callError.hidden = false;
  }
  function mutation(path: string, body?: object): Promise<Response> {
    return fetch(`${base}${path}`, {
      method: "POST",
      headers: {
        "X-CSRF-Token": csrf,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
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
    if (attempt.controlPending) clearTimeout(attempt.controlPending.timer);
    attempt.controlPending = undefined;
    if (attempt.polling) clearInterval(attempt.polling);
    if (active === attempt) {
      onSpeaking(false);
      onActivity(endActivity(attempt.activity));
      attempt.timings = attempt.timings.map((timing) =>
        ["complete", "no-audio", "failed", "interrupted"].includes(timing.status)
          ? timing
          : { ...timing, status: "interrupted" },
      );
      onTimings(attempt.timings);
      active = undefined;
      audio.srcObject = null;
      enableAudio.hidden = true;
      start.disabled = true;
      end.disabled = true;
      mute.disabled = true;
      pause.disabled = true;
      pause.textContent = "Pause";
      onPaused(false);
      audio.pause();
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
        reasoning.disabled = !sessionsEnabled;
        transportChoice.disabled = !sessionsEnabled;
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
  function callbacks(attempt: Attempt, webrtc?: SmallWebRTCTransport) {
    const transport = ++attempt.transport;
    const current = () => owns(attempt) && attempt.transport === transport;
    return {
      onServerMessage: (data: unknown) => {
        if (!current()) return;
        if (object(data) && data.type === "conversation-control") {
          const pending = attempt.controlPending;
          if (
            pending &&
            data.sequence === pending.sequence &&
            data.paused === pending.paused
          ) {
            clearTimeout(pending.timer);
            attempt.controlPending = undefined;
            if (!pending.paused) {
              attempt.paused = false;
              attempt.client?.enableMic(attempt.micWasEnabled);
              mute.disabled = false;
              onPaused(false);
              void playAudio(attempt);
            }
            pause.textContent = pending.paused ? "Resume" : "Pause";
            pause.disabled = false;
            status.textContent = pending.paused
              ? "Conversation paused."
              : "Connected. You can speak now.";
          }
          return;
        }
        const activity = parseActivity(data);
        if (activity) {
          if (!attempt.activity || activity.at_ms >= attempt.activity.at_ms) {
            attempt.activity = activity;
            onActivity(activity);
          }
          return;
        }
        const timing = parseTiming(data);
        if (!timing) return;
        attempt.timings = [
          ...attempt.timings.filter((entry) => entry.turn !== timing.turn),
          timing,
        ].sort((a, b) => b.turn - a.turn).slice(0, 8);
        onTimings(attempt.timings);
      },
      onBotStartedSpeaking: () => {
        if (current() && !attempt.paused) {
          onSpeaking(true);
          if (attempt.recoverableError) {
            attempt.recoverableError = false;
            callError.hidden = true;
            status.textContent = "Connected.";
          }
        }
      },
      onBotStoppedSpeaking: () => {
        if (current()) onSpeaking(false);
      },
      onConnected: () => {
        if (current()) attempt.mediaConnected = true;
      },
      onTrackStarted: (
        track: MediaStreamTrack,
        participant?: { local?: boolean },
      ) => {
        if (current() && participant?.local && track.kind === "audio" && webrtc) {
          if (attempt.paused) track.enabled = false;
          // The pinned transport only wires track replacement for its default
          // Daily manager. Its peer connection is omitted from the SDK typings.
          const { pc } = webrtc as SmallWebRTCTransport & {
            pc?: RTCPeerConnection | null;
          };
          const sender = pc
            ?.getSenders()
            .find((item) => item.track?.kind === "audio");
          if (sender && sender.track !== track) {
            void sender.replaceTrack(track).catch(() => {
              if (current()) {
                showError(
                  attempt,
                  "Microphone change failed. End the call and retry.",
                );
              }
            });
          }
        }
        if (current() && !participant?.local && track.kind === "audio") {
          audio.srcObject = new MediaStream([track]);
          if (attempt.paused) audio.pause();
          else void playAudio(attempt);
        }
      },
      onDisconnected: () => {
        if (current() && !attempt.connecting) void stopCurrent();
      },
      onError: (message: unknown) => {
        if (current()) {
          const data =
            object(message) && object(message.data) ? message.data : undefined;
          const error = typeof data?.error === "string" ? data.error : "";
          if (/^Model (response was invalid or incomplete\. Please retry\.|connection failed or timed out\. Please retry\.|request rejected \(HTTP \d{3}\)\.)$/.test(error)) {
            attempt.errors = [...attempt.errors, { text: error, createdAt: new Date().toISOString() }].slice(-8);
            onErrors(attempt.errors);
          }
          if (attempt.connecting && !attempt.mediaConnected && error.startsWith("websocket "))
            return;
          if (data?.fatal === false && !attempt.serviceError) {
            attempt.recoverableError = true;
            callError.textContent = typeof data.error === "string"
              ? data.error.slice(0, 512) : "A response failed.";
            callError.hidden = false;
            status.textContent = "Response error. You can keep talking while connected.";
            return;
          }
          showError(
            attempt,
            typeof data?.error === "string"
              ? data.error
              : "Call failed. End the call and retry.",
          );
          status.textContent = "Call failed. End the call and retry.";
        }
      },
    };
  }
  function guardMic(attempt: Attempt): void {
    const client = attempt.client;
    if (!client) return;
    const enableMic = client.enableMic.bind(client);
    client.enableMic = (enabled: boolean) => {
      if (enabled && attempt.paused) return;
      enableMic(enabled);
    };
  }
  async function pollNotices(attempt: Attempt): Promise<void> {
    if (attempt.pollingNotices) return;
    attempt.pollingNotices = true;
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
        attempt.notices = [...attempt.notices, entry.text.slice(0, 32768)].slice(-8);
      }
      onNotices(attempt.notices);
    } catch {
      if (owns(attempt)) status.textContent = "Connection interrupted.";
    } finally {
      attempt.pollingNotices = false;
    }
  }
  async function connectWebRTC(
    attempt: Attempt,
    servers: RTCIceServer[],
    options: object,
  ): Promise<void> {
    const webrtc = new SmallWebRTCTransport({
      iceServers: servers,
      mediaManager: new WavMediaManager(),
    });
    attempt.client = new PipecatClient({
      transport: webrtc,
      enableMic: true,
      enableCam: false,
      callbacks: callbacks(attempt, webrtc),
    });
    guardMic(attempt);
    onClient(attempt.client);
    await deadline(
      attempt,
      attempt.client.connect({
        webrtcRequestParams: {
          endpoint: `${base}/api/offer`,
          headers: { "X-CSRF-Token": csrf },
          requestData: options,
        },
      }),
    );
  }
  async function connectWebSocket(attempt: Attempt, options: object): Promise<void> {
    const ticketResponse = await mutation("/api/websocket-ticket", options);
    check(attempt);
    if (!ticketResponse.ok) throw new Error("WebSocket admission denied");
    const ticket: unknown = await ticketResponse.json();
    check(attempt);
    if (!object(ticket) || typeof ticket.ticket !== "string")
      throw new Error("Invalid ticket");
    const { WebSocketTransport, ProtobufFrameSerializer } = await import(
      "@pipecat-ai/websocket-transport"
    );
    check(attempt);
    attempt.client = new PipecatClient({
      transport: new WebSocketTransport({
        // Match the server's 16 kHz input and the WAV player's 24 kHz output.
        mediaManager: (attempt.websocketAudio = websocketAudioManager(() => attempt.paused)),
        serializer: new ProtobufFrameSerializer(),
        recorderSampleRate: 16000,
        playerSampleRate: 24000,
      }),
      enableMic: true,
      enableCam: false,
      callbacks: callbacks(attempt),
    });
    guardMic(attempt);
    onClient(attempt.client);
    const url = new URL(`${base}/api/websocket`, location.origin);
    url.protocol = "wss:";
    url.searchParams.set("ticket", ticket.ticket);
    await deadline(attempt, attempt.client.connect({ wsUrl: url.toString() }));
  }
  async function connect(servers: RTCIceServer[]): Promise<void> {
    if (active || !sessionsEnabled) return;
    const options = reasoning.value ? { reasoning_effort: reasoning.value } : {};
    const preferred = transportChoice.value === "websocket" ? "websocket" : "webrtc";
    let selected = preferred;
    reasoning.disabled = true;
    transportChoice.disabled = true;
    const attempt: Attempt = {
      cancelled: false,
      connecting: true,
      mediaConnected: false,
      serviceError: false,
      transport: 0,
      timings: [],
      notices: [],
      errors: [],
      paused: false,
      controlSequence: 0,
      micWasEnabled: true,
    };
    active = attempt;
    onPaused(false);
    enableAudio.hidden = true;
    onTimings([]);
    onActivity(undefined);
    onNotices([]);
    onErrors([]);
    onSpeaking(false);
    callError.textContent = "";
    callError.hidden = true;
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
      try {
        if (selected === "webrtc") await connectWebRTC(attempt, servers, options);
        else await connectWebSocket(attempt, options);
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
        attempt.activity = undefined;
        onActivity(undefined);
        check(attempt);
        attempt.serverCancel = mutation("/api/cancel");
        const cancelled = await attempt.serverCancel;
        check(attempt);
        if (!cancelled.ok) throw new Error("Cleanup denied");
        selected = preferred === "webrtc" ? "websocket" : "webrtc";
        transportChoice.value = selected;
        status.textContent = selected === "webrtc"
          ? "HTTPS WebSocket unavailable. Trying WebRTC…"
          : "WebRTC unavailable. Trying HTTPS WebSocket…";
        check(attempt);
        if (selected === "webrtc") await connectWebRTC(attempt, servers, options);
        else await connectWebSocket(attempt, options);
      }
      check(attempt);
      if (attempt.serviceError) throw new Error("Call failed during connection");
      attempt.connecting = false;
      mute.disabled = false;
      pause.disabled = false;
      status.textContent = `Connected using ${selected === "webrtc" ? "WebRTC" : "HTTPS WebSocket"}. You can speak now.`;
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
        status.textContent = attempt.serviceError
          ? "Call failed. Start a new conversation to retry."
          : "Could not connect. Check microphone permission or sign in again.";
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
    if (typeof data.model !== "string" || !data.model.trim())
      throw new Error("Invalid model configuration");
    onModel(data.model);
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
    reasoning.disabled = false;
    transportChoice.disabled = false;
    status.textContent = data.anonymous
      ? "Public bot using its scoped bot credential. Start when ready."
      : "Signed in. Start when ready.";
    startConversation = () => {
      void connect(servers);
    };
    start.addEventListener("click", startConversation);
  }
  const endConversation = () => {
    void stopCurrent();
  };
  const enablePlayback = () => {
    if (active) void playAudio(active);
  };
  const toggleMute = () => {
    const client = active?.client;
    if (!client || active?.paused) return;
    const enabled = !client.isMicEnabled;
    client.enableMic(enabled);
    mute.textContent = enabled ? "Mute microphone" : "Unmute microphone";
  };
  const togglePause = () => {
    const attempt = active;
    const client = attempt?.client;
    if (!attempt || !client || attempt.connecting || attempt.controlPending) return;
    const paused = !attempt.paused;
    pause.disabled = true;
    if (paused) {
      attempt.paused = true;
      attempt.micWasEnabled = client.isMicEnabled;
      client.enableMic(false);
      mute.disabled = true;
      onPaused(true);
      audio.pause();
      enableAudio.hidden = true;
      onSpeaking(false);
      void attempt.websocketAudio?.interruptBotAudio().catch(() => {
        if (owns(attempt)) void stopCurrent();
      });
      status.textContent = "Pausing conversation…";
    } else {
      status.textContent = "Resuming conversation…";
    }
    const sequence = ++attempt.controlSequence;
    const timer = setTimeout(() => {
      if (owns(attempt) && attempt.controlPending?.sequence === sequence) {
        showError(
          attempt,
          "Pause or resume was not confirmed. End the call and retry.",
        );
        void stopCurrent();
      }
    }, 5000);
    attempt.controlPending = { sequence, paused, timer };
    try {
      client.sendClientMessage("conversation-control", {
        action: paused ? "pause" : "resume",
        sequence,
      });
    } catch {
      clearTimeout(timer);
      attempt.controlPending = undefined;
      showError(attempt, "Could not change conversation state. End the call and retry.");
      void stopCurrent();
    }
  };
  const signOut = async () => {
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
  // React rerenders must not replace these session-owned button handlers.
  end.addEventListener("click", endConversation);
  enableAudio.addEventListener("click", enablePlayback);
  mute.addEventListener("click", toggleMute);
  pause.addEventListener("click", togglePause);
  logout.addEventListener("click", signOut);
  void initialize().catch(() => {
    status.textContent = "Unable to load this bot. Reload to retry.";
  });
  return () => {
    if (startConversation) start.removeEventListener("click", startConversation);
    end.removeEventListener("click", endConversation);
    enableAudio.removeEventListener("click", enablePlayback);
    mute.removeEventListener("click", toggleMute);
    pause.removeEventListener("click", togglePause);
    logout.removeEventListener("click", signOut);
    void stopCurrent();
  };
}
