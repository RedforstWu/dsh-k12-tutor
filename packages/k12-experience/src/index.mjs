import { readFile } from 'node:fs/promises';
import { apply as applyHost } from './host.mjs';
import { createPracticeService } from './practice.mjs';
export { name, inject } from './host.mjs';

export async function apply(ctx, config) {
  await applyHost(ctx, config);
  const practice = createPracticeService({ context: sessionId => ctx.k12Experience.context({ sessionId }) });
  ctx.provide('k12Practice', practice);
  for (const action of ['open', 'submit', 'close']) {
    const method = `k12.experience.practice_${action}`;
    ctx.effect(() => ctx.connection.fetch.register({ path: `/api/${method}`, methods: ['POST'], requestBody: 'buffered',
      fetch: async request => {
        let envelope;
        let result;
        try {
          envelope = await request.json();
          if (envelope.type !== 'client-request' || envelope.method !== method || typeof envelope.rpcId !== 'string' ||
            envelope.rpcId.length > 128 || Object.keys(envelope).some(key => !['type', 'method', 'rpcId', 'payload'].includes(key))) throw new Error();
          result = { ok: true, value: await practice[action](envelope.payload) };
        } catch {
          result = { ok: false, error: { code: 'k12/practice', message: '练习未保存，请重试或重新打开任务。', details: {} } };
        }
        return Response.json({ type: 'server-response', rpcId: typeof envelope?.rpcId === 'string' ? envelope.rpcId : 'invalid', result },
          { headers: { 'cache-control': 'no-store' } });
      } }));
  }
  const registry = JSON.parse(await readFile(new URL('../../../characters/registry.json', import.meta.url), 'utf8'));
  const assets = new Map(Object.values(registry).flatMap(character => Object.values(character.states)).map(asset => [asset, asset]));
  for (const character of ['mira', 'orbit']) assets.set(`${character}.svg`, registry[character].states.listening);
  for (const [route, asset] of assets) {
    if (!/^[a-z0-9-]+\.(svg|png)$/.test(asset)) throw new Error('Invalid character asset');
    ctx.effect(() => ctx.connection.fetch.register({
      path: `/api/k12-experience/assets/${route}`, methods: ['GET'], requestBody: 'buffered',
      fetch: async () => new Response(await readFile(new URL(`../../../characters/${asset}`, import.meta.url)),
        { headers: { 'content-type': asset.endsWith('.png') ? 'image/png' : 'image/svg+xml', 'cache-control': 'private, max-age=3600' } }),
    }));
  }
}
