'use strict';
const fs = require('node:fs');
const path = require('node:path');
function readJSON(file) { return JSON.parse(fs.readFileSync(file, 'utf8')); }
function loadConfig(file) {
  const data = readJSON(file);
  const host = data.listen?.host || '127.0.0.1';
  const port = data.listen?.port ?? 18003;
  if (!['127.0.0.1', '::1', 'localhost'].includes(host)) throw Error('The bridge must listen on loopback');
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw Error('Invalid listen port');
  if (typeof data.catalogFile !== 'string' || !data.catalogFile) throw Error('catalogFile is required');
  if (!Array.isArray(data.localModels) || !data.localModels.length) throw Error('At least one local model is required');
  const ids = new Set();
  const localModels = data.localModels.map(model => {
    if (typeof model.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(model.id) || ids.has(model.id)) throw Error('Invalid or duplicate local model id');
    ids.add(model.id);
    const url = new URL(model.baseUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw Error('baseUrl must be an HTTP(S) URL without credentials, query or fragment');
    if (!['native', 'omlx', 'ninfer'].includes(model.profile)) throw Error('Choose a native, omlx or ninfer profile');
    if (model.apiKeyEnv && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(model.apiKeyEnv)) throw Error('Invalid apiKeyEnv name');
    if (model.contextWindow !== undefined && (!Number.isInteger(model.contextWindow) || model.contextWindow < 1024)) throw Error('Invalid contextWindow');
    const efforts = model.reasoningEfforts || ['low', 'medium', 'high', 'xhigh'];
    if (!Array.isArray(efforts) || !efforts.length || efforts.some(v => !['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(v))) throw Error('Invalid reasoningEfforts');
    const defaultReasoning = model.defaultReasoning || efforts[0];
    if (!efforts.includes(defaultReasoning)) throw Error('defaultReasoning must occur in reasoningEfforts');
    return {...model, baseUrl: url.href.replace(/\/$/, ''), upstreamModel: model.upstreamModel || model.id, reasoningEfforts: efforts, defaultReasoning};
  });
  const requestTimeoutMs = data.requestTimeoutMs ?? 1800000;
  if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 1000 || requestTimeoutMs > 3600000) throw Error('Invalid requestTimeoutMs');
  return {listen: {host, port}, catalogFile: path.resolve(path.dirname(file), data.catalogFile), localModels, requestTimeoutMs};
}
function readCatalog(file) {
  const data = readJSON(file);
  if (!Array.isArray(data.models) || data.models.some(m => !m || typeof m.slug !== 'string')) throw Error('Expected a catalog with a models array of entries containing slug');
  if (new Set(data.models.map(m => m.slug)).size !== data.models.length) throw Error('Duplicate catalog model ids');
  return data;
}
module.exports = {loadConfig, readCatalog};
