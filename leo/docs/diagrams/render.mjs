// Render every *.mmd in this folder to .svg and a 2x .png with headless
// Chrome + Mermaid (CDN). Usage: node render.mjs [file.mmd ...]
import { spawn } from "node:child_process";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const files = process.argv.slice(2).length ? process.argv.slice(2) : readdirSync(here).filter((f) => f.endsWith(".mmd")).sort();
const chrome = spawn("google-chrome", ["--headless=new", "--remote-debugging-port=9334", "--no-first-run", "--no-sandbox",
  `--user-data-dir=/tmp/claude-1000/chrome-mmd-${Date.now()}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) { await sleep(200); try { target = (await (await fetch("http://127.0.0.1:9334/json")).json()).find((t) => t.type === "page"); } catch {} }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pending = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Page.enable"); await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 2000, height: 1400, deviceScaleFactor: 2, mobile: false });

for (const f of files) {
  const src = readFileSync(join(here, f), "utf8");
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;background:#fff;font-family:Inter,Segoe UI,Arial,sans-serif}#d{display:inline-block;padding:16px}</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script></head><body><div id="d"></div>
<script>mermaid.initialize({startOnLoad:false,theme:"default",securityLevel:"loose",flowchart:{htmlLabels:true,curve:"basis"},sequence:{mirrorActors:false},sankey:{width:1100,height:560,showValues:true,nodeAlignment:"justify"}});
mermaid.render("g", ${JSON.stringify(src)}).then(({svg})=>{const d=document.getElementById("d");d.innerHTML=svg;const s=d.querySelector("svg");const vb=s.viewBox.baseVal;s.style.maxWidth="none";s.setAttribute("width",vb.width);s.setAttribute("height",vb.height);window.__done="ok"}).catch(e=>{window.__done="ERR "+e.message});</script></body></html>`;
  const tmp = `/tmp/claude-1000/mmd-${Date.now()}.html`;
  writeFileSync(tmp, html);
  await send("Page.navigate", { url: "file://" + tmp });
  let done = null;
  for (let i = 0; i < 60 && !done; i++) { await sleep(250); done = (await send("Runtime.evaluate", { expression: "window.__done || ''", returnByValue: true })).result?.value; }
  if (!done || done.startsWith("ERR")) { console.log(`${f}: ${done || "timeout"}`); continue; }
  const svg = (await send("Runtime.evaluate", { expression: "document.querySelector('#d svg').outerHTML", returnByValue: true })).result.value;
  writeFileSync(join(here, f.replace(".mmd", ".svg")), svg);
  const box = (await send("Runtime.evaluate", { expression: "JSON.stringify(document.getElementById('d').getBoundingClientRect())", returnByValue: true })).result.value;
  const b = JSON.parse(box);
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: Math.ceil(b.width), height: Math.ceil(b.height), scale: 1 } });
  writeFileSync(join(here, f.replace(".mmd", ".png")), Buffer.from(shot.data, "base64"));
  console.log(`${f}: ok (${Math.round(b.width)}x${Math.round(b.height)})`);
}
ws.close(); chrome.kill(); process.exit(0);
