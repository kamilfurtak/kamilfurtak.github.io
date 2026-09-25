'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const zlib = require('node:zlib');
const {once} = require('node:events');
const {createServer, outgoingHeaders} = require('../lib/server.cjs');
async function listen(server, t) {
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  return `http://127.0.0.1:${server.address().port}`;
}
async function fixture(t, handler, extra = {}) {
  const received = [];
  const backend = await listen(http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const entry = {headers: req.headers, url: req.url, body: JSON.parse(Buffer.concat(chunks))};
    received.push(entry); handler(entry, res);
  }), t);
  const config = {listen: {host: '127.0.0.1', port: 18003}, requestTimeoutMs: 2000, localModels: [
    {id: 'local-test', upstreamModel: 'engine-model', profile: 'ninfer', baseUrl: backend + '/v1', apiKeyEnv: 'TEST_LOCAL_KEY'},
  ]};
  const catalog = {models: [{slug: 'local-test'}, {slug: 'gpt-test'}, {slug: 'removed-local'}], _local_model_bridge: {version: 1, cloudModels: ['gpt-test']}};
  const url = await listen(createServer({config, catalog, env: {TEST_LOCAL_KEY: 'local-only'}, cloudTargets: {chatgpt: backend + '/account', openai: backend + '/api'}, ...extra}), t);
  return {url, received};
}
const post = (url, body, headers = {}) => fetch(url + '/v1/responses', {method: 'POST', headers: {'content-type': 'application/json', ...headers}, body: typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body)});
const events = text => text.split('\n').filter(l => l.startsWith('data: ')).map(l => JSON.parse(l.slice(6)));
const completed = {type: 'response.completed', response: {id: 'r1', object: 'response', status: 'completed', output: []}};
test('local routing strips account credentials and adapts the complete desktop request', async t => {
  const logs = [];
  const {url, received} = await fixture(t, (_entry, res) => { res.setHeader('content-type', 'text/event-stream'); res.end(`data: ${JSON.stringify(completed)}\n\n`); }, {logger: row => logs.push(row)});
  const response = await post(url, {model: 'local-test', stream: true, input: 'private-prompt', reasoning: {effort: 'high', summary: 'auto'}, stream_options: {reasoning_summary_delivery: 'auto'}, tools: [{type: 'web_search'}, {type: 'function', name: 'pwd'}]}, {authorization: 'Bearer cloud-only', 'chatgpt-account-id': 'account-only', cookie: 'private-cookie', 'x-api-key': 'wrong-key'});
  assert.equal(events(await response.text()).at(-1).type, 'response.completed');
  const [r] = received;
  assert.equal(r.url, '/v1/responses'); assert.equal(r.body.model, 'engine-model');
  assert.deepEqual(r.body.reasoning, {effort: 'high'}); assert.equal(r.body.tools.length, 1);
  assert.equal(r.headers.authorization, 'Bearer local-only');
  for (const h of ['cookie', 'chatgpt-account-id', 'x-api-key']) assert.equal(r.headers[h], undefined);
  assert(!JSON.stringify(logs).includes('private-prompt'));
});
test('cloud requests preserve their body and separate subscription from API routing', async t => {
  const {url, received} = await fixture(t, (_entry, res) => res.end('{"ok":true}'));
  const body = {model: 'gpt-test', tools: [{type: 'web_search'}], reasoning: {summary: 'auto'}};
  await (await post(url, body, {authorization: 'Bearer cloud', 'chatgpt-account-id': 'account'})).text();
  await (await post(url, body, {authorization: 'Bearer api'})).text();
  assert.equal(received[0].url, '/account/responses'); assert.equal(received[1].url, '/api/responses');
  assert.deepEqual(received[0].body, body); assert.equal(received[0].headers.authorization, 'Bearer cloud');
});
test('unknown cloud-looking names, local compaction and browser-origin requests never reach upstream', async t => {
  const {url, received} = await fixture(t, (_entry, res) => res.end('{}'));
  assert.equal((await post(url, {model: 'gpt-unknown'})).status, 400);
  assert.equal((await post(url, {model: 'removed-local'})).status, 400);
  assert.equal((await post(url, {model: 'local-test'}, {origin: 'https://example.invalid'})).status, 403);
  assert.equal((await fetch(url + '/v1/responses/compact', {method: 'POST', headers: {'content-type': 'application/json'}, body: '{"model":"local-test"}'})).status, 400);
  assert.equal((await fetch(url + '/v1/models')).status, 200);
  assert.equal(received.length, 0);
});
test('upstream errors become terminal failures with their actual code', async t => {
  const {url} = await fixture(t, (_entry, res) => { res.writeHead(400, {'content-type': 'application/json'}); res.end('{"error":{"code":"invalid_tool","message":"Tool rejected"}}'); });
  const data = events(await (await post(url, {model: 'local-test', stream: true})).text());
  assert.equal(data.at(-1).type, 'response.failed'); assert.equal(data.at(-1).response.error.code, 'invalid_tool');
  assert(!data.some(e => e.type === 'response.completed'));
});
test('stream EOF without completion is reported as a failure', async t => {
  const {url} = await fixture(t, (_entry, res) => res.end('data: {"type":"response.output_text.delta","delta":"hi"}\n\n'));
  const data = events(await (await post(url, {model: 'local-test', stream: true})).text());
  assert.equal(data.at(-1).response.error.code, 'upstream_incomplete');
});
test('UTF-8 fragments, CRLF and multiline SSE data decode correctly', async t => {
  const {url} = await fixture(t, (_entry, res) => {
    const bytes = Buffer.from('data: {"type":"response.output_text.delta",\r\ndata: "delta":"żółw"}\r\n\r\n' + `data:${JSON.stringify(completed)}\r\n\r\n`);
    for (const byte of bytes) res.write(Buffer.from([byte])); res.end();
  });
  const data = events(await (await post(url, {model: 'local-test', stream: true})).text());
  assert.equal(data[0].delta, 'żółw'); assert.equal(data[1].type, 'response.completed');
});
test('gzip and zstd requests are decompressed before routing', async t => {
  const {url, received} = await fixture(t, (_entry, res) => res.end('{"output":[]}'));
  const body = Buffer.from('{"model":"local-test","input":"compressed"}');
  for (const [encoding, compress] of [['gzip', zlib.gzipSync], ['zstd', zlib.zstdCompressSync]]) {
    const response = await post(url, compress(body), {'content-encoding': encoding});
    assert.equal(response.status, 200); await response.text();
  }
  assert(received.every(r => r.body.input === 'compressed' && r.headers['content-encoding'] === undefined));
});
test('cancelling a client stream closes its upstream connection', async t => {
  let started, closed;
  const startedPromise = new Promise(resolve => { started = resolve; });
  const closedPromise = new Promise(resolve => { closed = resolve; });
  const {url} = await fixture(t, (_entry, res) => {
    res.writeHead(200, {'content-type': 'text/event-stream'}); res.write(': upstream ready\n\n'); started();
    res.once('close', closed);
  });
  const response = await post(url, {model: 'local-test', stream: true});
  await startedPromise; await response.body.cancel();
  await Promise.race([closedPromise, new Promise((_, reject) => { const timer = setTimeout(() => reject(Error('Upstream was not cancelled')), 1500); timer.unref(); })]);
});
test('missing local credentials fail without falling back to cloud authentication', () => {
  assert.throws(() => outgoingHeaders({authorization: 'Bearer cloud'}, {apiKeyEnv: 'UNSET'}, {}), /Missing local credential/);
});
