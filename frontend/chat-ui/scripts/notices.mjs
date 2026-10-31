import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rootPackage = JSON.parse(fs.readFileSync(path.join(project, 'package.json'), 'utf8'));
const packages = new Map();

function resolvePackage(name, from) {
  let directory = from;
  while (true) {
    const candidate = path.join(directory, 'node_modules', name);
    if (fs.existsSync(path.join(candidate, 'package.json'))) return candidate;
    const parent = path.dirname(directory);
    if (parent === directory) return null;
    directory = parent;
  }
}

function visit(name, from) {
  const directory = resolvePackage(name, from);
  if (!directory || packages.has(directory)) return;
  const pkg = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
  const files = fs.readdirSync(directory).filter((file) => /^(licen[sc]e|copying|notice)(\.|$)/i.test(file) && fs.statSync(path.join(directory, file)).isFile());
  packages.set(directory, { pkg, files: files.map((file) => ({ file, content: fs.readFileSync(path.join(directory, file), 'utf8') })) });
  for (const dependency of Object.keys({ ...pkg.dependencies, ...pkg.optionalDependencies })) visit(dependency, directory);
}

for (const dependency of Object.keys(rootPackage.dependencies)) visit(dependency, project);
const text = [
  'AURA offline chat frontend - third-party notices',
  '',
  'This frontend uses the real @assistant-ui/react package and its runtime and primitives.',
  'UI layout and CSS are maintained locally by AURA. The notices below cover the installed',
  'runtime dependency graph; some listed modules may be removed by production tree shaking.',
  'No frontend dependency makes a network request on behalf of this application.',
  '',
];
for (const { pkg, files } of [...packages.values()].sort((a, b) => a.pkg.name.localeCompare(b.pkg.name))) {
  text.push('='.repeat(78), `${pkg.name} ${pkg.version}`, `Declared license: ${typeof pkg.license === 'object' ? JSON.stringify(pkg.license) : pkg.license || 'see package notices'}`, '');
  for (const { file, content } of files) text.push(`--- ${file} ---`, content.trim(), '');
  if (!files.length) text.push(`Package source: https://www.npmjs.com/package/${pkg.name}/v/${pkg.version}`, '');
}
const output = path.resolve(project, '../../desktop_pet/ui/web_chat/THIRD_PARTY_NOTICES.txt');
fs.writeFileSync(output, text.join('\n'), 'utf8');
process.stdout.write(`Third-party notices: ${packages.size} runtime packages.\n`);
