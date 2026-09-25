'use strict';
const {createHash} = require('node:crypto');
const hash = value => createHash('sha256').update(value).digest('hex').slice(0, 24);
function wireName(namespace, name) {
  if (typeof name !== 'string' || !name) throw Error('Tool name is required');
  const raw = namespace ? `${namespace}__${name}` : name;
  return /^[A-Za-z0-9_-]{1,64}$/.test(raw) ? raw : raw.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 39) + '_' + hash(raw);
}
function stableJSON(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJSON).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + stableJSON(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
function encodeRequest(original, profile) {
  const request = structuredClone(original);
  const names = new Map();
  if (profile === 'native') return {request, names};
  const seen = new Set();
  const unscoped = new Set((request.tools || []).filter(t => t.type !== 'namespace').map(t => t.name));
  function convert(tool, namespace) {
    if (!['function', 'custom'].includes(tool.type)) throw Error(`Unsupported local tool type: ${tool.type}`);
    const name = wireName(namespace, tool.name);
    if (seen.has(name) || (namespace && unscoped.has(name))) throw Error('Tool alias collision');
    seen.add(name);
    if (namespace || tool.type === 'custom' || name !== tool.name) names.set(name, {namespace, name: tool.name, type: tool.type});
    if (tool.type === 'custom') return {
      type: 'function', name,
      description: (tool.description || '') + '\nPass the exact freeform tool input as the input string in the JSON object.',
      parameters: {type: 'object', properties: {input: {type: 'string'}}, required: ['input'], additionalProperties: false},
    };
    return {...tool, name};
  }
  request.tools = (request.tools || []).flatMap(t => t.type === 'namespace' ? t.tools.map(member => convert(member, t.name)) : [convert(t)]);
  if (Array.isArray(request.input)) {
    const usedIds = new Set(request.input.map(i => i.call_id).filter(Boolean));
    request.input = request.input.flatMap((item, index) => {
      const prefix = [];
      if (item.type === 'function_call_output' && !item.call_id) {
        const name = wireName(item.namespace, item.name);
        const call_id = 'call_notification_' + hash(stableJSON([index, item]));
        if (usedIds.has(call_id)) throw Error('Notification call ID collision');
        usedIds.add(call_id);
        // Completed tool history keeps desktop notifications at tool trust level.
        prefix.push({type: 'function_call', call_id, name, arguments: '{}'});
        item = {type: 'function_call_output', call_id, output: item.output};
      }
      if (['function_call', 'custom_tool_call'].includes(item.type)) {
        item.name = wireName(item.namespace, item.name);
        delete item.namespace;
        if (item.type === 'custom_tool_call') {
          item.type = 'function_call';
          item.arguments = JSON.stringify({input: item.input});
          delete item.input;
        }
      } else if (item.type === 'custom_tool_call_output') item.type = 'function_call_output';
      return [...prefix, item];
    });
  }
  if (request.tool_choice && typeof request.tool_choice === 'object' && ['function', 'custom'].includes(request.tool_choice.type)) {
    request.tool_choice.name = wireName(request.tool_choice.namespace, request.tool_choice.name);
    request.tool_choice.type = 'function';
    delete request.tool_choice.namespace;
  }
  if (profile === 'ninfer') normalizeNinfer(request);
  return {request, names};
}
function normalizeNinfer(request) {
  if (request.reasoning && typeof request.reasoning === 'object') delete request.reasoning.summary;
  if (Array.isArray(request.input)) request.input = request.input.flatMap(item => {
    if (item.type !== 'reasoning') return [item];
    const content = item.content || [];
    const summary = item.summary || [];
    if (!Array.isArray(content) || content.some(p => p.type !== 'reasoning_text' || typeof p.text !== 'string') ||
        !Array.isArray(summary) || summary.some(p => p.type !== 'summary_text' || typeof p.text !== 'string')) {
      throw Error('Unsupported NInfer reasoning history');
    }
    // NInfer accepts plaintext assistant reasoning, not summary parts or opaque
    // provider ciphertext. Prefer raw text; otherwise preserve the summary text
    // at the same assistant reasoning level without duplicating both versions.
    const raw = content.filter(p => p.text.length);
    const replay = raw.length ? raw : summary.filter(p => p.text.length).map(p => ({type: 'reasoning_text', text: p.text}));
    if (!replay.length) return [];
    const result = {...item, content: replay};
    delete result.summary;
    delete result.encrypted_content;
    return [result];
  });
  if ('stream_options' in request) {
    const options = request.stream_options;
    if (!options || Array.isArray(options) || typeof options !== 'object' || Object.keys(options).some(k => !['include_obfuscation', 'reasoning_summary_delivery'].includes(k))) throw Error('Unsupported NInfer stream_options');
    request.stream_options = {include_obfuscation: false};
  }
  for (const key of ['client_metadata', 'prompt_cache_key', 'parallel_tool_calls']) delete request[key];
  if ((request.include || []).some(v => v !== 'reasoning.encrypted_content')) throw Error('Unsupported response include');
  delete request.include;
  if (Object.keys(request.text || {}).some(k => k !== 'verbosity')) throw Error('NInfer text format options are not supported');
  delete request.text;
}
function decodeEvent(value, names) {
  if (Array.isArray(value)) return value.map(v => decodeEvent(v, names));
  if (!value || typeof value !== 'object') return value;
  const result = Object.fromEntries(Object.entries(value).map(([k,v]) => [k, decodeEvent(v, names)]));
  const tool = result.type === 'function_call' && names.get(result.name);
  if (tool) {
    result.name = tool.name;
    if (tool.namespace) result.namespace = tool.namespace;
    if (tool.type === 'custom') {
      const data = result.arguments ? JSON.parse(result.arguments) : {input: ''};
      if (typeof data.input !== 'string') throw Error('Custom tool input must be a string');
      delete result.arguments;
      result.type = 'custom_tool_call';
      result.input = data.input;
    }
  }
  return result;
}
class StreamDecoder {
  constructor(names) { this.names = names; this.customIds = new Set(); }
  decode(original) {
    let event = original;
    if (event.item?.type === 'function_call' && this.names.get(event.item.name)?.type === 'custom') this.customIds.add(event.item.id);
    if (this.customIds.has(event.item_id)) {
      if (event.type === 'response.function_call_arguments.delta') return null;
      if (event.type === 'response.function_call_arguments.done') {
        const data = JSON.parse(event.arguments);
        if (typeof data.input !== 'string') throw Error('Custom tool input must be a string');
        event = {...event, type: 'response.custom_tool_call_input.done', input: data.input};
        delete event.arguments;
      }
    }
    return decodeEvent(event, this.names);
  }
}
module.exports = {wireName, encodeRequest, decodeEvent, StreamDecoder};
