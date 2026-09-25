'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {configure, restore} = require('../lib/setup.cjs');
const {buildCatalog} = require('../lib/catalog.cjs');
const {loadConfig} = require('../lib/config.cjs');
function temporary(t) { const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'model-bridge-test-')); t.after(() => fs.rmSync(dir, {recursive: true, force: true})); return dir; }
const config = {listen: {host: '127.0.0.1', port: 18003}, catalogFile: '/temporary/models.json'};
test('configure/restore preserves unrelated edits and keeps an original backup', t => {
  const file = path.join(temporary(t), 'config.toml');
  const old = 'model = "gpt-test"\n[features]\nexample = true\n';
  fs.writeFileSync(file, old);
  const backup = configure(file, config);
  assert.equal(fs.readFileSync(backup, 'utf8'), old);
  assert(fs.readFileSync(file, 'utf8').startsWith('# BEGIN local-model-bridge\nopenai_base_url'));
  fs.appendFileSync(file, '# later user edit\n');
  restore(file);
  assert.equal(fs.readFileSync(file, 'utf8'), old + '# later user edit\n');
});
test('existing provider settings and modified bridge blocks are not overwritten', t => {
  const file = path.join(temporary(t), 'config.toml');
  fs.writeFileSync(file, 'openai_base_url = "http://existing.invalid"\n');
  assert.throws(() => configure(file, config), /already exist/);
  fs.writeFileSync(file, 'model = "gpt-test"\n');
  configure(file, config);
  const changed = fs.readFileSync(file, 'utf8').replace('18003', '18004'); fs.writeFileSync(file, changed);
  assert.throws(() => restore(file), /changed or removed/);
  assert.equal(fs.readFileSync(file, 'utf8'), changed);
});
test('catalog generation preserves cloud entries and customizes local capabilities', () => {
  const source = {models: [{slug: 'gpt-test', visibility: 'list', base_instructions: 'fixture instructions', future_schema_field: {a: 1}}]};
  const local = {id: 'my-model', profile: 'ninfer', contextWindow: 64000, reasoningEfforts: ['low'], defaultReasoning: 'low'};
  const result = buildCatalog(source, {localModels: [local]});
  assert.deepEqual(result.models[1], source.models[0]);
  assert.equal(result.models[0].slug, 'my-model'); assert.equal(result.models[0].context_window, 64000);
  assert.equal(result.models[0].supports_search_tool, false);
  assert.deepEqual(result.models[0].future_schema_field, {a: 1});
  assert.equal(source.models.length, 1);
  assert.throws(() => buildCatalog(source, {localModels: [{...local, id: 'gpt-test'}]}), /must differ/);
});
test('configuration rejects non-loopback listeners and credentials in URLs', t => {
  const file = path.join(temporary(t), 'config.json');
  const data = {catalogFile: 'models.generated.json', localModels: [{id: 'local', baseUrl: 'http://localhost:8080/v1', profile: 'native'}]};
  fs.writeFileSync(file, JSON.stringify(data)); assert.equal(loadConfig(file).localModels[0].id, 'local');
  fs.writeFileSync(file, JSON.stringify({...data, listen: {host: '0.0.0.0'}})); assert.throws(() => loadConfig(file), /loopback/);
  data.localModels[0].baseUrl = 'http://user:secret@localhost/v1';
  fs.writeFileSync(file, JSON.stringify(data)); assert.throws(() => loadConfig(file), /without credentials/);
});
