"""Localhost page for account usage, the forecast decision, and token totals.

Reads the usage store, the forecast sample ring, the auto-switch log, and
Claude Code's own session logs. It does not call Anthropic. Window percents
come from the usage store; token counts come from the session logs, which
record every model call with its usage and the organization signed in.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from claude_swap import allocation, forecast, tokens
from claude_swap.settings import load_settings
from claude_swap.switcher import ClaudeAccountSwitcher

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_RESCAN_S = 20.0
HISTORY_KEEP_S = 8 * 24 * 3600.0
PLAN_MEDIAN_MIN_POINTS = 25.0
LOOKBACKS_S = (3600.0, 5 * 3600.0, 24 * 3600.0, 7 * 24 * 3600.0)

TIER_LABELS = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_max": "Max",
    "default_claude_pro": "Pro",
}

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude usage</title>
<style>
  :root { color-scheme: dark; --bg: #121410; --panel: #1b1e18; --line: #2b2f27; --text: #e8e3d7; --muted: #a09b8d;
          --ok: #7d9a62; --warn: #c9a45a; --bad: #c45c4a; --opus: #8fb0d9; --sonnet: #d7a46a; --fable: #b48ead; --other: #8a8f85; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }
  main { max-width: 1180px; margin: 0 auto; padding: 28px 20px 64px; }
  h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  h2 { font-size: 14px; font-weight: 600; margin: 30px 0 10px; color: var(--muted); letter-spacing: 0.04em; text-transform: uppercase; }
  p, .muted { color: var(--muted); }
  .row { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
  .card { background: var(--panel); border-radius: 10px; padding: 14px 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  .card .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .decision { margin-top: 14px; padding: 12px 14px; background: var(--panel); border-radius: 10px; border-left: 3px solid var(--ok); }
  .decision.leave { border-left-color: var(--warn); }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
  th, td { padding: 8px 10px 8px 0; vertical-align: middle; border-bottom: 1px solid var(--line); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.active td { background: #1c2418; }
  .bar { height: 8px; width: 96px; background: var(--line); border-radius: 99px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }
  .bar > span { display: block; height: 100%; background: var(--ok); }
  .bar.high > span { background: var(--warn); }
  .bar.full > span { background: var(--bad); }
  .bar.pending { background: transparent; border: 1px dashed var(--line); box-sizing: border-box; }
  .bar.pending > span { width: 0; }
  svg.chart { width: 100%; height: 220px; background: var(--panel); border-radius: 10px; display: block; }
  .legend { display: flex; gap: 16px; margin: 8px 0 0; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
  .legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: middle; background: var(--c); }
  .tabs { display: inline-flex; gap: 4px; background: var(--panel); border-radius: 8px; padding: 3px; margin-bottom: 10px; }
  .tabs button { background: transparent; border: 0; color: var(--muted); padding: 5px 10px; border-radius: 6px; cursor: pointer; font: inherit; font-size: 13px; }
  .tabs button.on { background: #2a2f26; color: var(--text); }
  .log { font-family: ui-monospace, monospace; font-size: 12px; white-space: pre-wrap; color: #cfc8b8; background: var(--panel); border-radius: 10px; padding: 12px 14px; max-height: 260px; overflow: auto; }
  .axis text { fill: var(--muted); font-size: 11px; }
  .axis line { stroke: var(--line); }
  .card.ready { border-left: 3px solid var(--ok); }
  .card.waiting { border-left: 3px solid var(--warn); }
  .card.none { border-left: 3px solid var(--bad); }
  .card .value.small { font-size: 18px; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  th.sorted::after { content: " ↓"; }
  .pill { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 12px; background: var(--line); color: var(--muted); margin: 2px 6px 0 0; white-space: nowrap; }
  .pill.ok { color: var(--ok); }
  .pill.bad { color: var(--bad); }
  .nowrap { white-space: nowrap; }
  td.who { min-width: 260px; }
  td.who .pill { margin-left: 0; }
  .hero { display: grid; gap: 14px; grid-template-columns: 2fr 1fr 1fr 1fr; margin-top: 14px; }
  .hero .card .value { font-size: 20px; }
  .reset-list td { padding: 6px 10px 6px 0; }
  @media (max-width: 900px) { .hero { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<main>
  <h1>Claude usage</h1>
  <p id="subtitle">Reading local files. This page does not call Anthropic.</p>
  <div class="hero" id="hero"></div>
  <div class="decision" id="decision">Loading…</div>

  <h2>Next available</h2>
  <div class="row" id="next"></div>

  <h2>Tokens <span class="tabs" id="windowTabs" style="margin-left:10px; text-transform:none; letter-spacing:0"></span></h2>
  <div class="row" id="totals"></div>

  <h2>Tokens per hour, by model</h2>
  <svg class="chart" id="hourly" viewBox="0 0 1180 220" preserveAspectRatio="none"></svg>
  <div class="legend" id="hourlyLegend"></div>

  <h2>Accounts <span class="tabs" id="sortTabs" style="margin-left:10px; text-transform:none; letter-spacing:0"></span></h2>
  <p class="muted" id="sortHint"></p>
  <table id="accounts"></table>

  <h2>What a percent is worth</h2>
  <p class="muted" id="roomHint"></p>
  <div class="row" id="plans"></div>
  <table id="room" style="margin-top:14px"></table>

  <h2>Resets seen</h2>
  <p class="muted">A window that dropped by 20 points or more between two readings. The provider's own reset time is shown beside each; a drop well before it is an early reset or a quota grant.</p>
  <table class="reset-list" id="resets"></table>

  <h2>Active account, last five hours</h2>
  <svg class="chart" id="samples" viewBox="0 0 1180 220" preserveAspectRatio="none"></svg>
  <div class="legend"><span style="--c: var(--ok)">Session window</span><span style="--c: var(--opus)">Weekly window</span><span style="--c: var(--fable)">Fable week</span><span style="--c: var(--bad)">Leave line</span></div>

  <h2>Usage lookups</h2>
  <p>These are cswap's own checks of the usage endpoint, at most about 30 an hour per account. They are not model requests and are not your Claude quota. "Lookup throttled" means the endpoint answered one of those checks with a 429 within the last hour, so cswap slowed its polling on that account.</p>
  <table id="checks"></table>

  <h2>Rotator log</h2>
  <div class="log" id="log"></div>
</main>
<script>
const WINDOWS = [["1h", 3600], ["5h", 5 * 3600], ["24h", 24 * 3600], ["7d", 7 * 24 * 3600]];
let windowSeconds = 24 * 3600;
const SORTS = [
  ["slot", "Default"],
  ["fable", "Fable room"],
  ["opus", "Opus room"],
  ["any", "Any room"],
  ["soonest", "Unblocks soonest"],
  ["tokens", "Tokens"],
];
let sortKey = localStorage.getItem("cswap.sort") || "slot";
const pct = (w) => (w && w.pct != null) ? w.pct : null;
const resetMs = (w) => (w && w.resetsAt) ? Date.parse(w.resetsAt) : null;
// A reading taken before its own window's published reset describes a window
// that no longer exists: 94% read at 1:48 for a window that reset at 1:50 is
// 0% now. Settle such windows before anything renders, so room, sorting and
// the next-available cards treat them as reset, and the table says the row is
// waiting on a fresh read instead of showing the old bar next to "in now".
const settleResets = (a) => {
  for (const key of ["session", "weekly", "fable"]) {
    const w = a[key];
    const at = resetMs(w);
    if (w && at != null && a.readAt != null && a.readAt * 1000 < at && at <= Date.now()) {
      a[key] = { pct: 0, resetsAt: null, resetAt: w.resetsAt, awaitingRead: true };
    }
  }
  return a;
};
// Room for the shared windows Opus draws from: session and weekly. 0 when either is full.
const opusRoom = (a) => {
  const s = pct(a.session), w = pct(a.weekly);
  if (s == null && w == null) return null;
  return Math.max(0, 100 - Math.max(s ?? 0, w ?? 0));
};
// Fable also needs its own week open, and the switcher treats >80% as used up.
// A refusal from the API in the last hour overrides whatever the window says.
const fableRefused = (a) => a.fableRefusedAt != null && Date.now() / 1000 - a.fableRefusedAt < 3600;
const fableRoom = (a) => {
  const f = pct(a.fable);
  if (f == null) return null;
  if (fableRefused(a)) return 0;
  if (f > 80) return 0;
  const shared = opusRoom(a);
  return shared == null ? 100 - f : Math.min(100 - f, shared);
};
// When an account can next take a request for the given model: now if it has room, else its latest blocking reset.
const availableAt = (a, model) => {
  const room = model === "fable" ? fableRoom(a) : opusRoom(a);
  if (room == null) return null;
  if (room > 0) return 0;
  const blocking = [];
  if ((pct(a.session) ?? 0) >= 100) blocking.push(resetMs(a.session));
  if ((pct(a.weekly) ?? 0) >= 100) blocking.push(resetMs(a.weekly));
  if (model === "fable" && (pct(a.fable) ?? 0) > 80) blocking.push(resetMs(a.fable));
  const known = blocking.filter(t => t != null);
  return known.length ? Math.max(...known) : null;
};
const untilText = (ms) => {
  if (ms == null) return "unknown";
  const diff = ms - Date.now();
  if (diff <= 0) return "now";
  const totalMinutes = Math.round(diff / 60000);
  if (totalMinutes < 60) return totalMinutes + " min";
  const h = Math.floor(totalMinutes / 60), m = totalMinutes % 60;
  if (h < 48) return m ? `${h}h ${m}m` : `${h}h`;
  const d = Math.floor(h / 24), hh = h % 24;
  return hh ? `${d}d ${hh}h` : `${d}d`;
};
// The reset that unblocks an account: the latest of its full windows. Null when nothing is full.
const blockedUntil = (a) => {
  const full = [a.session, a.weekly, a.fable].filter(w => (pct(w) ?? 0) >= 100).map(resetMs).filter(t => t != null);
  return full.length ? Math.max(...full) : null;
};
const MODEL_COLOR = (name) => {
  const n = (name || "").toLowerCase();
  if (n.includes("opus")) return "var(--opus)";
  if (n.includes("sonnet")) return "var(--sonnet)";
  if (n.includes("fable")) return "var(--fable)";
  return "var(--other)";
};
const shortModel = (name) => (name || "").replace(/^claude-/, "").replace(/-\\d{8}$/, "");
const fmt = (n) => {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
};
const bar = (pct) => {
  if (pct == null) return "—";
  const kind = pct >= 92 ? "full" : pct >= 70 ? "high" : "";
  return `<span class="bar ${kind}"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></span>${Math.round(pct)}%`;
};
const TIME = { hour: "numeric", minute: "2-digit", hour12: true };
const clock = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  const time = d.toLocaleTimeString("en-US", TIME);
  if (sameDay) return `today ${time}`;
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) + " " + time;
};
const timeOnly = (ms) => new Date(ms).toLocaleTimeString("en-US", TIME);
const minutesText = (m) => {
  if (m == null) return "—";
  if (m < 1) return "under a minute";
  if (m < 90) return Math.round(m) + " min";
  if (m < 48 * 60) return (m / 60).toFixed(1) + " h";
  return (m / 1440).toFixed(1) + " days";
};
const who = (a) => a ? a.name || a.email : "";
const resetCell = (w) => {
  const iso = w && w.resetsAt;
  if (!iso) return "";
  return `${clock(iso)} <span class="muted">· in ${untilText(Date.parse(iso))}</span>`;
};
const age = (s) => s == null ? "" : s < 90 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago" : (s / 3600).toFixed(1) + "h ago";
const esc = (text) => String(text == null ? "" : text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function axisLeft(max, height, pad) {
  const steps = 4;
  let out = "";
  for (let i = 0; i <= steps; i++) {
    const v = (max / steps) * i;
    const y = height - pad - (v / max) * (height - pad * 2);
    out += `<line x1="60" x2="1170" y1="${y}" y2="${y}" />`;
    out += `<text x="54" y="${y + 4}" text-anchor="end">${fmt(v)}</text>`;
  }
  return `<g class="axis">${out}</g>`;
}

function hourlyChart(hours) {
  const svg = document.getElementById("hourly");
  const legend = document.getElementById("hourlyLegend");
  if (!hours.length) { svg.innerHTML = ""; legend.innerHTML = ""; return; }
  const models = [...new Set(hours.flatMap(h => Object.keys(h.models)))].sort();
  const max = Math.max(1, ...hours.map(h => Object.values(h.models).reduce((a, m) => a + m.total, 0)));
  const height = 220, pad = 24, left = 60, right = 10;
  const width = (1180 - left - right) / hours.length;
  let bars = "";
  hours.forEach((h, i) => {
    let y = height - pad;
    models.forEach(m => {
      const v = (h.models[m] || {}).total || 0;
      if (!v) return;
      const hgt = (v / max) * (height - pad * 2);
      y -= hgt;
      bars += `<rect x="${left + i * width + 1}" y="${y}" width="${Math.max(1, width - 2)}" height="${hgt}" fill="${MODEL_COLOR(m)}"><title>${clock(new Date(h.at * 1000).toISOString())} · ${shortModel(m)} · ${fmt(v)} tokens</title></rect>`;
    });
  });
  let labels = "";
  const every = Math.max(1, Math.round(hours.length / 8));
  hours.forEach((h, i) => {
    if (i % every) return;
    const d = new Date(h.at * 1000);
    const hourText = d.toLocaleTimeString("en-US", { hour: "numeric", hour12: true });
    const text = hours.length > 30 ? d.toLocaleDateString("en-US", { weekday: "short" }) + " " + hourText : hourText;
    labels += `<text x="${left + i * width + width / 2}" y="${height - 6}" text-anchor="middle">${text}</text>`;
  });
  svg.innerHTML = axisLeft(max, height, pad) + bars + `<g class="axis">${labels}</g>`;
  legend.innerHTML = models.map(m => `<span style="--c: ${MODEL_COLOR(m)}">${esc(shortModel(m))}</span>`).join("");
}

// Five hours of the active account's windows from the reading history. A
// drop is a reset, so the line breaks there instead of drawing a cliff.
function samplesChart(points) {
  const svg = document.getElementById("samples");
  if (points.length < 2) { svg.innerHTML = `<text class="axis" x="70" y="110" fill="#a09b8d">Waiting for readings of the active account.</text>`; return; }
  const height = 220, pad = 24, left = 60, right = 10;
  const now = Date.now() / 1000;
  const min = Math.min(now - 5 * 3600, ...points.map(p => p.at)), max = now, span = Math.max(1, max - min);
  const x = (t) => left + ((t - min) / span) * (1180 - left - right);
  const y = (p) => height - pad - (p / 100) * (height - pad * 2);
  const line = (key, color) => {
    let out = "", run = [];
    const flush = () => { if (run.length > 1) out += `<polyline fill="none" stroke="${color}" stroke-width="2.5" points="${run.join(" ")}" />`; run = []; };
    let last = null;
    for (const p of points) {
      if (p[key] == null) continue;
      if (last != null && p[key] < last - 20) flush();
      run.push(`${x(p.at).toFixed(1)},${y(p[key]).toFixed(1)}`);
      last = p[key];
    }
    flush();
    return out;
  };
  let grid = "";
  [0, 25, 50, 75, 100].forEach(p => { grid += `<line x1="${left}" x2="1170" y1="${y(p)}" y2="${y(p)}" /><text x="54" y="${y(p) + 4}" text-anchor="end">${p}%</text>`; });
  const leave = `<line x1="${left}" x2="1170" y1="${y(92)}" y2="${y(92)}" stroke="var(--bad)" stroke-dasharray="4 4" />`;
  let times = "";
  for (let t = Math.ceil(min / 3600) * 3600; t <= max; t += 3600) times += `<text x="${x(t)}" y="${height - 6}" text-anchor="middle">${new Date(t * 1000).toLocaleTimeString("en-US", { hour: "numeric", hour12: true })}</text>`;
  svg.innerHTML = `<g class="axis">${grid}${times}</g>` + leave + line("h5", "var(--ok)") + line("d7", "var(--opus)") + line("fable", "var(--fable)");
}

function renderHero(state) {
  const active = state.accounts.find(a => a.active);
  const box = document.getElementById("hero");
  if (!active) { box.innerHTML = `<div class="card"><div class="label">Current account</div><div class="value">none</div></div>`; return; }
  const w = (win, label) => !win
    ? `<div class="card"><div class="label">${label}</div><div class="value small">not on this plan</div></div>`
    : win.awaitingRead
      ? `<div class="card"><div class="label">${label}</div><div class="value small">reset ${clock(win.resetAt)}</div><div class="sub nowrap">awaiting a fresh read</div></div>`
      : `<div class="card"><div class="label">${label}</div><div class="value">${Math.round(pct(win))}% used</div><div class="sub nowrap">resets ${clock(win.resetsAt)}</div></div>`;
  box.innerHTML = `<div class="card ready"><div class="label">Current account</div><div class="value">${esc(who(active))}</div><div class="sub">${esc(active.tierLabel || "plan unknown")} · Fable ${minutesFor(active, "fable") != null ? minutesText(minutesFor(active, "fable")) : (fableRoom(active) > 0 ? Math.round(fableRoom(active)) + "%" : "full")} · Opus ${minutesFor(active, "opus") != null ? minutesText(minutesFor(active, "opus")) : (opusRoom(active) > 0 ? Math.round(opusRoom(active)) + "%" : "full")} left at your pace</div></div>` +
    w(active.session, "Session") + w(active.weekly, "Weekly") + w(active.fable, "Fable week");
}

function renderResets(state) {
  const byNumber = Object.fromEntries(state.accounts.map(a => [a.number, a]));
  const label = { h5: "session", d7: "weekly", fable: "Fable week" };
  const published = (a, key) => a ? ({ h5: a.session, d7: a.weekly, fable: a.fable }[key] || {}).resetsAt : null;
  const verdict = (a, r) => {
    const dropMs = r.at * 1000;
    if (r.scheduledAt) {
      // The reset published before the drop. Exact for any window.
      const gap = Date.parse(r.scheduledAt) - dropMs;
      if (gap > 10 * 60 * 1000) return `was due ${clock(r.scheduledAt)}, so this came ${untilText(Date.now() + gap)} early`;
      return "on schedule";
    }
    // Backfilled readings carry no published time. Weekly and Fable resets
    // land on a fixed weekday, so the current published time still tells:
    // a scheduled reset would have moved it a full week out.
    if (r.window === "h5") return "";
    const current = published(a, r.window);
    if (!current) return "";
    const gap = Date.parse(current) - dropMs;
    if (gap > 2 * 3600 * 1000 && gap < 6.5 * 24 * 3600 * 1000) return `published reset is ${clock(current)}, so this came early`;
    return "";
  };
  const rows = (state.resets || []).map(r => {
    const a = byNumber[r.slot];
    return `<tr><td class="nowrap">${clock(new Date(r.at * 1000).toISOString())}</td><td>${esc(who(a) || "removed account")}</td><td>${label[r.window]}</td><td class="num nowrap">${Math.round(r.fromPct)}% → ${Math.round(r.toPct)}%</td><td class="muted">${verdict(a, r)}</td></tr>`;
  });
  document.getElementById("resets").innerHTML = rows.length
    ? `<tr><th>When</th><th>Account</th><th>Window</th><th class="num">Drop</th><th></th></tr>` + rows.join("")
    : `<tr><td class="muted">No resets in the selected lookback.</td></tr>`;
}

function renderPlans(plans) {
  const box = document.getElementById("plans");
  if (!plans || !plans.length) { box.innerHTML = ""; return; }
  const cell = (p, key, label) => p[key] != null ? `${label} ${fmt(p[key])} <span class="muted">(${p[key + "Accounts"]} acct)</span>` : "";
  box.innerHTML = plans.map(p => `<div class="card"><div class="label">${esc(p.plan)} · tokens per 1%</div><div class="value small">${[cell(p, "h5", "session"), cell(p, "d7", "weekly"), cell(p, "fable", "Fable")].filter(Boolean).join("<br>")}</div><div class="sub">median of accounts that climbed at least 25 points</div></div>`).join("");
}

function render(state) {
  state.accounts.forEach(settleResets);
  const tokensNow = state.tokens;
  document.getElementById("subtitle").textContent =
    `Strategy ${state.strategy || "—"} · model ${state.model || "session and weekly only"} · tokens from ${tokensNow.files} session logs, rescanned ${age(tokensNow.ageSeconds)}.`;
  const d = state.decision;
  const box = document.getElementById("decision");
  box.className = "decision" + (d && d.switchTo ? " leave" : "");
  const target = d && d.switchTo ? state.accounts.find(a => a.number === d.switchTo) : null;
  const active = state.accounts.find(a => a.active);
  box.textContent = d
    ? `${active ? "On " + who(active) + ". " : ""}${d.switchTo ? `Rotator wants ${who(target) || "slot " + d.switchTo}. ` : "Staying. "}${d.detail.replace(/account (\\d+)/g, (m, n) => who(state.accounts.find(a => a.number === n)) || m)}`
    : "No forecast.";

  document.getElementById("windowTabs").innerHTML = WINDOWS.map(([label, s]) => `<button class="${s === windowSeconds ? "on" : ""}" data-s="${s}">${label}</button>`).join("");
  document.querySelectorAll("#windowTabs button").forEach(b => b.onclick = () => { windowSeconds = Number(b.dataset.s); tick(); });

  const t = tokensNow.total;
  const byModel = Object.entries(tokensNow.byModel).sort((a, b) => b[1].total - a[1].total);
  document.getElementById("totals").innerHTML = `
    <div class="card"><div class="label">All tokens</div><div class="value">${fmt(t.total)}</div><div class="sub">${fmt(t.calls)} model calls</div></div>
    <div class="card"><div class="label">Output</div><div class="value">${fmt(t.output)}</div><div class="sub">what the models wrote</div></div>
    <div class="card"><div class="label">Fresh input</div><div class="value">${fmt(t.input + t.cacheCreation)}</div><div class="sub">${fmt(t.cacheCreation)} written to cache</div></div>
    <div class="card"><div class="label">Cache reads</div><div class="value">${fmt(t.cacheRead)}</div><div class="sub">${t.total ? Math.round(100 * t.cacheRead / t.total) : 0}% of all tokens</div></div>` +
    byModel.map(([m, v]) => `<div class="card"><div class="label" style="color:${MODEL_COLOR(m)}">${esc(shortModel(m))}</div><div class="value">${fmt(v.total)}</div><div class="sub">${fmt(v.calls)} calls · ${fmt(v.output)} out</div></div>`).join("");

  hourlyChart(tokensNow.hours);

  renderHero(state);
  renderNext(state.accounts, state.demand);
  renderAccounts(state.accounts, tokensNow);
  renderPlans(state.plans);
  renderRoom(state.accounts, state.lookbackSeconds);
  renderResets(state);
  samplesChart(state.activeHistory || []);

  document.getElementById("checks").innerHTML = state.accounts.map(a => `<tr><td style="width:42%" class="muted">${esc(who(a))}</td><td>${a.pollIntervalSeconds != null ? "every " + Math.round(a.pollIntervalSeconds) + "s" : "no plan"}${a.consecutiveFailures ? " · " + a.consecutiveFailures + " failed checks in a row" : ""}${a.recent429 ? ` <span class="pill">lookup throttled</span>` : ""}</td></tr>`).join("");
  const named = (line) => line.replace(/Account-(\\d+)/g, (m, n) => { const a = state.accounts.find(x => x.number === n); return a ? who(a) : m; }).replace(/account (\\d+)/g, (m, n) => { const a = state.accounts.find(x => x.number === n); return a ? who(a) : m; });
  document.getElementById("log").textContent = (state.log || []).map(named).join("\\n");
}

// Minutes your current demand would last on this account for the model. Null when unmeasured.
const USEFUL_MINUTES = 10;
const minutesFor = (a, model) => a.room ? (model === "fable" ? a.room.minutesForFable : a.room.minutesForOpus) : null;
const tokensFor = (a, model) => {
  if (!a.room) return null;
  const keys = model === "fable" ? ["h5", "d7", "fable"] : ["h5", "d7"];
  const known = keys.map(k => a.room.windows[k] && a.room.windows[k].tokensLeft).filter(v => v != null);
  return known.length ? Math.min(...known) : null;
};
// Open means the windows have room AND that room is worth moving for: at least USEFUL_MINUTES at your current demand when measured.
const useful = (a, model) => {
  const room = model === "fable" ? fableRoom(a) : opusRoom(a);
  if (!(room > 0)) return false;
  const minutes = minutesFor(a, model);
  return minutes == null ? true : minutes >= USEFUL_MINUTES;
};
const roomText = (a, model) => {
  const minutes = minutesFor(a, model), tok = tokensFor(a, model);
  const parts = [];
  if (minutes != null) parts.push(`about ${minutesText(minutes)} at your current pace`);
  if (tok != null) parts.push(`${fmt(tok)} tokens`);
  if (!parts.length) parts.push(`${Math.round(model === "fable" ? fableRoom(a) : opusRoom(a))}% of the window`);
  return parts.join(" · ");
};
function nextFor(accounts, model) {
  const score = (a) => minutesFor(a, model) ?? ((model === "fable" ? fableRoom(a) : opusRoom(a)) ?? 0);
  const open = accounts.filter(a => useful(a, model)).sort((a, b) => score(b) - score(a));
  if (open.length) return { account: open[0], at: 0 };
  const waiting = accounts.map(a => ({ account: a, at: availableAt(a, model) })).filter(x => x.at != null && x.at > 0).sort((a, b) => a.at - b.at);
  return waiting[0] || null;
}
function renderNext(accounts, demand) {
  const active = accounts.find(a => a.active);
  const cards = [["Fable 5.1", "fable"], ["Opus 5.5", "opus"]].map(([label, model]) => {
    const activeOpen = active && useful(active, model);
    const next = nextFor(accounts, model);
    if (activeOpen) {
      const better = next && next.at === 0 && next.account.number !== active.number && (minutesFor(next.account, model) ?? 0) > (minutesFor(active, model) ?? 0) * 2;
      return `<div class="card ready"><div class="label">${label}</div><div class="value small">Open now on ${esc(who(active))}</div><div class="sub">${roomText(active, model)}${better ? `<br>Most room: ${esc(who(next.account))}, ${roomText(next.account, model)}` : ""}</div></div>`;
    }
    const activeNote = active && (model === "fable" ? fableRoom(active) : opusRoom(active)) > 0 && !useful(active, model)
      ? `This account has ${Math.round(model === "fable" ? fableRoom(active) : opusRoom(active))}% left, under ${USEFUL_MINUTES} minutes at your pace. `
      : "";
    if (!next) {
      return `<div class="card none"><div class="label">${label}</div><div class="value small">No account can take ${label} right now</div><div class="sub">${activeNote}No reset time is known yet.</div></div>`;
    }
    if (next.at === 0) {
      return `<div class="card waiting"><div class="label">${label}</div><div class="value small">Limited here · open on ${esc(who(next.account))}</div><div class="sub">${activeNote}${roomText(next.account, model)}</div></div>`;
    }
    return `<div class="card none"><div class="label">${label}</div><div class="value small">Limited everywhere · ${esc(who(next.account))} opens in ${untilText(next.at)}</div><div class="sub">${activeNote}${clock(new Date(next.at).toISOString())}</div></div>`;
  });
  const pace = demand && demand.allTokensPerMinute
    ? `<div class="card"><div class="label">Your pace, last ${Math.round((demand.lookbackSeconds || 1800) / 60)} min</div><div class="value small">${fmt(demand.allTokensPerMinute)} weighted tokens / min</div><div class="sub">${fmt(demand.fableTokensPerMinute)} / min on Fable · ${demand.readings || 0} readings in history</div></div>`
    : "";
  document.getElementById("next").innerHTML = cards.join("") + pace;
}

function sortAccounts(accounts) {
  const rows = [...accounts];
  const desc = (f) => (a, b) => ((f(b) ?? -1) - (f(a) ?? -1)) || (Number(a.number) - Number(b.number));
  if (sortKey === "fable") rows.sort(desc(fableRoom));
  else if (sortKey === "opus") rows.sort(desc(opusRoom));
  else if (sortKey === "any") rows.sort(desc(a => Math.max(fableRoom(a) ?? 0, opusRoom(a) ?? 0)));
  else if (sortKey === "tokens") rows.sort(desc(a => a.tokens ? a.tokens.total : 0));
  else if (sortKey === "soonest") rows.sort((a, b) => {
    // Open accounts first, then blocked ones by the reset that frees them. A
    // session reset does not help an account whose week is full.
    const t = (x) => blockedUntil(x) ?? 0;
    return t(a) - t(b) || Number(a.number) - Number(b.number);
  });
  else rows.sort((a, b) => Number(a.number) - Number(b.number));
  return rows;
}
function renderAccounts(accounts, tokensNow) {
  document.getElementById("sortTabs").innerHTML = SORTS.map(([key, label]) => `<button class="${key === sortKey ? "on" : ""}" data-k="${key}">${label}</button>`).join("");
  document.querySelectorAll("#sortTabs button").forEach(b => b.onclick = () => { sortKey = b.dataset.k; localStorage.setItem("cswap.sort", sortKey); renderAccounts(accounts, tokensNow); });
  document.getElementById("sortHint").textContent = {
    slot: "In the order the accounts were added.",
    fable: "Most Fable room first. Fable room needs its own week under 80% and an open session and weekly window.",
    opus: "Most Opus room first. Opus draws only from the session and the shared weekly window.",
    any: "Most room for either model first.",
    soonest: "Open accounts first, then blocked accounts by the reset that frees them.",
    tokens: "Most tokens in the selected lookback first.",
  }[sortKey];
  const roomPill = (a) => {
    const tag = (label, model) => {
      const v = model === "fable" ? fableRoom(a) : opusRoom(a);
      if (v == null) return "";
      const minutes = minutesFor(a, model);
      const text = v <= 0 ? "full" : minutes != null ? minutesText(minutes) : Math.round(v) + "%";
      return `<span class="pill ${useful(a, model) ? "ok" : "bad"}" title="${roomText(a, model)}">${label} ${text}</span>`;
    };
    return tag("Fable", "fable") + tag("Opus", "opus");
  };
  const byNumber = Object.fromEntries(accounts.map(a => [a.number, a]));
  const sharedNote = (a) => {
    if (!a.tokensSharedWith || !a.tokensSharedWith.length) return "";
    const names = a.tokensSharedWith.map(n => (byNumber[n] || {}).email || n).join(", ");
    return `<span class="pill" title="Claude Code's logs record the organization, not the seat. This is the whole ${esc(a.organization)} organization's total and cannot be split between its seats: ${esc(names)}.">${esc(a.organization)} total</span>`;
  };
  const windowCell = (w, model, a) => {
    if (!w) return `<span class="muted">${model === "fable" ? "not on this plan" : "—"}</span>`;
    if (w.awaitingRead) return `<span class="bar pending"><span></span></span><span class="muted">reset</span><div class="muted nowrap">${clock(w.resetAt)} · awaiting a fresh read</div>`;
    const refused = model === "fable" && a && fableRefused(a)
      ? `<div class="pill bad" title="Claude Code's log shows the API refused Fable on this organization and asked for usage credits. Treated as no Fable room for an hour.">refused ${age(Date.now() / 1000 - a.fableRefusedAt)}</div>`
      : "";
    return `${bar(pct(w))}<div class="muted nowrap">${resetCell(w)}</div>${refused}`;
  };
  document.getElementById("accounts").innerHTML = `<tr><th>Account</th><th>Session</th><th>Weekly</th><th>Fable</th><th class="num">Tokens</th><th class="num">Calls</th><th>Read</th></tr>` +
    sortAccounts(accounts).map(a => `<tr class="${a.active ? "active" : ""}"><td class="who"><div>${esc(who(a))}${a.active ? ' <span class="pill ok">active</span>' : ""}</div><div class="muted">${esc(a.tierLabel || "plan unknown")}</div><div>${roomPill(a)}</div></td><td>${windowCell(a.session)}</td><td>${windowCell(a.weekly)}</td><td>${windowCell(a.fable, "fable", a)}</td><td class="num">${fmt(a.tokens && a.tokens.total)}<div class="muted nowrap">${a.tokens ? fmt(a.tokens.output) + " out" : ""}</div><div>${sharedNote(a)}</div></td><td class="num">${fmt(a.tokens && a.tokens.calls)}</td><td class="muted nowrap">${age(a.readingAgeSeconds)}${a.lastError ? " · " + esc(a.lastError) : ""}</td></tr>`).join("") +
    (tokensNow.unattributed && tokensNow.unattributed.calls ? `<tr><td class="muted">Sessions with no organization marker</td><td></td><td></td><td></td><td class="num">${fmt(tokensNow.unattributed.total)}</td><td class="num">${fmt(tokensNow.unattributed.calls)}</td><td></td></tr>` : "");
}

// What one percent is worth on each account, measured over the selected lookback.
function renderRoom(accounts, lookback) {
  const label = (w) => ({ h5: "Session", d7: "Weekly", fable: "Fable" }[w]);
  const rows = [];
  for (const a of accounts) {
    if (!a.room) continue;
    for (const key of ["h5", "d7", "fable"]) {
      const w = a.room.windows[key];
      if (!w) continue;
      rows.push({ a, key, w });
    }
  }
  const source = (w) => w.tokensPerPointSource === "measured" ? "" : w.tokensPerPointSource === "same-tier" ? " <span class=\\"muted\\">(est. from same plan)</span>" : w.tokensPerPointSource === "any-account" ? " <span class=\\"muted\\">(est. from other accounts)</span>" : "";
  document.getElementById("roomHint").textContent = `Tokens here are cost-weighted (output ×5, cache write ×1.25, cache read ×0.1, in input-token units), which is what the limits track. Tokens per 1% are measured as weighted tokens spent while that window climbed, over the last ${untilText(Date.now() + lookback * 1000)}. Time left divides the remaining weighted tokens by your weighted pace over the last 30 minutes. Climb is how fast the window rose in the lookback.`;
  let lastAccount = null;
  document.getElementById("room").innerHTML = `<tr><th>Account</th><th>Window</th><th class="num">Used</th><th class="num">Climb</th><th class="num">Tokens per 1%</th><th class="num">Tokens left</th><th class="num">Time left</th><th>Measured on</th></tr>` +
    rows.map(({ a, key, w }) => {
      const first = lastAccount !== a.number; lastAccount = a.number;
      const name = first ? `<div class="nowrap">${esc(who(a))}</div><div class="muted">${esc(a.tierLabel || "plan unknown")}</div>` : "";
      // Used, tokens left and time left are measured against the stored reading;
      // once its window has rolled over they describe a window that is gone.
      const settled = a[{ h5: "session", d7: "weekly", fable: "fable" }[key]];
      const pending = settled && settled.awaitingRead;
      const used = pending ? `<span class="muted">reset ${clock(settled.resetAt)}</span>` : w.usedPct != null ? Math.round(w.usedPct) + "%" : "—";
      return `<tr class="${a.active ? "active" : ""}"><td class="who">${name}</td><td>${label(key)}</td><td class="num nowrap">${used}</td><td class="num nowrap">${w.burnPctPerHour != null ? w.burnPctPerHour.toFixed(1) + "% / h" : "—"}</td><td class="num nowrap">${w.tokensPerPoint != null ? fmt(w.tokensPerPoint) : "—"}${source(w)}</td><td class="num">${!pending && w.tokensLeft != null ? fmt(w.tokensLeft) : "—"}</td><td class="num nowrap">${pending ? "—" : minutesText(w.minutesLeft)}</td><td class="muted nowrap">${w.pointsRisen ? `${Math.round(w.pointsRisen)} points · ${fmt(w.tokensSpent)} tokens` : "no climb in the lookback"}</td></tr>`;
    }).join("");
}
async function tick() {
  const response = await fetch(`/api/state?window=${windowSeconds}`);
  render(await response.json());
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


def _window(usage: dict | None, key: str) -> dict | None:
    if not isinstance(usage, dict):
        return None
    raw = usage.get(key)
    if not isinstance(raw, dict) or not isinstance(raw.get("pct"), (int, float)):
        return None
    return {"pct": float(raw["pct"]), "resetsAt": raw.get("resets_at")}


def _fable(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict) or not isinstance(usage.get("scoped"), list):
        return None
    for item in usage["scoped"]:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].lower() == "fable"
            and isinstance(item.get("pct"), (int, float))
        ):
            return {"pct": float(item["pct"]), "resetsAt": item.get("resets_at")}
    return None


def _samples(backup_dir: Path, number: str | None) -> list[forecast.Sample]:
    if not number:
        return []
    path = backup_dir / "autoswitch_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    stored = raw.get("forecastSamples") if isinstance(raw, dict) else None
    rows = stored.get(str(number)) if isinstance(stored, dict) else None
    if not isinstance(rows, list):
        return []
    return forecast.samples_from(rows)


def _tier(switcher: ClaudeAccountSwitcher, number: str, email: str) -> dict:
    """Plan tier from the account's stored Claude config. Team seats carry it
    on the user; personal plans carry it on the organization."""
    path = switcher.configs_dir / f".claude-config-{number}-{email}.json"
    try:
        account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        return {"tier": None, "label": None, "kind": None}
    raw = account.get("userRateLimitTier") or account.get("organizationRateLimitTier")
    kind = account.get("organizationType")
    label = TIER_LABELS.get(raw or "", raw)
    if kind == "claude_team" and label:
        label = f"Team seat ({label})"
    return {"tier": raw, "label": label, "kind": kind}


def _display_name(email: str, organization: str, kind: str | None) -> str:
    if kind == "claude_team" or (organization and not organization.endswith("'s Organization")):
        return f"{email} · {organization}"
    return f"{email} · personal"


def _log_lines(backup_dir: Path, limit: int = 14) -> list[str]:
    path = backup_dir / "auto.stdout.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    kept = [
        line for line in lines
        if "no switch:" in line or line.startswith("Switched ") or "would switch" in line
        or "Switched Account" in line
    ]
    return kept[-limit:]


class StateReader:
    """Builds the page's JSON. Keeps the token ledger and reading history
    between requests, and backfills history once from the rotator's log."""

    def __init__(self, switcher: ClaudeAccountSwitcher, ledger: tokens.TokenLedger | None = None):
        self.switcher = switcher
        self.ledger = ledger or tokens.TokenLedger(tokens.default_root())
        self._lock = threading.Lock()
        self._backfill: list[allocation.Reading] | None = None
        self._pruned = False

    def _history(self, now: float) -> list[allocation.Reading]:
        store = self.switcher._usage_store
        if not self._pruned:
            store.prune_history(HISTORY_KEEP_S)
            self._pruned = True
        recorded = allocation.readings_from_history(store.history(now - HISTORY_KEEP_S))
        if self._backfill is None:
            organizations = {
                str(number): (info.get("organizationUuid") or "")
                for number, info in (self.switcher._get_sequence_data() or {}).get("accounts", {}).items()
            }
            self._backfill = allocation.readings_from_auto_log(
                self.switcher.backup_dir / "auto.stdout.log",
                datetime.now().astimezone(),
                organizations,
            )
        first_recorded = recorded[0].at if recorded else float("inf")
        merged = [reading for reading in self._backfill if reading.at < first_recorded] + recorded
        merged.sort(key=lambda reading: reading.at)
        return merged

    def __call__(self, window_s: float = 24 * 3600.0) -> dict:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            if now - self.ledger.last_scan_at >= TOKEN_RESCAN_S:
                self.ledger.refresh(now)
            token_summary = self.ledger.summary(window_s, now)
            files = len(self.ledger.cursors)
            scanned_at = self.ledger.last_scan_at
            calls = list(self.ledger.calls)
            refusals = list(self.ledger.refusals)
            history = self._history(now)
        return read_state(
            self.switcher,
            token_summary=token_summary,
            token_files=files,
            token_age_s=now - scanned_at,
            calls=calls,
            refusals=refusals,
            history=history,
            lookback_s=window_s,
        )


def read_state(
    switcher: ClaudeAccountSwitcher,
    *,
    token_summary: dict | None = None,
    token_files: int = 0,
    token_age_s: float | None = None,
    calls: list[tokens.Call] | None = None,
    refusals: list[tokens.Refusal] | None = None,
    history: list[allocation.Reading] | None = None,
    lookback_s: float = 24 * 3600.0,
) -> dict:
    """One JSON document for the page, from disk only."""
    settings = load_settings(switcher.backup_dir)
    snapshot = switcher.accounts_snapshot(fetch=set())
    samples = _samples(switcher.backup_dir, snapshot.active_number)
    now = datetime.now(timezone.utc).timestamp()
    by_organization = (token_summary or {}).get("byOrganization", {})
    # Latest Fable refusal per organization in the last hour. The usage
    # endpoint can report Fable room on an account the API refuses.
    refused_fable: dict[str, float] = {}
    for refusal in refusals or []:
        if "fable" in refusal.model.lower() and refusal.at >= now - 3600.0:
            refused_fable[refusal.organization] = max(refused_fable.get(refusal.organization, 0.0), refusal.at)
    tiers = {account.number: _tier(switcher, account.number, account.email) for account in snapshot.accounts}
    # Session logs name the organization, not the account. Slots that share
    # an organization (several seats on one team) share one token total.
    slots_by_organization: dict[str, list[str]] = {}
    for account in snapshot.accounts:
        if account.org_uuid:
            slots_by_organization.setdefault(account.org_uuid, []).append(account.number)
    views = []
    forecast_accounts: list[forecast.AccountSnapshot] = []
    attributed: set[str] = set()
    for account in snapshot.accounts:
        usage = account.usage.last_good
        forecast_account = forecast.snapshot(account.number, usage)
        if forecast_account is not None:
            forecast_accounts.append(forecast_account)
        recent_429 = account.usage.recent_429(now) if account.usage.last_429_at else False
        organization_tokens = by_organization.get(account.org_uuid or "")
        if organization_tokens is not None and account.org_uuid:
            attributed.add(account.org_uuid)
        tier = tiers[account.number]
        views.append({
            "number": account.number,
            "email": account.email,
            "organization": account.org_name,
            "organizationUuid": account.org_uuid,
            "name": _display_name(account.email, account.org_name, tier["kind"]),
            "tier": tier["tier"],
            "tierLabel": tier["label"],
            "active": account.is_active,
            "disabled": account.disabled,
            "session": _window(usage, "five_hour"),
            "weekly": _window(usage, "seven_day"),
            "fable": _fable(usage),
            "fableRefusedAt": refused_fable.get(account.org_uuid or ""),
            "tokens": organization_tokens,
            "tokensSharedWith": [
                number for number in slots_by_organization.get(account.org_uuid or "", [])
                if number != account.number
            ],
            "readingAgeSeconds": account.usage.age_s,
            "readAt": account.usage.fetched_at,
            "lastError": account.usage.last_error,
            "pollIntervalSeconds": account.usage.poll_interval_s,
            "consecutiveFailures": account.usage.consecutive_failures,
            "recent429": recent_429,
        })
    unattributed = tokens._empty()
    for organization, bucket in by_organization.items():
        if organization in attributed:
            continue
        for key in unattributed:
            unattributed[key] += bucket.get(key, 0)
    decision = None
    if snapshot.active_number and forecast_accounts:
        names = {part.strip().lower() for part in (settings.model or "").split(",")}
        made = forecast.decide(
            forecast_accounts,
            current=str(snapshot.active_number),
            samples=samples,
            hysteresis_pct=settings.hysteresis_pct,
            prefer_fable="fable" in names or "all" in names,
        )
        decision = {
            "switchTo": made.switch_to,
            "reason": made.reason,
            "detail": made.detail,
            "fableAvailable": made.fable_available,
        }
    summary = token_summary or {
        "sinceSeconds": 0,
        "calls": 0,
        "total": tokens._empty(),
        "byOrganization": {},
        "byModel": {},
        "hours": [],
    }
    rooms: dict[str, dict] = {}
    demand = {"allTokensPerMinute": 0.0, "fableTokensPerMinute": 0.0}
    if history is not None and calls is not None:
        measure = allocation.Allocation(
            history,
            calls,
            slot_organizations={account.number: account.org_uuid or "" for account in snapshot.accounts},
            tiers={number: tier["tier"] for number, tier in tiers.items()},
            now=now,
        )
        latest = {
            view["number"]: {
                "h5": view["session"],
                "d7": view["weekly"],
                "fable": view["fable"],
            }
            for view in views
        }
        rooms = {number: room.as_dict() for number, room in measure.summarize(latest, lookback_s).items()}
        demand = {
            "allTokensPerMinute": measure.demand_tokens_per_minute(fable_only=False),
            "fableTokensPerMinute": measure.demand_tokens_per_minute(fable_only=True),
            "lookbackSeconds": allocation.DEMAND_LOOKBACK_S,
            "readings": len(history),
        }
    for view in views:
        view["room"] = rooms.get(view["number"])
    reset_events = allocation.resets(history, now - lookback_s) if history else []
    active_history = (
        allocation.series_for(history, str(snapshot.active_number), now - 5 * 3600.0)
        if history and snapshot.active_number
        else []
    )
    # Plan-level medians of tokens per point, so unequal allowances show up.
    by_plan: dict[str, dict[str, list[float]]] = {}
    for view in views:
        room = view.get("room") or {}
        label = view.get("tierLabel") or "unknown"
        for key, window in (room.get("windows") or {}).items():
            # An account that barely moved in the lookback gives a noisy
            # ratio; a plan median should rest on real climbs.
            if (
                window.get("tokensPerPointSource") == "measured"
                and window.get("tokensPerPoint")
                and (window.get("pointsRisen") or 0) >= PLAN_MEDIAN_MIN_POINTS
            ):
                by_plan.setdefault(label, {}).setdefault(key, []).append(window["tokensPerPoint"])
    plans = []
    for label, windows in by_plan.items():
        entry = {"plan": label}
        for key, values in windows.items():
            values.sort()
            entry[key] = values[len(values) // 2]
            entry[key + "Accounts"] = len(values)
        plans.append(entry)
    return {
        "strategy": settings.strategy,
        "model": settings.model,
        "threshold": settings.threshold,
        "active": snapshot.active_number,
        "decision": decision,
        "accounts": views,
        "samples": [
            {"at": sample.at, "session": sample.session_pct, "fable": sample.fable_pct}
            for sample in samples
        ],
        "tokens": {
            **summary,
            "unattributed": unattributed,
            "files": token_files,
            "ageSeconds": token_age_s,
        },
        "demand": demand,
        "lookbackSeconds": lookback_s,
        "resets": reset_events[:40],
        "activeHistory": active_history,
        "plans": plans,
        "log": _log_lines(switcher.backup_dir),
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            try:
                window_s = float(query.get("window", ["86400"])[0])
            except ValueError:
                window_s = 86400.0
            window_s = min(max(window_s, 60.0), 7 * 24 * 3600.0)
            try:
                payload = json.dumps(self.server.reader(window_s)).encode("utf-8")
            except Exception as error:
                payload = json.dumps({"error": str(error)}).encode("utf-8")
                self._send(500, payload, "application/json")
                return
            self._send(200, payload, "application/json")
            return
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def make_server(reader: Callable[[float], dict], port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), _Handler)
    server.reader = reader
    server.daemon_threads = True
    return server


def serve(switcher: ClaudeAccountSwitcher, port: int = DEFAULT_PORT) -> None:
    """Serve the page on the loopback interface until interrupted."""
    server = make_server(StateReader(switcher), port)
    print(f"Claude usage: http://{HOST}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
