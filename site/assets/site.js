/* Irris marketing site: nav behaviour and the drawn scenes. No dependencies. */
(() => {
  "use strict";

  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const C = { ink: "#0A0A0B", paper: "#F5F5F3", graphite: "#1C1C1F", mist: "#8A8A8F", lime: "#D7FF3D", flag: "#FF6A3D" };
  const mono = (px, w = 500) => `${w} ${px}px "JetBrains Mono", ui-monospace, monospace`;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const hash = (n) => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };

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
  function mount(canvas, draw) {
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, raf = 0, visible = false;
    // The free band between the headline and the buttons, in canvas coordinates.
    const safe = { top: 0, bottom: 0 };
    const panel = canvas.parentElement;
    const measure = () => {
      const r = canvas.getBoundingClientRect();
      const copy = panel.querySelector(".copy"), actions = panel.querySelector(".actions");
      safe.top = copy ? copy.getBoundingClientRect().bottom - r.top : h * 0.3;
      safe.bottom = actions ? actions.getBoundingClientRect().top - r.top : h * 0.85;
    };
    const frame = (now) => {
      ctx.clearRect(0, 0, w, h);
      draw(ctx, w, h, reduce ? 6 : now / 1000, safe);
      if (visible && !reduce) raf = requestAnimationFrame(frame);
    };
    const size = () => {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      const r = canvas.getBoundingClientRect();
      w = r.width; h = r.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      measure();
      if (!visible || reduce) frame(performance.now());
    };
    new ResizeObserver(size).observe(canvas);
    new IntersectionObserver(([e]) => {
      visible = e.isIntersecting;
      cancelAnimationFrame(raf);
      if (visible) raf = requestAnimationFrame(frame);
    }).observe(canvas);
    document.fonts && document.fonts.ready.then(size);
  }

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

  function hud(ctx, w, h, left, right, color = "rgba(245,245,243,0.5)") {
    if (w < 700) return;
    ctx.font = mono(11); ctx.fillStyle = color; ctx.textBaseline = "alphabetic";
    ctx.fillText(left, 40, h - 36);
    const tw = ctx.measureText(right).width;
    ctx.fillText(right, w - 40 - tw, h - 36);
  }

  /* ---------- Scene: production line (hero and final) ---------- */
  function line(opts = {}) {
    const labels = opts.labels !== false;
    return (ctx, w, h, t, safe) => {
      const small = w < 700;
      const band = Math.max(safe.bottom - safe.top, h * 0.3);
      const vx = w * 0.5, vy = safe.top + band * (small ? 0.12 : 0.08), f = Math.min(w * (small ? 1.1 : 0.85), h * 1.15);
      const BX = small ? 0.7 : 1.9;    // the belt runs off to the right of the eye line
      const P = (x, y, z) => [vx + ((x + BX) * f) / z, vy + (y * f) / z];
      const G = (x, y, z) => [vx + (x * f) / z, vy + (y * f) / z];   // the floor, without the belt offset
      const FLOOR = 2.4, BELT = 1.35, ZN = 2.4, ZF = 80;

      // Floor grid
      ctx.lineWidth = 1;
      for (let x = -20; x <= 20; x += 1.6) {
        const a = G(x, FLOOR, ZN), b = G(x, FLOOR, ZF);
        ctx.strokeStyle = "rgba(245,245,243,0.06)";
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }
      for (let z = ZN; z < ZF; z *= 1.16) {
        const a = G(-20, FLOOR, z), b = G(20, FLOOR, z);
        ctx.strokeStyle = `rgba(245,245,243,${0.08 * clamp(1 - z / ZF, 0, 1)})`;
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }

      // Gantry with a camera
      const gz = 13, gx = 2.2, top = -0.45;
      ctx.strokeStyle = "rgba(245,245,243,0.22)"; ctx.lineWidth = 1;
      [[-gx, gz], [gx, gz]].forEach(([x, z]) => {
        const a = P(x, FLOOR, z), b = P(x, top, z);
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      });
      const g1 = P(-gx, top, gz), g2 = P(gx, top, gz), cam = P(0, top + 0.12, gz);
      ctx.beginPath(); ctx.moveTo(g1[0], g1[1]); ctx.lineTo(g2[0], g2[1]); ctx.stroke();
      ctx.fillStyle = "rgba(245,245,243,0.5)"; ctx.fillRect(cam[0] - 5, cam[1] - 3, 10, 6);
      // Its field of view, dashed, down onto the belt
      ctx.setLineDash([3, 5]); ctx.strokeStyle = "rgba(215,255,61,0.25)";
      [P(-1.3, BELT, gz - 4), P(1.3, BELT, gz - 4), P(-1.3, BELT, gz + 5), P(1.3, BELT, gz + 5)].forEach((p) => {
        ctx.beginPath(); ctx.moveTo(cam[0], cam[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
      });
      ctx.setLineDash([]);
      if (labels && cam[1] - 24 > safe.top) chip(ctx, cam[0] + 10, cam[1] - 2, "CAM 02", "rgba(245,245,243,0.1)", "rgba(245,245,243,0.75)", 10);

      // Conveyor
      const rail = (x) => { const a = P(x, BELT, ZN), b = P(x, BELT, ZF); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); };
      ctx.strokeStyle = "rgba(245,245,243,0.35)"; rail(-1.15); rail(1.15);
      ctx.strokeStyle = "rgba(245,245,243,0.14)"; rail(-1.25); rail(1.25);
      for (let z = 4; z < ZF; z += 4) {
        ctx.strokeStyle = `rgba(245,245,243,${0.18 * (1 - z / ZF)})`;
        [-1.2, 1.2].forEach((x) => { const a = P(x, BELT, z), b = P(x, FLOOR, z); ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); });
      }
      const speed = opts.speed || 2.2, gap = 1.1, span = ZF - ZN;
      for (let i = 0; i < span / gap; i++) {
        const z = ZN + ((((i * gap - t * speed) % span) + span) % span);
        const a = P(-1.15, BELT, z), b = P(1.15, BELT, z);
        ctx.strokeStyle = `rgba(245,245,243,${0.16 * clamp(1 - z / ZF, 0, 1)})`;
        ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      }

      // Parts riding the belt toward the viewer, nearest drawn last
      const spacing = 6.5, zStart = ZF - 4;
      const parts = [];
      for (let i = 0; i < Math.ceil(span / spacing); i++) {
        const travel = t * speed + i * spacing;
        const z = zStart - (travel % (zStart - ZN));
        const id = Math.floor(travel / (zStart - ZN)) * 17 + i;
        parts.push({ z, id });
      }
      parts.sort((a, b) => b.z - a.z);
      let tracking = 0, flags = 0;
      const chips = [];
      for (const { z, id } of parts) {
        if (z < ZN + 0.2) continue;
        const kind = hash(id);
        const hw = kind < 0.5 ? 0.62 : 0.42, hh = kind < 0.5 ? 0.34 : 0.55, hd = kind < 0.5 ? 0.5 : 0.42;
        const fade = clamp((zStart - z) / 8, 0, 1) * clamp((z - ZN) / 1.2, 0, 1);
        const pts = [];
        for (const dx of [-hw, hw]) for (const dy of [BELT - hh * 2, BELT]) for (const dz of [-hd, hd]) pts.push(P(dx, dy, z + dz));
        const E = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
        ctx.strokeStyle = `rgba(245,245,243,${0.62 * fade})`; ctx.lineWidth = 1;
        ctx.beginPath();
        for (const [a, b] of E) { ctx.moveTo(pts[a][0], pts[a][1]); ctx.lineTo(pts[b][0], pts[b][1]); }
        ctx.stroke();
        if (!labels || z > 24) continue;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
        const pad = 6, flagged = hash(id + 3) > 0.82;
        const color = flagged ? C.flag : C.lime;
        tracking++; if (flagged) flags++;
        if (y0 - pad - 24 < safe.top) continue;   // never draw over the headline
        ctx.globalAlpha = fade;
        brackets(ctx, x0 - pad, y0 - pad, x1 - x0 + pad * 2, y1 - y0 + pad * 2, color, 12);
        const conf = (0.9 + hash(id + 9) * 0.09).toFixed(2);
        const text = flagged ? `WRONG PART · PN-22${13 + (id % 7)}` : `PN-22${31 + (id % 9)} · ${conf}`;
        const cy = y0 - pad - 4;
        if (z < 15 && !chips.some((c) => Math.abs(c - cy) < 24)) { chip(ctx, clamp(x0 - pad, 8, w - 210), cy, text, color, C.ink, z < 7 ? 12 : 10); chips.push(cy); }
        ctx.globalAlpha = 1;
      }
      if (labels) hud(ctx, w, h, "LINE 3 · CAM 02 · 30 FPS", `TRACKING ${String(tracking).padStart(2, "0")} · ${flags} FLAG${flags === 1 ? "" : "S"}`);
    };
  }

  /* ---------- Scene: reading a gauge (It sees) ---------- */
  const SAYINGS = [
    "Platen at 182 °C. In range.",
    "Hold still. Reading the dial.",
    "Stop. That's the 100 Ω resistor. You need 10 Ω.",
    "Step 3 done. Torque the cover bolts next.",
  ];
  function gauge(ctx, w, h, t, safe) {
    // Phone viewfinder
    const m = Math.min(w, h) * 0.06;
    brackets(ctx, m, m + 40, w - m * 2, h - m * 2 - 40, "rgba(245,245,243,0.28)", 28, 1.5);

    const small = w <= 640;
    const band = safe.bottom - safe.top;
    const below = small ? 92 : 56;                 // room for the reading chip and the subtitle
    const b0 = clamp((band - below - 40) / 2, 70, small ? (w - 40) / 2 : 230);
    const r = b0 / 1.22, cx = w * 0.5, cy = safe.top + 30 + b0;
    const A0 = Math.PI * 0.75, SWEEP = Math.PI * 1.5, MAX = 300;
    const ang = (v) => A0 + (v / MAX) * SWEEP;

    // Body
    ctx.strokeStyle = "rgba(245,245,243,0.5)"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = "rgba(245,245,243,0.18)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, r * 1.08, 0, Math.PI * 2); ctx.stroke();
    // Red zone
    ctx.strokeStyle = "rgba(255,106,61,0.7)"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.arc(cx, cy, r * 0.86, ang(250), ang(300)); ctx.stroke();
    // Ticks
    for (let v = 0; v <= MAX; v += 10) {
      const a = ang(v), major = v % 50 === 0;
      const r0 = r * (major ? 0.78 : 0.83), r1 = r * 0.9;
      ctx.strokeStyle = major ? "rgba(245,245,243,0.85)" : "rgba(245,245,243,0.4)"; ctx.lineWidth = major ? 1.5 : 1;
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0); ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1); ctx.stroke();
      if (major && r > 90) {
        ctx.font = mono(Math.max(9, r * 0.065), 400); ctx.fillStyle = "rgba(245,245,243,0.6)";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(String(v), cx + Math.cos(a) * r * 0.64, cy + Math.sin(a) * r * 0.64);
        ctx.textAlign = "start";
      }
    }
    ctx.font = mono(Math.max(9, r * 0.06), 400); ctx.fillStyle = "rgba(245,245,243,0.45)";
    ctx.textAlign = "center"; ctx.fillText("°C", cx, cy + r * 0.42); ctx.textAlign = "start";
    // Needle
    const v = 182.4 + Math.sin(t * 1.3) * 0.6 + Math.sin(t * 3.7) * 0.2;
    const a = ang(v);
    ctx.strokeStyle = C.paper; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx - Math.cos(a) * r * 0.12, cy - Math.sin(a) * r * 0.12); ctx.lineTo(cx + Math.cos(a) * r * 0.74, cy + Math.sin(a) * r * 0.74); ctx.stroke();
    ctx.fillStyle = C.paper; ctx.beginPath(); ctx.arc(cx, cy, 4, 0, Math.PI * 2); ctx.fill();
    // The needle detection: a short lime trace along the needle
    ctx.strokeStyle = "rgba(215,255,61,0.9)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.arc(cx, cy, r * 0.74, a - 0.06, a + 0.06); ctx.stroke(); ctx.setLineDash([]);

    // Detection box and readout
    const b = r * 1.22;
    brackets(ctx, cx - b, cy - b, b * 2, b * 2, C.lime, 16);
    chip(ctx, cx - b, cy - b - 4, "GAUGE T-04 · ANALOG", C.lime, C.ink, 11);
    const reading = `${v.toFixed(1)} °C · conf 0.97`;
    if (w > 640) {
      const x = cx + b + 18, y = cy - r * 0.2;
      ctx.strokeStyle = "rgba(215,255,61,0.6)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * r * 0.74, cy + Math.sin(a) * r * 0.74); ctx.lineTo(x, y - 10); ctx.stroke();
      chip(ctx, x, y, reading, C.lime, C.ink, 13);
    } else {
      chip(ctx, cx - b, cy + b + 30, reading, C.lime, C.ink, 12);
    }

    // What Irris says, as a subtitle under the gauge
    const i = reduce ? 2 : Math.floor(t / 4) % SAYINGS.length;
    const local = reduce ? 1 : (t % 4) / 4;
    const alpha = clamp(Math.min(local * 8, (1 - local) * 8), 0, 1);
    const said = SAYINGS[i];
    const sy = cy + b + (small ? 64 : 40);
    ctx.globalAlpha = alpha; ctx.textBaseline = "alphabetic";
    ctx.font = mono(11); const lw = ctx.measureText("IRRIS  ").width;
    ctx.font = `500 ${w < 500 ? 14 : 16}px "Instrument Sans", system-ui, sans-serif`;
    const sw = ctx.measureText(said).width;
    const sx = clamp(cx - (sw + lw) / 2, 16, w - 16 - sw - lw);
    ctx.font = mono(11); ctx.fillStyle = C.lime; ctx.fillText("IRRIS", sx, sy);
    ctx.font = `500 ${w < 500 ? 14 : 16}px "Instrument Sans", system-ui, sans-serif`;
    ctx.fillStyle = C.paper; ctx.fillText(said, sx + lw, sy);
    ctx.globalAlpha = 1;

    hud(ctx, w, h, "PHONE · REAR CAMERA · 1080P", "OCR · NEEDLE · VLM");
  }

  /* ---------- Scene: sources feeding the twin (It knows your plant) ---------- */
  const SOURCES = ["postgres://mes", "SOP-114.pdf", "line-3/plc", "erp/orders", "maint-log.xlsx", "webhook/qc", "cam-02 frames", "lessons"];
  const CITES = ["SOP-114.pdf p.3", "postgres://mes · work_orders", "line-3/plc · 08:15", "maint-log.xlsx · row 412"];
  function knows(ctx, w, h, t, safe) {
    const small = w < 640;
    const list = small ? SOURCES.slice(0, 6) : SOURCES;
    const band = safe.bottom - safe.top;
    const cx = w / 2, cy = safe.top + band / 2;
    const rx = Math.min(w * (small ? 0.36 : 0.4), 520), ry = clamp(band / 2 - 44, 60, 230);
    const nodes = list.map((name, i) => {
      const a = (i / list.length) * Math.PI * 2 - Math.PI / 2 + 0.3;
      return { name, x: cx + Math.cos(a) * rx, y: cy + Math.sin(a) * ry, a };
    });
    const hot = Math.floor(t / 2.6) % nodes.length;

    // Links and records flowing in
    nodes.forEach((n, i) => {
      ctx.strokeStyle = i === hot ? "rgba(10,10,11,0.55)" : "rgba(10,10,11,0.12)";
      ctx.lineWidth = i === hot ? 1.5 : 1;
      ctx.beginPath(); ctx.moveTo(n.x, n.y); ctx.lineTo(cx, cy); ctx.stroke();
      for (let k = 0; k < 4; k++) {
        const p = ((t * 0.35 + k / 4 + hash(i) ) % 1);
        const x = n.x + (cx - n.x) * p, y = n.y + (cy - n.y) * p;
        ctx.fillStyle = `rgba(10,10,11,${0.55 * (1 - p)})`;
        ctx.fillRect(x - 1.5, y - 1.5, 3, 3);
      }
    });
    // Center: the twin
    const R = clamp(ry * 0.28, 20, 56);
    ctx.strokeStyle = "rgba(10,10,11,0.8)"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
    ctx.setLineDash([2, 6]); ctx.lineWidth = 1; ctx.strokeStyle = "rgba(10,10,11,0.4)";
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * 0.15);
    ctx.beginPath(); ctx.arc(0, 0, R * 1.5, 0, Math.PI * 2); ctx.stroke();
    ctx.rotate(-t * 0.35);
    ctx.beginPath(); ctx.arc(0, 0, R * 2.1, 0, Math.PI * 2); ctx.stroke();
    ctx.restore(); ctx.setLineDash([]);
    ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(cx, cy, 3.5, 0, Math.PI * 2); ctx.fill();
    const count = 1204331 + Math.floor(t * 23);
    ctx.font = mono(11);
    const label = `IRRIS · ${count.toLocaleString("en-US")} records`;
    const lw = ctx.measureText(label).width + 12;
    chip(ctx, cx - lw / 2, cy - R * 2.1 - 10, label, C.lime, C.ink, 11);

    // Source nodes
    nodes.forEach((n, i) => {
      ctx.fillStyle = i === hot ? C.ink : C.paper;
      ctx.strokeStyle = C.ink; ctx.lineWidth = 1.5;
      ctx.fillRect(n.x - 5, n.y - 5, 10, 10); ctx.strokeRect(n.x - 5, n.y - 5, 10, 10);
      ctx.font = mono(small ? 10 : 12); ctx.fillStyle = i === hot ? C.ink : "rgba(10,10,11,0.62)";
      ctx.textBaseline = "middle";
      const tw = ctx.measureText(n.name).width;
      const right = Math.cos(n.a) >= -0.05;
      let x = right ? n.x + 14 : n.x - 14 - tw;
      x = clamp(x, 8, w - 8 - tw);
      const y = n.y + (Math.abs(Math.cos(n.a)) < 0.3 ? (Math.sin(n.a) < 0 ? -16 : 18) : 0);
      ctx.fillText(n.name, Math.abs(Math.cos(n.a)) < 0.3 ? clamp(n.x - tw / 2, 8, w - 8 - tw) : x, y);
    });

    // The answer that cites it
    const cite = `cites ${CITES[hot % CITES.length]}`;
    ctx.font = mono(11); const cw = ctx.measureText(cite).width + 12;
    const local = (t % 2.6) / 2.6;
    ctx.globalAlpha = reduce ? 1 : clamp(Math.min(local * 6, (1 - local) * 6), 0, 1);
    chip(ctx, cx - cw / 2, cy + R * 2.1 + 26, cite, C.ink, C.paper, 11);
    ctx.globalAlpha = 1;
  }

  /* ---------- Scene: drifting data captured in brackets (standard page) ---------- */
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

  const SCENES = { line: line(), quiet: line({ labels: false, speed: 1.1 }), gauge, knows, tokens };
  document.querySelectorAll("canvas[data-scene]").forEach((c) => SCENES[c.dataset.scene] && mount(c, SCENES[c.dataset.scene]));

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
