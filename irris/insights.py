"""Numbers: aggregation over records (the assistant's `stats` tool) and insights that profile readings.

The insights job looks at each measured series (a subject, or a title, plus its unit) over the last week and writes
`insight` records for what a good engineer would flag: readings outside their limits, the latest value far from
normal (z-score), and drift (the recent level moved away from the earlier one). Insights are upserted with stable
ids, and ones that no longer hold are removed, so the list stays current instead of growing.
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time

from irris import irf, jobs, store
from irris.db import Database, loads, now

WINDOW_HOURS = 168
MIN_POINTS = 8


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            x = float(v.strip())
            return x if math.isfinite(x) else None
        except ValueError:
            return None
    return None


def _field(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _series_key(r: dict, data: dict, group_by: str) -> str:
    if group_by == "subject":
        return r["subject"] or r["title"] or r["source"]
    if group_by == "title":
        return r["title"] or r["subject"] or r["source"]
    if group_by == "source":
        return r["source"]
    if group_by == "none":
        return "all"
    v = _field(data, group_by)
    return str(v) if v is not None else "(none)"


def trend_per_hour(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    xs = [(t - points[0][0]) / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def aggregate(db: Database, workspace_id: str, *, type: str = "reading", subject: str = "", source: str = "",
              field: str = "value", hours: float | None = None, group_by: str = "subject", contains: str = "",
              limit: int = 50_000) -> dict:
    """count / mean / min / max / std / latest / trend of a numeric field, per group."""
    where, params = ["workspace_id = ?"], [workspace_id]
    if type:
        where.append("type = ?")
        params.append(type)
    if subject:
        where.append("subject = ?")
        params.append(subject)
    if source:
        where.append("(source = ? OR source LIKE ?)")
        params += [source, source.rstrip("/") + "/%"]
    if hours:
        where.append("time >= ?")
        params.append(now() - float(hours) * 3600)
    rows = db.execute(f"SELECT subject, title, source, time, data, units FROM records WHERE {' AND '.join(where)} "
                      f"ORDER BY time LIMIT ?", [*params, limit])
    groups: dict[str, dict] = {}
    skipped = 0
    for r in rows:
        if contains and contains.lower() not in (r["title"] + " " + r["subject"]).lower():
            continue
        data = loads(r["data"], {})
        v = _num(_field(data, field))
        if v is None:
            skipped += 1
            continue
        key = _series_key(r, data, group_by)
        g = groups.setdefault(key, {"points": [], "unit": "", "out_of_limits": 0})
        g["points"].append((float(r["time"]), v))
        g["unit"] = g["unit"] or str(data.get("unit") or loads(r["units"], {}).get(field) or "")
        lo, hi = _num(data.get("min")), _num(data.get("max"))
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            g["out_of_limits"] += 1
    out = []
    for key, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["points"])):
        vals = [v for _, v in g["points"]]
        last_t, last_v = g["points"][-1]
        tr = trend_per_hour(g["points"])
        out.append({"group": key, "unit": g["unit"], "count": len(vals), "mean": round(statistics.fmean(vals), 4),
                    "min": min(vals), "max": max(vals),
                    "std": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
                    "latest": last_v, "latest_time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(last_t)),
                    "first_time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(g["points"][0][0])),
                    "trend_per_hour": round(tr, 5) if tr is not None else None,
                    "out_of_limits": g["out_of_limits"]})
    return {"field": field, "group_by": group_by, "groups": out[:100], "records": len(rows),
            "without_a_number": skipped}


def profile(points: list[tuple[float, float]], lo: float | None = None, hi: float | None = None) -> list[dict]:
    """What's worth flagging about one series. Returns findings (kind, severity, numbers)."""
    vals = [v for _, v in points]
    found = []
    if lo is not None or hi is not None:
        bad = [v for v in vals if (lo is not None and v < lo) or (hi is not None and v > hi)]
        if bad:
            found.append({"kind": "out_of_limits", "severity": "high" if len(bad) > 2 or (
                hi is not None and vals[-1] > hi) or (lo is not None and vals[-1] < lo) else "medium",
                "count": len(bad), "of": len(vals), "min": lo, "max": hi, "latest": vals[-1]})
    if len(vals) >= MIN_POINTS:
        base = vals[:-1]
        mean, sd = statistics.fmean(base), statistics.pstdev(base)
        if sd > 0:
            z = (vals[-1] - mean) / sd
            if abs(z) >= 3:
                found.append({"kind": "anomaly", "severity": "high" if abs(z) >= 4 else "medium", "z": round(z, 2),
                              "latest": vals[-1], "mean": round(mean, 4), "std": round(sd, 4)})
        cut = int(len(vals) * 0.75)
        early, late = vals[:cut], vals[cut:]
        if len(early) >= 5 and len(late) >= 3:
            sd_e = statistics.pstdev(early)
            shift = statistics.fmean(late) - statistics.fmean(early)
            if sd_e > 0 and abs(shift) / sd_e >= 1.5:
                found.append({"kind": "drift", "severity": "medium", "shift": round(shift, 4),
                              "sigma": round(abs(shift) / sd_e, 2), "from": round(statistics.fmean(early), 4),
                              "to": round(statistics.fmean(late), 4)})
    return found


def _sentence(key: str, unit: str, f: dict) -> str:
    u = f" {unit}" if unit else ""
    if f["kind"] == "out_of_limits":
        lim = " to ".join(str(x) for x in (f.get("min"), f.get("max")) if x is not None)
        return (f"{key}: {f['count']} of {f['of']} readings outside the limits ({lim}{u}) this week; "
                f"latest {f['latest']:g}{u}.")
    if f["kind"] == "anomaly":
        return (f"{key}: the latest reading, {f['latest']:g}{u}, is {abs(f['z']):.1f} standard deviations "
                f"{'above' if f['z'] > 0 else 'below'} normal (mean {f['mean']:g}{u}).")
    return f"{key}: drifting {'up' if f['shift'] > 0 else 'down'} from {f['from']:g}{u} to {f['to']:g}{u} ({f['sigma']:.1f}σ)."


def run_insights(db: Database, workspace_id: str) -> dict:
    since = now() - WINDOW_HOURS * 3600
    rows = db.execute("SELECT subject, title, source, time, data, units FROM records WHERE workspace_id = ? AND "
                      "type = 'reading' AND time >= ? ORDER BY time LIMIT 100000", [workspace_id, since])
    series: dict[tuple, dict] = {}
    for r in rows:
        data = loads(r["data"], {})
        v = _num(data.get("value"))
        if v is None:
            continue
        unit = str(data.get("unit") or loads(r["units"], {}).get("value") or "")
        key = (r["subject"] or r["title"] or r["source"], unit)
        s = series.setdefault(key, {"points": [], "min": None, "max": None})
        s["points"].append((float(r["time"]), v))
        lo, hi = _num(data.get("min")), _num(data.get("max"))
        s["min"] = lo if lo is not None else s["min"]
        s["max"] = hi if hi is not None else s["max"]
    recs, keep = [], set()
    for (key, unit), s in series.items():
        for f in profile(s["points"], s["min"], s["max"]):
            ext = f"{key}|{unit}|{f['kind']}"[:280]
            keep.add(ext)
            recs.append(irf.normalize({"type": "insight", "source": "insights", "id": ext, "subject": key,
                                       "title": f"{key}: {f['kind'].replace('_', ' ')}",
                                       "text": _sentence(key, unit, f), "time": s["points"][-1][0],
                                       "data": {**f, "series": key, "unit": unit, "points": len(s["points"]),
                                                "window_hours": WINDOW_HOURS}}, now=now()))
    if recs:
        store.write(db, workspace_id, recs)
    stale = [r["id"] for r in db.execute("SELECT id, ext_id FROM records WHERE workspace_id = ? AND type = 'insight' "
                                          "AND source = 'insights'", [workspace_id]) if r["ext_id"] not in keep]
    for rid in stale:
        store.delete(db, workspace_id, rid)
    return {"series": len(series), "insights": len(recs), "cleared": len(stale)}


def insights(db: Database, workspace_id: str, limit: int = 50) -> list[dict]:
    rows = db.execute("SELECT * FROM records WHERE workspace_id = ? AND type = 'insight' ORDER BY time DESC LIMIT ?",
                      [workspace_id, limit])
    order = {"high": 0, "medium": 1, "low": 2}
    out = [{"id": r["id"], "title": r["title"], "text": r["text"], "subject": r["subject"], "time": r["time"],
            **{k: v for k, v in loads(r["data"], {}).items() if k in ("kind", "severity", "unit", "latest", "z",
                                                                      "count", "of")}} for r in rows]
    return sorted(out, key=lambda i: (order.get(i.get("severity"), 3), -i["time"]))


@jobs.handler("insights")
async def insights_job(ctx: jobs.JobCtx) -> dict:
    return await asyncio.to_thread(run_insights, ctx.db, ctx.workspace_id)


@jobs.handler("embed")
async def embed_job(ctx: jobs.JobCtx) -> dict:
    from irris import embed
    if embed.get() is None:
        return {"skipped": "embeddings are off"}
    total = done = 0
    left = await asyncio.to_thread(embed.pending, ctx.db, ctx.workspace_id)
    while True:
        n = await asyncio.to_thread(embed.embed_missing, ctx.db, ctx.workspace_id, 256)
        if not n:
            n = await asyncio.to_thread(embed.embed_missing, ctx.db, ctx.workspace_id, 256)   # writes that landed meanwhile
            if not n:
                break
        done += n
        total = max(total, left)
        await ctx.progress(min(0.99, done / max(total, 1)), f"Understood {done:,} of {total:,} passages", save=False)
    return {"embedded": done}
