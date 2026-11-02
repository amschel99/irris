// "Record what you see": the camera with everything Irris shows drawn in (the reticle, the step, the verdict and
// reading, alerts, the last captions), plus the mic and Irris's own voice mixed with Web Audio.
// "Camera only": the raw camera and the mic. "Whole screen" (desktop): the screen, mic and tab audio.

import { speaker } from "./voice";

export type RecMode = "view" | "camera" | "screen";

export interface Overlay {
  title: string;
  mirrored: boolean;
  step?: { k: string; text: string; spec?: string } | null;
  verdict?: { tone: "pass" | "fail" | "unsure" | "danger" | "info"; head: string; text: string } | null;
  reading?: string | null;
  banner?: { danger: boolean; text: string } | null;
  captions: { who: "irris" | "you"; text: string }[];
  checking?: boolean;
}

export interface RecResult {
  blob: Blob;
  mime: string;
  ms: number;
  mode: RecMode;
}

const LIME = "#D7FF3D";
const RED = "#FF4D3D";
const AMBER = "#FFB547";

export function pickMime(): string {
  const MR = (window as any).MediaRecorder;
  return ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm", "video/mp4"].find((t) => MR?.isTypeSupported?.(t)) || "";
}

export function canRecord(): boolean {
  return !!(window as any).MediaRecorder && !!HTMLCanvasElement.prototype.captureStream;
}

export function canRecordScreen(): boolean {
  return !!navigator.mediaDevices?.getDisplayMedia && window.matchMedia("(pointer: fine)").matches;
}

export const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

function wrap(c: CanvasRenderingContext2D, text: string, maxW: number): string[] {
  const out: string[] = [];
  let line = "";
  for (const w of String(text).split(/\s+/)) {
    const t = line ? `${line} ${w}` : w;
    if (c.measureText(t).width > maxW && line) {
      out.push(line);
      line = w;
    } else line = t;
  }
  if (line) out.push(line);
  return out;
}

function rr(c: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  c.beginPath();
  if ((c as any).roundRect) (c as any).roundRect(x, y, w, h, r);
  else c.rect(x, y, w, h);
}

export class LiveRecorder {
  private rec: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private comp: HTMLCanvasElement | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private drawing = false;
  private dest: MediaStreamAudioDestinationNode | null = null;
  private sources: AudioNode[] = [];
  private display: MediaStream | null = null;
  private started = 0;
  private mode: RecMode = "view";
  private finished: ((r: RecResult) => void) | null = null;

  constructor(
    private opts: {
      source: () => HTMLVideoElement | HTMLImageElement | null;
      overlay: () => Overlay;
      box: () => DOMRect | null;
      cameraStream?: () => MediaStream | null;
      mic?: () => MediaStream | null;
      onStop?: (r: RecResult) => void;
    },
  ) {}

  get recording() {
    return !!this.rec && this.rec.state === "recording";
  }

  get elapsed() {
    return this.recording ? Date.now() - this.started : 0;
  }

  private size() {
    const r = this.opts.box() || new DOMRect(0, 0, 1280, 720);
    const k = Math.min(window.devicePixelRatio || 1, 1280 / Math.max(r.width, r.height, 1));
    this.comp = this.comp || document.createElement("canvas");
    this.ctx = this.comp.getContext("2d");
    this.comp.width = Math.max(2, Math.round((r.width * k) / 2) * 2);
    this.comp.height = Math.max(2, Math.round((r.height * k) / 2) * 2);
  }

  /** Throws with a person-readable message when recording can't start. */
  async start(mode: RecMode): Promise<void> {
    if (this.rec) return;
    if (!(window as any).MediaRecorder) throw new Error("This browser can't record video.");
    this.mode = mode;
    // audio: the mic and Irris's voice into one track
    const ac = speaker.context();
    await ac.resume().catch(() => {});
    this.dest = ac.createMediaStreamDestination();
    const mic = this.opts.mic?.();
    if (mic && mic.getAudioTracks().length) {
      const s = ac.createMediaStreamSource(mic);
      s.connect(this.dest);
      this.sources.push(s);
    }
    speaker.setRecordDestination(this.dest);

    let video: MediaStreamTrack[] = [];
    if (mode === "screen") {
      try {
        this.display = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: true });
      } catch {
        this.cleanup();
        throw new Error("Screen recording was cancelled.");
      }
      if (this.display.getAudioTracks().length) {
        const s = ac.createMediaStreamSource(this.display);
        s.connect(this.dest);
        this.sources.push(s);
      }
      video = this.display.getVideoTracks();
      video[0].onended = () => this.rec?.state === "recording" && this.rec.stop();
    } else if (mode === "camera" && this.opts.cameraStream?.()) {
      video = this.opts.cameraStream!()!.getVideoTracks();
    } else {
      if (!HTMLCanvasElement.prototype.captureStream) {
        this.cleanup();
        throw new Error("This browser can't record the view.");
      }
      this.mode = "view";
      this.size();
      video = this.comp!.captureStream(30).getVideoTracks();
    }
    const stream = new MediaStream([...video, ...this.dest.stream.getAudioTracks()]);
    const type = pickMime();
    try {
      this.rec = new MediaRecorder(stream, type ? { mimeType: type, videoBitsPerSecond: 2_500_000 } : undefined);
    } catch {
      this.cleanup();
      throw new Error("This browser can't record video.");
    }
    this.chunks = [];
    this.rec.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.rec.onstop = () => {
      const mime = this.rec?.mimeType || type || "video/webm";
      const result: RecResult = { blob: new Blob(this.chunks, { type: mime }), mime, ms: Date.now() - this.started, mode: this.mode };
      this.rec = null;
      this.cleanup();
      this.opts.onStop?.(result);
      this.finished?.(result);
      this.finished = null;
    };
    this.started = Date.now();
    this.rec.start(1000);
    if (this.mode === "view") {
      this.drawing = true;
      requestAnimationFrame(this.draw);
    }
  }

  stop(): Promise<RecResult> | null {
    if (!this.rec) return null;
    const p = new Promise<RecResult>((resolve) => (this.finished = resolve));
    if (this.rec.state !== "inactive") this.rec.stop();
    return p;
  }

  private cleanup() {
    this.drawing = false;
    speaker.setRecordDestination(null);
    this.sources.forEach((s) => { try { s.disconnect(); } catch { /* gone */ } });
    this.sources = [];
    this.display?.getTracks().forEach((t) => t.stop());
    this.display = null;
    this.dest = null;
  }

  private draw = () => {
    if (!this.drawing || !this.ctx || !this.comp) return;
    const c = this.ctx;
    const W = this.comp.width;
    const H = this.comp.height;
    const box = this.opts.box();
    const u = W / Math.max(1, box?.width || W);
    const o = this.opts.overlay();
    c.fillStyle = "#0A0A0B";
    c.fillRect(0, 0, W, H);

    // the camera, cover-fit (mirrored for a selfie camera)
    const src = this.opts.source();
    const sw = src instanceof HTMLVideoElement ? src.videoWidth : src?.naturalWidth || 0;
    const sh = src instanceof HTMLVideoElement ? src.videoHeight : src?.naturalHeight || 0;
    if (src && sw && sh) {
      const sc = Math.max(W / sw, H / sh);
      const dw = sw * sc;
      const dh = sh * sc;
      c.save();
      if (o.mirrored) {
        c.translate(W, 0);
        c.scale(-1, 1);
      }
      try { c.drawImage(src, (W - dw) / 2, (H - dh) / 2, dw, dh); } catch { /* frame not ready */ }
      c.restore();
    }
    const font = (px: number, wt = 500, mono = false) =>
      `${wt} ${px * u}px ${mono ? '"JetBrains Mono", ui-monospace, monospace' : '"Instrument Sans", system-ui, sans-serif'}`;
    const m = 14 * u;

    // reticle (the vision overlay)
    const fail = o.verdict && (o.verdict.tone === "fail" || o.verdict.tone === "danger");
    c.strokeStyle = fail ? RED : LIME;
    c.lineWidth = 2 * u;
    const rw = W * 0.56, rh = H * 0.5, rx = (W - rw) / 2, ry = (H - rh) / 2, k = 22 * u;
    c.beginPath();
    for (const [x, y, dx, dy] of [[rx, ry, 1, 1], [rx + rw, ry, -1, 1], [rx, ry + rh, 1, -1], [rx + rw, ry + rh, -1, -1]]) {
      c.moveTo(x + dx * k, y);
      c.lineTo(x, y);
      c.lineTo(x, y + dy * k);
    }
    c.stroke();

    // bottom shade for legibility
    const g = c.createLinearGradient(0, H * 0.5, 0, H);
    g.addColorStop(0, "rgba(10,10,11,0)");
    g.addColorStop(1, "rgba(10,10,11,.7)");
    c.fillStyle = g;
    c.fillRect(0, H * 0.5, W, H * 0.5);

    // top: recording, who and how long
    c.textBaseline = "top";
    c.fillStyle = RED;
    c.beginPath();
    c.arc(m + 5 * u, m + 8 * u, 5 * u, 0, Math.PI * 2);
    c.fill();
    c.font = font(12, 500, true);
    c.fillStyle = "#F5F5F3";
    c.fillText(`REC ${mmss(Date.now() - this.started)} · ${o.title.toUpperCase()}`, m + 18 * u, m + 2 * u);
    let y = m + 30 * u;

    if (o.banner) {
      c.font = font(15, 600);
      const lines = wrap(c, o.banner.text, W - 2 * m - 28 * u).slice(0, 3);
      const h = lines.length * 20 * u + 22 * u;
      c.fillStyle = o.banner.danger ? "rgba(255,77,61,.94)" : "rgba(255,181,71,.94)";
      rr(c, m, y, W - 2 * m, h, 8 * u);
      c.fill();
      c.fillStyle = "#0A0A0B";
      lines.forEach((l, i) => c.fillText(l, m + 14 * u, y + 11 * u + i * 20 * u));
      y += h + 8 * u;
    }

    if (o.step) {
      c.font = font(15, 500);
      const maxW = Math.min(W - 2 * m, 560 * u) - 28 * u;
      const lines = wrap(c, o.step.text, maxW).slice(0, 4);
      const extra = o.step.spec ? 20 * u : 0;
      const h = lines.length * 20 * u + 44 * u + extra;
      c.fillStyle = "rgba(18,18,20,.82)";
      rr(c, m, y, maxW + 28 * u, h, 8 * u);
      c.fill();
      c.font = font(11, 500, true);
      c.fillStyle = LIME;
      c.fillText(o.step.k.toUpperCase(), m + 14 * u, y + 12 * u);
      c.font = font(15, 500);
      c.fillStyle = "#F5F5F3";
      lines.forEach((l, i) => c.fillText(l, m + 14 * u, y + 32 * u + i * 20 * u));
      if (o.step.spec) {
        c.font = font(12, 500, true);
        c.fillStyle = "#A3A3A8";
        c.fillText(o.step.spec, m + 14 * u, y + 34 * u + lines.length * 20 * u);
      }
      y += h + 8 * u;
    }

    if (o.reading) {
      c.font = font(14, 500, true);
      const w = c.measureText(o.reading).width + 26 * u;
      c.fillStyle = "rgba(18,18,20,.82)";
      rr(c, W - m - w, m + 26 * u, w, 30 * u, 6 * u);
      c.fill();
      c.fillStyle = LIME;
      c.fillText(o.reading, W - m - w + 13 * u, m + 34 * u);
    }

    // bottom: captions (hers left, yours right), and the latest verdict above them
    let by = H - m;
    c.font = font(15, 500);
    const capW = Math.min(W * 0.84, 520 * u) - 24 * u;
    for (const cp of [...o.captions].slice(-3).reverse()) {
      const lines = wrap(c, cp.text, capW).slice(-4);
      const w = Math.min(capW, Math.max(...lines.map((l) => c.measureText(l).width))) + 24 * u;
      const h = lines.length * 20 * u + 16 * u;
      by -= h;
      const me = cp.who === "you";
      c.fillStyle = me ? "rgba(215,255,61,.92)" : "rgba(18,18,20,.84)";
      rr(c, me ? W - m - w : m, by, w, h, 10 * u);
      c.fill();
      c.fillStyle = me ? "#0A0A0B" : "#F5F5F3";
      lines.forEach((l, i) => c.fillText(l, (me ? W - m - w : m) + 12 * u, by + 8 * u + i * 20 * u));
      by -= 6 * u;
    }
    if (o.verdict) {
      const tone = o.verdict.tone === "pass" ? LIME : o.verdict.tone === "unsure" || o.verdict.tone === "info" ? AMBER : RED;
      c.font = font(14, 500);
      const lines = wrap(c, o.verdict.text, Math.min(W - 2 * m, 560 * u) - 30 * u).slice(0, 3);
      const h = lines.length * 19 * u + 38 * u;
      by -= h;
      const w = Math.min(W - 2 * m, 560 * u);
      c.fillStyle = "rgba(10,10,11,.86)";
      rr(c, m, by, w, h, 6 * u);
      c.fill();
      c.fillStyle = tone;
      c.fillRect(m, by, 3 * u, h);
      c.font = font(11, 500, true);
      c.fillText(o.verdict.head.toUpperCase(), m + 14 * u, by + 10 * u);
      c.font = font(14, 500);
      c.fillStyle = "#F5F5F3";
      lines.forEach((l, i) => c.fillText(l, m + 14 * u, by + 28 * u + i * 19 * u));
    }
    requestAnimationFrame(this.draw);
  };
}
