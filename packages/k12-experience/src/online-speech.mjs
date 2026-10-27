import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function terminate(child) {
  if (child.exitCode != null || child.signalCode != null) return;
  if (process.platform !== 'win32' || !Number.isSafeInteger(child.pid)) { child.kill(); return; }
  const killer = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/taskkill.exe'),
    ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
  killer.on('error', () => child.kill());
}

const root = fileURLToPath(new URL('../../../', import.meta.url));
export function createOnlineSpeech({ spawnProcess = spawn, timeoutMs = 60000 } = {}) {
  const children = new Map();
  const synthesize = (input, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted || children.size >= 2) { reject(new Error('online speech unavailable')); return; }
    const bundled = path.join(root, 'bin/win32-x64/uv.exe');
    const command = process.platform === 'win32' && existsSync(bundled) ? bundled : 'uv';
    const child = spawnProcess(command, ['run', '--no-project', '--quiet', '--python', '3.12', '--with', 'edge-tts==7.3.1',
      'python', path.join(root, 'engine/k12_engine/online_speech.py')], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } });
    let done = false, size = 0;
    const chunks = [];
    const finish = (error, value) => {
      if (done) return;
      done = true; clearTimeout(timer); signal?.removeEventListener('abort', abort);
      if (error) { if (children.has(child)) terminate(child); reject(new Error('online speech unavailable')); }
      else { children.delete(child); resolve(value); }
    };
    const abort = () => finish(true);
    children.set(child, abort);
    const timer = setTimeout(abort, timeoutMs);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    child.on('error', () => { children.delete(child); abort(); });
    child.stdin.on('error', abort);
    child.stderr.resume();
    child.stdout.on('error', abort);
    child.stdout.on('data', data => {
      size += data.length;
      if (size > 2 * 1024 * 1024) finish(true);
      else chunks.push(data);
    });
    child.on('close', code => {
      children.delete(child);
      finish(code !== 0 || size === 0,
        { voice: input.voice, mime: 'audio/mpeg', audioBase64: Buffer.concat(chunks).toString('base64') });
    });
    child.stdin.end(JSON.stringify(input));
  });
  return { synthesize, dispose() { for (const abort of children.values()) abort(); } };
}
