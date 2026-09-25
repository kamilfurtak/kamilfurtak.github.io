#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {loadConfig, readCatalog} = require('../lib/config.cjs');
const {buildCatalog} = require('../lib/catalog.cjs');
const {configure, restore} = require('../lib/setup.cjs');
const {createServer} = require('../lib/server.cjs');
function main(argv) {
  const command = argv.shift();
  if (!command || ['help', '--help', '-h'].includes(command)) {
    console.log(`Local Model Bridge (experimental)\n\nCommands:\n  catalog   --config FILE --openai-catalog FILE [--force]\n  serve     --config FILE\n  configure --config FILE [--codex-config FILE]\n  restore   [--codex-config FILE]\n\nNode.js 24+ is required. Configure edits user settings only, never application files.\nRead README.md before configuring an existing desktop installation.`);
    return;
  }
  const options = {};
  while (argv.length) {
    const flag = argv.shift();
    if (flag === '--force') { options.force = true; continue; }
    if (!['--config', '--openai-catalog', '--codex-config'].includes(flag) || !argv.length || argv[0].startsWith('--')) throw Error('Invalid command arguments; run --help');
    options[flag.slice(2)] = argv.shift();
  }
  const codexFile = path.resolve(options['codex-config'] || path.join(process.env.CODEX_HOME || path.join(os.homedir(), '.codex'), 'config.toml'));
  if (command === 'restore') {
    const backup = restore(codexFile);
    console.log(`Removed bridge settings from ${codexFile}. Unrelated edits were preserved.\nOriginal backup: ${backup}\nRestart the desktop to reload settings, then stop the bridge.`);
    return;
  }
  if (!['catalog', 'serve', 'configure'].includes(command) || !options.config) throw Error('Specify a command and --config FILE');
  const config = loadConfig(path.resolve(options.config));
  if (command === 'catalog') {
    if (!options['openai-catalog']) throw Error('--openai-catalog must point to your own desktop model catalog/cache');
    const input = path.resolve(options['openai-catalog']);
    if (input === config.catalogFile) throw Error('Source and generated catalog paths must differ');
    const catalog = buildCatalog(readCatalog(input), config);
    if (fs.existsSync(config.catalogFile) && !options.force) throw Error('Generated catalog already exists; use --force to refresh it');
    fs.writeFileSync(config.catalogFile, JSON.stringify(catalog, null, 2) + '\n', {mode: 0o600, flag: options.force ? 'w' : 'wx'});
    console.log(`Generated ${config.catalogFile}. Keep this runtime file private; do not commit it.`);
  } else if (command === 'configure') {
    readCatalog(config.catalogFile);
    const backup = configure(codexFile, config);
    console.log(`Configured ${codexFile}\nBackup: ${backup}\nStart the bridge before restarting the desktop. Authentication is unchanged.`);
  } else {
    const catalog = readCatalog(config.catalogFile);
    if (catalog._local_model_bridge?.version !== 1 || !Array.isArray(catalog._local_model_bridge.cloudModels)) throw Error('Generate the catalog with the catalog command before serving');
    for (const model of config.localModels) if (!catalog.models.some(m => m.slug === model.id)) throw Error(`Local model ${model.id} is missing from the generated catalog`);
    const server = createServer({config, catalog, logger: row => console.log(JSON.stringify(row))});
    server.on('error', error => { console.error(error.code === 'EADDRINUSE' ? 'The configured port is already in use; choose another port' : error.message); process.exitCode = 1; });
    server.listen(config.listen.port, config.listen.host, () => console.log(`Local Model Bridge listening on ${config.listen.host}:${config.listen.port}`));
    const stop = () => { server.close(); server.closeAllConnections(); };
    process.once('SIGINT', stop); process.once('SIGTERM', stop);
  }
}
try { main(process.argv.slice(2)); }
catch (error) { console.error(error.message); process.exitCode = 1; }
