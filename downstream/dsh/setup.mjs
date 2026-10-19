#!/usr/bin/env node
// Import only provider/model configuration into an isolated ACP profile.
// Credentials stay in the local data directory, never in the checkout.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { configureFullAccess } from './configure-access.mjs';

const [entry, dataDir, sourceHome = path.join(os.homedir(), '.dsh')] = process.argv.slice(2);
if (!entry || !dataDir || !existsSync(entry)) {
  throw new Error('Usage: node downstream/dsh/setup.mjs ABSOLUTE_DSH_BIN_JS ORX_DATA_DIR [DSH_HOME]');
}
const require = createRequire(path.resolve(entry));
const YAML = require('yaml');
const source = YAML.parseDocument(
  readFileSync(path.join(sourceHome, 'profiles/web/cordis.patch.yml'), 'utf8'),
  { customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: value => value }] },
);
const entries = source.contents.items.filter(row =>
  ['llm-pi-ai', 'agent-default-model'].includes(row.get('id')),
);
if (entries.length !== 2) {
  throw new Error('Configure provider and default model in DSH Web first.');
}
const selection = entries.find(row => row.get('id') === 'agent-default-model').get('config').toJSON();
const profile = path.join(dataDir, 'agents/dsh/profiles/acp');
mkdirSync(profile, { recursive: true });
writeFileSync(path.join(profile, 'package.json'), JSON.stringify({
  private: true,
  dsh: { profile: { bundles: ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-acp-app'] } },
}, null, 2));
writeFileSync(path.join(profile, 'cordis.yml'), '[]\n');
const overlay = new YAML.Document();
overlay.contents = overlay.createNode([]);
for (const row of entries) overlay.contents.add(row);
overlay.contents.add(overlay.createNode({
  id: 'acp', config: { provider: selection.provider, model: selection.model },
}));
writeFileSync(path.join(profile, 'cordis.patch.yml'), overlay.toString(), { mode: 0o600 });
configureFullAccess(profile, YAML);
const credentials = YAML.parse(readFileSync(path.join(sourceHome, '.credentials.yaml'), 'utf8'));
const serialized = overlay.toString();
const refs = Object.fromEntries(
  Object.entries(credentials.refs ?? {}).filter(([key]) => serialized.includes(key)),
);
if (!Object.keys(refs).length) throw new Error('No matching DSH credential references found.');
writeFileSync(
  path.join(dataDir, 'agents/dsh/.credentials.yaml'),
  YAML.stringify({ version: 1, refs }),
  { mode: 0o600 },
);
writeFileSync(path.join(dataDir, 'dsh.json'), JSON.stringify({
  executable: process.execPath,
  args: [path.resolve(entry), '--profile', 'acp'],
  home: path.resolve(dataDir, 'agents/dsh'),
  version: require('@deepseek-ai/dsh/package.json').version,
  default_model: JSON.stringify([selection.provider, selection.model]),
  default_effort: selection.reasoningEffort,
}, null, 2));
console.log('DSH ACP configured. Models and effort will be discovered from ACP; no inference requested.');
