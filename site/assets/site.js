/* Irris marketing site: nav behaviour and the drawn scenes. No dependencies.
 *
 * Every scene is a function (ctx, w, h, t, safe) drawn on a devicePixelRatio-aware canvas. A scene only animates
 * while it is on screen and the tab is visible; with reduced motion it draws one composed still frame.
 */
(() => {
  "use strict";

  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const C = {
    ink: "#0A0A0B", paper: "#F5F5F3", graphite: "#1C1C1F", mist: "#8A8A8F", lime: "#D7FF3D", flag: "#FF6A3D",
  };
  const mono = (px, w = 500) => `${w} ${px}px "JetBrains Mono", ui-monospace, monospace`;
  const sans = (px, w = 500) => `${w} ${px}px "Instrument Sans", system-ui, sans-serif`;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, k) => a + (b - a) * k;
  const hash = (n) => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };
  const rgba = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  };

  // cubic-bezier(x1, y1, x2, y2) as a function of progress 0..1, like CSS.
  function bezier(x1, y1, x2, y2) {
    const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    const X = (t) => ((ax * t + bx) * t + cx) * t, Y = (t) => ((ay * t + by) * t + cy) * t;
    const dX = (t) => (3 * ax * t + 2 * bx) * t + cx;
    return (p) => {
      if (p <= 0) return 0;
      if (p >= 1) return 1;
      let t = p;
      for (let i = 0; i < 6; i++) { const d = dX(t); if (Math.abs(d) < 1e-6) break; t -= (X(t) - p) / d; }
      return Y(clamp(t, 0, 1));
    };
  }
  const OUT = bezier(0.23, 1, 0.32, 1);          // UI: fast in, long settle
  const INOUT = bezier(0.77, 0, 0.175, 1);       // on-screen movement
  const SNAP = bezier(0.2, 0.9, 0.1, 1);         // a lock snapping onto a target
  const span = (t, a, b) => clamp((t - a) / (b - a), 0, 1);

  /* ---------- Nav ---------- */
  const nav = document.querySelector(".nav");
  if (nav) {
    const onScroll = () => nav.classList.toggle("scrolled", scrollY > 8);
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    // Match the nav to the tone of whatever section sits under it.
    const toned = document.querySelectorAll("[data-tone]:not(.nav)");
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) nav.dataset.tone = e.target.dataset.tone;
    }, { rootMargin: "0px 0px -96% 0px" });
    toned.forEach((el) => io.observe(el));
  }

  const sheet = document.getElementById("sheet");
  const menuBtn = document.querySelector(".menu-btn");
  if (sheet && menuBtn) {
    const closeBtn = sheet.querySelector(".close");
    const setOpen = (open) => {
      sheet.classList.toggle("open", open);
      sheet.setAttribute("aria-hidden", String(!open));
      menuBtn.setAttribute("aria-expanded", String(open));
      document.body.style.overflow = open ? "hidden" : "";
      if (open) closeBtn.focus(); else menuBtn.focus();
    };
    menuBtn.addEventListener("click", () => setOpen(true));
    closeBtn.addEventListener("click", () => setOpen(false));
    sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
    addEventListener("keydown", (e) => { if (e.key === "Escape" && sheet.classList.contains("open")) setOpen(false); });
  }

  /* ---------- Canvas scaffolding ---------- */
  const mounted = [];
  function mount(canvas, draw, still = 6) {
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, raf = 0, visible = false;
    // The free band between the headline and the buttons, in canvas coordinates.
    const safe = { top: 0, bottom: 0 };
    const panel = canvas.parentElement;
    const measure = () => {
      const r = canvas.getBoundingClientRect();
      const copy = panel.querySelector(".copy"), actions = panel.querySelector(".actions");
      safe.top = copy ? copy.getBoundingClientRect().bottom - r.top : 0;
      safe.bottom = actions ? actions.getBoundingClientRect().top - r.top : h;
    };
    const frame = (now) => {
      ctx.clearRect(0, 0, w, h);
      draw(ctx, w, h, reduce ? still : now / 1000, safe);
      if (visible && !reduce && !document.hidden) raf = requestAnimationFrame(frame);
    };
    const size = () => {
      const r = canvas.getBoundingClientRect();
      w = r.width; h = r.height;
      if (!w || !h) return;
      // sharp on phones, but never more than ~1.8 million canvas pixels, so big tablets stay smooth
      const dpr = Math.max(1, Math.min(devicePixelRatio || 1, 2, Math.sqrt(1.8e6 / (w * h))));
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      measure();
      if (!visible || reduce) frame(performance.now());
    };
    const run = () => { cancelAnimationFrame(raf); if (visible && !document.hidden) raf = requestAnimationFrame(frame); };
    new ResizeObserver(size).observe(canvas);
    new IntersectionObserver(([e]) => { visible = e.isIntersecting; run(); }).observe(canvas);
    mounted.push(run);
    document.fonts && document.fonts.ready.then(size);
  }
  document.addEventListener("visibilitychange", () => mounted.forEach((run) => run()));

  /* ---------- Drawing helpers ---------- */
  function brackets(ctx, x, y, w, h, color, len = 12, lw = 1.5) {
    len = Math.min(len, w / 3, h / 3);
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.beginPath();
    ctx.moveTo(x, y + len); ctx.lineTo(x, y); ctx.lineTo(x + len, y);
    ctx.moveTo(x + w - len, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + len);
    ctx.moveTo(x + w, y + h - len); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - len, y + h);
    ctx.moveTo(x + len, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - len);
    ctx.stroke();
  }

  // A filled label chip whose bottom-left corner sits at (x, y). Returns its width.
  function chip(ctx, x, y, text, bg, fg, px = 11) {
    ctx.font = mono(px);
    const tw = ctx.measureText(text).width, ph = px + 9;
    ctx.fillStyle = bg; ctx.fillRect(x, y - ph, tw + 12, ph);
    ctx.fillStyle = fg; ctx.textBaseline = "middle"; ctx.fillText(text, x + 6, y - ph / 2 + 0.5);
    return tw + 12;
  }
  const chipW = (ctx, text, px = 11) => { ctx.font = mono(px); return ctx.measureText(text).width + 12; };

  function hud(ctx, w, h, left, right, color = "rgba(245,245,243,0.5)") {
    if (w < 700 || h < 600) return;
    ctx.font = mono(11); ctx.fillStyle = color; ctx.textBaseline = "alphabetic";
    ctx.fillText(left, 40, h - 36);
    const tw = ctx.measureText(right).width;
    ctx.fillText(right, w - 40 - tw, h - 36);
  }

  function poly(ctx, pts) {
    ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.closePath();
  }
  const area = (pts) => {
    let s = 0;
    for (let i = 0; i < pts.length; i++) { const a = pts[i], b = pts[(i + 1) % pts.length]; s += a[0] * b[1] - b[0] * a[1]; }
    return s / 2;
  };
  function bbox(pts) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  // A vision lock: brackets that snap from wide to tight, then a label chip that types itself out.
  // k = 0..1 progress of the lock; label shows once k is past 0.55.
  function lock(ctx, b, k, color, label, opts = {}) {
    const { w, h, safeTop = 0, px = 11, alpha = 1 } = opts;
    if (k <= 0 || alpha <= 0) return;
    const e = SNAP(k), pad = lerp(36, 6, e);
    const x = b.x - pad, y = b.y - pad, bw = b.w + pad * 2, bh = b.h + pad * 2;
    ctx.globalAlpha = alpha * clamp(k * 3, 0, 1);
    brackets(ctx, x, y, bw, bh, color, lerp(22, 11, e), 1.5);
    if (label && k > 0.55) {
      const n = Math.ceil(label.length * clamp((k - 0.55) / 0.35, 0, 1));
      const text = label.slice(0, n);
      const cw = chipW(ctx, label, px);
      let cx = clamp(x, 10, w - cw - 10);
      let cy = y - 6;
      if (cy - px - 9 < safeTop + 4) cy = y + bh + px + 15;   // below the box when the headline is above it
      chip(ctx, cx, cy, text, color, C.ink, px);
    }
    ctx.globalAlpha = 1;
  }

  // Speech subtitle: "IRRIS  <line>" centred, fading in and out over its slot.
  function subtitle(ctx, w, y, said, alpha, color = C.paper) {
    if (alpha <= 0) return;
    ctx.globalAlpha = alpha; ctx.textBaseline = "alphabetic";
    const px = w < 500 ? 14 : 16;
    ctx.font = mono(11); const lw = ctx.measureText("IRRIS  ").width;
    ctx.font = sans(px); const sw = ctx.measureText(said).width;
    const sx = clamp(w / 2 - (sw + lw) / 2, 16, Math.max(16, w - 16 - sw - lw));
    ctx.font = mono(11); ctx.fillStyle = C.lime; ctx.fillText("IRRIS", sx, y);
    ctx.font = sans(px); ctx.fillStyle = color; ctx.fillText(said, sx + lw, y);
    ctx.globalAlpha = 1;
  }

  /* ================================================================ Breadboard (hero, final, 404) */
  // Board coordinates: u = column (0..63, one hole pitch apart), v = row. Rails at v 0,1 and 16,17; rows a-e at
  // v 3..7, the centre channel between 7 and 10, rows f-j at v 10..14. z is height above the board, in pitches.
  const BAND = {
    black: "#1b1b1d", brown: "#7A4A2C", red: "#B8432F", orange: "#D2802F", yellow: "#D9B43C", green: "#4E7F4A",
    blue: "#4D6E9C", violet: "#6A5A86", grey: "#8A8A8F", white: "#E6E6E0", gold: "#B89A4A",
  };
  const OHMS = {
    "10k": ["brown", "black", "orange", "gold"], "220": ["red", "red", "brown", "gold"],
    "100": ["brown", "black", "brown", "gold"], "1k": ["brown", "black", "red", "gold"],
    "4k7": ["yellow", "violet", "red", "gold"],
  };
  const WIRE = { red: "#B5503F", black: "#4A4A50", orange: "#C77B3F", blue: "#5E7FA8", white: "#CFCFC8", yellow: "#C9A646", green: "#5F8F5A" };

  const BOARD = {
    resistors: [
      { id: "R1", u0: 22, u1: 26, v: 12, ohm: "10k" },
      { id: "R2", u0: 40, u1: 44, v: 4, ohm: "220" },
      { id: "R3", u0: 34, u1: 38, v: 6, ohm: "10k" },
      { id: "R4", u0: 44, u1: 48, v: 11, ohm: "1k" },
      { id: "R5", u0: 11, u1: 15, v: 13, ohm: "4k7" },
      { id: "R7", u0: 51, u1: 55, v: 6, ohm: "100", fixTo: "10k" },
    ],
    // flat jumpers lying on the board: [u0, v0, u1, v1, colour]
    flat: [[3, 1, 3, 3, "red"], [4, 17, 4, 14, "blue"], [28, 0, 28, 3, "red"], [29, 17, 29, 14, "blue"],
      [39, 1, 39, 5, "red"], [49, 17, 49, 12, "blue"], [60, 1, 60, 3, "red"], [61, 17, 61, 14, "blue"],
      [18, 14, 18, 17, "blue"], [20, 3, 20, 1, "red"]],
    // arched jumpers between holes: [u0, v0, u1, v1, colour, arch height]
    arcs: [[19, 10, 28, 11, "orange", 1.4], [26, 12, 33, 12, "blue", 1.1], [33, 7, 40, 6, "yellow", 1.3],
      [38, 14, 44, 13, "green", 1.0], [55, 6, 57, 8, "white", 0.8], [8, 7, 8, 4, "white", 0.7], [16, 13, 16, 10, "blue", 0.6]],
    // from the dev board's header to the breadboard: [header pin, u, v, colour]
    fly: [[0, 2, 0, "red"], [1, 3, 17, "black"], [4, 9, 3, "orange"], [6, 14, 4, "blue"], [8, 31, 3, "yellow"], [10, 45, 3, "white"]],
  };
  // the microcontroller dev board, standing off behind the breadboard; its header runs along the near edge
  const DEV = { u0: 3, u1: 25, v0: -15, v1: -4.5 };
  const HEADER = (i) => [6 + i * 1.5, -5.4];

  const BB_TARGETS = [
    { id: "U1", box: [5.6, 7.35, 19.4, 9.65, 0, 1.1], label: "U1 · ATmega328P-PU · 0.99" },
    { id: "Y1", box: [20.4, 4.7, 23.6, 5.3, 0, 1.6], label: "Y1 · 16.000 MHz · 0.96" },
    { id: "R3", box: [34, 5.55, 38, 6.45, 0, 1.0], label: "R3 · 10 kΩ · 0.98" },
    { id: "C1", box: [36.1, 11.1, 37.9, 12.9, 0, 2.4], label: "C1 · 100 µF 25 V · 0.97" },
    { id: "D1", box: [46.9, 12.4, 48.1, 13.6, 0, 1.6], label: "D1 · LED · ON" },
    { id: "R7", box: [51, 5.55, 55, 6.45, 0, 1.0], label: "R7 · 100 Ω · expected 10 kΩ", fixed: "R7 · 10 kΩ · fixed" },
  ];
  const BB_LOOP = 17;
  const BB_AT = [0.8, 2.0, 3.2, 4.4, 5.6, 7.4];   // when each target is locked
  const BB_FIX = 11.2;                            // when R7 gets swapped for the right part

  function breadboard(opts = {}) {
    const labels = opts.labels !== false;
    return (ctx, w, h, t, safe) => {
      const portrait = w / Math.max(h, 1) < 0.8;
      const tl = labels ? (reduce ? 9.4 : t % BB_LOOP) : 0;

      // ---- camera: a slow drift around a three-quarter view
      const drift = reduce ? 0 : 1;
      const yaw = (portrait ? -0.62 : -0.2) + Math.sin(t * 0.11) * 0.03 * drift + (opts.yaw || 0);
      const pitch = (portrait ? 0.66 : 0.7) + Math.sin(t * 0.07 + 1) * 0.02 * drift;
      const cu = portrait ? 22 : 26, cv = portrait ? 4 : 3, D = portrait ? 46 : 44;
      const ca = Math.cos(yaw), sa = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
      let f = 1, ox = 0, oy = 0;
      const raw = (u, v, z) => {
        const X = u - cu, Y = v - cv;
        const x1 = X * ca - Y * sa, y1 = X * sa + Y * ca;
        const d = D - y1 * sp - z * cp;
        return [x1 / d, (y1 * cp - z * sp) / d, d];
      };
      // fit the region of interest into the band between headline and buttons
      const region = portrait ? [[2, -13], [40, -13], [40, 18], [2, 18]] : [[-1.6, -14], [58, -14], [58, 18.6], [-1.6, 18.6]];
      const rp = region.map(([u, v]) => raw(u, v, 0));
      const rb = bbox(rp);
      // the board stays clear of the buttons: it fits between the headline and a margin above them
      const top = Math.max(safe.top + (portrait ? 4 : 12), 40), bot = Math.min(safe.bottom - (portrait ? 20 : 32), h - 20);
      const availW = w * (portrait ? 1.1 : 0.92), availH = Math.max(bot - top, h * 0.28);
      f = Math.min(availW / rb.w, availH / rb.h) * (opts.zoom || 1);
      ox = w / 2 - (rb.x + rb.w / 2) * f + Math.sin(t * 0.05) * 6 * drift;
      oy = (top + bot) / 2 - (rb.y + rb.h / 2) * f;
      const P = (u, v, z = 0) => { const r = raw(u, v, z); return [ox + r[0] * f, oy + r[1] * f, r[2]]; };
      const S = (u, v) => f / raw(u, v, 0)[2];          // pixels per pitch at that spot
      const unit = S(cu, cv);

      // ---- the board: a slab with a thickness edge
      const B0 = [-1.6, -1.6], B1 = [64.6, 18.6], TH = -0.9;
      const corners = [[B0[0], B0[1]], [B1[0], B0[1]], [B1[0], B1[1]], [B0[0], B1[1]]];
      const topF = corners.map(([u, v]) => P(u, v, 0)), botF = corners.map(([u, v]) => P(u, v, TH));
      for (let i = 0; i < 4; i++) {           // side faces that face the camera
        const face = [topF[i], topF[(i + 1) % 4], botF[(i + 1) % 4], botF[i]];
        if (area(face) < 0) continue;
        poly(ctx, face); ctx.fillStyle = "#0f0f11"; ctx.fill();
        ctx.strokeStyle = "rgba(245,245,243,0.16)"; ctx.lineWidth = 1; ctx.stroke();
      }
      poly(ctx, topF); ctx.fillStyle = "#151517"; ctx.fill();
      ctx.strokeStyle = "rgba(245,245,243,0.32)"; ctx.lineWidth = 1; ctx.stroke();
      // centre channel
      const ch = [P(-1.2, 8.15), P(64.2, 8.15), P(64.2, 8.85), P(-1.2, 8.85)];
      poly(ctx, ch); ctx.fillStyle = "#0c0c0d"; ctx.fill();
      // rail stripes (muted red and blue)
      for (const [v, col] of [[-0.7, BAND.red], [2.0, BAND.blue], [15.1, BAND.red], [17.8, BAND.blue]]) {
        const a = P(0.5, v), b = P(62.5, v);
        ctx.strokeStyle = rgba(col, 0.55); ctx.lineWidth = Math.max(1, unit * 0.09);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }
      // tie points
      ctx.fillStyle = "rgba(0,0,0,0.85)";
      const rows = [0, 1, 3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 16, 17];
      for (const v of rows) {
        const rail = v < 2 || v > 15;
        for (let u = 0; u <= 63; u++) {
          if (rail && (u % 6 === 5 || u < 1 || u > 62)) continue;   // rails come in groups of five
          const p = P(u, v), s = Math.max(1, 0.3 * f / p[2]);
          ctx.fillRect(p[0] - s / 2, p[1] - s / 2 * cp, s, s * cp);
        }
      }
      if (unit > 7) {            // row letters and column numbers, faint, when there's room
        ctx.fillStyle = "rgba(245,245,243,0.22)"; ctx.font = mono(Math.max(7, unit * 0.32), 400);
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        "abcde".split("").forEach((r, i) => { const p = P(-0.9, 3 + i); ctx.fillText(r, p[0], p[1]); });
        "fghij".split("").forEach((r, i) => { const p = P(-0.9, 10 + i); ctx.fillText(r, p[0], p[1]); });
        for (let u = 0; u <= 60; u += 5) { const p = P(u, 2.35); ctx.fillText(String(u + 1), p[0], p[1]); }
        ctx.textAlign = "start";
      }

      // ---- the dev board, off to the left
      const devTop = [[DEV.u0, DEV.v0], [DEV.u1, DEV.v0], [DEV.u1, DEV.v1], [DEV.u0, DEV.v1]].map(([u, v]) => P(u, v, 0.4));
      const devBot = [[DEV.u0, DEV.v0], [DEV.u1, DEV.v0], [DEV.u1, DEV.v1], [DEV.u0, DEV.v1]].map(([u, v]) => P(u, v, 0.15));
      for (let i = 0; i < 4; i++) {
        const face = [devTop[i], devTop[(i + 1) % 4], devBot[(i + 1) % 4], devBot[i]];
        if (area(face) < 0) continue;
        poly(ctx, face); ctx.fillStyle = "#101413"; ctx.fill(); ctx.strokeStyle = "rgba(245,245,243,0.18)"; ctx.stroke();
      }
      poly(ctx, devTop); ctx.fillStyle = "#131817"; ctx.fill(); ctx.strokeStyle = "rgba(245,245,243,0.4)"; ctx.stroke();
      prism(ctx, P, 5, -16.2, 9.5, -12.5, 0.4, 1.9, "#26262a", "rgba(245,245,243,0.6)");      // USB-B, overhanging the back edge
      prism(ctx, P, 19.5, -15.6, 23.5, -13.2, 0.4, 1.4, "#151517", "rgba(245,245,243,0.45)");  // barrel jack
      prism(ctx, P, 12.5, -12.2, 17.5, -7.2, 0.4, 0.75, "#0d0d0e", "rgba(245,245,243,0.5)");   // MCU
      onPlane(ctx, P, 12.5, -12.2, 0.76, () => {
        ctx.fillStyle = "rgba(245,245,243,0.55)"; ctx.font = mono(0.6, 500); ctx.textBaseline = "middle";
        ctx.fillText("MCU", 1.4, 2.5);
      });
      ctx.strokeStyle = "rgba(245,245,243,0.12)"; ctx.lineWidth = 1;     // traces fanning out of the MCU
      for (let i = 0; i < 9; i++) { const a = P(13 + i * 0.5, -7.2, 0.42), b = P(6 + i * 1.5, -6.2, 0.42); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
      prism(ctx, P, 5.3, -5.9, 22.7, -4.9, 0.4, 1.2, "#0b0b0c", "rgba(245,245,243,0.4)");     // female header
      for (let i = 0; i < 12; i++) {
        const [hu, hv] = HEADER(i), p = P(hu, hv, 1.21), s = Math.max(1.4, unit * 0.32);
        ctx.fillStyle = "#000"; ctx.fillRect(p[0] - s / 2, p[1] - s / 2, s, s * 0.8);
      }
      const pwr = P(23, -9, 0.45);                // power LED on the dev board
      ctx.fillStyle = rgba(C.lime, 0.8); ctx.beginPath(); ctx.arc(pwr[0], pwr[1], Math.max(1.4, unit * 0.22), 0, 7); ctx.fill();

      // ---- flat jumpers
      ctx.lineCap = "round";
      for (const [u0, v0, u1, v1, col] of BOARD.flat) {
        // pre-formed jumper: down into each hole, a short flat run between
        const pts = [P(u0, v0, 0), P(u0, v0, 0.3), P(u1, v1, 0.3), P(u1, v1, 0)];
        ctx.lineJoin = "round";
        ctx.strokeStyle = "rgba(0,0,0,0.55)"; ctx.lineWidth = Math.max(2.2, unit * 0.34); line(ctx, pts, 0, 1.5);
        ctx.strokeStyle = WIRE[col]; ctx.lineWidth = Math.max(1.4, unit * 0.24); line(ctx, pts);
        ctx.strokeStyle = "rgba(255,255,255,0.2)"; ctx.lineWidth = Math.max(0.6, unit * 0.06); line(ctx, pts.slice(1, 3), -0.4, -0.6);
      }

      // ---- parts, far to near
      const parts = [];
      parts.push({ v: 8.5, d: () => dip(ctx, P, unit, 5.6, 19.4, 14, "ATMEGA328P-PU") });
      parts.push({ v: 8.5, d: () => dip(ctx, P, unit, 28.6, 32.4, 4, "NE555P") });
      parts.push({ v: 5, d: () => crystal(ctx, P, unit) });
      for (const [u, v] of [[21, 3], [23, 3]]) parts.push({ v, d: () => prism(ctx, P, u - 0.45, v - 0.12, u + 0.45, v + 0.12, 0.25, 1.05, "#8e5a24", "rgba(245,245,243,0.45)") });
      parts.push({ v: 12, d: () => cylinder(ctx, P, 37, 12, 0.9, 0.2, 2.4, "#16233a", "rgba(245,245,243,0.55)", true) });
      parts.push({ v: 4.5, d: () => cylinder(ctx, P, 26.5, 4.5, 0.6, 0.2, 1.5, "#16233a", "rgba(245,245,243,0.5)", true) });
      const blink = reduce ? 1 : (Math.sin(t * Math.PI * 1.4) > -0.1 ? 1 : 0.15);
      parts.push({ v: 13, d: () => led(ctx, P, unit, 47.5, 13, "#E0563B", blink) });
      parts.push({ v: 4, d: () => led(ctx, P, unit, 57.5, 4, "#E2A23A", reduce ? 1 : (Math.sin(t * 2.3 + 1) > 0.3 ? 1 : 0.2)) });
      parts.push({ v: 11.5, d: () => button(ctx, P, 58.4, 10.3, 61.6, 13.5) });
      const fixK = labels ? INOUT(span(tl, BB_FIX, BB_FIX + 0.9)) : 1;
      for (const r of BOARD.resistors) {
        parts.push({ v: r.v, d: () => resistor(ctx, P, unit, r, r.fixTo ? fixK : 0) });
      }
      parts.sort((a, b) => a.v - b.v);
      // with a strong yaw, sort by projected depth instead of row
      parts.forEach((p) => p.d());

      // ---- arched jumpers and the wires from the dev board, with current pulses
      const paths = [];
      for (const [u0, v0, u1, v1, col, hgt] of BOARD.arcs) paths.push({ col, pts: arch(P, u0, v0, u1, v1, hgt) });
      for (const [pin, u, v, col] of BOARD.fly) {
        const [hu, hv] = HEADER(pin);
        const len = Math.hypot(u - hu, v - hv);
        paths.push({ col, pts: arch(P, hu, hv, u, v, clamp(len * 0.22, 2.2, 5), 1.2), fly: true });
      }
      paths.forEach((p, i) => {
        ctx.lineCap = "round"; ctx.lineJoin = "round";
        ctx.strokeStyle = "rgba(0,0,0,0.55)"; ctx.lineWidth = Math.max(2.4, unit * 0.36);
        line(ctx, p.pts, 0, 1.5);
        ctx.strokeStyle = WIRE[p.col]; ctx.lineWidth = Math.max(1.5, unit * 0.24);
        line(ctx, p.pts);
        ctx.strokeStyle = "rgba(255,255,255,0.18)"; ctx.lineWidth = Math.max(0.6, unit * 0.06);
        line(ctx, p.pts, -0.5, -0.5);
        if (reduce) return;
        // a pulse of current: a short bright run with a fading tail
        const spd = p.fly ? 0.32 : 0.45, ph = (t * spd + hash(i + 2)) % 1.6;
        if (ph > 1) return;
        const n = p.pts.length - 1;
        for (let k = 0; k < 7; k++) {
          const s = ph - k * 0.018; if (s < 0 || s > 1) continue;
          const q = s * n, j = Math.floor(q), fr = q - j, A = p.pts[j], Bq = p.pts[Math.min(n, j + 1)];
          ctx.fillStyle = `rgba(255,244,226,${0.85 * (1 - k / 7)})`;
          ctx.beginPath(); ctx.arc(lerp(A[0], Bq[0], fr), lerp(A[1], Bq[1], fr), Math.max(1.2, unit * 0.15) * (1 - k / 10), 0, 7); ctx.fill();
        }
      });

      // ---- vision: Irris locks onto parts one by one, catches the wrong resistor, then sees it fixed
      if (!labels) return;
      const out = 1 - INOUT(span(tl, BB_LOOP - 1.4, BB_LOOP - 0.2));
      let locked = 0, flags = 0;
      BB_TARGETS.forEach((tg, i) => {
        const k = span(tl, BB_AT[i], BB_AT[i] + 0.75);
        if (k <= 0) return;
        locked++;
        const [u0, v0, u1, v1, z0, z1] = tg.box;
        const pts = [];
        for (const u of [u0, u1]) for (const v of [v0, v1]) for (const z of [z0, z1]) pts.push(P(u, v, z));
        const b = bbox(pts);
        const isFlag = !!tg.fixed;
        const fixed = isFlag && tl >= BB_FIX + 0.6;
        if (isFlag && !fixed) flags++;
        const color = isFlag && !fixed ? C.flag : C.lime;
        // the newest lock (and the flag) keep their label; older ones settle to a short id
        let newest = -1; BB_AT.forEach((a, j) => { if (a <= tl) newest = j; });
        const settled = !(i === newest || (isFlag && !fixed) || (isFlag && fixed && tl < BB_FIX + 3.2));
        const label = settled ? tg.id : fixed ? tg.fixed : tg.label;
        const alpha = out * (settled ? 0.6 : 1);
        lock(ctx, b, k, color, label, { w, h, safeTop: safe.top, px: settled ? 9 : w < 600 ? 10 : 11, alpha });
        if (isFlag && !fixed && !reduce) {   // the flag pulses until it's fixed
          const pulse = 0.5 + 0.5 * Math.sin(t * 6);
          ctx.globalAlpha = out * 0.25 * pulse;
          brackets(ctx, b.x - 12, b.y - 12, b.w + 24, b.h + 24, C.flag, 10, 1);
          ctx.globalAlpha = 1;
        }
      });
      hud(ctx, w, h, "BENCH 02 · CAM 01 · 30 FPS", `LOCKED ${String(locked).padStart(2, "0")} · ${flags} FLAG${flags === 1 ? "" : "S"}`);
    };
  }

  // Map local plane coordinates (u, v at height z, 1 = one pitch) onto the screen for text and flat details.
  function onPlane(ctx, P, u0, v0, z, fn) {
    const o = P(u0, v0, z), a = P(u0 + 1, v0, z), b = P(u0, v0 + 1, z);
    ctx.save(); ctx.transform(a[0] - o[0], a[1] - o[1], b[0] - o[0], b[1] - o[1], o[0], o[1]); fn(); ctx.restore();
  }
  // An axis-aligned box on the board, its visible faces drawn.
  function prism(ctx, P, u0, v0, u1, v1, z0, z1, fill, stroke) {
    const T = [[u0, v0], [u1, v0], [u1, v1], [u0, v1]].map(([u, v]) => P(u, v, z1));
    const Bm = [[u0, v0], [u1, v0], [u1, v1], [u0, v1]].map(([u, v]) => P(u, v, z0));
    for (let i = 0; i < 4; i++) {
      const face = [T[i], T[(i + 1) % 4], Bm[(i + 1) % 4], Bm[i]];
      if (area(face) < 0) continue;
      poly(ctx, face); ctx.fillStyle = shade(fill, 0.75); ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke();
    }
    poly(ctx, T); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke();
    return T;
  }
  function shade(hex, k) {
    const n = parseInt(hex.slice(1), 16);
    const c = (x) => Math.round(clamp(x * k, 0, 255));
    return `rgb(${c(n >> 16)},${c((n >> 8) & 255)},${c(n & 255)})`;
  }
  // A DIP package straddling the channel: legs into rows e and f, pin-1 notch, part number on top.
  function dip(ctx, P, unit, u0, u1, pins, name) {
    ctx.strokeStyle = "rgba(200,200,196,0.75)"; ctx.lineWidth = Math.max(1, unit * 0.12);
    for (let i = 0; i < pins; i++) {
      const u = Math.round(u0 + 0.4) + i;
      for (const [v, vb] of [[7, 7.35], [10, 9.65]]) {
        const a = P(u, v, 0), b = P(u, vb, 0.45), c2 = P(u, vb, 0.55);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c2[0], c2[1]); ctx.stroke();
      }
    }
    prism(ctx, P, u0, 7.35, u1, 9.65, 0.25, 1.1, "#121214", "rgba(245,245,243,0.6)");
    onPlane(ctx, P, u0, 7.35, 1.1, () => {
      ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.lineWidth = 0.06;
      ctx.beginPath(); ctx.arc(0, 1.15, 0.38, -Math.PI / 2, Math.PI / 2); ctx.stroke();     // pin-1 notch
      ctx.fillStyle = "rgba(245,245,243,0.5)"; ctx.beginPath(); ctx.arc(0.75, 1.85, 0.12, 0, 7); ctx.fill();
      ctx.fillStyle = "rgba(245,245,243,0.72)"; ctx.font = mono(Math.min(0.62, (u1 - u0) / name.length * 1.35), 500);
      ctx.textBaseline = "middle"; ctx.fillText(name, 1.1, 1.1);
    });
  }
  function crystal(ctx, P, unit) {
    ctx.strokeStyle = "rgba(200,200,196,0.75)"; ctx.lineWidth = Math.max(1, unit * 0.1);
    for (const u of [21, 23]) { const a = P(u, 5, 0), b = P(u, 5, 0.3); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
    prism(ctx, P, 20.4, 4.7, 23.6, 5.3, 0.3, 1.6, "#5c5f63", "rgba(245,245,243,0.7)");
    onPlane(ctx, P, 20.4, 5.3, 0.95, () => { ctx.fillStyle = "rgba(10,10,11,0.75)"; ctx.font = mono(0.5, 600); ctx.textBaseline = "middle"; ctx.fillText("16.000", 0.45, 0); });
  }
  // A vertical cylinder (electrolytic capacitor) with its sleeve stripe.
  function cylinder(ctx, P, u, v, r, z0, z1, fill, stroke, stripe) {
    const N = 22, top = [], bot = [];
    for (let i = 0; i < N; i++) { const a = (i / N) * Math.PI * 2; top.push(P(u + Math.cos(a) * r, v + Math.sin(a) * r, z1)); bot.push(P(u + Math.cos(a) * r, v + Math.sin(a) * r, z0)); }
    let li = 0, ri = 0;
    for (let i = 0; i < N; i++) { if (top[i][0] < top[li][0]) li = i; if (top[i][0] > top[ri][0]) ri = i; }
    const side = [top[li], top[ri], bot[ri], bot[li]];
    ctx.beginPath(); ctx.moveTo(top[li][0], top[li][1]);
    const ring = (arr, from, to, dir) => { for (let k = from; k !== to; k = (k + dir + N) % N) ctx.lineTo(arr[k][0], arr[k][1]); ctx.lineTo(arr[to][0], arr[to][1]); };
    ctx.lineTo(bot[li][0], bot[li][1]);
    // the near half of the bottom ellipse (the one with larger y)
    const mid = (li + N / 2) % N, nearDir = bot[(li + 1) % N][1] > bot[(li - 1 + N) % N][1] ? 1 : -1;
    ring(bot, li, ri, nearDir);
    ctx.lineTo(top[ri][0], top[ri][1]);
    ctx.closePath(); ctx.fillStyle = fill; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke();
    void side; void mid;
    if (stripe) {   // the minus stripe, a band on the near side
      const a = Math.PI * 0.35, s0 = P(u + Math.cos(a) * r, v + Math.sin(a) * r, z1 - 0.05), s1 = P(u + Math.cos(a) * r, v + Math.sin(a) * r, z0 + 0.1);
      ctx.strokeStyle = "rgba(220,220,214,0.55)"; ctx.lineWidth = 2.2; ctx.beginPath(); ctx.moveTo(s0[0], s0[1]); ctx.lineTo(s1[0], s1[1]); ctx.stroke();
    }
    poly(ctx, top); ctx.fillStyle = "#2b2f36"; ctx.fill(); ctx.strokeStyle = stroke; ctx.stroke();
    ctx.strokeStyle = "rgba(245,245,243,0.25)"; ctx.lineWidth = 0.8;     // vent cross
    const c0 = P(u - r * 0.5, v, z1), c1 = P(u + r * 0.5, v, z1), c2 = P(u, v - r * 0.5, z1), c3 = P(u, v + r * 0.5, z1);
    ctx.beginPath(); ctx.moveTo(c0[0], c0[1]); ctx.lineTo(c1[0], c1[1]); ctx.moveTo(c2[0], c2[1]); ctx.lineTo(c3[0], c3[1]); ctx.stroke();
  }
  function led(ctx, P, unit, u, v, color, on) {
    ctx.strokeStyle = "rgba(200,200,196,0.75)"; ctx.lineWidth = Math.max(1, unit * 0.1);
    for (const du of [-0.5, 0.5]) { const a = P(u + du, v, 0), b = P(u + du * 0.4, v, 0.5); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
    cylinder(ctx, P, u, v, 0.55, 0.5, 1.4, shade(color, 0.35 + 0.65 * on), "rgba(245,245,243,0.5)", false);
    const d = P(u, v, 1.55);
    ctx.fillStyle = rgba(color, 0.25 + 0.7 * on); ctx.beginPath(); ctx.arc(d[0], d[1], Math.max(2, unit * 0.5), 0, 7); ctx.fill();
    if (on > 0.5) {   // a small, honest glow on the board under it
      const g = ctx.createRadialGradient(d[0], d[1], 0, d[0], d[1], unit * 3.2);
      g.addColorStop(0, rgba(color, 0.22)); g.addColorStop(1, rgba(color, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(d[0], d[1], unit * 3.2, 0, 7); ctx.fill();
    }
  }
  function button(ctx, P, u0, v0, u1, v1) {
    prism(ctx, P, u0, v0, u1, v1, 0.1, 0.9, "#1d1d20", "rgba(245,245,243,0.5)");
    const c = P((u0 + u1) / 2, (v0 + v1) / 2, 1.3);
    cylinder(ctx, P, (u0 + u1) / 2, (v0 + v1) / 2, 0.8, 0.9, 1.3, "#2a2a2e", "rgba(245,245,243,0.45)", false);
    void c;
  }
  // An axial resistor: legs bent down into two holes, a tan body with four colour bands.
  function resistor(ctx, P, unit, r, fixK) {
    const { u0, u1, v } = r, zc = 0.62, rad = 0.36;
    ctx.strokeStyle = "rgba(200,200,196,0.75)"; ctx.lineWidth = Math.max(1, unit * 0.1);
    for (const [ue, ub] of [[u0, u0 + 0.7], [u1, u1 - 0.7]]) {
      const a = P(ue, v, 0), b = P(ue, v, zc), c2 = P(ub, v, zc);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.lineTo(c2[0], c2[1]); ctx.stroke();
    }
    const b0 = u0 + 0.7, b1 = u1 - 0.7, n = 10;
    const upper = [], lower = [];
    for (let i = 0; i <= n; i++) {
      const u = lerp(b0, b1, i / n), bulge = i === 0 || i === n ? 0.75 : (i === 1 || i === n - 1 ? 1.08 : 1);
      upper.push(P(u, v - rad * 0.55 * bulge, zc + rad * 0.85 * bulge));
      lower.push(P(u, v + rad * 0.6 * bulge, zc - rad * 0.7 * bulge));
    }
    poly(ctx, [...upper, ...lower.reverse()]);
    ctx.fillStyle = "#b9a27c"; ctx.fill(); ctx.strokeStyle = "rgba(245,245,243,0.45)"; ctx.lineWidth = 0.8; ctx.stroke();
    lower.reverse();
    const bands = OHMS[r.ohm].slice(), to = r.fixTo ? OHMS[r.fixTo] : null;
    [0.2, 0.34, 0.48, 0.78].forEach((pos, i) => {
      const k0 = Math.round(pos * n), k1 = k0 + 1;
      const quad = [upper[k0], upper[k1], lower[k1], lower[k0]];
      let col = BAND[bands[i]];
      if (to && fixK > 0 && bands[i] !== to[i]) col = mix(BAND[bands[i]], BAND[to[i]], fixK);
      poly(ctx, quad); ctx.fillStyle = col; ctx.fill();
    });
    // a soft highlight along the top
    ctx.strokeStyle = "rgba(255,255,255,0.22)"; ctx.lineWidth = 1;
    ctx.beginPath(); upper.slice(1, -1).forEach((p, i) => (i ? ctx.lineTo(p[0], p[1] + 1) : ctx.moveTo(p[0], p[1] + 1))); ctx.stroke();
  }
  function mix(a, b, k) {
    const A = parseInt(a.slice(1), 16), B = parseInt(b.slice(1), 16);
    const c = (s) => Math.round(lerp((A >> s) & 255, (B >> s) & 255, k));
    return `rgb(${c(16)},${c(8)},${c(0)})`;
  }
  // A wire between two holes that rises and sags back down, like stiff solid-core jumper wire.
  function arch(P, u0, v0, u1, v1, hgt, z0 = 0) {
    const pts = [], N = 26;
    for (let i = 0; i <= N; i++) {
      const s = i / N;
      // vertical-ish exits from the holes, a soft crest, a little sag to one side
      const e = s < 0.12 ? s / 0.12 : s > 0.88 ? (1 - s) / 0.12 : 1;
      const z = z0 + hgt * Math.pow(Math.sin(Math.PI * s), 0.7) * (0.6 + 0.4 * e);
      const side = Math.sin(Math.PI * s) * 0.6;
      pts.push(P(lerp(u0, u1, s), lerp(v0, v1, s) + side, z + 0.05));
    }
    return pts;
  }
  function line(ctx, pts, dx = 0, dy = 0) {
    ctx.beginPath(); ctx.moveTo(pts[0][0] + dx, pts[0][1] + dy);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0] + dx, pts[i][1] + dy);
    ctx.stroke();
  }

  /* ================================================================ Engine cutaway (It sees what you see) */
  const ENGINE_SAYS = [
    "Cylinder 3 head at 182 °C. In range.",
    "Keep pulling. 25 newton-metres… there.",
    "Next bolt. Work from the centre out.",
    "Intake timing looks right on cylinder 2.",
  ];
  const PHASE = [0, 3, 1, 2];    // firing order 1-3-4-2, as quarter-cycles of 4π
  function engine(ctx, w, h, t, safe) {
    const portrait = w / Math.max(h, 1) < 0.85;
    const N = portrait ? 2 : 4, first = portrait ? 1 : 0;
    // Phone viewfinder framing the scene
    const m = Math.min(w, h) * 0.05;
    brackets(ctx, m, Math.max(m + 40, 64), w - m * 2, h - Math.max(m + 40, 64) - m, "rgba(245,245,243,0.22)", 28, 1.5);

    // too little room between headline and buttons (a phone on its side): the engine becomes a dim backdrop
    const cramped = safe.bottom - safe.top < 190;
    const top = cramped ? Math.max(70, h * 0.18) : safe.top + (portrait ? 26 : 20);
    const bot = cramped ? h - 16 : safe.bottom - (portrait ? 44 : 40);
    const H = Math.max(bot - top, h * 0.34);
    if (cramped) ctx.globalAlpha = 0.35;
    // engine units: one cylinder pitch = 1. From the wrench (-1.15) to the oil pan (3.6) = 4.75
    const u = Math.min((w * (portrait ? 0.86 : 0.62)) / (N + 1.7), H / 4.75);
    const ox = w / 2 - ((N - 1) / 2) * u + (portrait ? 0 : 0.35 * u), oy = top + 1.15 * u;
    const X = (x) => ox + x * u, Y = (y) => oy + y * u;
    const omega = reduce ? 0 : Math.PI * 2 * 0.55;      // crank speed (rad/s): slow enough to read
    const crank = reduce ? 0.6 : t * omega;
    const bore = 0.78, r = 0.36, l = 1.2, yc = 2.94, deck = 1.0;
    const lw = Math.max(1, u * 0.012);

    // ---- block, head and covers
    const L0 = X(-0.62), L1 = X(N - 1 + 0.62);
    const dx = u * 0.16, dy = -u * 0.1;              // the far face of the cutaway, offset for depth
    ctx.strokeStyle = "rgba(245,245,243,0.14)"; ctx.lineWidth = lw;
    ctx.beginPath();
    ctx.moveTo(L0 + dx, Y(-0.38) + dy); ctx.lineTo(L1 + dx, Y(-0.38) + dy); ctx.lineTo(L1 + dx, Y(2.55) + dy);
    ctx.moveTo(L1, Y(-0.38)); ctx.lineTo(L1 + dx, Y(-0.38) + dy);
    ctx.moveTo(L0 + u * 0.22, Y(-0.55)); ctx.lineTo(L0 + u * 0.22 + dx, Y(-0.55) + dy); ctx.lineTo(L1 - u * 0.22 + dx, Y(-0.55) + dy);
    ctx.moveTo(L1, Y(2.55)); ctx.lineTo(L1 + dx, Y(2.55) + dy);
    ctx.stroke();
    ctx.lineWidth = lw; ctx.strokeStyle = "rgba(245,245,243,0.55)";
    ctx.beginPath();                                   // cam cover
    ctx.moveTo(L0 + u * 0.06, Y(0.42)); ctx.lineTo(L0 + u * 0.06, Y(-0.38));
    ctx.quadraticCurveTo(L0 + u * 0.06, Y(-0.55), L0 + u * 0.22, Y(-0.55));
    ctx.lineTo(L1 - u * 0.22, Y(-0.55)); ctx.quadraticCurveTo(L1 - u * 0.06, Y(-0.55), L1 - u * 0.06, Y(-0.38));
    ctx.lineTo(L1 - u * 0.06, Y(0.42)); ctx.stroke();
    ctx.beginPath(); ctx.rect(L0, Y(0.42), L1 - L0, Y(deck) - Y(0.42)); ctx.stroke();       // head
    ctx.strokeStyle = "rgba(245,245,243,0.4)";
    ctx.beginPath(); ctx.moveTo(L0, Y(deck)); ctx.lineTo(L0, Y(2.55)); ctx.lineTo(L0 + u * 0.18, Y(3.05));
    ctx.moveTo(L1, Y(deck)); ctx.lineTo(L1, Y(2.55)); ctx.lineTo(L1 - u * 0.18, Y(3.05)); ctx.stroke();       // block skirt
    ctx.setLineDash([3, 4]); ctx.strokeStyle = "rgba(245,245,243,0.22)";                       // oil pan
    ctx.beginPath(); ctx.moveTo(L0 + u * 0.18, Y(3.05)); ctx.lineTo(L0 + u * 0.32, Y(3.55)); ctx.lineTo(L1 - u * 0.32, Y(3.55)); ctx.lineTo(L1 - u * 0.18, Y(3.05)); ctx.stroke();
    ctx.setLineDash([]);
    // head gasket line
    ctx.strokeStyle = "rgba(245,245,243,0.3)"; ctx.beginPath(); ctx.moveTo(L0, Y(deck) + 2); ctx.lineTo(L1, Y(deck) + 2); ctx.stroke();

    // ---- timing belt on the left end (only on wide screens)
    if (!portrait) {
      const pc = [X(-1.05), Y(yc)], pcam = [X(-1.05), Y(0.05)], rc = 0.2 * u, rcam = 0.36 * u;
      ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.arc(pc[0], pc[1], rc, 0, 7); ctx.stroke();
      ctx.beginPath(); ctx.arc(pcam[0], pcam[1], rcam, 0, 7); ctx.stroke();
      for (let k = 0; k < 6; k++) {   // pulley spokes show the 2:1 ratio
        const a = crank / 2 + (k * Math.PI) / 3; ctx.beginPath(); ctx.moveTo(pcam[0], pcam[1]); ctx.lineTo(pcam[0] + Math.cos(a) * rcam * 0.8, pcam[1] + Math.sin(a) * rcam * 0.8); ctx.stroke();
      }
      for (let k = 0; k < 3; k++) { const a = crank + (k * Math.PI * 2) / 3; ctx.beginPath(); ctx.moveTo(pc[0], pc[1]); ctx.lineTo(pc[0] + Math.cos(a) * rc * 0.8, pc[1] + Math.sin(a) * rc * 0.8); ctx.stroke(); }
      ctx.setLineDash([u * 0.05, u * 0.05]); ctx.lineDashOffset = -crank * rc;
      ctx.strokeStyle = "rgba(245,245,243,0.35)"; ctx.lineWidth = Math.max(1.5, u * 0.022);
      ctx.beginPath(); ctx.moveTo(pc[0] - rc - 3, pc[1]); ctx.lineTo(pcam[0] - rcam - 3, pcam[1]);
      ctx.moveTo(pc[0] + rc + 3, pc[1]); ctx.lineTo(pcam[0] + rcam + 3, pcam[1]); ctx.stroke();
      ctx.setLineDash([]); ctx.lineDashOffset = 0;
    }

    // ---- each cylinder
    let hot = null;
    for (let i = 0; i < N; i++) {
      const cyl = first + i, x = i;
      const c = crank + PHASE[cyl] * Math.PI;               // position in the 4-stroke cycle (0 = firing TDC)
      const cyc = ((c % (Math.PI * 4)) + Math.PI * 4) % (Math.PI * 4);
      const th = c;                                         // crank angle for this cylinder
      const pinX = x + r * Math.sin(th), pinY = yc - r * Math.cos(th);
      const pistonPin = yc - (r * Math.cos(th) + Math.sqrt(l * l - r * r * Math.sin(th) ** 2));
      const pTop = pistonPin - 0.3;

      // bore walls and water jacket
      ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.moveTo(X(x - bore / 2), Y(deck)); ctx.lineTo(X(x - bore / 2), Y(2.12));
      ctx.moveTo(X(x + bore / 2), Y(deck)); ctx.lineTo(X(x + bore / 2), Y(2.12)); ctx.stroke();
      ctx.setLineDash([2, 5]); ctx.strokeStyle = "rgba(245,245,243,0.16)";
      ctx.beginPath(); ctx.moveTo(X(x - bore / 2 - 0.07), Y(deck + 0.05)); ctx.lineTo(X(x - bore / 2 - 0.07), Y(1.9));
      ctx.moveTo(X(x + bore / 2 + 0.07), Y(deck + 0.05)); ctx.lineTo(X(x + bore / 2 + 0.07), Y(1.9)); ctx.stroke(); ctx.setLineDash([]);

      // combustion: a brief warm flash just after firing TDC
      const burn = cyc < 0.9 ? Math.pow(1 - cyc / 0.9, 2) : 0;
      if (burn > 0.01 && !reduce) {
        ctx.fillStyle = rgba(C.flag, 0.32 * burn);
        ctx.fillRect(X(x - bore / 2) + 1, Y(deck - 0.06), bore * u - 2, Y(pTop) - Y(deck - 0.06));
      }
      // pent-roof chamber
      ctx.strokeStyle = "rgba(245,245,243,0.45)";
      ctx.beginPath(); ctx.moveTo(X(x - bore / 2), Y(deck)); ctx.lineTo(X(x), Y(deck - 0.14)); ctx.lineTo(X(x + bore / 2), Y(deck)); ctx.stroke();
      // spark plug
      ctx.strokeStyle = "rgba(245,245,243,0.55)"; ctx.beginPath(); ctx.moveTo(X(x), Y(deck - 0.14)); ctx.lineTo(X(x), Y(0.18)); ctx.stroke();
      ctx.strokeRect(X(x) - u * 0.05, Y(0.18), u * 0.1, u * 0.2);
      if (burn > 0.6 && !reduce) { ctx.fillStyle = rgba(C.flag, 0.9); ctx.beginPath(); ctx.arc(X(x), Y(deck - 0.12), Math.max(1.5, u * 0.022), 0, 7); ctx.fill(); }

      // valves: exhaust (left) peaks mid exhaust stroke, intake (right) mid intake stroke
      const lift = (peak) => { const d = Math.cos((cyc - peak) / 2); return d > 0.55 ? Math.pow((d - 0.55) / 0.45, 1.6) * 0.16 : 0; };
      [[-0.2, 1.5 * Math.PI, "EX"], [0.2, 2.5 * Math.PI, "IN"]].forEach(([dx, peak], vi) => {
        const L = lift(peak), vx = x + dx;
        // cam: base circle and a lobe that points down while it's lifting
        const ca = (cyc - peak) / 2 + Math.PI / 2, cx = X(vx), cy = Y(0.02);
        ctx.strokeStyle = "rgba(245,245,243,0.6)"; ctx.lineWidth = lw;
        ctx.beginPath();
        for (let k = 0; k <= 28; k++) {
          const a = (k / 28) * Math.PI * 2, rel = Math.cos(a - ca);
          const rr = 0.1 + (rel > 0.55 ? Math.pow((rel - 0.55) / 0.45, 1.6) * 0.16 : 0);
          const px = cx + Math.cos(a) * rr * u, py = cy + Math.sin(a) * rr * u;
          k ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        }
        ctx.closePath(); ctx.stroke();
        ctx.fillStyle = "rgba(245,245,243,0.6)"; ctx.beginPath(); ctx.arc(cx, cy, Math.max(1, u * 0.018), 0, 7); ctx.fill();
        // bucket, stem, spring, valve head
        const tappet = 0.28 + L, head = deck - 0.06 + L;
        ctx.strokeStyle = "rgba(245,245,243,0.55)";
        ctx.strokeRect(X(vx) - u * 0.07, Y(tappet - 0.06), u * 0.14, u * 0.08);
        ctx.beginPath(); ctx.moveTo(X(vx), Y(tappet + 0.02)); ctx.lineTo(X(vx), Y(head)); ctx.stroke();
        const s0 = Y(tappet + 0.04), s1 = Y(0.72), coils = 6;
        ctx.strokeStyle = "rgba(245,245,243,0.32)"; ctx.beginPath();
        for (let k = 0; k <= coils * 2; k++) { const py = lerp(s0, s1, k / (coils * 2)); const px = X(vx) + (k % 2 ? 1 : -1) * u * 0.06; k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }
        ctx.stroke();
        ctx.strokeStyle = "rgba(245,245,243,0.7)"; ctx.beginPath();
        ctx.moveTo(X(vx - 0.12), Y(head)); ctx.lineTo(X(vx - 0.03), Y(head - 0.05)); ctx.lineTo(X(vx + 0.03), Y(head - 0.05)); ctx.lineTo(X(vx + 0.12), Y(head)); ctx.closePath(); ctx.stroke();
        if (vi === 1 && cyl === 1 && L > 0.08 && !portrait) hot = hot || null;
      });

      // crank web: a counterweight lobe opposite the pin, joined to the pin by a web
      const cwa = th + Math.PI / 2;                       // canvas angle pointing away from the pin
      ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.fillStyle = "#18181c"; ctx.lineWidth = lw;
      ctx.beginPath(); ctx.arc(X(x), Y(yc), u * 0.4, cwa - 1.05, cwa + 1.05);
      ctx.lineTo(X(pinX + Math.cos(th) * 0.13), Y(pinY + Math.sin(th) * 0.13));
      ctx.lineTo(X(pinX - Math.cos(th) * 0.13), Y(pinY - Math.sin(th) * 0.13));
      ctx.closePath(); ctx.fill(); ctx.stroke();

      // piston with rings and wrist pin
      ctx.fillStyle = "#16161a"; ctx.fillRect(X(x - bore / 2 + 0.02), Y(pTop), (bore - 0.04) * u, 0.55 * u);
      ctx.strokeStyle = "rgba(245,245,243,0.8)"; ctx.lineWidth = lw * 1.2;
      ctx.strokeRect(X(x - bore / 2 + 0.02), Y(pTop), (bore - 0.04) * u, 0.55 * u);
      ctx.lineWidth = lw; ctx.strokeStyle = "rgba(245,245,243,0.4)";
      for (const ry of [0.07, 0.13, 0.19]) { ctx.beginPath(); ctx.moveTo(X(x - bore / 2 + 0.02), Y(pTop + ry)); ctx.lineTo(X(x + bore / 2 - 0.02), Y(pTop + ry)); ctx.stroke(); }
      // connecting rod: big end round the crank pin, small end round the wrist pin
      const ang = Math.atan2(pinY - pistonPin, pinX - x), nx = -Math.sin(ang), ny = Math.cos(ang);
      ctx.strokeStyle = "rgba(245,245,243,0.75)"; ctx.fillStyle = "#121215";
      ctx.beginPath();
      ctx.moveTo(X(x + nx * 0.05), Y(pistonPin + ny * 0.05)); ctx.lineTo(X(pinX + nx * 0.1), Y(pinY + ny * 0.1));
      ctx.lineTo(X(pinX - nx * 0.1), Y(pinY - ny * 0.1)); ctx.lineTo(X(x - nx * 0.05), Y(pistonPin - ny * 0.05)); ctx.closePath();
      ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.arc(X(x), Y(pistonPin), u * 0.07, 0, 7); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.arc(X(pinX), Y(pinY), u * 0.12, 0, 7); ctx.fillStyle = "#121215"; ctx.fill(); ctx.strokeStyle = "rgba(245,245,243,0.75)"; ctx.stroke();
      ctx.fillStyle = "rgba(245,245,243,0.75)"; ctx.beginPath(); ctx.arc(X(pinX), Y(pinY), Math.max(1, u * 0.025), 0, 7); ctx.fill();

      if (cyl === 2) hot = { x: X(x - bore / 2 - 0.05), y: Y(0.42), w: (bore + 0.1) * u, h: Y(pTop) - Y(0.42) + 0.04 * u };
    }
    // crankshaft main bearings along the axis
    ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.lineWidth = lw;
    ctx.beginPath(); ctx.moveTo(X(-0.62), Y(yc)); ctx.lineTo(X(N - 1 + 0.62), Y(yc)); ctx.stroke();
    for (let i = 0; i <= N; i++) { const x = i - 0.5; ctx.beginPath(); ctx.arc(X(x), Y(yc), u * 0.1, 0, 7); ctx.stroke(); }

    // ---- head bolts on the cam cover, torqued one at a time
    const bolts = [];
    for (let i = 0; i <= N; i++) bolts.push(X(i - 0.5));
    const order = portrait ? [1, 0, 2] : [2, 1, 3, 0, 4];
    const slot = 3.2, cycle = reduce ? 1.6 : t % (slot * order.length);
    const cur = order[Math.floor(cycle / slot) % order.length], prog = (cycle % slot) / slot;
    bolts.forEach((bx, i) => {
      const done = order.indexOf(i) < order.indexOf(cur);
      ctx.fillStyle = "#0f0f11"; ctx.strokeStyle = "rgba(245,245,243,0.7)"; ctx.lineWidth = lw;
      ctx.fillRect(bx - u * 0.06, Y(-0.67), u * 0.12, u * 0.12); ctx.strokeRect(bx - u * 0.06, Y(-0.67), u * 0.12, u * 0.12);
      if (done) { ctx.fillStyle = C.lime; ctx.fillRect(bx - u * 0.06, Y(-0.67) - 3, u * 0.12, 2); }     // torque stripe
    });
    // the wrench on the current bolt: socket, extension, and a handle that swings through the pull
    const bx = bolts[cur], pull = OUT(span(prog, 0.12, 0.7));
    const swing = lerp(-0.9, 0.35, pull), hl = u * 0.95 * Math.cos(swing);
    ctx.strokeStyle = "rgba(245,245,243,0.85)"; ctx.lineWidth = Math.max(1.5, u * 0.02);
    ctx.strokeRect(bx - u * 0.08, Y(-0.82), u * 0.16, u * 0.15);
    ctx.beginPath(); ctx.moveTo(bx, Y(-0.82)); ctx.lineTo(bx, Y(-1.02)); ctx.stroke();
    ctx.lineWidth = Math.max(2.5, u * 0.04); ctx.beginPath(); ctx.moveTo(bx, Y(-1.02)); ctx.lineTo(bx + hl, Y(-1.02) - u * 0.1 * Math.sin(swing)); ctx.stroke();
    ctx.lineWidth = Math.max(5, u * 0.07); ctx.strokeStyle = "rgba(245,245,243,0.4)";
    ctx.beginPath(); ctx.moveTo(bx + hl * 0.72, Y(-1.02) - u * 0.07 * Math.sin(swing)); ctx.lineTo(bx + hl, Y(-1.02) - u * 0.1 * Math.sin(swing)); ctx.stroke();
    const nm = 25 * pull, ok = prog > 0.72;

    ctx.globalAlpha = 1;
    if (cramped) return;
    // ---- vision overlays
    const sx = w < 640;
    if (hot) {
      const v = 182.4 + (reduce ? 0 : Math.sin(t * 1.3) * 0.5);
      lock(ctx, hot, reduce ? 1 : clamp((t % 12) / 0.8, 0, 1), C.lime, `CYL 3 · ${v.toFixed(1)} °C · conf 0.97`, { w, h, safeTop: safe.top, px: sx ? 10 : 12 });
    }
    const bb = { x: bx - u * 0.12, y: Y(-1.08), w: u * 0.24, h: Y(-0.55) - Y(-1.08) };
    const label = ok ? `M8 · 25.0 N·m ✓` : `M8 · ${nm.toFixed(1)} N·m`;
    ctx.globalAlpha = 1;
    brackets(ctx, bb.x - 6, bb.y - 6, bb.w + 12, bb.h + 12, ok ? C.lime : rgba(C.lime, 0.8), 9, 1.5);
    const lpx = sx ? 10 : 12, lwid = chipW(ctx, "M8 · 25.0 N·m ✓", lpx);
    let lx = bx + Math.max(hl, u * 0.4) + 14;
    if (lx + lwid > w - 10) lx = bx - u * 0.2 - lwid - 8;
    chip(ctx, clamp(lx, 10, w - lwid - 10), Y(-0.86) + lpx / 2 + 4, label, ok ? C.lime : C.ink, ok ? C.ink : C.lime, lpx);

    // ---- what Irris says
    const i = reduce ? 1 : Math.floor(t / 4.5) % ENGINE_SAYS.length, local = reduce ? 0.5 : (t % 4.5) / 4.5;
    const sy = safe.bottom - (portrait ? 8 : 6), al = clamp(Math.min(local * 7, (1 - local) * 7), 0, 1);
    ctx.globalAlpha = al * 0.86; ctx.fillStyle = C.graphite;
    ctx.font = sans(w < 500 ? 14 : 16); const tw2 = ctx.measureText(ENGINE_SAYS[i]).width + 72;
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(w / 2 - tw2 / 2, sy - 22, tw2, 34, 17); else ctx.rect(w / 2 - tw2 / 2, sy - 22, tw2, 34); ctx.fill();
    ctx.globalAlpha = 1;
    subtitle(ctx, w, sy, ENGINE_SAYS[i], al);
    hud(ctx, w, h, "PHONE · REAR CAMERA · 1080P", "850 RPM · IDLE · OCR · VLM");
  }

  /* ================================================================ Cables into the twin (It knows your operation) */
  const SOURCES = [
    { name: "press-7", icon: "press", cite: "press-7 · cycle 4410" },
    { name: "line-3/plc", icon: "plc", cite: "line-3/plc · 08:15" },
    { name: "TC-04", icon: "probe", cite: "TC-04 · 182.4 °C" },
    { name: "cam-02", icon: "camera", cite: "cam-02 · frame 18822" },
    { name: "postgres://mes", icon: "db", cite: "postgres://mes · work_orders" },
    { name: "SOP-114.pdf", icon: "pdf", cite: "SOP-114.pdf p.3" },
    { name: "maint-log.xlsx", icon: "sheet", cite: "maint-log.xlsx · row 412" },
    { name: "erp/orders", icon: "rack", cite: "erp/orders · WO-88213" },
  ];
  const PACKETS = ["182.4 °C", "WO-88213", "p.3", "E-41", "2.0 bar", "row 412", "B-1042", "12.1 N·m"];
  function knows(ctx, w, h, t, safe) {
    const portrait = w / Math.max(h, 1) < 0.85;
    const list = portrait ? SOURCES.filter((_, i) => i !== 3 && i !== 7) : SOURCES;
    const band = safe.bottom - safe.top;
    const cx = w / 2, cy = safe.top + band / 2;
    const R = clamp(Math.min(w, band) * 0.075, 22, 46);
    const ink = (a) => `rgba(10,10,11,${a})`;
    const hot = reduce ? 5 % list.length : Math.floor(t / 2.8) % list.length;
    const icon = clamp(Math.min(w / 18, band / 9), 26, 46);

    // where each source sits: two columns on wide screens, a row above and below on tall ones
    const nodes = list.map((s, i) => {
      if (!portrait) {
        const left = i < 4, k = left ? i : i - 4;
        const x = left ? lerp(w * 0.08, w * 0.16, k % 2) + icon : w - lerp(w * 0.08, w * 0.16, k % 2) - icon;
        const y = cy + (k - 1.5) * Math.min(band / 4.6, 120);
        return { ...s, x, y, side: left ? -1 : 1 };
      }
      const upper = i < 3, k = upper ? i : i - 3;
      return { ...s, x: w * (0.18 + k * 0.32), y: upper ? cy - band * 0.4 : cy + band * 0.4, side: upper ? -1 : 1, above: upper };
    });

    // cables: a dark outer sheath and a thin inner core, bezier from the port into the twin
    const cable = (n) => {
      const a = portrait ? [n.x, n.y + n.side * -icon * 0.7] : [n.x - n.side * icon * 0.9, n.y];
      const b = portrait ? [cx + (n.x - cx) * 0.22, cy + n.side * R * 1.15] : [cx + n.side * R * 1.25, cy + (n.y - cy) * 0.16];
      const c1 = portrait ? [a[0], lerp(a[1], b[1], 0.55)] : [lerp(a[0], b[0], 0.55), a[1]];
      const c2 = portrait ? [b[0], lerp(a[1], b[1], 0.45)] : [lerp(a[0], b[0], 0.45), b[1]];
      return [a, c1, c2, b];
    };
    const at = (c, s) => {
      const m = 1 - s;
      return [m * m * m * c[0][0] + 3 * m * m * s * c[1][0] + 3 * m * s * s * c[2][0] + s * s * s * c[3][0],
        m * m * m * c[0][1] + 3 * m * m * s * c[1][1] + 3 * m * s * s * c[2][1] + s * s * s * c[3][1]];
    };
    nodes.forEach((n, i) => {
      const c = cable(n), on = i === hot;
      ctx.lineCap = "round";
      ctx.strokeStyle = on ? ink(0.9) : ink(0.2); ctx.lineWidth = on ? 3.6 : 3;
      ctx.beginPath(); ctx.moveTo(c[0][0], c[0][1]); ctx.bezierCurveTo(c[1][0], c[1][1], c[2][0], c[2][1], c[3][0], c[3][1]); ctx.stroke();
      if (on) {   // the live cable gets a thin highlight down its sheath
        ctx.strokeStyle = "rgba(245,245,243,0.35)"; ctx.lineWidth = 0.8;
        ctx.beginPath(); ctx.moveTo(c[0][0], c[0][1] - 0.8); ctx.bezierCurveTo(c[1][0], c[1][1] - 0.8, c[2][0], c[2][1] - 0.8, c[3][0], c[3][1] - 0.8); ctx.stroke();
      }
      // records travelling along it
      const count = on ? 3 : 2;
      for (let k = 0; k < count; k++) {
        const s = reduce ? (k + 0.5) / count : ((t * (on ? 0.32 : 0.2) + k / count + hash(i)) % 1);
        const p = at(c, INOUT(s));
        const fade = clamp(Math.min(s * 6, (1 - s) * 6), 0, 1);
        ctx.globalAlpha = fade;
        if (on && k === 0 && w > 520) {
          const txt = PACKETS[(i + Math.floor(t / 2.8)) % PACKETS.length];
          const cw2 = chipW(ctx, txt, 10);
          chip(ctx, p[0] - cw2 / 2, p[1] - 6, txt, C.ink, C.paper, 10);
        } else {
          ctx.fillStyle = on ? C.ink : ink(0.5); ctx.fillRect(p[0] - 4, p[1] - 2.5, 8, 5);
        }
        ctx.globalAlpha = 1;
      }
    });

    // the twin: the Irris mark at scale, two rings and the lime lens, with slow orbits
    ctx.save(); ctx.translate(cx, cy);
    const k = R / 9, gap = 3.5 * k;
    ctx.setLineDash([2, 7]); ctx.strokeStyle = ink(0.28); ctx.lineWidth = 1;
    ctx.save(); ctx.rotate(reduce ? 0 : t * 0.12); ctx.beginPath(); ctx.arc(0, 0, R * 2.05, 0, 7); ctx.stroke(); ctx.restore();
    ctx.save(); ctx.rotate(reduce ? 0 : -t * 0.2); ctx.beginPath(); ctx.arc(0, 0, R * 2.7, 0, 7); ctx.stroke(); ctx.restore();
    ctx.setLineDash([]);
    const breathe = reduce ? 0 : Math.sin(t * 1.4) * 0.03;
    ctx.fillStyle = C.lime;
    ctx.beginPath();
    const hh = Math.sqrt(81 - 12.25) * k * (1 + breathe);
    ctx.moveTo(0, -hh); ctx.arc(-gap, 0, 9 * k, -Math.asin(hh / (9 * k)), Math.asin(hh / (9 * k)));
    ctx.arc(gap, 0, 9 * k, Math.PI - Math.asin(hh / (9 * k)), Math.PI + Math.asin(hh / (9 * k)));
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = C.ink; ctx.lineWidth = Math.max(2, 2.2 * k);
    ctx.beginPath(); ctx.arc(-gap, 0, 9 * k, 0, 7); ctx.stroke();
    ctx.beginPath(); ctx.arc(gap, 0, 9 * k, 0, 7); ctx.stroke();
    ctx.restore();
    const count = 1204331 + Math.floor((reduce ? 0 : t) * 23);
    const label = `IRRIS · ${count.toLocaleString("en-US")} records`;
    const lw2 = chipW(ctx, label, 11);
    chip(ctx, cx - lw2 / 2, cy - R * 2.7 - 10, label, C.lime, C.ink, 11);

    // sources, drawn over their cable ends
    nodes.forEach((n, i) => drawSource(ctx, n, icon, i === hot, t));

    // the answer that cites it
    const cite = `cites ${nodes[hot].cite}`;
    const cw = chipW(ctx, cite, 11);
    const local = reduce ? 0.5 : (t % 2.8) / 2.8;
    ctx.globalAlpha = clamp(Math.min(local * 6, (1 - local) * 6), 0, 1);
    chip(ctx, cx - cw / 2, cy + R * 2.7 + 26, cite, C.ink, C.paper, 11);
    ctx.globalAlpha = 1;
  }

  function drawSource(ctx, n, s, on, t) {
    const x = n.x, y = n.y, ink = (a) => `rgba(10,10,11,${a})`;
    ctx.save(); ctx.translate(x, y);
    ctx.lineWidth = 1.4; ctx.strokeStyle = on ? C.ink : ink(0.6); ctx.fillStyle = C.paper;
    const blink = (k) => (reduce ? 1 : (Math.sin(t * 5 + k) > 0 ? 1 : 0.25));
    switch (n.icon) {
      case "plc": {
        ctx.fillRect(-s * 0.55, -s * 0.5, s * 1.1, s); ctx.strokeRect(-s * 0.55, -s * 0.5, s * 1.1, s);
        for (let k = 0; k < 6; k++) { const px = -s * 0.45 + k * s * 0.18; ctx.beginPath(); ctx.arc(px, -s * 0.38, s * 0.04, 0, 7); ctx.stroke(); ctx.beginPath(); ctx.arc(px, s * 0.38, s * 0.04, 0, 7); ctx.stroke(); }
        for (let k = 0; k < 4; k++) { ctx.fillStyle = k === 1 ? rgba("#3E8E41", blink(k)) : ink(0.15); ctx.fillRect(-s * 0.4 + k * s * 0.14, -s * 0.12, s * 0.08, s * 0.08); }
        ctx.fillStyle = ink(0.5); ctx.font = mono(s * 0.16); ctx.textBaseline = "middle"; ctx.fillText("PLC", -s * 0.42, s * 0.12);
        break;
      }
      case "probe": {
        ctx.fillRect(-s * 0.18, -s * 0.5, s * 0.36, s * 0.32); ctx.strokeRect(-s * 0.18, -s * 0.5, s * 0.36, s * 0.32);
        ctx.beginPath(); ctx.moveTo(0, -s * 0.18); ctx.lineTo(0, s * 0.5); ctx.lineWidth = 2.4; ctx.stroke();
        ctx.lineWidth = 1.4; ctx.beginPath(); ctx.arc(0, s * 0.5, s * 0.04, 0, 7); ctx.stroke();
        break;
      }
      case "press": {
        ctx.beginPath(); ctx.moveTo(-s * 0.45, s * 0.5); ctx.lineTo(-s * 0.45, -s * 0.5); ctx.lineTo(s * 0.45, -s * 0.5); ctx.lineTo(s * 0.45, -s * 0.3);
        ctx.moveTo(-s * 0.55, s * 0.5); ctx.lineTo(s * 0.55, s * 0.5); ctx.stroke();
        const ram = reduce ? 0.1 : (Math.sin(t * 2.2) * 0.5 + 0.5) * 0.36;
        ctx.strokeRect(s * 0.15, -s * 0.3 + ram * s, s * 0.3, s * 0.18);
        ctx.strokeRect(s * 0.05, s * 0.3, s * 0.5, s * 0.12);
        break;
      }
      case "camera": {
        ctx.fillRect(-s * 0.45, -s * 0.28, s * 0.75, s * 0.56); ctx.strokeRect(-s * 0.45, -s * 0.28, s * 0.75, s * 0.56);
        ctx.beginPath(); ctx.moveTo(s * 0.3, -s * 0.12); ctx.lineTo(s * 0.55, -s * 0.25); ctx.lineTo(s * 0.55, s * 0.25); ctx.lineTo(s * 0.3, s * 0.12); ctx.stroke();
        ctx.fillStyle = rgba(C.flag, blink(2)); ctx.beginPath(); ctx.arc(-s * 0.32, -s * 0.15, s * 0.05, 0, 7); ctx.fill();
        break;
      }
      case "db": {
        const rw = s * 0.42, rh = s * 0.12;
        ctx.beginPath(); ctx.moveTo(-rw, -s * 0.38); ctx.lineTo(-rw, s * 0.38); ctx.ellipse(0, s * 0.38, rw, rh, 0, Math.PI, 0, true); ctx.lineTo(rw, -s * 0.38); ctx.fill(); ctx.stroke();
        ctx.beginPath(); ctx.ellipse(0, -s * 0.38, rw, rh, 0, 0, 7); ctx.fill(); ctx.stroke();
        for (const yy of [-0.08, 0.15]) { ctx.beginPath(); ctx.ellipse(0, s * yy, rw, rh, 0, 0, Math.PI); ctx.stroke(); }
        break;
      }
      case "pdf": {
        ctx.beginPath(); ctx.moveTo(-s * 0.36, -s * 0.5); ctx.lineTo(s * 0.18, -s * 0.5); ctx.lineTo(s * 0.36, -s * 0.32); ctx.lineTo(s * 0.36, s * 0.5); ctx.lineTo(-s * 0.36, s * 0.5); ctx.closePath(); ctx.fill(); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(s * 0.18, -s * 0.5); ctx.lineTo(s * 0.18, -s * 0.32); ctx.lineTo(s * 0.36, -s * 0.32); ctx.stroke();
        ctx.strokeStyle = ink(0.35); for (let k = 0; k < 5; k++) { ctx.beginPath(); ctx.moveTo(-s * 0.24, -s * 0.18 + k * s * 0.13); ctx.lineTo(s * (k === 4 ? 0.02 : 0.24), -s * 0.18 + k * s * 0.13); ctx.stroke(); }
        break;
      }
      case "sheet": {
        ctx.fillRect(-s * 0.42, -s * 0.42, s * 0.84, s * 0.84); ctx.strokeRect(-s * 0.42, -s * 0.42, s * 0.84, s * 0.84);
        ctx.strokeStyle = ink(0.3);
        for (let k = 1; k < 4; k++) { ctx.beginPath(); ctx.moveTo(-s * 0.42, -s * 0.42 + k * s * 0.21); ctx.lineTo(s * 0.42, -s * 0.42 + k * s * 0.21); ctx.stroke(); }
        for (let k = 1; k < 3; k++) { ctx.beginPath(); ctx.moveTo(-s * 0.42 + k * s * 0.28, -s * 0.42); ctx.lineTo(-s * 0.42 + k * s * 0.28, s * 0.42); ctx.stroke(); }
        if (on) { ctx.fillStyle = rgba(C.lime, 0.8); ctx.fillRect(-s * 0.14 + 1, -s * 0.0 + 1, s * 0.28 - 2, s * 0.21 - 2); }
        break;
      }
      case "rack": {
        for (const yy of [-0.42, 0.04]) { ctx.fillRect(-s * 0.5, s * yy, s, s * 0.38); ctx.strokeRect(-s * 0.5, s * yy, s, s * 0.38);
          ctx.fillStyle = rgba("#3E8E41", blink(yy * 9)); ctx.fillRect(s * 0.3, s * yy + s * 0.15, s * 0.08, s * 0.08); ctx.fillStyle = C.paper;
          ctx.strokeStyle = ink(0.3); ctx.beginPath(); ctx.moveTo(-s * 0.38, s * yy + s * 0.19); ctx.lineTo(s * 0.1, s * yy + s * 0.19); ctx.stroke(); ctx.strokeStyle = on ? C.ink : ink(0.6); }
        break;
      }
    }
    ctx.restore();
    ctx.font = mono(clamp(s * 0.26, 10, 12)); ctx.fillStyle = on ? C.ink : "rgba(10,10,11,0.6)"; ctx.textBaseline = "top";
    const tw = ctx.measureText(n.name).width;
    ctx.fillText(n.name, clamp(x - tw / 2, 8, ctx.canvas.clientWidth - tw - 8), n.above ? y - s * 0.62 - 14 : y + s * 0.62);
    if (on) brackets(ctx, x - s * 0.75, y - s * 0.72, s * 1.5, s * 1.44, C.ink, 8, 1.2);
  }

  /* ================================================================ Bench camera inset (procedure) */
  // Top view of a cover plate on the bench: a gloved hand torques six bolts in a star pattern.
  const STAR = [0, 3, 1, 4, 2, 5];
  function bench(ctx, w, h, t) {
    ctx.fillStyle = "#101012"; ctx.fillRect(0, 0, w, h);
    // bench texture: faint grain lines
    ctx.strokeStyle = "rgba(245,245,243,0.035)"; ctx.lineWidth = 1;
    for (let y = 6; y < h; y += 9) { ctx.beginPath(); ctx.moveTo(0, y + Math.sin(y) * 2); ctx.lineTo(w, y + Math.cos(y * 0.7) * 2); ctx.stroke(); }
    const cx = w * 0.42, cy = h * 0.54, pw = Math.min(w * 0.5, h * 1.1), ph = pw * 0.62;
    // cover plate with gasket and dowels
    ctx.strokeStyle = "rgba(245,245,243,0.7)"; ctx.lineWidth = 1.4; ctx.fillStyle = "#18181b";
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(cx - pw / 2, cy - ph / 2, pw, ph, ph * 0.18); else ctx.rect(cx - pw / 2, cy - ph / 2, pw, ph); ctx.fill(); ctx.stroke();
    ctx.setLineDash([3, 3]); ctx.strokeStyle = "rgba(245,245,243,0.25)";
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(cx - pw / 2 + 7, cy - ph / 2 + 7, pw - 14, ph - 14, ph * 0.14); else ctx.rect(cx - pw / 2 + 7, cy - ph / 2 + 7, pw - 14, ph - 14); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = "rgba(245,245,243,0.18)"; ctx.beginPath(); ctx.ellipse(cx, cy, pw * 0.2, ph * 0.22, 0, 0, 7); ctx.stroke();
    const bolts = [[-0.4, -0.32], [0, -0.38], [0.4, -0.32], [0.4, 0.32], [0, 0.38], [-0.4, 0.32]].map(([a, b]) => [cx + a * pw, cy + b * ph]);
    const slot = 2.6, cyc = reduce ? 1.6 : t % (slot * 6), n = Math.floor(cyc / slot), prog = (cyc % slot) / slot;
    const cur = STAR[n];
    bolts.forEach(([bx, by], i) => {
      const done = STAR.indexOf(i) < n || (i === cur && prog > 0.75);
      hex(ctx, bx, by, Math.max(4, pw * 0.035), "rgba(245,245,243,0.75)");
      if (done) { ctx.strokeStyle = C.lime; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(bx - 3, by + 6); ctx.lineTo(bx + 4, by + 6); ctx.stroke(); }
    });
    // wrench and hand: the handle swings about the socket during the pull
    const [bx, by] = bolts[cur], pull = OUT(span(prog, 0.15, 0.72));
    const aim = Math.atan2(h * 0.66 - by, w * 0.8 - bx);
    const ang = aim + lerp(-0.3, 0.25, pull), L = Math.min(pw * 0.62, Math.hypot(w * 0.8 - bx, h * 0.66 - by));
    const ex = bx + Math.cos(ang) * L, ey = by + Math.sin(ang) * L;
    ctx.strokeStyle = "rgba(245,245,243,0.9)"; ctx.lineWidth = Math.max(3, pw * 0.03); ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.beginPath(); ctx.arc(bx, by, Math.max(5, pw * 0.05), 0, 7); ctx.stroke();
    // glove: palm wrapped round the grip, four finger bands, thumb on top
    ctx.save(); ctx.translate(ex, ey); ctx.rotate(ang);
    const g = Math.max(9, pw * 0.1);
    ctx.fillStyle = "#26262a"; ctx.strokeStyle = "rgba(245,245,243,0.55)"; ctx.lineWidth = 1.2;
    ctx.beginPath(); if (ctx.roundRect) ctx.roundRect(-g * 1.5, -g * 0.85, g * 2.3, g * 1.7, g * 0.6); else ctx.rect(-g * 1.5, -g * 0.85, g * 2.3, g * 1.7); ctx.fill(); ctx.stroke();
    for (let k = 0; k < 4; k++) { ctx.beginPath(); ctx.moveTo(-g * 1.2 + k * g * 0.48, -g * 0.85); ctx.lineTo(-g * 1.2 + k * g * 0.48, g * 0.85); ctx.stroke(); }
    ctx.beginPath(); ctx.ellipse(-g * 1.5, -g * 0.2, g * 0.55, g * 0.3, -0.5, 0, 7); ctx.fill(); ctx.stroke();
    ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(g * 0.8, -g * 0.6); ctx.lineTo(g * 1.6, -g * 0.7); ctx.moveTo(g * 0.8, g * 0.6); ctx.lineTo(g * 1.6, g * 0.7); ctx.stroke();   // cuff
    ctx.restore();
    // vision: lock on the bolt and read the torque from the wrench's display
    const tq = lerp(0, 12.1, pull), ok = prog > 0.75;
    const b = { x: bx - 9, y: by - 9, w: 18, h: 18 };
    lock(ctx, b, reduce ? 1 : clamp(prog * 4, 0, 1), C.lime, ok ? `B${cur + 1} · 12.1 N·m ✓` : `B${cur + 1} · ${tq.toFixed(1)} N·m`, { w, h, safeTop: 18, px: 10 });
    // HUD
    ctx.font = mono(10); ctx.textBaseline = "top";
    ctx.fillStyle = C.flag; ctx.beginPath(); ctx.arc(12, 13, 3.2, 0, 7); ctx.fill();
    ctx.fillStyle = "rgba(245,245,243,0.7)"; ctx.fillText("CAM 02 · BENCH", 21, 8);
    const r = `STEP 4 · ${STAR.indexOf(cur) + (ok ? 1 : 0)}/6`, tw = ctx.measureText(r).width;
    ctx.fillText(r, w - tw - 10, 8);
  }
  function hex(ctx, x, y, r, stroke) {
    ctx.beginPath();
    for (let k = 0; k < 6; k++) { const a = (k / 6) * Math.PI * 2 + Math.PI / 6; k ? ctx.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r) : ctx.moveTo(x + Math.cos(a) * r, y + Math.sin(a) * r); }
    ctx.closePath(); ctx.fillStyle = "#101012"; ctx.fill(); ctx.strokeStyle = stroke; ctx.lineWidth = 1.2; ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y, r * 0.45, 0, 7); ctx.stroke();
  }

  /* ================================================================ Drifting data caught in brackets (standard page) */
  const TOKENS = ["182.4 °C", "PN-2231", "B-1042", "p.3", "work_order", "12.1 N·m", "line-3/plc", "asset:press-7", "2 bar · 60 s", "row 412"];
  function tokens(ctx, w, h, t, safe) {
    const n = w < 640 ? 6 : TOKENS.length;
    const top = safe.top + 36, band = Math.max(safe.bottom - top - 36, 60);
    for (let i = 0; i < n; i++) {
      const sx = hash(i + 1), sy = (i + 0.5) / n;   // spread evenly down the band, scattered across
      const x = (0.08 + sx * 0.84) * w + Math.sin(t * 0.3 + i) * 18;
      const y = top + sy * band + Math.cos(t * 0.25 + i * 2) * 8;
      const text = TOKENS[i];
      ctx.font = mono(13, 400); const tw = ctx.measureText(text).width;
      const caught = ((t * 0.4 + hash(i + 5) * 4) % 4) < 1.6;
      ctx.fillStyle = caught ? C.paper : "rgba(245,245,243,0.35)"; ctx.textBaseline = "middle";
      const xx = clamp(x - tw / 2, 12, w - 12 - tw);
      ctx.fillText(text, xx, y);
      if (caught) brackets(ctx, xx - 8, y - 14, tw + 16, 28, C.lime, 7);
    }
  }

  const SCENES = {
    breadboard: [breadboard(), 9.4],
    quiet: [breadboard({ labels: false, zoom: 0.92 }), 6],
    engine: [engine, 3.2],
    gauge: [engine, 3.2],            // older pages
    line: [breadboard(), 9.4],
    knows: [knows, 6],
    bench: [bench, 6],
    tokens: [tokens, 6],
  };
  document.querySelectorAll("canvas[data-scene]").forEach((c) => {
    const s = SCENES[c.dataset.scene];
    if (s) mount(c, s[0], s[1]);
  });

  /* ---------- Procedure: steps ticking and the batch record writing itself ---------- */
  const proc = document.querySelector("[data-procedure]");
  if (proc) {
    const steps = [...proc.querySelectorAll(".steps li")];
    const record = proc.querySelector(".record");
    const LOG = [
      ["S1", "LOTO tag seen · cam 02", "OK"],
      ["S2", "Gasket G-12 · lip out", "OK"],
      ["S3", "Dowels aligned", "OK"],
      ["S4", "Torque 9.8 N·m · min 11.5", "LOW"],
      ["S4", "Re-torqued 12.1 N·m", "OK"],
      ["S5", "2.0 bar held 60 s · Δ 0.00", "OK"],
      ["S6", "Label B-1042 read", "OK"],
      ["S7", "Photo + sign-off · J. Mwangi", "OK"],
    ];
    const stamp = (i) => { const s = 15 * 60 + 2 + i * 41; return `08:${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
    const add = (i) => {
      const [s, text, res] = LOG[i];
      const li = document.createElement("li");
      li.innerHTML = `<span class="t">${stamp(i)}</span><span class="s">${s}</span><span>${text}</span><span class="r ${res === "OK" ? "ok" : "bad"}">${res}</span>`;
      record.appendChild(li);
    };
    const STEP_OF = [0, 1, 2, 3, 3, 4, 5, 6];   // which step each record line belongs to
    // Paint the checklist after the first k record lines have been written.
    const paint = (k) => {
      const last = k ? LOG[k - 1] : null, s = k ? STEP_OF[k - 1] : -1, low = last && last[2] !== "OK";
      steps.forEach((li, j) => {
        const st = li.querySelector(".state");
        let cls = "", txt = "";
        if (j < s || (j === s && !low)) { cls = "done"; txt = "OK"; }
        else if (j === s && low) { cls = "flag"; txt = "DEVIATION"; }
        else if (j === s + 1) { cls = "now"; txt = "CHECKING"; }
        li.className = cls; st.textContent = txt;
      });
    };
    if (reduce) {
      LOG.forEach((_, i) => add(i));
      paint(LOG.length);
    } else {
      let k = 0, timer = 0, running = false;
      const tick = () => {
        if (k === LOG.length) { record.innerHTML = ""; k = 0; paint(0); timer = setTimeout(tick, 900); return; }
        add(k); k++; paint(k);
        timer = setTimeout(tick, k === LOG.length ? 3600 : LOG[k - 1][2] !== "OK" ? 2200 : 1500);
      };
      new IntersectionObserver(([e]) => {
        if (e.isIntersecting && !running) { running = true; paint(0); timer = setTimeout(tick, 900); }
        else if (!e.isIntersecting && running) { running = false; clearTimeout(timer); record.innerHTML = ""; k = 0; paint(0); }
      }, { threshold: 0.35 }).observe(proc);
      paint(0);
    }
  }

  /* ---------- Tabbed code boxes ---------- */
  document.querySelectorAll("[data-tabs]").forEach((box) => {
    const tabs = [...box.querySelectorAll('[role="tab"]')];
    const panes = tabs.map((tb) => document.getElementById(tb.getAttribute("aria-controls")));
    let i = 0, auto = box.hasAttribute("data-auto") && !reduce, timer = 0;
    const show = (n, focus) => {
      i = (n + tabs.length) % tabs.length;
      tabs.forEach((tb, j) => { tb.setAttribute("aria-selected", String(j === i)); tb.tabIndex = j === i ? 0 : -1; });
      panes.forEach((p, j) => { p.hidden = j !== i; if (j === i) { p.classList.remove("fade"); void p.offsetWidth; p.classList.add("fade"); } });
      if (focus) tabs[i].focus();
    };
    const stop = () => { auto = false; clearInterval(timer); };
    tabs.forEach((tb, j) => {
      tb.addEventListener("click", () => { stop(); show(j); });
      tb.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); stop(); show(i + (e.key === "ArrowRight" ? 1 : -1), true); }
      });
    });
    if (auto) {
      new IntersectionObserver(([e]) => {
        clearInterval(timer);
        if (e.isIntersecting && auto) timer = setInterval(() => show(i + 1), 4200);
      }, { threshold: 0.4 }).observe(box);
    }
    show(0);
  });
})();
