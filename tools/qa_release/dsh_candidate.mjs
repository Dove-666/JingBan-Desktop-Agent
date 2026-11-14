/** Independent installed-payload acceptance. Uses no development dependencies or live API. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createReadStream, realpathSync } from 'node:fs';
import { cp, mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { createRequire, registerHooks } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { crc32 } from 'node:zlib';

const script = fileURLToPath(import.meta.url);
const repository = resolve(dirname(script), '..', '..');
const args = process.argv.slice(2);
const option = name => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
const payloadArg = option('--payload');
const skipNativeInput = args.includes('--skip-native-input');
if (!payloadArg || (!skipNativeInput && !option('--fixture'))) throw new Error('Usage: node dsh_candidate.mjs --payload <installed directory> (--fixture <owned fixture.exe> | --skip-native-input) [--output <summary.json>]');
const payload = realpathSync(resolve(payloadArg));
const nodePath = join(payload, '_internal', 'runtime', 'node', 'node.exe');
const backend = join(payload, '_internal', 'agent_backend', 'deepseek_harness');
const within = (root, file) => {
  const path = relative(root, file);
  return path === '' || (!isAbsolute(path) && path !== '..' && !path.startsWith(`..${sep}`));
};
assert.ok(!within(repository, payload), 'Candidate must be installed outside the development checkout.');
const output = resolve(option('--output') ?? join(tmpdir(), 'jingban-dsh-candidate.json'));
const fixtureSource = option('--fixture') ? realpathSync(resolve(option('--fixture'))) : undefined;

if (!args.includes('--isolated-child')) {
  const work = await mkdtemp(join(tmpdir(), '鲸伴 独立安装验收 '));
  const profile = join(work, '用户 资料');
  await Promise.all(['tmp', 'AppData', 'LocalAppData'].map(name => mkdir(join(profile, name), { recursive: true })));
  const env = {};
  for (const name of ['SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA']) {
    if (process.env[name]) env[name] = process.env[name];
  }
  Object.assign(env, { PATH: [dirname(nodePath), join(process.env.SYSTEMROOT ?? 'C:\\Windows', 'System32')].join(';'),
    USERPROFILE: profile, HOME: profile, APPDATA: join(profile, 'AppData'), LOCALAPPDATA: join(profile, 'LocalAppData'),
    TEMP: join(profile, 'tmp'), TMP: join(profile, 'tmp'), LANG: 'zh_CN.UTF-8', TZ: 'Asia/Shanghai' });
  const child = spawn(nodePath, [script, '--payload', payload, '--output', output, '--isolated-child', '--work', work,
    ...skipNativeInput ? ['--skip-native-input'] : [],
    ...fixtureSource ? ['--fixture', fixtureSource] : []], {
    cwd: work, env, windowsHide: true, stdio: ['ignore', 'inherit', 'inherit'],
  });
  child.once('error', error => { console.error(`Candidate Node could not start: ${error.code ?? error.name}`); process.exitCode = 1; });
  child.once('exit', code => { process.exitCode = code ?? 1; });
} else {
  await accept();
}

async function digest(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

function packagePath(require, name) {
  const path = realpathSync(require.resolve(name));
  assert.ok(within(payload, path), `Package resolved outside installed payload: ${name}`);
  return path;
}

async function verifyNative(directory) {
  const manifest = JSON.parse(await readFile(join(directory, 'BUILD.json'), 'utf8'));
  assert.equal(manifest.self_contained, true);
  assert.equal(manifest.runtime, 'win-x64');
  const entries = Object.entries(manifest.sha256 ?? {});
  assert.ok(entries.length > 400, 'Native runtime manifest is incomplete.');
  for (const [name, expected] of entries) {
    assert.match(expected, /^[a-f0-9]{64}$/);
    const path = realpathSync(resolve(directory, name));
    assert.ok(within(directory, path), 'Native manifest path escapes runtime directory.');
    assert.equal(await digest(path), expected, `Native payload hash mismatch: ${name}`);
  }
  const actual = [];
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      assert.ok(!entry.isSymbolicLink(), 'Native runtime must not link to development files.');
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await walk(path); else actual.push(path);
    }
  }
  await walk(directory);
  assert.equal(actual.length, entries.length + 1, 'Unexpected unmanifested native files.');
  return { files: entries.length, manifest_sha256: await digest(join(directory, 'BUILD.json')),
    helper_sha256: await digest(join(directory, 'dsh-cua-windows.exe')),
    assembly_sha256: await digest(join(directory, 'dsh-cua-windows.dll')) };
}

async function mockProvider(responder) {
  let count = 0;
  const sockets = new Set();
  const errors = [];
  const server = http.createServer(async (req, res) => {
    try {
      assert.equal(req.url, '/v1/chat/completions');
      assert.equal(req.headers.authorization, 'Bearer owned-payload-test');
      let raw = '';
      for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw);
      const answer = await responder(body, ++count);
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const frame = delta => ({ id: 'fixture', object: 'chat.completion.chunk', created: 1,
        model: 'candidate-fixture', choices: [{ index: 0, delta, finish_reason: null }] });
      res.write(`data: ${JSON.stringify(frame({ role: 'assistant' }))}\n\n`);
      if (answer.tool) res.write(`data: ${JSON.stringify(frame({ tool_calls: [{ index: 0, id: `call-${count}`,
        type: 'function', function: { name: answer.tool, arguments: JSON.stringify(answer.arguments) } }] }))}\n\n`);
      else res.write(`data: ${JSON.stringify(frame({ content: answer.text }))}\n\n`);
      const end = frame({});
      end.choices[0].finish_reason = answer.tool ? 'tool_calls' : 'stop';
      res.end(`data: ${JSON.stringify(end)}\n\ndata: [DONE]\n\n`);
    } catch (error) {
      errors.push(error);
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'Candidate fixture assertion failed.' } }));
    }
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}/v1`, errors,
    get count() { return count; }, close() { for (const socket of sockets) socket.destroy(); server.close(); } };
}

// A minimal standards-based DOCX fixture, using only built-in ZIP primitives.
function docxFixture() {
  const entries = [
    ['[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'],
    ['word/document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DeepSeek Harness installed Office fixture.</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'],
  ];
  const parts = [], central = [];
  let offset = 0;
  for (const [filename, text] of entries) {
    const name = Buffer.from(filename), data = Buffer.from(text), crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    parts.push(local, name, data);
    const row = Buffer.alloc(46);
    row.writeUInt32LE(0x02014b50); row.writeUInt16LE(20, 4); row.writeUInt16LE(20, 6);
    row.writeUInt32LE(crc, 16); row.writeUInt32LE(data.length, 20); row.writeUInt32LE(data.length, 24);
    row.writeUInt16LE(name.length, 28); row.writeUInt32LE(offset, 42);
    central.push(row, name); offset += local.length + name.length + data.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, directory, end]);
}

async function accept() {
  const work = realpathSync(option('--work'));
  const summary = { ok: false, acceptance_complete: false, payload, work, real_api_calls: 0,
    native_input: skipNativeInput ? 'not_run_desktop_busy' : 'pending',
    scope: skipNativeInput ? 'files-resume-readonly-computer-office-native-hashes' : 'full-installed-candidate',
    started_at: new Date().toISOString() };
  const runners = [];
  let server, converter;
  const timeout = setTimeout(() => { for (const runner of runners) runner.cancel().catch(() => {}); }, 240000).unref();
  try {
    assert.equal(realpathSync(process.execPath).toLowerCase(), realpathSync(nodePath).toLowerCase());
    assert.equal(realpathSync(process.cwd()), work);
    assert.ok(!within(repository, work));
    assert.match(work, /鲸伴.* /);
    assert.equal(process.env.NODE_PATH, undefined);
    assert.equal(process.env.NODE_OPTIONS, undefined);
    summary.node = { version: process.version, path: process.execPath, sha256: await digest(nodePath) };
    summary.driver_sha256 = await digest(script);
    // Parent-process module loads are fail-closed; the SDK's child is launched
    // from this payload, with no checkout cwd, NODE_PATH or inherited imports.
    const hook = registerHooks({ resolve(specifier, context, nextResolve) {
      const result = nextResolve(specifier, context);
      if (result.url.startsWith('file:')) {
        const file = realpathSync(fileURLToPath(result.url));
        assert.ok(within(payload, file) || file === script, 'Module resolution escaped installed payload.');
      }
      return result;
    } });
    const require = createRequire(join(backend, 'package.json'));
    const resolved = {};
    for (const name of ['@deepseek-ai/dsh-sdk-client', '@deepseek-ai/dsh/package.json',
      '@deepseek-ai/dsh-cua-windows', '@deepseek-ai/dsh-computer-use', '@deepseek-ai/libreoffice-kit']) {
      resolved[name] = packagePath(require, name);
    }
    summary.module_entries = resolved;
    const runnerPath = realpathSync(join(backend, 'runner.mjs'));
    assert.ok(within(payload, runnerPath));
    summary.runner_sha256 = await digest(runnerPath);
    const { HarnessRunner } = await import(pathToFileURL(runnerPath).href);
    const native = join(dirname(packagePath(require, '@deepseek-ai/dsh-cua-windows/package.json')), 'native', 'bin', 'win-x64');
    summary.native = await verifyNative(native);
    const bytes = 'DSH independently wrote this installed-payload fixture.\n';
    const events = [];
    server = await mockProvider((body, count) => {
      assert.equal(body.messages[0].role, 'system');
      assert.ok(!body.messages.some(message => message.role === 'developer'));
      assert.equal(body.reasoning_effort, 'high');
      if (count === 1) return { tool: 'write', arguments: { file_path: '验收 报告.txt', content: bytes } };
      if (count === 2) return { tool: 'read', arguments: { file_path: '验收 报告.txt' } };
      if (count === 3) {
        assert.ok(body.messages.at(-1).content.includes(bytes.trim()));
        return { tool: 'present', arguments: { files: [{ path: '验收 报告.txt' }] } };
      }
      if (count === 4) return { text: '文件已交付。' };
      if (count === 5) {
        assert.ok(JSON.stringify(body.messages).includes(bytes.trim()));
        assert.ok(body.messages.some(message => message.role === 'tool'));
        return { text: '已恢复刚才的原生会话。' };
      }
      if (count === 6) {
        assert.ok(body.tools.some(tool => tool.function.name === 'cua'));
        assert.ok(JSON.stringify(body.messages).includes('Windows: accessibility tree operations'));
        return { tool: 'cua', arguments: { method: 'listApps', params: {} } };
      }
      assert.equal(count, 7);
      const apps = JSON.parse(body.messages.at(-1).content);
      assert.ok(Array.isArray(apps) && apps.every(app => Number.isInteger(app.pid) && typeof app.name === 'string'));
      return { text: '电脑工具只读验收完成。' };
    });
    const frame = { type: 'run', id: 'candidate-file', task: '写入、读取并交付验收文件。',
      config: { base_url: server.url, model: 'candidate-fixture', api_key: 'owned-payload-test',
        supports_vision: false, reasoning: { profile: 'deepseek', value: 'high', control: 'effort', values: ['default', 'none', 'high'] } },
      options: { workspace: join(work, '任务 工作区'), tools_enabled: true, computer_enabled: false },
      storage: { home: join(work, 'DSH 私有状态'), session_id: null }, context: {} };
    async function run(input) {
      const runner = new HarnessRunner({ emit: event => events.push(event) });
      runners.push(runner);
      let result;
      try { result = await runner.run(input); } catch (error) { throw server.errors[0] ?? error; }
      const runtime = runner.harness.client.runtime;
      assert.equal(realpathSync(runtime.command).toLowerCase(), realpathSync(nodePath).toLowerCase());
      assert.ok(within(work, realpathSync(runtime.cwd)));
      const entry = runtime.args.find(arg => isAbsolute(arg) && /\.(?:mjs|js)$/.test(arg));
      assert.ok(entry && within(payload, realpathSync(entry)), 'SDK child entry must be shipped in the candidate.');
      assert.notEqual(runner.harness.client.child.exitCode, null, 'SDK child must exit before delivery.');
      assert.equal(runner.shutdownConfirmed, true, 'Native receipt and SDK shutdown must both be verified.');
      return result;
    }
    const first = await run(frame);
    assert.equal(first.status, 'completed');
    assert.equal(first.reply, '文件已交付。');
    const file = join(frame.options.workspace, '验收 报告.txt');
    assert.equal(await readFile(file, 'utf8'), bytes);
    assert.equal(first.artifacts[0].sha256, await digest(file));
    assert.equal(first.artifacts[0].size_bytes, Buffer.byteLength(bytes));
    const second = await run({ ...frame, id: 'candidate-resume', task: '恢复并确认刚才的文件。',
      storage: { ...frame.storage, session_id: first.dsh_session_id } });
    assert.equal(second.dsh_session_id, first.dsh_session_id);
    assert.equal(second.reply, '已恢复刚才的原生会话。');
    const computer = await run({ ...frame, id: 'candidate-computer', task: '仅列出应用，不操作任何窗口。',
      options: { ...frame.options, computer_enabled: true },
      storage: { home: join(work, 'DSH 电脑状态'), session_id: null } });
    assert.equal(computer.reply, '电脑工具只读验收完成。');
    assert.ok(events.some(event => event.kind === 'computer' && event.name === 'cua' && event.status === 'completed'));
    assert.ok(!events.some(event => event.type === 'interaction'));
    summary.agent = { file_sha256: await digest(file), resumed_session: true, computer_tool: 'cua',
      completed_tools: events.filter(event => event.type === 'activity' && event.status === 'completed').length,
      mock_model_requests: server.count, child_processes_closed: true };
    server.close(); server = null;
    if (!skipNativeInput) {
    assert.ok(fixtureSource, 'Pass --fixture pointing to the independently built owned desktop fixture.');
    assert.equal(basename(fixtureSource), 'dsh-cua-desktop-fixture.exe');
    const fixtureDirectory = join(work, '自有 原生测试窗口');
    await cp(dirname(fixtureSource), fixtureDirectory, { recursive: true, errorOnExist: true });
    const nativeReport = join(work, 'native-input-stop.json');
    const oracle = join(repository, 'agent_backend', 'deepseek_harness', 'test', 'native-cancel.test.mjs');
    summary.native_oracle_sha256 = await digest(oracle);
    const nativeResult = await new Promise((resolve, reject) => {
      const child = spawn(nodePath, ['--test', oracle], { cwd: work, windowsHide: true,
        env: { ...process.env, JINGBAN_DSH_NATIVE_BACKEND: backend,
          JINGBAN_DSH_NATIVE_FIXTURE: join(fixtureDirectory, basename(fixtureSource)),
          JINGBAN_DSH_NATIVE_REPORT: nativeReport }, stdio: ['ignore', 'pipe', 'pipe'] });
      // Only aggregate status is reported; fixture/model content stays local.
      child.stdout.resume(); child.stderr.resume();
      child.once('error', reject); child.once('exit', code => resolve(code));
    });
    const nativeEvidence = JSON.parse(await readFile(nativeReport, 'utf8'));
    assert.equal(nativeResult, 0, 'Installed native window/control/stop regression failed.');
    assert.equal(nativeEvidence.passed, true);
    for (const check of ['sdk-window-state-uia-input', 'sdk-owned-screenshot-to-model',
      'native-input-observed-before-stop', 'native-release-acknowledged', 'no-input-after-stop']) {
      assert.ok(nativeEvidence.checks.includes(check), `Missing independent native evidence: ${check}`);
    }
    summary.native_operation = nativeEvidence;
    summary.native_input = 'passed';
    }
    const office = await import(pathToFileURL(resolved['@deepseek-ai/libreoffice-kit']).href);
    const inputPath = join(work, '随包 Office 验收.docx'), outputPath = join(work, '随包 Office 验收.pdf');
    await writeFile(inputPath, docxFixture());
    converter = await office.createConverter({ timeoutMs: 120000, fontMetadataCacheDirectory: false,
      resolveEnginePackage: name => packagePath(require, `${name}/package.json`) });
    const converted = await converter.render({ inputPath, outputPath });
    await converter.dispose(); converter = null;
    assert.equal(converted.backend, 'native');
    assert.equal(converted.pageCount, 1);
    const pdf = await readFile(outputPath);
    assert.equal(pdf.subarray(0, 5).toString(), '%PDF-');
    assert.ok(pdf.length > 500);
    summary.office = { backend: converted.backend, pages: converted.pageCount, bytes: pdf.length,
      pdf_sha256: createHash('sha256').update(pdf).digest('hex') };
    summary.module_isolation = true;
    summary.ok = true;
    summary.acceptance_complete = !skipNativeInput;
    hook.deregister();
  } catch (error) {
    summary.failure = { name: error.name, code: error.code ?? null, message: String(error.message).slice(0, 800) };
    process.exitCode = 1;
  } finally {
    clearTimeout(timeout);
    server?.close();
    if (converter) {
      try { await converter.dispose(); }
      catch (error) {
        summary.ok = false;
        summary.cleanup_failure = { name: error.name, code: error.code ?? null };
        process.exitCode = 1;
      }
    }
    await Promise.allSettled(runners.map(runner => runner.cancel()));
    summary.finished_at = new Date().toISOString();
    await mkdir(dirname(output), { recursive: true });
    await writeFile(output, JSON.stringify(summary, null, 2));
    console.log(JSON.stringify(summary));
  }
}
