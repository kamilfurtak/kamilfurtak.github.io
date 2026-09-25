'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {encodeRequest, decodeEvent, StreamDecoder, wireName} = require('../lib/protocol.cjs');
test('long names round-trip across tool definitions, history and tool choice', () => {
  const namespace = 'provider_namespace_' + 'x'.repeat(30), name = 'long_operation_' + 'y'.repeat(50);
  const call = {type: 'function_call', namespace, name, call_id: 'call_1', arguments: '{}'};
  const source = {tools: [{type: 'namespace', name: namespace, tools: [{type: 'function', name}]}], input: [call], tool_choice: {type: 'function', namespace, name}};
  const {request, names} = encodeRequest(source, 'omlx');
  const alias = request.tools[0].name;
  assert.match(alias, /^[A-Za-z0-9_-]{1,64}$/);
  assert.equal(request.input[0].name, alias);
  assert.equal(request.tool_choice.name, alias);
  assert.deepEqual(decodeEvent(request.input[0], names), call);
  assert.equal(encodeRequest({input: [call]}, 'omlx').request.input[0].name, alias);
  assert.equal(source.input[0].name, name);
});
test('collisions are rejected and distinct long names remain distinct', () => {
  assert.notEqual(wireName('x'.repeat(90), 'one'), wireName('x'.repeat(90), 'two'));
  assert.throws(() => encodeRequest({tools: [{type: 'function', name: 'a__b'}, {type: 'namespace', name: 'a', tools: [{type: 'function', name: 'b'}]}]}, 'omlx'), /collision/);
});
test('freeform tools retain exact text through history and fragmented output', () => {
  const input = '*** Begin Patch\n*** Add File: example.txt\n+hello\n*** End Patch';
  const {request, names} = encodeRequest({tools: [{type: 'custom', name: 'apply_patch'}], input: [
    {type: 'custom_tool_call', name: 'apply_patch', input, call_id: 'a'},
    {type: 'custom_tool_call_output', call_id: 'a', output: 'done'},
  ]}, 'omlx');
  assert.equal(JSON.parse(request.input[0].arguments).input, input);
  assert.equal(request.input[1].type, 'function_call_output');
  const decoder = new StreamDecoder(names);
  const added = decoder.decode({type: 'response.output_item.added', item: {type: 'function_call', name: 'apply_patch', id: 'i', arguments: ''}});
  assert.equal(added.item.type, 'custom_tool_call');
  assert.equal(decoder.decode({type: 'response.function_call_arguments.delta', item_id: 'i', delta: '{"input":'}), null);
  const done = decoder.decode({type: 'response.function_call_arguments.done', item_id: 'i', arguments: JSON.stringify({input})});
  assert.equal(done.type, 'response.custom_tool_call_input.done');
  assert.equal(done.input, input);
});
test('desktop notifications become completed tool history without role promotion', () => {
  const item = {type: 'function_call_output', name: 'send_message_to_thread', namespace: 'desktop', output: 'message'};
  const first = encodeRequest({input: [item, item]}, 'omlx').request.input;
  const second = encodeRequest({input: [item, item]}, 'omlx').request.input;
  assert.deepEqual(first, second);
  assert.equal(first[0].call_id, first[1].call_id);
  assert.notEqual(first[0].call_id, first[2].call_id);
  assert.equal(first[1].output, item.output);
  assert(first.every(i => i.type === 'function_call' || i.type === 'function_call_output'));
  assert.throws(() => encodeRequest({input: [{type: 'function_call_output', output: 'unknown'}]}, 'omlx'));
});
test('NInfer transport changes preserve explicit reasoning and reject unknown options', () => {
  const source = {reasoning: {effort: 'xhigh', summary: 'auto'}, stream_options: {include_obfuscation: true, reasoning_summary_delivery: 'auto'}, parallel_tool_calls: true, include: ['reasoning.encrypted_content'], text: {verbosity: 'low'}};
  const {request} = encodeRequest(source, 'ninfer');
  assert.deepEqual(request.reasoning, {effort: 'xhigh'});
  assert.deepEqual(request.stream_options, {include_obfuscation: false});
  assert(!('text' in request) && !('parallel_tool_calls' in request));
  assert.equal(source.reasoning.summary, 'auto');
  assert.throws(() => encodeRequest({stream_options: {unknown: true}}, 'ninfer'));
  assert.throws(() => encodeRequest({text: {format: {type: 'json_schema'}}}, 'ninfer'));
});
test('native profile preserves requests', () => {
  const source = {tools: [{type: 'custom', name: 'patch'}], reasoning: {effort: 'high', summary: 'auto'}};
  assert.deepEqual(encodeRequest(source, 'native').request, source);
});
test('switching to NInfer preserves summary history as assistant reasoning text', () => {
  const reason = {type: 'reasoning', id: 'r1', summary: [{type: 'summary_text', text: 'Use the existing tool result.\nKeep this text.'}], content: []};
  const answer = {type: 'message', role: 'assistant', content: [{type: 'output_text', text: 'Done.'}]};
  const source = {input: [reason, answer, {role: 'user', content: 'Continue.'}]};
  const result = encodeRequest(source, 'ninfer').request;
  assert.deepEqual(result.input, [{type: 'reasoning', id: 'r1', content: [{type: 'reasoning_text', text: reason.summary[0].text}]}, ...source.input.slice(1)]);
  assert.deepEqual(source.input[0], reason);
  assert.deepEqual(encodeRequest(source, 'omlx').request.input, source.input);
  assert.deepEqual(encodeRequest(source, 'native').request, source);
});
test('NInfer replays raw reasoning once and omits unreadable encrypted-only items', () => {
  const content = [{type: 'reasoning_text', text: 'Original plaintext.'}];
  const answer = {role: 'assistant', content: 'Done.'};
  const source = {input: [
    {type: 'reasoning', content, summary: [{type: 'summary_text', text: 'Shorter summary.'}], encrypted_content: 'opaque'},
    answer,
    {type: 'reasoning', summary: [], encrypted_content: 'opaque-only'},
    {role: 'user', content: 'Continue.'},
  ]};
  assert.deepEqual(encodeRequest(source, 'ninfer').request.input, [{type: 'reasoning', content}, answer, source.input[3]]);
  assert.equal(source.input[0].encrypted_content, 'opaque');
  assert.throws(() => encodeRequest({input: [{type: 'reasoning', content: [{type: 'unknown', text: 'x'}]}]}, 'ninfer'), /Unsupported NInfer reasoning history/);
});
