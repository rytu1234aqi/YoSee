import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';

const root = path.resolve('desktop/build');
await mkdir(root, { recursive: true });
const temporary = await mkdtemp(path.join(root, 'ui-check-'));
const backend = spawn(path.resolve('desktop/src-tauri/target/release/backend/youdekan-backend.exe'), [
  '--data-dir', path.join(temporary, 'library'), '--import-database',
  path.join(process.env.APPDATA, 'com.youdekan.reader', 'youdekan.sqlite'),
  '--no-auto-fetch', '--watch-parent',
], { cwd: temporary, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
let backendLog = '';
backend.stderr.on('data', data => { backendLog += data; });
let edge;
let socket;
const timeout = (promise, ms = 30000) => Promise.race([
  promise,
  new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('Timed out')), ms); timer.unref(); }),
]);
try {
  const url = await timeout(new Promise((resolve, reject) => {
    backend.once('error', reject);
    backend.once('exit', code => reject(new Error(`Backend exited ${code}: ${backendLog}`)));
    const lines = readline.createInterface({ input: backend.stdout });
    lines.on('line', line => { if (line.startsWith('YOUDEKAN_READY=')) resolve(line.slice(15)); });
  }));
  edge = spawn('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--remote-debugging-port=0',
    `--user-data-dir=${path.join(temporary, 'browser')}`, 'about:blank',
  ], { windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'] });
  const wsUrl = await timeout(new Promise((resolve, reject) => {
    let log = '';
    edge.once('error', reject);
    edge.once('exit', code => reject(new Error(`Edge exited ${code}: ${log}`)));
    edge.stderr.on('data', data => {
      log += data;
      const match = log.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) resolve(match[1]);
    });
  }));
  socket = new WebSocket(wsUrl);
  await timeout(once(socket, 'open'));
  let sequence = 0;
  const requests = new Map();
  const errors = [];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    const pending = requests.get(message.id);
    if (pending) {
      requests.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    }
  });
  function command(method, params = {}, sessionId) {
    const id = ++sequence;
    return timeout(new Promise((resolve, reject) => {
      requests.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params, sessionId }));
    }));
  }
  const target = await command('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await command('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  await command('Runtime.enable', {}, sessionId);
  await command('Page.enable', {}, sessionId);
  async function evaluate(expression) {
    const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async function waitFor(expression) {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`UI did not reach expected state: ${expression}; errors=${JSON.stringify(errors)}`);
  }
  await command('Page.navigate', { url }, sessionId);
  await waitFor('document.querySelectorAll(".feed-card").length > 0 && document.querySelectorAll(".source-row").length > 0');
  console.log('PASS: browser executes all modules and loads sources and article list');
  await evaluate('document.querySelector(".feed-card").click()');
  await waitFor('document.querySelector("#article-body") && document.querySelector("#read")');
  const readBefore = await evaluate('document.querySelector("#read").getAttribute("aria-pressed")');
  await evaluate('document.querySelector("#read").click()');
  await waitFor(`document.querySelector('#read').getAttribute('aria-pressed') !== ${JSON.stringify(readBefore)} && !document.querySelector('#read').disabled`);
  const savedBefore = await evaluate('document.querySelector("#save").getAttribute("aria-pressed")');
  await evaluate('document.querySelector("#save").click()');
  await waitFor(`document.querySelector('#save').getAttribute('aria-pressed') !== ${JSON.stringify(savedBefore)} && !document.querySelector('#save').disabled`);
  console.log('PASS: article selection, mark read and save buttons');
  await evaluate('document.querySelector("#add-source").click()');
  assert.equal(await evaluate('document.querySelector("#source-dialog").open'), true);
  await evaluate('document.querySelector("#source-dialog").close()');
  await evaluate('document.querySelector("#theme").click()');
  assert.equal(await evaluate('document.documentElement.dataset.theme'), 'dark');
  await evaluate('document.querySelector("#language").click()');
  assert.equal(await evaluate('document.documentElement.lang'), 'en');
  await evaluate('document.querySelector("#query").value = "ui-check-nonexistent-article-817365"; document.querySelector("#query").dispatchEvent(new Event("input", {bubbles:true}))');
  await waitFor('document.querySelectorAll(".feed-card").length === 0 && document.querySelector("#feed-status").textContent === "No articles found."');
  await evaluate('document.querySelector("#query").value = ""; document.querySelector("#query").dispatchEvent(new Event("input", {bubbles:true}))');
  await waitFor('document.querySelectorAll(".feed-card").length > 0');
  await evaluate('document.querySelector("button[data-status=saved]").click()');
  await waitFor('document.querySelector("#feed-title").textContent === "Saved" && document.querySelectorAll(".feed-card").length > 0');
  await evaluate('document.querySelector("button[data-status=all]").click(); document.querySelector("#refresh").click()');
  await waitFor('document.querySelector("#feed-title").textContent === "Latest" && document.querySelectorAll(".feed-card").length > 0');
  assert.deepEqual(errors, []);
  const screenshot = await command('Page.captureScreenshot', { format: 'png' }, sessionId);
  await writeFile(path.join(root, 'reader-ui-verified.png'), Buffer.from(screenshot.data, 'base64'));
  console.log('PASS: add-source dialog, theme, language, search, saved filter and refresh; no JavaScript exceptions');
  await command('Browser.close');
  socket.close();
  backend.stdin.end();
  const [exitCode] = await timeout(once(backend, 'exit'), 10000);
  assert.equal(exitCode, 0);
  console.log('PASS: bundled backend shuts down cleanly; checks used a temporary database copy');
} catch (error) {
  console.error(backendLog);
  throw error;
} finally {
  socket?.close();
  if (edge && edge.exitCode === null) edge.kill();
  if (backend.exitCode === null) backend.kill();
}
