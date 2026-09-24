import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

const origin = new URL(process.argv[2] ?? 'http://dx.1catai.com:51727/');
const localHtml = await readFile(new URL('../deploy/public/index.html', import.meta.url));
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
async function get(path) {
  const response = await fetch(new URL(path, origin), { signal: AbortSignal.timeout(12000), cache: 'no-store' });
  assert.equal(response.status, 200, path);
  return response;
}
const response = await get('/');
const bytes = Buffer.from(await response.arrayBuffer());
assert.equal(sha(bytes), sha(localHtml), 'public release equals exact local build');
const html = bytes.toString('utf8');
assert.match(html, /data-design-version="v3-20260905"/);
assert.doesNotMatch(html, /https?:\/\/(?:127\.0\.0\.1|localhost):\d+/);
const assets = [...new Set([...html.matchAll(/(?:src|href)=["'](\/_next\/[^"'#?]+)/g)].map((match) => match[1]))];
for (const path of assets) {
  const asset = await get(path);
  assert.ok((await asset.arrayBuffer()).byteLength > 0, path);
}
const health = await (await get('/api/health')).json();
assert.equal(health.status, 'ok');
const status = await (await get('/api/status')).json();
assert.equal(status.nodes.length, 8);
assert.equal(status.edges.length, 28);
assert.ok(Number.isFinite(status.host.cpuUtilizationPct));
assert.ok(Number.isFinite(status.host.cpuFrequencyMHz));
assert.ok(status.electricity.components, 'power breakdown present');
const stream = await get('/api/stream');
assert.match(stream.headers.get('content-type'), /text\/event-stream/);
const reader = stream.body.getReader();
const decoder = new TextDecoder();
let buffer = '', frames = 0, firstTime = 0, lastTime = 0, firstSequence = null, lastSequence = null;
const edgePairs = new Set();
let minEdgesPerFrame = 28, maxEdgesPerFrame = 0;
let repeatedCachedFrames = 0;
try {
  while (frames < 120) {
    const { done, value } = await reader.read();
    assert.ok(!done, 'stream did not end early');
    buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n');
    let separator;
    while (frames < 120 && (separator = buffer.indexOf('\n\n')) >= 0) {
      const event = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const data = event.split('\n').find((line) => line.startsWith('data:'));
      if (!data) continue;
      const frame = JSON.parse(data.slice(5));
      assert.equal(frame.nodes.length, 8);
      assert.ok(frame.edges.length > 0 && frame.edges.length <= 28, 'bounded batched link updates');
      minEdgesPerFrame = Math.min(minEdgesPerFrame, frame.edges.length);
      maxEdgesPerFrame = Math.max(maxEdgesPerFrame, frame.edges.length);
      for (const edge of frame.edges) {
        assert.ok(edge.source >= 0 && edge.source < edge.target && edge.target < 8);
        edgePairs.add(`${edge.source}-${edge.target}`);
      }
      assert.ok(!('electricity' in frame), 'slow history stays out of fast stream');
      assert.ok(Number.isInteger(frame.sequence));
      if (lastSequence !== null) {
        assert.ok(frame.sequence >= lastSequence, 'cached sequence never moves backwards');
        if (frame.sequence === lastSequence) repeatedCachedFrames++;
      }
      if (frames === 0) { firstTime = performance.now(); firstSequence = frame.sequence; }
      lastTime = performance.now(); lastSequence = frame.sequence; frames++;
    }
  }
} finally { await reader.cancel(); }
assert.equal(edgePairs.size, 28, 'stream covers the full matrix across batches');
assert.ok(lastSequence > firstSequence, 'collector advances while connected');
console.log(JSON.stringify({
  status: 'passed', url: origin.href, htmlBytes: bytes.length, htmlSha256: sha(bytes), assets: assets.length,
  nodes: status.nodes.length, nodePairs: status.edges.length, online: status.fleet.online, linksUp: status.fleet.linksUp, p9Up: status.fleet.p9Up,
  cpu: { usagePct: status.host.cpuUtilizationPct, frequencyMHz: status.host.cpuFrequencyMHz, watts: status.host.cpuPowerW, powerAvailable: status.host.cpuPowerAvailable },
  electricity: { status: status.electricity.status, inputWatts: status.electricity.currentW, source: status.electricity.source },
  sse: { frames, firstSequence, lastSequence, repeatedCachedFrames, minEdgesPerFrame, maxEdgesPerFrame, totalPairsCovered: edgePairs.size, receivedFps: Number(((frames - 1) * 1000 / (lastTime - firstTime)).toFixed(2)) },
  browserTest: false,
}, null, 2));
