import { randomUUID } from 'node:crypto';
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Engine, DEFAULTS } from '../../../ops/k12-tutor.mjs';
import { studyBridge } from './study-server.mjs';
export const name = 'k12-whiteboard';

export function apply(ctx, config = {}) {
  const worker = new Engine({ python: config.python, engineDir: config.engineDir || DEFAULTS.engineDir });
  const boot = randomUUID();
  const study = studyBridge(ctx, worker, config);
  const engine = { studyAllowed: study.allowed, configureStudy: study.configure, call: (op, args) => study.call(op, op === 'whiteboard' ? { ...args, boot } : args) };
  ctx.provide('k12Engine', engine);
  ctx.effect(() => () => worker.stop());
  ctx.on('session/event', (session, event) => {
    if ((event.type === 'user/message' || (event.type === 'turn/end' && event.data.reason.kind === 'aborted')) && session.header.cwd) {
      void engine.call('whiteboard', { cwd: session.header.cwd, session: session.id, action: 'invalidate' }).catch(error => ctx.logger.warn(error));
    }
  });
  ctx.inject(['connection', 'sessionController', 'webServer'], scope => {
    scope.effect(async () => {
      const root = fileURLToPath(new URL('../lib/fonts/', import.meta.url));
      const files = await readdir(root, { recursive: true, withFileTypes: true });
      const disposers = [];
      for (const file of files.filter(f => f.isFile())) {
        const absolute = path.join(file.parentPath, file.name);
        const relative = path.relative(root, absolute).split(path.sep).join('/');
        disposers.push(await scope.connection.fetch.register({ path: `/api/k12-whiteboard/assets/fonts/${relative}`,
          methods: ['GET', 'HEAD'], requestBody: 'buffered', fetch: async () => new Response(await readFile(absolute), { headers: { 'content-type': 'font/woff2' } }) }));
      }
      return async () => { for (const dispose of disposers) await dispose(); };
    });
    for (const endpoint of ['read', 'list', 'open', 'close', 'claim', 'next', 'pause', 'heartbeat', 'release']) {
      scope.effect(() => scope.connection.fetch.register({ path: `/api/k12.whiteboard.${endpoint}`,
        methods: ['POST'], requestBody: 'buffered', fetch: async request => {
      let envelope;
      let result;
      try {
        envelope = await request.json();
        if (envelope.type !== 'client-request' || typeof envelope.rpcId !== 'string' || envelope.method !== `k12.whiteboard.${endpoint}`) throw new Error('invalid board envelope');
        const payload = envelope.payload;
        const signal = request.signal;
        if (!payload || typeof payload !== 'object' || typeof payload.sessionId !== 'string') throw new Error('session required');
        const allowed = ['sessionId', 'boardId', 'token', 'generation', 'expectedApplied'];
        if (Object.keys(payload).some(key => !allowed.includes(key))) throw new Error('unexpected board field');
        const inspection = await scope.sessionController.inspect(payload.sessionId, signal);
        if (!inspection?.meta?.cwd || inspection.meta.id !== payload.sessionId) throw new Error('session unavailable');
        signal.throwIfAborted();
        const value = await engine.call('whiteboard', { action: endpoint, boardId: payload.boardId,
          token: payload.token, generation: payload.generation, expectedApplied: payload.expectedApplied,
          cwd: inspection.meta.cwd, session: inspection.meta.id });
        result = { ok: true, value };
      } catch (error) {
        result = { ok: false, error: { code: 'k12/board', message: error.message, details: {} } };
      }
      return Response.json({ type: 'server-response', rpcId: envelope?.rpcId || 'invalid', result });
      } }));
    }
  });
}
