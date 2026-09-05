import test from "node:test";
import assert from "node:assert/strict";
import { pages } from "../src/data.js";
import { renderPage } from "../src/pages.js";
import { esc, pageShell } from "../src/components.js";

test("all 45 surfaces have unique bookmarkable IDs", () => {
  assert.equal(pages.length, 45);
  assert.equal(new Set(pages.map((p) => p.id)).size, 45);
  for (const p of pages) assert.match(p.id, /^[a-z][a-z-]+$/);
});

for (const [index, page] of pages.entries()) {
  test(`renders ${page.id} as actual HTML`, () => {
    const html = pageShell(page, renderPage(page), index + 1);
    assert.ok(html.includes(page.title));
    assert.ok(html.includes("示例数据"));
    assert.ok(html.includes("page-footer"));
    assert.ok(html.length > 800);
    assert.ok(!html.includes("undefined"));
    assert.ok(!html.includes("generated_images"));
    assert.ok(!html.includes("data:image"));
    assert.ok(
      !/<(?:input|button|iframe)\b/.test(html),
      "product canvas is a static card, not account controls",
    );
  });
}

test("escape markup and attributes without interpreting user text", () => {
  assert.equal(
    esc("<img src=\"x\" onerror='1'>&"),
    "&lt;img src=&quot;x&quot; onerror=&#39;1&#39;&gt;&amp;",
  );
});
test("base preview retains production information groups", () => {
  const html = renderPage(pages.find((p) => p.id === "base"));
  for (const field of [
    "当前存票",
    "增长速度",
    "预计满仓",
    "待采样",
    "心情",
    "工作",
    "回满",
    "消耗",
  ])
    assert.ok(html.includes(field), field);
});
test("ownership preview has no invented weapon statistics", () => {
  for (const id of ["ownership-group", "ownership-global"]) {
    const html = renderPage(pages.find((p) => p.id === id));
    for (const label of [
      "国服",
      "亚服",
      "未持有",
      "0潜",
      "5潜",
      "未知",
      "有效样本",
    ])
      assert.ok(html.includes(label), label);
    assert.ok(!html.includes("assets/weapon-"));
  }
});
test("empty challenge does not fabricate zero completion or records", () => {
  const html = renderPage(pages.find((p) => p.id === "challenge-empty"));
  assert.ok(html.includes("无记录不等于 0% 进度"));
  assert.ok(!html.includes('class="record'));
  assert.ok(!html.includes("secret-role-id"));
});
test("gacha isolates free pulls and keeps pity fields", () => {
  const html = renderPage(pages.find((p) => p.id === "gacha-operator"));
  for (const label of [
    "付费",
    "免费",
    "不计入保底",
    "小保底",
    "大保底",
    "六星期望",
    "逐抽明细",
  ])
    assert.ok(html.includes(label), label);
});
