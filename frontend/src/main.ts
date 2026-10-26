import { ConnectionTimeoutError, PipecatClient, TransportStartError } from '@pipecat-ai/client-js';
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';
import './style.css';

function element<T extends HTMLElement>(id: string, type: { new(): T }): T {
  const node = document.getElementById(id);
  if (!(node instanceof type)) throw new Error(`Missing element: ${id}`);
  return node;
}
const status = element('status', HTMLParagraphElement);
const start = element('connect', HTMLButtonElement);
const end = element('disconnect', HTMLButtonElement);
const mute = element('mute', HTMLButtonElement);
const logout = element('logout', HTMLButtonElement);
const audio = element('audio', HTMLAudioElement);
const base = location.pathname.replace(/\/$/, '');
let client: PipecatClient | undefined;
let csrf = '';
let polling: ReturnType<typeof setInterval> | undefined;
let connecting = false;
let generation = 0;

class MediaTimeout extends Error {}

async function mutation(path: string): Promise<Response> {
  return fetch(`${base}${path}`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function iceList(value: unknown): RTCIceServer[] {
  if (!Array.isArray(value)) throw new Error('Invalid ICE configuration');
  return value.map((entry: unknown) => {
    if (!object(entry) || !(typeof entry.urls === 'string' ||
      (Array.isArray(entry.urls) && entry.urls.every((url: unknown) => typeof url === 'string')))) {
      throw new Error('Invalid ICE server');
    }
    if (entry.username !== undefined && typeof entry.username !== 'string') throw new Error('Invalid ICE username');
    if (entry.credential !== undefined && typeof entry.credential !== 'string') throw new Error('Invalid ICE credential');
    return { urls: entry.urls, username: entry.username, credential: entry.credential };
  });
}
async function pollNotices(): Promise<void> {
  try {
    const response = await fetch(`${base}/api/notices`);
    if (!response.ok) { await stop(); status.textContent = 'Session expired. Reload to sign in.'; return; }
    const data: unknown = await response.json();
    if (!object(data) || !Array.isArray(data.notices)) return;
    for (const entry of data.notices) {
      if (!object(entry) || entry.type !== 'pii-notice' || typeof entry.text !== 'string') continue;
      const report = document.createElement('pre');
      report.textContent = entry.text;
      const container = element('notices', HTMLDivElement);
      container.append(report);
      while (container.children.length > 8) container.firstElementChild?.remove();
    }
  } catch { status.textContent = 'Connection interrupted.'; }
}
async function stop(): Promise<void> {
  generation++;
  if (polling) clearInterval(polling);
  polling = undefined;
  const previous = client;
  client = undefined;
  try { await previous?.disconnect(); } finally {
    await mutation('/api/cancel').catch(() => undefined);
    audio.srcObject = null;
    start.disabled = false; end.disabled = true; mute.disabled = true;
    mute.textContent = 'Mute microphone';
    status.textContent = 'Conversation ended.';
  }
}
async function initialize(): Promise<void> {
  const response = await fetch(`${base}/api/config`);
  if (!response.ok) throw new Error('Configuration unavailable');
  const data: unknown = await response.json();
  if (!object(data) || typeof data.title !== 'string' || typeof data.authenticated !== 'boolean') {
    throw new Error('Invalid configuration');
  }
  element('title', HTMLHeadingElement).textContent = data.title;
  document.title = data.title;
  if (!data.authenticated) {
    const link = element('login', HTMLAnchorElement);
    link.href = `${base}/auth/login`; link.hidden = false;
    status.textContent = data.sessionsEnabled === true ? 'Sign in to use this bot.' :
      'Voice sessions are not configured. You can still sign in.'; return;
  }
  if (typeof data.csrf !== 'string') throw new Error('Invalid session');
  csrf = data.csrf;
  const servers = iceList(data.iceServers);
  logout.hidden = false;
  if (data.sessionsEnabled !== true) {
    status.textContent = typeof data.unavailableReason === 'string' ? data.unavailableReason : 'Voice sessions are unavailable.';
    return;
  }
  start.disabled = false;
  status.textContent = data.anonymous ? 'Anonymous bot. Start when ready.' : 'Signed in. Start when ready.';
  start.onclick = async () => {
    start.disabled = true; end.disabled = false; status.textContent = 'Connecting…';
    connecting = true;
    const attempt = ++generation;
    let mediaConnected = false;
    let serviceError = false;
    const callbacks = {
      onConnected: () => { mediaConnected = true; },
      onTrackStarted: (track: MediaStreamTrack, participant?: { local?: boolean }) => {
        if (!participant?.local && track.kind === 'audio') {
          audio.srcObject = new MediaStream([track]);
          void audio.play().catch(() => { status.textContent = 'Press play to hear the assistant.'; });
        }
      },
      onDisconnected: () => { if (!connecting) void stop(); },
      onError: () => { serviceError = true; status.textContent = 'Call failed. End the call and retry.'; },
    };
    try {
      // Permission requests are deliberately outside the networking timeout/fallback path.
      const permission = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      permission.getTracks().forEach(track => track.stop());
      if (attempt !== generation) return;
      client = new PipecatClient({
        transport: new SmallWebRTCTransport({ iceServers: servers }),
        enableMic: true, enableCam: false,
        callbacks,
      });
      let timeout: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          client.connect({ webrtcRequestParams: {
            endpoint: `${base}/api/offer`, headers: { 'X-CSRF-Token': csrf },
          } }),
          new Promise<never>((_, reject) => {
            timeout = setTimeout(() => reject(new MediaTimeout()), 15000);
          }),
        ]);
      } catch (error: unknown) {
        const refused = object(error) && typeof error.status === 'number';
        const networkFailure = error instanceof MediaTimeout || error instanceof ConnectionTimeoutError ||
          (error instanceof TransportStartError && !refused);
        if (attempt !== generation || mediaConnected || serviceError || !networkFailure) throw error;
        const previous = client; client = undefined;
        await previous.disconnect();
        const cancelled = await mutation('/api/cancel');
        if (!cancelled.ok) throw new Error('Session unavailable');
        const ticketResponse = await mutation('/api/websocket-ticket');
        if (!ticketResponse.ok) throw new Error('Fallback admission denied');
        const data: unknown = await ticketResponse.json();
        if (!object(data) || typeof data.ticket !== 'string') throw new Error('Invalid ticket');
        status.textContent = 'WebRTC unavailable. Connecting using HTTPS WebSocket audio…';
        const { WebSocketTransport, ProtobufFrameSerializer } = await import('@pipecat-ai/websocket-transport');
        client = new PipecatClient({
          transport: new WebSocketTransport({ serializer: new ProtobufFrameSerializer(),
            recorderSampleRate: 16000, playerSampleRate: 24000 }),
          enableMic: true, enableCam: false, callbacks,
        });
        const url = new URL(`${base}/api/websocket`, location.origin);
        url.protocol = 'wss:'; url.searchParams.set('ticket', data.ticket);
        if (timeout) clearTimeout(timeout);
        await Promise.race([client.connect({ wsUrl: url.toString() }), new Promise<never>((_, reject) => {
          timeout = setTimeout(() => reject(new MediaTimeout()), 15000);
        })]);
      } finally { if (timeout) clearTimeout(timeout); }
      if (attempt !== generation) { await client?.disconnect(); return; }
      end.disabled = false; mute.disabled = false;
      status.textContent = 'Connected. You can speak now.';
      polling = setInterval(() => { void pollNotices(); }, 1500);
    } catch { await stop(); status.textContent = 'Could not connect. Check microphone permission or sign in again.'; }
    finally { connecting = false; }
  };
}
end.onclick = () => { void stop(); };
mute.onclick = () => {
  if (!client) return;
  const enabled = !client.isMicEnabled;
  client.enableMic(enabled);
  mute.textContent = enabled ? 'Mute microphone' : 'Unmute microphone';
};
logout.onclick = async () => {
  await stop();
  await fetch(`${base}/auth/logout`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
  location.reload();
};
void initialize().catch(() => { status.textContent = 'Unable to load this bot. Reload to retry.'; });
