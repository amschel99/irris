// Interruption manners: everything Irris says on the Live and phone screens goes through one scheduler.
//
// critical -> spoken now, even over the person (safety, a wrong part about to go in)
// major    -> spoken once the person has been quiet for 700 ms
// minor    -> held for 15 s, then dropped if the issue cleared on its own (people fix small slips themselves)
// One line per topic per 25 s. Proactive lines are cut to 12 words when spoken; the full text stays on screen.
// Replies to something the person asked, and step instructions, are never cut or rate-limited.
//
// The research behind it: instructors interrupt within ~5 s only for critical mistakes and let small ones be
// self-corrected (HoloAssist); short spoken prompts that point at places; never talk over the user (EgoSpeak).

import { speaker } from "./voice";

export type Severity = "critical" | "major" | "minor";
export type SayKind = "warning" | "reminder" | "question" | "confirm" | "verdict" | "narration" | "instruction" | "reply";

export interface SayItem {
  text: string;
  severity?: Severity;
  topic?: string;
  kind?: SayKind;
  step?: number;
}

export interface LogEntry {
  t: number;
  event: "spoken" | "captioned" | "dropped" | "queued";
  why?: string;
  text: string;
  severity: Severity;
  topic: string;
}

interface Pending {
  item: Required<Pick<SayItem, "text" | "severity" | "topic">> & SayItem;
  at: number;          // when it may be spoken, at the earliest
  expires: number;
}

export const SILENCE_MS = 700;
export const MINOR_WAIT_MS = 15000;
export const TOPIC_COOLDOWN_MS = 25000;
export const MAX_WORDS = 12;
const MAJOR_TTL_MS = 30000;

const EXEMPT = new Set<SayKind>(["reply", "instruction"]);

export function trimWords(text: string, max = MAX_WORDS): string {
  const clean = text.replace(/\s+/g, " ").trim();
  const words = clean.split(" ");
  if (words.length <= max) return clean;
  return words.slice(0, max).join(" ").replace(/[,;:\-–—]+$/, "") + ".";
}

/** Reminders are asked, not told: "Have you purged the line?" keeps a false alarm cheap. */
export function asQuestion(instruction: string): string {
  const t = instruction.trim().replace(/[.!]+$/, "");
  if (t.endsWith("?")) return t;
  const lower = t.charAt(0).toLowerCase() + t.slice(1);
  return `Have you ${/^(the|a|an)\b/.test(lower) ? "done: " + lower : lower}?`;
}

export class SpeechScheduler {
  /** How speech comes out. Live and the phone page use the shared speaker (server voice, browser fallback). */
  speak: (text: string, interrupt: boolean) => void = (text, interrupt) => speaker.say(text, { interrupt });
  /** Full text, for the captions on screen. */
  onCaption: ((full: string, spoken: string, item: SayItem) => void) | null = null;
  /** Muted: lines still appear as captions, nothing is spoken. */
  enabled: () => boolean = () => true;
  /** The ears: is the person talking right now, and when did we last hear them? */
  ears: { speaking: () => boolean; lastHeard: () => number } = { speaking: () => false, lastHeard: () => 0 };
  /** Has the issue behind this line gone away? (step done, right part picked up) */
  cleared: (item: SayItem) => boolean = () => false;
  now: () => number = () => performance.now();
  log: LogEntry[] = [];

  private lastTopic = new Map<string, number>();
  private queue: Pending[] = [];
  private timer = 0;

  private record(event: LogEntry["event"], p: Pending["item"], why?: string) {
    this.log.push({ t: Date.now(), event, why, text: p.text, severity: p.severity, topic: p.topic });
    if (this.log.length > 200) this.log.splice(0, this.log.length - 200);
  }

  say(input: SayItem | string): void {
    const raw: SayItem = typeof input === "string" ? { text: input } : input;
    const text = (raw.text || "").trim();
    if (!text) return;
    const item = {
      ...raw,
      text,
      kind: raw.kind || "warning",
      severity: raw.severity || "major",
      topic: raw.topic || `text:${text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().slice(0, 60)}`,
    } as Pending["item"];
    const exempt = EXEMPT.has(item.kind!);
    if (!exempt && this.coolingDown(item.topic)) {
      this.record("dropped", item, "cooldown");
      return;
    }
    if (!exempt) this.dropQueued(item.topic);           // a newer line on the same topic replaces the old one
    const now = this.now();
    if (item.severity === "critical") {
      this.out(item, true);
      return;
    }
    const wait = item.severity === "minor" ? MINOR_WAIT_MS : 0;
    this.queue.push({ item, at: now + wait, expires: now + wait + MAJOR_TTL_MS });
    this.record("queued", item);
    this.arm();
  }

  /** Forget everything waiting (the person said "stop", or the run ended). */
  flush() {
    this.queue = [];
    window.clearTimeout(this.timer);
    this.timer = 0;
  }

  reset() {
    this.flush();
    this.lastTopic.clear();
    this.log = [];
  }

  get pending(): number {
    return this.queue.length;
  }

  private coolingDown(topic: string): boolean {
    const last = this.lastTopic.get(topic);
    return last !== undefined && this.now() - last < TOPIC_COOLDOWN_MS;
  }

  private dropQueued(topic: string) {
    this.queue = this.queue.filter((p) => p.item.topic !== topic);
  }

  private arm() {
    if (this.timer || !this.queue.length) return;
    this.timer = window.setTimeout(() => {
      this.timer = 0;
      this.pump();
    }, 100);
  }

  private pump() {
    const now = this.now();
    // never over the person, and one thing at a time
    const quiet = !this.ears.speaking() && now - this.ears.lastHeard() >= SILENCE_MS;
    const free = !(speaker.speaking && this.enabled());
    const next: Pending[] = [];
    let chosen: Pending | null = null;
    for (const p of this.queue) {
      const exempt = EXEMPT.has(p.item.kind!);
      const due = now >= p.at;
      if (now > p.expires) {
        this.record("dropped", p.item, "stale");
        continue;
      }
      if (due && !exempt && this.cleared(p.item)) {
        this.record("dropped", p.item, "cleared");
        continue;
      }
      if (!chosen && due && quiet && free) {
        if (!exempt && this.coolingDown(p.item.topic)) {
          this.record("dropped", p.item, "cooldown");
          continue;
        }
        chosen = p;
        continue;
      }
      next.push(p);
    }
    this.queue = next;
    if (chosen) this.out(chosen.item, false);
    this.arm();
  }

  private out(item: Pending["item"], interrupt: boolean) {
    const exempt = EXEMPT.has(item.kind!);
    const spoken = exempt ? item.text : trimWords(item.text);
    this.lastTopic.set(item.topic, this.now());
    this.onCaption?.(item.text, spoken, item);
    if (this.enabled()) {
      this.speak(spoken, interrupt);
      this.record("spoken", item, interrupt ? "interrupt" : undefined);
    } else {
      this.record("captioned", item, "muted");
    }
  }
}

/** One scheduler for the page: Live, the phone page and app-wide alerts all speak through it. */
export const scheduler = new SpeechScheduler();
