import { pages, groups } from "./data.js";
import { esc, pageShell } from "./components.js";
import { renderPage } from "./pages.js";

const byId = new Map(pages.map((p) => [p.id, p]));
const capture = new URLSearchParams(location.search).get("capture") === "1";
document.body.classList.toggle("capture-mode", capture);
const artboard = document.querySelector("#artboard");
const wrap = document.querySelector("#canvas-wrap");
const nav = document.querySelector("#page-nav");
const search = document.querySelector("#page-search");
let current = byId.get(location.hash.slice(1)) || byId.get("operator");

function renderNav() {
  const query = search.value.trim().toLowerCase();
  nav.innerHTML =
    groups
      .map((group) => {
        const matches = pages.filter(
          (p) =>
            p.group === group &&
            `${p.title} ${p.command}`.toLowerCase().includes(query),
        );
        return matches.length
          ? `<section><h2>${esc(group)}</h2>${matches.map((p) => `<a href="#${p.id}" ${p.id === current.id ? 'aria-current="page"' : ""}><span>${String(pages.indexOf(p) + 1).padStart(2, "0")}</span>${esc(p.title)}</a>`).join("")}</section>`
          : "";
      })
      .join("") || '<p class="nav-empty">没有匹配的页面</p>';
}

function fit() {
  if (capture) {
    artboard.style.transform = "none";
    wrap.style.width = "1440px";
    wrap.style.height = "auto";
    return;
  }
  const available = document.querySelector(".preview-mat").clientWidth - 48;
  const scale = Math.min(1, Math.max(0.15, available / 1440));
  artboard.style.transform = `scale(${scale})`;
  wrap.style.width = `${1440 * scale}px`;
  wrap.style.height = `${artboard.offsetHeight * scale}px`;
}

async function show() {
  current = byId.get(location.hash.slice(1)) || byId.get("operator");
  window.previewReady = false;
  artboard.dataset.ready = "false";
  const index = pages.indexOf(current);
  artboard.className = `sheet sheet-${current.kind} ${current.variant}`;
  artboard.dataset.pageId = current.id;
  artboard.innerHTML = pageShell(current, renderPage(current), index + 1);
  document.querySelector("#review-title").textContent = current.title;
  document.querySelector("#page-count").textContent =
    `${index + 1} / ${pages.length} 页`;
  document.querySelector("#capture-link").href = `?capture=1#${current.id}`;
  document.title = `${current.title} · ENDFIELD 代码预览`;
  renderNav();
  fit();
  await document.fonts.ready;
  await Promise.all(
    [...artboard.querySelectorAll("img")].map((img) =>
      img.decode().catch(() => {}),
    ),
  );
  fit();
  window.previewReady = true;
  artboard.dataset.ready = "true";
}

function advance(direction) {
  location.hash =
    pages[
      (pages.indexOf(current) + direction + pages.length) % pages.length
    ].id;
}
document
  .querySelector("#previous")
  .addEventListener("click", () => advance(-1));
document.querySelector("#next").addEventListener("click", () => advance(1));
search.addEventListener("input", renderNav);
window.addEventListener("hashchange", show);
window.addEventListener("resize", fit);
window.addEventListener("keydown", (event) => {
  if (
    event.target instanceof HTMLInputElement ||
    event.ctrlKey ||
    event.metaKey ||
    event.altKey
  )
    return;
  if (event.key === "ArrowRight") advance(1);
  if (event.key === "ArrowLeft") advance(-1);
});
new ResizeObserver(fit).observe(artboard);
// Read-only metadata for the screenshot / smoke-test harness. No bot imports or API.
window.previewPages = pages.map((p) => ({
  id: p.id,
  title: p.title,
  group: p.group,
}));
show();
