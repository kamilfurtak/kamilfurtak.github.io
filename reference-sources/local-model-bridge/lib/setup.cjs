'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const BEGIN = '# BEGIN local-model-bridge';
const END = '# END local-model-bridge';
function atomicWrite(file, data, mode = 0o600) {
  const temporary = file + '.tmp-' + randomUUID();
  try { fs.writeFileSync(temporary, data, {mode, flag: 'wx'}); fs.renameSync(temporary, file); }
  finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}
function configure(file, config) {
  if (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink()) throw Error('Pass the real configuration path with --codex-config when config.toml is a symlink');
  const old = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
  if (old.includes(BEGIN) || /^\s*(openai_base_url|model_catalog_json)\s*=/m.test(old)) throw Error('Provider/catalog overrides already exist. Review them before replacing them; no files were changed');
  const stateFile = file + '.local-model-bridge.json';
  if (fs.existsSync(stateFile)) throw Error('A bridge setup record already exists; restore that setup first');
  const host = config.listen.host === '::1' ? '[::1]' : config.listen.host;
  const block = `${BEGIN}\nopenai_base_url = ${JSON.stringify(`http://${host}:${config.listen.port}/v1`)}\nmodel_catalog_json = ${JSON.stringify(config.catalogFile)}\n${END}\n\n`;
  const backup = file + '.before-local-model-bridge-' + randomUUID();
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(backup, old, {mode: 0o600, flag: 'wx'});
  fs.writeFileSync(stateFile, JSON.stringify({block, backup}, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
  const mode = fs.existsSync(file) ? fs.statSync(file).mode & 0o777 : 0o600;
  try { atomicWrite(file, block + old, mode); }
  catch (error) { fs.unlinkSync(stateFile); throw error; }
  return backup;
}
function restore(file) {
  if (fs.lstatSync(file).isSymbolicLink()) throw Error('Pass the real configuration path with --codex-config');
  const stateFile = file + '.local-model-bridge.json';
  const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
  const current = fs.readFileSync(file, 'utf8');
  if (typeof state.block !== 'string' || !state.block.startsWith(BEGIN + '\n') || !state.block.includes('\n' + END + '\n')) throw Error('Invalid bridge setup record');
  if (current.split(state.block).length !== 2) throw Error('The installed block was changed or removed; review the configuration manually');
  atomicWrite(file, current.replace(state.block, ''), fs.statSync(file).mode & 0o777);
  fs.unlinkSync(stateFile);
  return state.backup;
}
module.exports = {configure, restore};
