import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export function configureFullAccess(profile, YAML) {
  const plugin = path.join(profile, 'orx-full-access.mjs');
  writeFileSync(plugin, readFileSync(new URL('./full-access.mjs', import.meta.url)));
  const patchPath = path.join(profile, 'cordis.patch.yml');
  const patch = YAML.parseDocument(readFileSync(patchPath, 'utf8'), {
    customTags: [{ tag: 'tag:yaml.org,2002:js', resolve: value => value }],
  });
  const alreadyInstalled = patch.toJSON().some(row =>
    row.insert?.some(entry => entry.id === 'orx-full-access'));
  if (!alreadyInstalled) {
    patch.contents.add(patch.createNode({ insert: [{ id: 'orx-full-access', name: plugin }] }));
    writeFileSync(patchPath, patch.toString(), { mode: 0o600 });
  }
}
