import { operators, slots, gearArt, stamp } from "./data.js";
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const art = (name, cls = "", alt = "") =>
  `<img class="${esc(cls)}" src="./assets/${esc(name)}.webp" alt="${esc(alt)}" loading="eager">`;
export const native = (name, cls = "", alt = "") =>
  `<img class="${esc(cls)}" src="/shared/ui/${esc(name)}.png" alt="${esc(alt)}">`;
export const icon = (name = "common_info") => native(name, "glyph");
export const tag = (text, color = "") =>
  `<span class="tag ${esc(color)}">${esc(text)}</span>`;
export const note = (text) => `<p class="notice">${esc(text)}</p>`;
export const section = (title, body, hint = "", cls = "") =>
  `<section class="section ${cls}"><div class="section-title"><h2>${esc(title)}</h2><small>${esc(hint)}</small></div>${body}</section>`;
export const metric = (label, value, hint = "") =>
  `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(hint)}</small></div>`;
export const metrics = (rows) =>
  `<div class="metrics">${rows.map((x) => metric(...x)).join("")}</div>`;
export const table = (headers, rows, cls = "") =>
  `<div class="table-wrap"><table class="${cls}"><thead><tr>${headers.map((x) => `<th scope="col">${esc(x)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
export const team = (start = 0, label = true) =>
  `<div class="team">${Array.from({ length: 4 }, (_, i) => {
    const o = operators[(i + start) % operators.length];
    return `<div>${art(o.id, "", o.name)}${label ? `<span>${o.name}</span>` : ""}</div>`;
  }).join("")}</div>`;
export const gear = (i, showLabel = true) =>
  `<div class="gear-slot">${showLabel ? `<span>${slots[i]}</span>` : ""}${i === 4 ? native("profile_stat_files", "", "道具占位") : art(gearArt[i], "", slots[i])}<small>LV.${i === 4 ? "70" : "70"}${i < 4 ? " · 锻造Ⅰ" : ""}</small></div>`;
export const gearRow = () =>
  `<div class="gear-row">${slots.map((_, i) => gear(i)).join("")}</div>`;
export const identity = () =>
  `<div class="identity">${art("endministrator", "avatar", "示例管理员")}<div><strong>示例管理员</strong><small>UID 10****28 · 国服</small></div><span class="identity-level">LV.<b>60</b></span></div>`;
export const bar = (value = 60, cls = "") =>
  `<div class="bar ${cls}"><i style="width:${Math.max(0, Math.min(100, value))}%"></i></div>`;
export const statList = (rows) =>
  `<dl class="stat-list">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`;
export const pageShell = (page, body, count) =>
  `<div class="page-grid" aria-hidden="true"></div><header class="page-header"><div><div class="eyebrow">ENDFIELD INDUSTRIES <span>／ OTAE ARCHIVE</span></div><h1>${esc(page.title)}</h1></div><div class="page-index"><span>DESIGN PREVIEW · 示例数据</span><b>${String(count).padStart(2, "0")}<i> / 45</i></b></div></header><main class="page-content ${page.kind}-page">${body}</main><footer class="page-footer"><code>${esc(page.command)}</code><span>${stamp}</span><strong>非官方设计预览 <i>■</i></strong></footer>`;
