'use strict';
const http = require('node:http');
const https = require('node:https');
const zlib = require('node:zlib');
const {once} = require('node:events');
const {StringDecoder} = require('node:string_decoder');
const {randomUUID} = require('node:crypto');
const {encodeRequest, decodeEvent, StreamDecoder} = require('./protocol.cjs');
const MAX_BODY = 32 * 1024 * 1024;
const MAX_EVENT = 16 * 1024 * 1024;
const CLOUD = {chatgpt: 'https://chatgpt.com/backend-api/codex', openai: 'https://api.openai.com/v1'};
const HOP = new Set(['host', 'connection', 'transfer-encoding', 'content-length', 'content-encoding', 'accept-encoding', 'upgrade', 'proxy-authorization', 'proxy-connection', 'keep-alive', 'te', 'trailer']);
function outgoingHeaders(incoming, local, env = process.env) {
  const headers = {};
  if (!local) for (const [name, value] of Object.entries(incoming)) if (!HOP.has(name)) headers[name] = value;
  headers['content-type'] = 'application/json';
  headers['accept'] = 'text/event-stream, application/json';
  headers['accept-encoding'] = 'identity';
  if (local?.apiKeyEnv) {
    const key = env[local.apiKeyEnv];
    if (!key) throw Error(`Missing local credential environment variable: ${local.apiKeyEnv}`);
    headers.authorization = 'Bearer ' + key;
  }
  return headers;
}
function route(model, headers, config, catalog, cloudTargets = CLOUD) {
  const local = config.localModels.find(m => m.id === model);
  if (local) return {kind: 'local', baseUrl: local.baseUrl, local};
  if (typeof model !== 'string' || !catalog.models.some(m => m.slug === model) || !catalog._local_model_bridge?.cloudModels?.includes(model)) throw Error('Model is absent from the configured cloud allowlist; request was not sent');
  const kind = headers['chatgpt-account-id'] ? 'chatgpt' : 'openai';
  return {kind, baseUrl: cloudTargets[kind]};
}
async function collect(readable, limit) {
  const chunks = []; let size = 0;
  for await (const chunk of readable) {
    size += chunk.length;
    if (size > limit) throw Error('Body exceeds the configured size limit');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
function failEvent(code, message) {
  return {type: 'response.failed', response: {id: 'resp_error_' + randomUUID(), object: 'response', status: 'failed', output: [], error: {code, message}}};
}
async function* sseEvents(readable) {
  const decoder = new StringDecoder('utf8');
  let buffer = '';
  for await (const chunk of readable) {
    buffer += decoder.write(chunk);
    let match;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const frame = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      if (Buffer.byteLength(frame) > MAX_EVENT) throw Error('SSE event too large');
      const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (data.trim() && data.trim() !== '[DONE]') yield JSON.parse(data);
    }
    if (Buffer.byteLength(buffer) > MAX_EVENT) throw Error('SSE event too large');
  }
  buffer += decoder.end();
  if (buffer.trim() && !buffer.trim().startsWith(':')) throw Error('Incomplete SSE frame');
}
function createServer({config, catalog, logger = () => {}, cloudTargets = CLOUD, env = process.env}) {
  const server = http.createServer((req, res) => {
    handle(req, res).catch(() => {
      if (!res.headersSent) json(res, 500, {error: {message: 'Bridge request failed'}});
      else res.destroy();
    });
  });
  server.on('upgrade', (_req, socket) => socket.end('HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'));
  async function handle(req, res) {
    const controller = new AbortController();
    let upstreamRequest, upstream, heartbeat, deadline;
    res.on('close', () => { controller.abort(); upstreamRequest?.destroy(); upstream?.destroy(); clearInterval(heartbeat); clearTimeout(deadline); });
    async function write(data) {
      if (res.destroyed) throw Error('Client disconnected');
      if (!res.write(data)) await once(res, 'drain', {signal: controller.signal});
    }
    async function event(data) { await write(`event: ${data.type}\ndata: ${JSON.stringify(data)}\n\n`); }
    let localStreaming = false;
    try {
      // Native clients do not send Origin. Reject browser-origin requests and
      // unexpected Host values so a web page cannot use this loopback bridge.
      const host = req.headers.host || '';
      if (req.headers.origin || !/^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/.test(host)) return json(res, 403, {error: {message: 'Only native loopback clients are accepted'}});
      if (req.method === 'GET' && req.url === '/health') return json(res, 200, {ok: true, upstreamsProbed: false});
      if (req.method === 'GET' && req.url === '/v1/models') return json(res, 200, {object: 'list', data: catalog.models.map(m => ({id: m.slug, object: 'model'}))});
      if (req.method !== 'POST' || !['/v1/responses', '/v1/responses/compact'].includes(req.url)) return json(res, 404, {error: {message: 'Unsupported endpoint'}});
      if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) return json(res, 415, {error: {message: 'Expected application/json'}});
      let body = await collect(req, MAX_BODY);
      const encoding = req.headers['content-encoding'];
      if (encoding === 'zstd') body = zlib.zstdDecompressSync(body, {maxOutputLength: MAX_BODY});
      else if (encoding === 'gzip') body = zlib.gunzipSync(body, {maxOutputLength: MAX_BODY});
      else if (encoding && encoding !== 'identity') throw Error('Unsupported request compression');
      let payload = JSON.parse(body);
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw Error('Expected a JSON request object');
      const target = route(payload.model, req.headers, config, catalog, cloudTargets);
      const requestedModel = payload.model;
      let names = new Map();
      if (target.local) {
        if (req.url !== '/v1/responses') throw Error('Local compaction is not supported');
        if (target.local.dropWebSearch ?? target.local.profile !== 'native') {
          if (['web_search', 'web_search_preview'].includes(payload.tool_choice?.type)) throw Error('Built-in web search is unavailable for this local profile');
          if (Array.isArray(payload.tools)) payload.tools = payload.tools.filter(t => !['web_search', 'web_search_preview'].includes(t.type));
        }
        ({request: payload, names} = encodeRequest(payload, target.local.profile));
        payload.model = target.local.upstreamModel;
        body = Buffer.from(JSON.stringify(payload));
      }
      const headers = outgoingHeaders(req.headers, target.local, env);
      headers['content-length'] = body.length;
      const url = new URL(target.baseUrl + req.url.slice(3));
      localStreaming = Boolean(target.local && payload.stream === true);
      if (localStreaming) {
        res.writeHead(200, {'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'close'});
        await write(': waiting for model\n\n');
        heartbeat = setInterval(() => { if (!res.destroyed && res.writableLength < 16384) res.write(': waiting for model\n\n'); }, 10000);
      }
      upstream = await new Promise((resolve, reject) => {
        upstreamRequest = (url.protocol === 'https:' ? https : http).request(url, {method: 'POST', headers, signal: controller.signal}, resolve);
        deadline = setTimeout(() => upstreamRequest.destroy(Error('Model request exceeded its deadline')), config.requestTimeoutMs);
        upstreamRequest.on('error', reject);
        upstreamRequest.end(body);
      });
      logger({time: new Date().toISOString(), route: target.kind, model: requestedModel, status: upstream.statusCode});
      if (!target.local) {
        const responseHeaders = Object.fromEntries(Object.entries(upstream.headers).filter(([k]) => !['transfer-encoding', 'connection', 'keep-alive'].includes(k)));
        res.writeHead(upstream.statusCode, responseHeaders);
        for await (const chunk of upstream) await write(chunk);
      } else if (upstream.statusCode >= 400) {
        let error = {code: 'upstream_error', message: 'Local model rejected the request'};
        try {
          const parsed = JSON.parse(await collect(upstream, 65536)).error;
          if (parsed && typeof parsed === 'object') error = {
            code: typeof parsed.code === 'string' ? parsed.code : 'upstream_error',
            message: typeof parsed.message === 'string' ? parsed.message.slice(0, 4096) : error.message,
          };
        } catch { /* Never forward upstream HTML or debug dumps. */ }
        if (localStreaming) await event(failEvent(error.code, error.message));
        else return json(res, upstream.statusCode, {error});
      } else if (localStreaming) {
        const decoder = new StreamDecoder(names);
        let terminal = false;
        for await (let item of sseEvents(upstream)) {
          if (item.type === 'error') item = failEvent(item.code || 'upstream_error', item.message || 'Local model failed');
          item = decoder.decode(item);
          if (!item) continue;
          if (typeof item.type !== 'string' || !/^[a-zA-Z0-9_.-]+$/.test(item.type)) throw Error('Invalid upstream event type');
          if (['response.completed', 'response.failed', 'response.incomplete'].includes(item.type)) terminal = true;
          await event(item);
        }
        if (!terminal) await event(failEvent('upstream_incomplete', 'Local model closed its stream without a terminal response'));
      } else {
        return json(res, upstream.statusCode, decodeEvent(JSON.parse(await collect(upstream, MAX_BODY)), names));
      }
      res.end();
    } catch (error) {
      if (!res.destroyed) {
        if (localStreaming && res.headersSent) {
          try { await event(failEvent('bridge_transport_error', 'Local model stream failed or timed out')); res.end(); } catch { res.destroy(); }
        } else if (!res.headersSent) json(res, upstreamRequest ? 502 : 400, {error: {message: upstreamRequest ? 'Model upstream unavailable' : error.message}});
        else res.destroy();
      }
    } finally {
      clearInterval(heartbeat); clearTimeout(deadline);
      upstreamRequest?.destroy(); upstream?.destroy();
    }
  }
  return server;
}
function json(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {'content-type': 'application/json', 'content-length': Buffer.byteLength(body)});
  res.end(body);
}
module.exports = {createServer, route, outgoingHeaders, sseEvents};
