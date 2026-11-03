// The fast layer, in the browser: object and hand detection on every frame (MediaPipe Tasks Vision), drawn live and
// summarised as a short line of text ("hands: 2 moving · holding: scissors · objects: cup, keyboard") that goes
// with every question and check, so the server's slow vision model gets what's happening for free.
//
// Nothing here touches the server. The WASM runtime comes from jsDelivr and the two small models from Google's
// MediaPipe model store; the browser caches both. Without WebGL/WASM (or offline) it reports why and stays off.

import type { HandLandmarker, ObjectDetector } from "@mediapipe/tasks-vision";

const VERSION = "1.1.1";
const WASM = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${VERSION}/wasm`;
const OBJECT_MODEL = "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite";
const HAND_MODEL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";

export interface Det {
  label: string;
  score: number;
  box: { x: number; y: number; w: number; h: number };     // 0..1 of the frame
}

export interface Hand {
  points: { x: number; y: number }[];                          // 21 landmarks, 0..1
  side: string;
  center: { x: number; y: number };
  moving: boolean;
}

export interface Scene {
  objects: Det[];
  hands: Hand[];
  holding: string[];
  near: string[];
  activity: string;          // for people: "2 hands moving · holding scissors · bench left"
  summary: string;           // for the model: the same, plus the objects in view
  fps: number;
  t: number;
}

export type SceneEvent =
  | { kind: "picked"; label: string }
  | { kind: "put-down"; label: string }
  | { kind: "hands-in" }
  | { kind: "hands-out" }
  | { kind: "paused" };

// MediaPipe hand skeleton (landmark index pairs)
const BONES: [number, number][] = [
  [0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16], [13, 17], [17, 18], [18, 19], [19, 20], [0, 17],
];
const TIPS = [4, 8, 12, 16, 20];
const IGNORE = new Set(["person", "dining table", "bed", "couch", "chair", "tv"]);

function region(p: { x: number; y: number }): string {
  const h = p.x < 0.34 ? "left" : p.x > 0.66 ? "right" : "centre";
  const v = p.y < 0.34 ? "top" : p.y > 0.66 ? "bottom" : "";
  return v ? `${v} ${h}` : h;
}

function inside(p: { x: number; y: number }, b: Det["box"], pad = 0.04) {
  return p.x >= b.x - pad && p.x <= b.x + b.w + pad && p.y >= b.y - pad && p.y <= b.y + b.h + pad;
}

export class Perceiver {
  private od: ObjectDetector | null = null;
  private hl: HandLandmarker | null = null;
  private raf = 0;
  private el: HTMLVideoElement | HTMLImageElement | null = null;
  private lastTs = 0;
  private lastRun = 0;
  private frames: number[] = [];
  private trail: { t: number; x: number; y: number }[][] = [[], []];
  private objects: Det[] = [];
  private hands: Hand[] = [];
  private prevHolding = new Set<string>();
  private prevHands = 0;
  private movingSince = 0;
  private stillSince = 0;
  private flip = false;
  private gap = 0;
  private injectedUntil = 0;
  ready = false;
  loading = false;
  error = "";
  scene: Scene | null = null;
  onScene: ((s: Scene) => void) | null = null;
  onEvent: ((e: SceneEvent) => void) | null = null;
  /** Detections per second cap: enough to feel live, light enough for a phone. */
  maxFps = 15;

  async init(): Promise<boolean> {
    if (this.ready) return true;
    if (this.loading) return false;
    this.loading = true;
    try {
      if (typeof WebAssembly !== "object") throw new Error("This browser can't run the on-device detector.");
      const { FilesetResolver, ObjectDetector, HandLandmarker } = await import("@mediapipe/tasks-vision");
      const files = await FilesetResolver.forVisionTasks(WASM);
      const make = async (delegate: "GPU" | "CPU") => {
        const od = await ObjectDetector.createFromOptions(files, {
          baseOptions: { modelAssetPath: OBJECT_MODEL, delegate },
          runningMode: "VIDEO",
          scoreThreshold: 0.35,
          maxResults: 8,
        });
        const hl = await HandLandmarker.createFromOptions(files, {
          baseOptions: { modelAssetPath: HAND_MODEL, delegate },
          runningMode: "VIDEO",
          numHands: 2,
        });
        return { od, hl };
      };
      let m;
      try {
        m = await make("GPU");
      } catch {
        m = await make("CPU");          // no WebGL: slower, still live
      }
      this.od = m.od;
      this.hl = m.hl;
      this.ready = true;
      this.error = "";
      return true;
    } catch (e: any) {
      this.error = e?.message ? String(e.message) : "The on-device detector couldn't load.";
      return false;
    } finally {
      this.loading = false;
    }
  }

  start(el: HTMLVideoElement | HTMLImageElement) {
    this.el = el;
    if (this.raf) return;
    const loop = () => {
      this.raf = requestAnimationFrame(loop);
      this.tick();
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.el = null;
    this.scene = null;
    this.objects = [];
    this.hands = [];
    this.trail = [[], []];
    this.prevHolding.clear();
    this.prevHands = 0;
  }

  close() {
    this.stop();
    try { this.od?.close(); } catch { /* closed */ }
    try { this.hl?.close(); } catch { /* closed */ }
    this.od = this.hl = null;
    this.ready = false;
  }

  /** Feed a scene as if the detector had seen it (tests and demos): drawn, summarised and watched like a real one. */
  inject(partial: Partial<Scene> & { objects?: Det[]; hands?: Hand[] }) {
    this.injectedUntil = performance.now() + 2000;          // the live detector pauses so the two don't interleave
    this.objects = partial.objects || [];
    this.hands = (partial.hands || []).map((h) => ({ ...h, center: h.center || h.points[9] || h.points[0], moving: h.moving ?? true, side: h.side || "" }));
    this.compose(performance.now());
  }

  private size(): [number, number] {
    const el = this.el!;
    return el instanceof HTMLVideoElement ? [el.videoWidth, el.videoHeight] : [el.naturalWidth, el.naturalHeight];
  }

  private tick() {
    const el = this.el;
    if (!el || !this.ready || document.hidden || performance.now() < this.injectedUntil) return;
    const [w, h] = this.size();
    if (!w || !h) return;
    if (el instanceof HTMLVideoElement && el.readyState < 2) return;
    const now = performance.now();
    // never more than maxFps, and never more than about a third of the page's time: on a slow device (no GPU) the
    // detector backs off instead of making the page sluggish
    if (now - this.lastRun < Math.max(1000 / this.maxFps, this.gap)) return;
    this.lastRun = now;
    const ts = Math.max(now, this.lastTs + 1);
    this.lastTs = ts;
    try {
      // alternate the two models frame by frame: each runs at half the rate, together they stay light
      this.flip = !this.flip;
      if (this.flip && this.od) {
        const r = this.od.detectForVideo(el, ts);
        this.objects = (r.detections || []).flatMap((d) => {
          const c = d.categories?.[0];
          const b = d.boundingBox;
          if (!c || !b) return [];
          return [{ label: c.categoryName || "object", score: c.score, box: { x: b.originX / w, y: b.originY / h, w: b.width / w, h: b.height / h } }];
        });
      } else if (this.hl) {
        const r: any = this.hl.detectForVideo(el, ts);
        const sides = r.handedness || r.handednesses || [];
        this.hands = (r.landmarks || []).map((pts: { x: number; y: number }[], i: number) => {
          const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
          const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
          const tr = (this.trail[i] = [...(this.trail[i] || []), { t: now, x: cx, y: cy }].filter((p) => now - p.t < 600));
          const first = tr[0];
          const speed = first && now - first.t > 150 ? Math.hypot(cx - first.x, cy - first.y) / ((now - first.t) / 1000) : 0;
          return { points: pts.map((p) => ({ x: p.x, y: p.y })), side: sides[i]?.[0]?.categoryName || "", center: { x: cx, y: cy }, moving: speed > 0.12 };
        });
        for (let i = this.hands.length; i < 2; i++) this.trail[i] = [];
      }
    } catch {
      return;          // a frame the detector couldn't take (e.g. a tab switch mid-frame)
    }
    const cost = performance.now() - now;
    this.gap = Math.min(2000, this.gap * 0.7 + cost * 3 * 0.3);
    this.frames = [...this.frames.filter((t) => now - t < 1000), now];
    this.compose(now);
  }

  private compose(now: number) {
    const holding: string[] = [];
    const near: string[] = [];
    const objs = this.objects.filter((o) => !IGNORE.has(o.label));
    for (const hand of this.hands) {
      const tips = TIPS.map((i) => hand.points[i]).filter(Boolean);
      let best: Det | null = null;
      for (const o of objs) {
        const tipsIn = tips.filter((p) => inside(p, o.box)).length;
        if (tipsIn >= 2 || inside(hand.center, o.box, 0)) {
          if (!best || o.box.w * o.box.h < best.box.w * best.box.h) best = o;
        }
      }
      if (best) {
        if (best.box.w * best.box.h < 0.25) holding.push(best.label);
        else near.push(best.label);
      }
    }
    const moving = this.hands.some((hd) => hd.moving);
    const handsLine = this.hands.length
      ? `${this.hands.length} hand${this.hands.length > 1 ? "s" : ""} ${moving ? "moving" : "still"} at ${[...new Set(this.hands.map((hd) => region(hd.center)))].join(" and ")}`
      : "no hands in view";
    const uniqHold = [...new Set(holding)];
    const uniqNear = [...new Set(near)].filter((n) => !uniqHold.includes(n));
    const activity = [handsLine, uniqHold.length ? `holding ${uniqHold.join(", ")}` : "", uniqNear.length ? `near ${uniqNear.join(", ")}` : ""]
      .filter(Boolean).join(" · ");
    const inView = [...new Set(objs.map((o) => o.label))].slice(0, 8);
    const summary = `${activity}${inView.length ? ` · in view: ${inView.join(", ")}` : ""}`;
    this.scene = {
      objects: this.objects, hands: this.hands, holding: uniqHold, near: uniqNear, activity, summary,
      fps: this.frames.length, t: Date.now(),
    };
    this.onScene?.(this.scene);
    this.events(uniqHold, moving, now);
  }

  private events(holding: string[], moving: boolean, now: number) {
    if (!this.onEvent) return;
    const cur = new Set(holding);
    for (const l of cur) if (!this.prevHolding.has(l)) this.onEvent({ kind: "picked", label: l });
    for (const l of this.prevHolding) if (!cur.has(l)) this.onEvent({ kind: "put-down", label: l });
    this.prevHolding = cur;
    const n = this.hands.length;
    if (n && !this.prevHands) this.onEvent({ kind: "hands-in" });
    if (!n && this.prevHands) this.onEvent({ kind: "hands-out" });
    this.prevHands = n;
    if (moving) {
      this.movingSince = this.movingSince || now;
      this.stillSince = 0;
    } else if (n) {
      this.stillSince = this.stillSince || now;
      // worked for a while, then stopped: often the moment a step is done
      if (this.movingSince && now - this.stillSince > 2500 && this.stillSince - this.movingSince > 4000) {
        this.onEvent({ kind: "paused" });
        this.movingSince = 0;
      }
    }
  }
}

/** Draw the detections over the picture: lime brackets on objects, hand skeletons, mono labels. */
export function drawScene(canvas: HTMLCanvasElement, scene: Scene | null, content: DOMRect | null, mirrored: boolean) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width * dpr);
  const H = Math.round(rect.height * dpr);
  if (canvas.width !== W || canvas.height !== H) {
    canvas.width = W;
    canvas.height = H;
  }
  const g = canvas.getContext("2d");
  if (!g) return;
  g.clearRect(0, 0, W, H);
  if (!scene || !content) return;
  const ox = (content.left - rect.left) * dpr;
  const oy = (content.top - rect.top) * dpr;
  const cw = content.width * dpr;
  const ch = content.height * dpr;
  const X = (x: number) => ox + (mirrored ? 1 - x : x) * cw;
  const Y = (y: number) => oy + y * ch;
  const lime = "#D7FF3D";
  g.lineWidth = 1.5 * dpr;
  g.font = `500 ${10.5 * dpr}px "JetBrains Mono", ui-monospace, monospace`;
  g.textBaseline = "top";
  const held = new Set(scene.holding);
  for (const o of scene.objects) {
    if (o.label === "person") continue;
    const x0 = X(mirrored ? o.box.x + o.box.w : o.box.x);
    const y0 = Y(o.box.y);
    const w = o.box.w * cw;
    const h = o.box.h * ch;
    const k = Math.min(14 * dpr, w / 3, h / 3);
    g.strokeStyle = held.has(o.label) ? lime : "rgba(215,255,61,.65)";
    g.beginPath();
    for (const [cx, cy, dx, dy] of [[x0, y0, 1, 1], [x0 + w, y0, -1, 1], [x0, y0 + h, 1, -1], [x0 + w, y0 + h, -1, -1]] as const) {
      g.moveTo(cx + dx * k, cy);
      g.lineTo(cx, cy);
      g.lineTo(cx, cy + dy * k);
    }
    g.stroke();
    const label = `${o.label.toUpperCase()} ${o.score.toFixed(2)}${held.has(o.label) ? " · HELD" : ""}`;
    const tw = g.measureText(label).width + 8 * dpr;
    const ly = y0 - 16 * dpr < oy ? y0 + 2 * dpr : y0 - 16 * dpr;
    g.fillStyle = held.has(o.label) ? lime : "rgba(10,10,11,.72)";
    g.fillRect(x0, ly, tw, 14 * dpr);
    g.fillStyle = held.has(o.label) ? "#0A0A0B" : lime;
    g.fillText(label, x0 + 4 * dpr, ly + 2 * dpr);
  }
  for (const hand of scene.hands) {
    g.strokeStyle = hand.moving ? "rgba(215,255,61,.9)" : "rgba(245,245,243,.75)";
    g.lineWidth = 1.25 * dpr;
    g.beginPath();
    for (const [a, b] of BONES) {
      const p = hand.points[a];
      const q = hand.points[b];
      if (!p || !q) continue;
      g.moveTo(X(p.x), Y(p.y));
      g.lineTo(X(q.x), Y(q.y));
    }
    g.stroke();
    g.fillStyle = lime;
    for (const i of TIPS) {
      const p = hand.points[i];
      if (p) g.fillRect(X(p.x) - 1.5 * dpr, Y(p.y) - 1.5 * dpr, 3 * dpr, 3 * dpr);
    }
  }
}

/** Where the picture sits inside a video or image element, for object-fit contain or cover. */
export function mediaRect(el: HTMLVideoElement | HTMLImageElement | null): DOMRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const nw = el instanceof HTMLVideoElement ? el.videoWidth : el.naturalWidth;
  const nh = el instanceof HTMLVideoElement ? el.videoHeight : el.naturalHeight;
  if (!nw || !nh || !r.width || !r.height) return null;
  const cover = getComputedStyle(el).objectFit === "cover";
  const k = cover ? Math.max(r.width / nw, r.height / nh) : Math.min(r.width / nw, r.height / nh);
  const w = nw * k;
  const h = nh * k;
  return new DOMRect(r.left + (r.width - w) / 2, r.top + (r.height - h) / 2, w, h);
}

/** A quick, spoken-style line from the detector alone, for an instant first answer. */
export function sceneSentence(s: Scene | null): string {
  if (!s) return "";
  if (s.holding.length) return `I can see you holding ${s.holding.join(" and ")}`;
  if (s.hands.length && s.near.length) return `Your hands are near the ${s.near[0]}`;
  if (s.hands.length) return `I can see your ${s.hands.length > 1 ? "hands" : "hand"} ${s.hands.some((h) => h.moving) ? "working" : "in view"}`;
  const objs = [...new Set(s.objects.filter((o) => !IGNORE.has(o.label)).map((o) => o.label))];
  if (objs.length) return `I can see ${objs.slice(0, 3).join(", ")}`;
  return "";
}
