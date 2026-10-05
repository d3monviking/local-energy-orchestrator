// Print LEO_submission.html to LEO_Detailed_Documentation.pdf with headless Chrome (A4, page numbers).
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const chrome = spawn("google-chrome", ["--headless=new", "--remote-debugging-port=9335", "--no-first-run", "--no-sandbox",
  "--allow-file-access-from-files", `--user-data-dir=/tmp/claude-1000/chrome-pdf-${Date.now()}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target;
for (let i = 0; i < 50 && !target; i++) { await sleep(200); try { target = (await (await fetch("http://127.0.0.1:9335/json")).json()).find((t) => t.type === "page"); } catch {} }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let id = 0; const pending = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result ?? m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send("Page.enable");
await send("Page.navigate", { url: "file://" + join(here, "LEO_submission.html") });
await sleep(4000);
const footer = `<div style="font-family:Arial;font-size:7.5pt;color:#6e7781;width:100%;padding:0 16mm;display:flex;justify-content:space-between">
<span>LEO — Local Energy Orchestrator · Yuva Yodha Energy Tech Hackathon</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`;
const pdf = await send("Page.printToPDF", { printBackground: true, preferCSSPageSize: true, displayHeaderFooter: true,
  headerTemplate: "<div></div>", footerTemplate: footer, marginBottom: 0.7, marginTop: 0.6 });
if (!pdf.data) { console.log("print failed", JSON.stringify(pdf).slice(0, 300)); process.exit(1); }
writeFileSync(join(here, "LEO_Detailed_Documentation.pdf"), Buffer.from(pdf.data, "base64"));
console.log("written LEO_Detailed_Documentation.pdf");
ws.close(); chrome.kill(); process.exit(0);
