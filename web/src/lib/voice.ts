// Irris's voice and ears in the browser.
//
// Speaking: the server's TTS (free Kokoro/Piper, or the workspace's premium voice) played through Web Audio, so the
// same sound can also feed a recording. If the server can't speak, the browser's speechSynthesis takes over (it
// can't be recorded, but captions still are).
// If the server's first sentence takes more than ~2 s (a busy CPU), the browser's voice starts instead.
// Listening: the browser's own speech recognition first (instant, continuous, with partial words), where there is
// one (Chrome, Edge, Safari); otherwise MediaRecorder clips sent to /voice/stt (Whisper on the server).

import { getWorkspaceId } from "./api";

type Caption = (who: "irris" | "you", text: string) => void;

function splitSentences(text: string): string[] {
  const clean = text
    .replace(/\[(\d+)\]/g, "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[*_#>`|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  const parts = clean.match(/[^.!?]+[.!?]+["')\]]*|[^.!?]+$/g) || [];
  const out: string[] = [];
  for (const p of parts) {
    const s = p.trim();
    if (!s) continue;
    if (out.length && (out[out.length - 1].length < 40)) out[out.length - 1] += " " + s;
    else out.push(s);
  }
  return out.slice(0, 12);
}

export class Speaker {
  private ac: AudioContext | null = null;
  private current: AudioBufferSourceNode | null = null;
  private queue: string[] = [];
  private playing = false;
  private gen = 0;
  private recordDest: MediaStreamAudioDestinationNode | null = null;
  ttsUrl = "/api/v1/voice/tts";
  headers: () => Record<string, string> = () => {
    const ws = getWorkspaceId();
    const h: Record<string, string> = {};
    if (ws) h["X-Workspace"] = ws;
    return h;
  };
  onCaption: Caption | null = null;
  onState: ((speaking: boolean) => void) | null = null;
  lastEngine = "";
  /** What she's saying right now, so the ears can ignore her own voice. */
  spokenText = "";
  spokenUntil = 0;
  firstSentenceMs = 2000;

  context(): AudioContext {
    if (!this.ac) {
      const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
      this.ac = new AC();
    }
    if (this.ac!.state === "suspended") this.ac!.resume().catch(() => {});
    return this.ac!;
  }

  /** While recording, her voice also goes into this node (mixed with the mic). */
  setRecordDestination(dest: MediaStreamAudioDestinationNode | null) {
    this.recordDest = dest;
  }

  get speaking() {
    return this.playing;
  }

  stop() {
    this.gen++;
    this.queue = [];
    try { this.current?.stop(); } catch { /* already stopped */ }
    this.current = null;
    try { window.speechSynthesis?.cancel(); } catch { /* none */ }
    if (this.playing) {
      this.playing = false;
      this.onState?.(false);
    }
  }

  /** Say text: sentence by sentence, the next one fetched while the current one plays. */
  say(text: string, opts: { interrupt?: boolean } = { interrupt: true }) {
    const sentences = splitSentences(text);
    if (!sentences.length) return;
    if (opts.interrupt !== false) this.stop();
    this.onCaption?.("irris", sentences.join(" "));
    this.spokenText = (this.playing ? this.spokenText + " " : "") + sentences.join(" ");
    this.queue.push(...sentences);
    if (!this.playing) this.pump(this.gen);
  }

  private async fetchAudio(sentence: string): Promise<AudioBuffer | null> {
    const r = await fetch(this.ttsUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...this.headers() },
      body: JSON.stringify({ text: sentence }),
    });
    if (!r.ok) throw new Error(`tts ${r.status}`);
    this.lastEngine = r.headers.get("X-Voice-Engine") || "";
    const ac = this.context();
    return await ac.decodeAudioData(await r.arrayBuffer());
  }

  private async pump(gen: number) {
    this.playing = true;
    this.onState?.(true);
    let next: Promise<AudioBuffer | null> | null = null;
    let first = true;
    while (this.queue.length && gen === this.gen) {
      const sentence = this.queue.shift()!;
      let buf: Promise<AudioBuffer | null> = next ? next : this.fetchAudio(sentence);
      if (first && !next) {
        // a busy server must not leave her silent: if the first sentence isn't ready in ~2 s, the browser speaks
        const slow = new Promise<"slow">((r) => window.setTimeout(() => r("slow"), this.firstSentenceMs));
        const won = await Promise.race([buf.catch(() => null), slow]);
        if (won === "slow" && gen === this.gen) {
          const rest = [sentence, ...this.queue].join(" ");
          this.queue = [];
          this.lastEngine = "browser";
          await this.browserSay(rest, gen);
          break;
        }
        buf = Promise.resolve(won as AudioBuffer | null);
      }
      first = false;
      next = this.queue.length ? this.fetchAudio(this.queue[0]).catch(() => null) : null;
      let audio: AudioBuffer | null = null;
      try {
        audio = await buf;
      } catch {
        audio = null;
      }
      if (gen !== this.gen) break;
      if (!audio) {
        // the server couldn't speak: the browser's voice says the rest
        const rest = [sentence, ...this.queue].join(" ");
        this.queue = [];
        await this.browserSay(rest, gen);
        break;
      }
      await this.play(audio, gen);     // `next` (already fetching) is queue[0]'s audio
    }
    if (gen === this.gen) {
      this.playing = false;
      this.spokenUntil = performance.now() + 1500;
      this.onState?.(false);
    }
  }

  /** True when `heard` is most likely her own voice coming back through the mic. */
  isEcho(heard: string): boolean {
    if (!this.playing && performance.now() > this.spokenUntil) return false;
    const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2);
    const said = new Set(norm(this.spokenText));
    const words = norm(heard);
    if (!words.length) return true;
    const hit = words.filter((w) => said.has(w)).length;
    return hit / words.length >= 0.6;
  }

  private play(buf: AudioBuffer, gen: number): Promise<void> {
    return new Promise((resolve) => {
      if (gen !== this.gen) return resolve();
      const ac = this.context();
      const src = ac.createBufferSource();
      src.buffer = buf;
      src.connect(ac.destination);
      if (this.recordDest && this.recordDest.context === ac) src.connect(this.recordDest);
      src.onended = () => resolve();
      this.current = src;
      src.start();
    });
  }

  private browserSay(text: string, gen: number): Promise<void> {
    return new Promise((resolve) => {
      if (!("speechSynthesis" in window) || gen !== this.gen) return resolve();
      try {
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(text);
        u.rate = 1.04;
        u.onend = u.onerror = () => resolve();
        window.speechSynthesis.speak(u);
      } catch {
        resolve();
      }
    });
  }
}

/** One shared voice per page load. */
export const speaker = new Speaker();

// ---------------------------------------------------------------- listening

export function micSupported(): boolean {
  return !!navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function" && !!(window as any).MediaRecorder;
}

export async function openMic(): Promise<MediaStream> {
  if (!micSupported()) throw new Error(window.isSecureContext ? "This browser can't record audio." : "The microphone only works over HTTPS.");
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e: any) {
    if (e?.name === "NotAllowedError") throw new Error("Microphone access was blocked. Allow it in the site settings.");
    throw new Error(e?.message || "Couldn't open the microphone.");
  }
}

function audioMime(): string {
  const MR = (window as any).MediaRecorder;
  return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => MR?.isTypeSupported?.(t)) || "";
}

export async function transcribe(blob: Blob, url = "/api/v1/voice/stt", headers: Record<string, string> = {}): Promise<string> {
  const fd = new FormData();
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  fd.append("file", blob, `speech.${ext}`);
  const ws = getWorkspaceId();
  const r = await fetch(url, { method: "POST", credentials: "same-origin", headers: { ...(ws ? { "X-Workspace": ws } : {}), ...headers }, body: fd });
  const b = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(b.detail || "Couldn't understand that.");
  return String(b.text || "").trim();
}

export function browserRecognition(): any {
  return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition || null;
}

/** Live partial words from the browser's own recogniser, where there is one (Chrome, Edge, Safari). */
export function partials(onText: (t: string) => void): (() => void) | null {
  const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
  if (!SR) return null;
  try {
    const rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = true;
    rec.onresult = (e: any) => {
      let t = "";
      for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
      onText(t.trim());
    };
    rec.onerror = () => {};
    rec.start();
    return () => { try { rec.stop(); } catch { /* stopped */ } };
  } catch {
    return null;
  }
}

/** Record from an open mic until stop() is called. */
export function recordClip(mic: MediaStream): { stop: () => Promise<Blob> } {
  const type = audioMime();
  const rec = new MediaRecorder(mic, type ? { mimeType: type } : undefined);
  const chunks: Blob[] = [];
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.start(250);
  return {
    stop: () =>
      new Promise((resolve) => {
        rec.onstop = () => resolve(new Blob(chunks, { type: rec.mimeType || type || "audio/webm" }));
        if (rec.state !== "inactive") rec.stop();
        else resolve(new Blob(chunks, { type: rec.mimeType || "audio/webm" }));
      }),
  };
}

// ---------------------------------------------------------------- hands-free (voice activity detection)

export interface HandsFreeOptions {
  onUtterance: (text: string) => void;
  onLevel?: (level: number, speaking: boolean) => void;
  onError?: (msg: string) => void;
  onPartial?: (text: string) => void;
  onMode?: (mode: "browser" | "server") => void;
  sttUrl?: string;
  sttHeaders?: Record<string, string>;
  /** Use server Whisper even where the browser can recognise speech itself. */
  serverOnly?: boolean;
}

/**
 * Always listening while on: an energy VAD cuts utterances, each is transcribed on the server. Talking while Irris
 * speaks interrupts her (barge-in); her own voice is ignored thanks to the browser's echo cancellation and a
 * higher threshold while she talks.
 */
export class HandsFree {
  private mic: MediaStream | null = null;
  private ac: AudioContext | null = null;
  private an: AnalyserNode | null = null;
  private buf: Float32Array<ArrayBuffer> | null = null;
  private timer = 0;
  private floor = 0.01;
  private speech = 0;
  private silence = 0;
  private clip: { stop: () => Promise<Blob> } | null = null;
  private started = 0;
  private stopPartials: (() => void) | null = null;
  private rec: any = null;
  private recFails = 0;
  mode: "browser" | "server" = "server";
  on = false;

  constructor(private opts: HandsFreeOptions) {}

  async start(existingMic?: MediaStream) {
    this.mic = existingMic || (await openMic());
    const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
    this.ac = new AC();
    await this.ac!.resume().catch(() => {});
    const src = this.ac!.createMediaStreamSource(this.mic);
    this.an = this.ac!.createAnalyser();
    this.an.fftSize = 1024;
    src.connect(this.an);
    this.buf = new Float32Array(new ArrayBuffer(1024 * 4));
    this.on = true;
    const SR = browserRecognition();
    this.mode = SR && !this.opts.serverOnly ? "browser" : "server";
    if (this.mode === "browser") this.startRecognition(SR);
    this.opts.onMode?.(this.mode);
    this.timer = window.setInterval(() => this.tick(), 50);
  }

  /** The browser's recogniser: continuous, partial words as you talk, the final text the moment you pause. */
  private startRecognition(SR: any) {
    try {
      const rec = new SR();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;
      rec.continuous = true;
      rec.onresult = (e: any) => {
        let interim = "";
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          const t = String(r[0].transcript || "").trim();
          if (!t) continue;
          if (r.isFinal) {
            if (speaker.isEcho(t)) continue;
            this.opts.onPartial?.("");
            if (t.replace(/[^a-z]/gi, "").length > 1) this.opts.onUtterance(t);
          } else {
            interim += t + " ";
          }
        }
        interim = interim.trim();
        if (interim && !speaker.isEcho(interim)) {
          if (speaker.speaking && interim.split(" ").length >= 2) speaker.stop();      // barge-in
          this.opts.onPartial?.(interim);
        }
      };
      rec.onerror = (e: any) => {
        const err = e?.error || "";
        if (err === "not-allowed" || err === "service-not-allowed" || err === "audio-capture") {
          this.fallBackToServer();
        } else if (err !== "no-speech" && err !== "aborted") {
          this.recFails++;
        }
      };
      rec.onend = () => {
        // the recogniser stops after a while or after silence: keep listening while hands-free is on
        if (this.on && this.mode === "browser") {
          if (this.recFails > 5) return this.fallBackToServer();
          window.setTimeout(() => {
            try {
              if (this.on && this.mode === "browser") rec.start();
            } catch {
              /* already starting */
            }
          }, 250);
        }
      };
      rec.start();
      this.rec = rec;
    } catch {
      this.fallBackToServer();
    }
  }

  private fallBackToServer() {
    if (this.mode === "server") return;
    this.mode = "server";
    try { this.rec?.abort(); } catch { /* stopped */ }
    this.rec = null;
    this.opts.onMode?.("server");
  }

  get stream() {
    return this.mic;
  }

  stop(keepMic = false) {
    this.on = false;
    window.clearInterval(this.timer);
    try { this.rec?.abort(); } catch { /* stopped */ }
    this.rec = null;
    this.clip?.stop().catch(() => {});
    this.clip = null;
    this.stopPartials?.();
    this.stopPartials = null;
    try { this.ac?.close(); } catch { /* closed */ }
    this.ac = null;
    if (!keepMic) this.mic?.getTracks().forEach((t) => t.stop());
    this.mic = null;
  }

  private tick() {
    if (!this.an || !this.buf || !this.mic) return;
    this.an.getFloatTimeDomainData(this.buf);
    let sum = 0;
    for (let i = 0; i < this.buf.length; i++) sum += this.buf[i] * this.buf[i];
    const rms = Math.sqrt(sum / this.buf.length);
    const busy = speaker.speaking;
    const threshold = Math.max(0.012, this.floor * (busy ? 4.5 : 2.6));
    const loud = rms > threshold;
    if (!this.clip && !loud) this.floor = this.floor * 0.97 + rms * 0.03;
    this.opts.onLevel?.(Math.min(1, rms / 0.12), !!this.clip || (this.mode === "browser" && loud));
    if (this.mode === "browser") return;          // the browser's recogniser does the listening
    if (loud) {
      this.speech++;
      this.silence = 0;
      if (!this.clip && this.speech >= 3) {
        if (busy) speaker.stop();                       // barge-in
        this.clip = recordClip(this.mic);
        this.started = performance.now();
        if (this.opts.onPartial) this.stopPartials = partials(this.opts.onPartial);
      }
    } else {
      this.speech = 0;
      if (this.clip) {
        this.silence++;
        const long = performance.now() - this.started > 15000;
        if (this.silence >= 16 || long) this.finish();      // ~0.8 s of quiet ends the utterance
      }
    }
  }

  private async finish() {
    const clip = this.clip;
    this.clip = null;
    this.silence = 0;
    this.stopPartials?.();
    this.stopPartials = null;
    if (!clip) return;
    const dur = performance.now() - this.started;
    const blob = await clip.stop();
    if (dur < 450 || blob.size < 2000) return;
    try {
      const text = await transcribe(blob, this.opts.sttUrl, this.opts.sttHeaders);
      if (text && text.replace(/[^a-z]/gi, "").length > 1) this.opts.onUtterance(text);
    } catch (e: any) {
      this.opts.onError?.(e?.message || "Couldn't understand that.");
    }
  }
}

// ---------------------------------------------------------------- spoken commands in Live

export type Command = "check" | "pass" | "fail" | "skip" | "back" | "reading" | "repeat" | "stop" | "record" | null;

export function parseCommand(text: string): Command {
  const t = text.toLowerCase().replace(/[^a-z' ]/g, " ").replace(/\s+/g, " ").trim();
  if (!t || t.split(" ").length > 6) return null;           // longer phrases are questions for Irris
  if (/^(check( it| this| the step)?|is (this|it) (right|ok|correct)|look)$/.test(t)) return "check";
  if (/^(pass|passed|done|next|next step|ok next|it's done|that's done)$/.test(t)) return "pass";
  if (/^(fail|failed|mark (it )?failed|that's wrong)$/.test(t)) return "fail";
  if (/^(skip|skip (it|this|step))$/.test(t)) return "skip";
  if (/^(back|go back|previous|previous step)$/.test(t)) return "back";
  if (/(what's|what is) the reading|read (it|the (gauge|display|reading))|reading$/.test(t)) return "reading";
  if (/^(repeat|say (that|it) again|again|what's the step|what was that)$/.test(t)) return "repeat";
  if (/^(stop|be quiet|quiet|shush|stop talking)$/.test(t)) return "stop";
  if (/^(record|start recording|stop recording)$/.test(t)) return "record";
  return null;
}

/** Persisted on/off preferences (per browser). */
export function pref(key: string, fallback: boolean): boolean {
  try {
    const v = localStorage.getItem(`irris.${key}`);
    return v === null ? fallback : v === "1";
  } catch {
    return fallback;
  }
}

export function setPref(key: string, on: boolean) {
  try {
    localStorage.setItem(`irris.${key}`, on ? "1" : "0");
  } catch {
    /* private mode */
  }
}
