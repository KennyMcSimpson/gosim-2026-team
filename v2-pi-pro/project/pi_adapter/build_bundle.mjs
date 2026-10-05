import fs from 'node:fs';
import crypto from 'node:crypto';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const outfile = path.join(root, 'worker.bundle.mjs');
const result = await build({
  absWorkingDir: root, entryPoints: ['node_worker.mjs'], outfile,
  bundle: true, platform: 'node', target: 'node22', format: 'esm',
  legalComments: 'inline', metafile: true,
  banner: { js: "import { createRequire as bundleCreateRequire } from 'node:module'; const require = bundleCreateRequire(import.meta.url);" },
});
const externals = Object.values(result.metafile.outputs).flatMap(o => o.imports.filter(i => i.external));
if (externals.some(i => !i.path.startsWith('node:') && ![
  'assert', 'async_hooks', 'buffer', 'child_process', 'crypto', 'diagnostics_channel', 'dns',
  'events', 'fs', 'http', 'http2', 'https', 'module', 'net', 'os', 'path', 'perf_hooks',
  'process', 'querystring', 'readline', 'stream', 'string_decoder', 'timers', 'tls', 'url', 'util',
  'v8', 'vm', 'worker_threads', 'zlib', 'fs/promises', 'stream/web', 'util/types', 'timers/promises',
].includes(i.path))) throw new Error('Bundle has an external package dependency');
const digest = crypto.createHash('sha256').update(fs.readFileSync(outfile)).digest('hex');
fs.writeFileSync(path.join(root, 'worker.bundle.sha256'), digest + '  worker.bundle.mjs\n');
const packageRoots = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.match(/^node_modules\/((?:@[^/]+\/)?[^/]+)/);
  if (match) packageRoots.add(match[1]);
}
const licenses = [...packageRoots].sort().map(name => {
  const directory = path.join(root, 'node_modules', name);
  const files = fs.readdirSync(directory).filter(n => /^licen[sc]e(?:\.|$)/i.test(n));
  const text = files.map(n => fs.readFileSync(path.join(directory, n), 'utf8')).join('\n') ||
    (name.startsWith('@earendil-works/pi-') ? fs.readFileSync(path.join(root, 'Pi.LICENSE.txt'), 'utf8') : '');
  if (!text) throw new Error('Missing bundled package license: ' + name);
  return name + '\n' + text;
}).join('\n\n');
fs.writeFileSync(path.join(root, 'THIRD_PARTY_LICENSES.txt'), licenses + '\n');
console.log(JSON.stringify({ sha256: digest, bytes: fs.statSync(outfile).size,
  inputs: Object.keys(result.metafile.inputs).length, externalNodeModulesOnly: true }));
