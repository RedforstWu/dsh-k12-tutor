import { randomUUID } from 'node:crypto';
import { mkdir, readdir, readFile, realpath, rename, writeFile, unlink } from 'node:fs/promises';
import path from 'node:path';

const queues = new Map();
const question = Object.freeze({ initialA: 20, rateA: 3, initialB: 5, rateB: 6, unit: '单位', timeUnit: '分钟' });
const questionId = 'resource-equality';

function fail(code) {
  throw Object.assign(new Error(`practice_${code}`), { code: `k12/practice_${code}` });
}

function fields(input, allowed, required = allowed) {
  if (!input || typeof input !== 'object' || Array.isArray(input) ||
      Object.keys(input).some(key => !allowed.includes(key)) ||
      required.some(key => !Object.hasOwn(input, key))) fail('input');
}

function identifier(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/.test(value)) fail('input');
  return value;
}

function snapshot(binding) {
  if (!binding) return null;
  if (!binding.studentId || !binding.siteOrigin || binding.linked === false) fail('binding');
  return { studentId: binding.studentId, siteOrigin: binding.siteOrigin };
}

async function serialized(cwd, action) {
  const previous = queues.get(cwd) || Promise.resolve();
  const next = previous.catch(() => {}).then(action);
  queues.set(cwd, next);
  try { return await next; }
  finally { if (queues.get(cwd) === next) queues.delete(cwd); }
}

async function save(file, record) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, JSON.stringify(record), { flag: 'wx', mode: 0o600 });
    await rename(temporary, file);
  } finally {
    await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }
}

export function createPracticeService({ context }) {
  async function inSession(input, action) {
    const sessionId = identifier(input.sessionId);
    const trusted = await context(sessionId);
    if (!trusted || typeof trusted.cwd !== 'string' || !path.isAbsolute(trusted.cwd)) fail('session');
    const cwd = await realpath(trusted.cwd);
    return serialized(cwd, async () => {
      const current = await context(sessionId);
      if (!current || typeof current.cwd !== 'string' || !path.isAbsolute(current.cwd) ||
          await realpath(current.cwd) !== cwd) fail('session');
      const binding = snapshot(current.binding);
      const directory = path.join(cwd, '.k12', 'experience-practice');
      await mkdir(directory, { recursive: true });
      return action({ directory, sessionId, binding });
    });
  }

  async function existing(input, action) {
    const activityId = identifier(input.activityId);
    return inSession(input, async ({ directory, sessionId, binding }) => {
      const file = path.join(directory, `${activityId}.json`);
      let record;
      try { record = JSON.parse(await readFile(file, 'utf8')); }
      catch (error) { if (error.code === 'ENOENT') fail('activity'); throw error; }
      if (record.sessionId !== sessionId || record.activityId !== activityId ||
          record.questionId !== questionId || record.version !== 1) fail('activity');
      if (JSON.stringify(record.binding) !== JSON.stringify(binding)) fail('binding');
      return action(record, file);
    });
  }

  return {
    async latest(input) {
      fields(input, ['sessionId']);
      return inSession(input, async ({ directory, sessionId, binding }) => {
        const records = [];
        for (const name of await readdir(directory)) {
          if (!/^[a-f0-9-]{36}\.json$/.test(name)) continue;
          const record = JSON.parse(await readFile(path.join(directory, name), 'utf8'));
          if (record.sessionId === sessionId && record.questionId === questionId && record.version === 1 &&
            JSON.stringify(record.binding) === JSON.stringify(binding)) records.push(record);
        }
        return records.sort((a, b) => b.openedAt.localeCompare(a.openedAt))[0] || null;
      });
    },
    async open(input) {
      fields(input, ['sessionId']);
      return inSession(input, async ({ directory, sessionId, binding }) => {
        const activityId = randomUUID();
        const record = { activityId, sessionId, binding, questionId, version: 1,
          status: 'open', openedAt: new Date().toISOString(), events: [] };
        await save(path.join(directory, `${activityId}.json`), record);
        return { activityId, questionId, version: 1, question: { ...question } };
      });
    },
    async submit(input) {
      fields(input, ['sessionId', 'activityId', 'eventId', 'answer']);
      const eventId = identifier(input.eventId);
      fields(input.answer, ['prediction', 'relation'], ['prediction']);
      const { prediction, relation } = input.answer;
      if (typeof prediction !== 'number' || !Number.isFinite(prediction) || prediction < 0 || prediction > 1440 ||
          (relation !== undefined && (typeof relation !== 'string' || relation.length > 256))) fail('answer');
      const answer = { prediction, ...(relation === undefined ? {} : { relation }) };
      return existing(input, async (record, file) => {
        const duplicate = record.events.find(event => event.eventId === eventId);
        if (duplicate) {
          if (JSON.stringify(duplicate.answer) !== JSON.stringify(answer)) fail('event_collision');
        } else {
          if (record.status !== 'open') fail('closed');
          record.events.push({ eventId, questionId, version: 1, answer,
            result: { correct: prediction === 5 }, receivedAt: new Date().toISOString() });
          await save(file, record);
        }
        return { activityId: record.activityId, eventId, status: 'recorded' };
      });
    },
    async close(input) {
      fields(input, ['sessionId', 'activityId']);
      return existing(input, async (record, file) => {
        if (record.status !== 'closed') {
          record.status = 'closed';
          record.closedAt = new Date().toISOString();
          await save(file, record);
        }
        return { activityId: record.activityId, status: 'closed' };
      });
    },
    async read(input) {
      fields(input, ['sessionId', 'activityId']);
      return existing(input, async record => record);
    },
  };
}
