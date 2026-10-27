import { createHash, randomUUID } from 'node:crypto';
import { mkdir, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { openExperienceStore } from './store.mjs';
import { createOnlineSpeech } from './online-speech.mjs';
import onlineVoices from '../../../engine/k12_engine/online_voices.json' with { type: 'json' };

export const name = 'k12-experience';
export const inject = ['credentials', 'storageDomain', 'workspaceRegistry', 'sessionController', 'k12Engine', 'connection', 'profileContext'];
const ACTIONS = { me: '/me', mastery: '/mastery', gaps: '/diagnosis', papers: '/papers', paper: '/papers/',
  bank: '/bank/questions?type=single&limit=20' };
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/;
const SAFE_VERSION = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const KNOWLEDGE_ACTIONS = ['knowledge_manifest', 'knowledge_search', 'knowledge_node', 'knowledge_graph'];
const FAMILY_SOURCES = ['teachingProfile', 'mastery', 'gaps'];
const MESSAGES = {
  input: '请求参数不正确。', network: '暂时无法连接学习站点，请稍后重试。',
  auth: '学习站点授权已失效，请由家长重新连接。', service: '学习站点服务暂不可用，请稍后重试。',
  version: '知识图谱版本已变化，请重新读取清单后再查询。',
  knowledge: '共享知识图谱当前不可用或知识点不存在，请先检查清单和知识点 ID。',
  catalog: '学习站点返回的共享知识图谱数据格式无效。',
  identity: '授权对应的学生与已确认身份不一致。', unbound: '当前会话尚未连接学习站点。',
  workspace: '学生工作区不可用。', conflict: '此工作区已属于另一位学生，或此学生已绑定其他工作区。',
  session: '会话不可用。', active: '会话仍有任务执行，请先停止或等待完成。',
  expired: '身份核对已过期，请重新验证授权。', internal: '本机连接操作失败，请重试。',
  unsupported: '首期在线练习仅支持单选和多选题。', request: '请求与已保存的提交状态冲突，请由家长核对站点试卷。',
  speech: '微软在线语音合成暂不可用，请检查网络后重试。',
};

class ExperienceError extends Error {
  constructor(code) { super(MESSAGES[code]); this.code = `k12/${code}`; }
}
const fail = code => { throw new ExperienceError(code); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
function fields(value, allowed, required = []) {
  if (!object(value) || Object.keys(value).some(key => !allowed.includes(key)) || required.some(key => !(key in value))) fail('input');
}
function id(value) { if (typeof value !== 'string' || !SAFE_ID.test(value)) fail('input'); return value; }
function knowledgeResource(input) {
  if (input.action === 'knowledge_manifest') return '/kg/catalog';
  const params = new URLSearchParams();
  if (input.action === 'knowledge_search') {
    params.set('q', input.query);
    params.set('limit', String(input.limit ?? 20));
  }
  if (input.version !== undefined) params.set('version', input.version);
  const query = params.toString();
  const suffix = query ? `?${query}` : '';
  if (input.action === 'knowledge_search') return `/kg/catalog/search${suffix}`;
  const node = `/kg/catalog/nodes/${encodeURIComponent(input.resourceId)}`;
  return input.action === 'knowledge_graph' ? `${node}/graph${suffix}` : `${node}${suffix}`;
}
function knowledgeData(input, data) {
  if (!object(data)) fail('catalog');
  if (input.action === 'knowledge_manifest') {
    if (!(data.version === null || typeof data.version === 'string' && SAFE_VERSION.test(data.version))
      || data.schemaVersion !== 'k12-knowledge-catalog/v1' || !Array.isArray(data.sources) || !Array.isArray(data.books)
      || !['nodeCount', 'knowledgeCount', 'edgeCount'].every(key => Number.isInteger(data[key]) && data[key] >= 0)) fail('catalog');
    return data;
  }
  if (typeof data.version !== 'string' || !SAFE_VERSION.test(data.version)) fail('catalog');
  if (input.version !== undefined && input.version !== data.version) fail('version');
  if (input.action === 'knowledge_search' && (!Array.isArray(data.items) || typeof data.truncated !== 'boolean')) fail('catalog');
  if (input.action === 'knowledge_node' && (!object(data.node) || data.node.id !== input.resourceId)) fail('catalog');
  if (input.action === 'knowledge_graph' && (data.centerId !== input.resourceId || !Array.isArray(data.nodes)
    || !Array.isArray(data.edges) || typeof data.truncated !== 'boolean')) fail('catalog');
  return data;
}
function sessionInput(value) { return id(value); }
function origin(value) {
  if (typeof value !== 'string' || value.length > 2048) fail('input');
  let url;
  try { url = new URL(value); } catch { fail('input'); }
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))) ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash) fail('input');
  return url.origin;
}
function tokenValue(value) {
  if (typeof value !== 'string' || !/^[\x21-\x7e]{1,4096}$/.test(value)) fail('input');
  return value;
}
function scrub(value, token) {
  if (typeof value === 'string') {
    let result = value;
    for (const secret of [token, encodeURIComponent(token)].filter(Boolean)) result = result.split(secret).join('[redacted]');
    return result.replace(/Bearer\s+[^\s"<>]+/gi, 'Bearer [redacted]');
  }
  if (Array.isArray(value)) return value.map(item => scrub(item, token));
  if (object(value)) return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/(token|authorization|secret|password|credential|api.?key)/i.test(key))
    .map(([key, item]) => [scrub(key, token), scrub(item, token)]));
  return value;
}
function studentOf(envelope, token) {
  const student = envelope?.data?.student;
  if (!object(student) || typeof student.displayName !== 'string' || !student.displayName.trim() ||
      typeof student.id !== 'string' || !SAFE_ID.test(student.id) || student.id.includes(token)) fail('service');
  return { id: student.id, displayName: scrub(student.displayName, token),
    ...(typeof student.currentBookId === 'string' && student.currentBookId ? { currentBookId: scrub(student.currentBookId, token) } : {}) };
}
function publicBinding(bindingId, record) {
  const { linked, teachingProfile, ...value } = record;
  if (teachingProfile) {
    value.studentName = teachingProfile.nickname;
    delete value.currentBookId;
    if (teachingProfile.currentBookId) value.currentBookId = teachingProfile.currentBookId;
  }
  return structuredClone({ bindingId, ...value });
}
const fingerprint = (bindingId, token) => createHash('sha256').update(`${bindingId}\0${token}`).digest('hex');
function paperReceipt(data, binding, token) {
  if (!object(data) || typeof data.paperId !== 'string' || !SAFE_ID.test(data.paperId) ||
      data.paperId.includes(token) || typeof data.answerLink !== 'string') fail('service');
  let answer;
  try { answer = new URL(data.answerLink); } catch { fail('service'); }
  if (answer.origin !== binding.siteOrigin || answer.username || answer.password || answer.search || answer.hash ||
      answer.pathname !== `/s/${binding.studentId}/papers/${data.paperId}`) fail('service');
  return { paperId: data.paperId, answerLink: answer.href };
}
async function canonical(directory) {
  if (typeof directory !== 'string' || !path.isAbsolute(directory)) fail('workspace');
  try {
    const resolved = await realpath(directory);
    if (!(await stat(resolved)).isDirectory()) fail('workspace');
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
  } catch { fail('workspace'); }
}

export function createExperienceService({ bindings, requests, credentials, credentialKey, workspaceRegistry, sessionController,
  engine, workspaceRoot, sessionActivity = async () => [], fetch: siteFetch = globalThis.fetch, synthesizeSpeech }) {
  const pending = new Map();
  const snapshots = new Map();
  const attempts = new Map();
  const rejected = new Map();
  const revisions = new Map();
  let mutationTail = Promise.resolve();
  const mutate = work => {
    const result = mutationTail.then(work);
    mutationTail = result.catch(() => {});
    return result;
  };
  async function requestSite(siteOrigin, token, resource, signal, write) {
    let response;
    try {
      response = await siteFetch(`${siteOrigin}/api/agent/v1${resource}`, {
        method: write ? 'POST' : 'GET', headers: { authorization: `Bearer ${token}`, accept: 'application/json',
          ...(write ? { 'content-type': 'application/json', 'idempotency-key': write.key } : {}) },
        ...(write ? { body: JSON.stringify(write.body) } : {}), redirect: 'manual',
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
      });
    } catch { fail('network'); }
    if ([401, 403].includes(response.status)) fail('auth');
    if (response.redirected || response.status >= 300 && response.status < 400) fail('service');
    let result;
    try { result = await response.json(); } catch { fail('service'); }
    if (response.status === 409 && result?.error?.code === 'IDEMPOTENCY_KEY_REUSED') fail('request');
    if (response.status === 409 && result?.error?.code === 'KNOWLEDGE_VERSION_MISMATCH') fail('version');
    if (resource.startsWith('/kg/catalog') && result?.error?.code === 'NOT_FOUND') fail('knowledge');
    if (!response.ok || !object(result) || result.ok !== true || !('data' in result)) fail('service');
    return result;
  }
  async function workspace(record) {
    const value = workspaceRegistry.get(record.workspaceId);
    if (!value || await canonical(value.path) !== await canonical(record.workspacePath)) fail('workspace');
    return value;
  }
  async function inspect(sessionId, signal) {
    let value;
    try { value = await sessionController.inspect(sessionId, signal); } catch { fail('session'); }
    if (value?.meta?.id !== sessionId || !value.meta.cwd) fail('session');
    signal?.throwIfAborted();
    return { inspection: value, cwd: await canonical(value.meta.cwd) };
  }
  async function sessionBinding(sessionId, signal) {
    const resolved = await inspect(sessionId, signal);
    const matches = [...bindings.entries()].filter(([, record]) => record.linked &&
      (process.platform === 'win32' ? record.workspacePath.toLowerCase() : record.workspacePath) === resolved.cwd);
    if (matches.length > 1) fail('conflict');
    if (!matches.length) return { ...resolved, binding: null };
    const [bindingId, record] = matches[0];
    await workspace(record);
    return { ...resolved, binding: publicBinding(bindingId, record) };
  }
  async function active(sessionId, signal) {
    const { items } = await sessionController.list({}, signal || new AbortController().signal);
    const row = items.find(item => item.sessionId === sessionId);
    if (!row) fail('session');
    if (row.running || (await sessionActivity(sessionId)).length) fail('active');
    return row;
  }
  const scopeOf = access => JSON.stringify([access.binding.bindingId, access.binding.siteOrigin, access.binding.studentId,
    access.binding.workspaceId, access.cwd, access.binding.updatedAt, access.revision, access.fingerprint]);
  function rememberAttempts(access, sources) {
    const scope = scopeOf(access);
    const previous = attempts.get(access.binding.bindingId);
    attempts.set(access.binding.bindingId, { scope, sources: { ...(previous?.scope === scope ? previous.sources : {}), ...sources } });
  }
  async function authorization(sessionId, signal) {
    const { binding, cwd } = await sessionBinding(sessionId, signal);
    if (!binding) fail('unbound');
    const revision = revisions.get(binding.bindingId) || 0;
    const credential = await credentials.readRecord(credentialKey(name, binding.bindingId));
    if (credential?.kind !== 'api-key' || !credential.key) fail('auth');
    const token = tokenValue(credential.key);
    return { binding, cwd, token, revision, fingerprint: fingerprint(binding.bindingId, token) };
  }
  async function revalidate(sessionId, access, signal) {
    const current = await authorization(sessionId, signal);
    if (current.binding.bindingId !== access.binding.bindingId || current.binding.studentId !== access.binding.studentId ||
        current.binding.siteOrigin !== access.binding.siteOrigin || current.binding.workspaceId !== access.binding.workspaceId ||
        current.cwd !== access.cwd || current.binding.updatedAt !== access.binding.updatedAt || current.revision !== access.revision) fail('identity');
    if (current.token !== access.token) fail('auth');
    const record = bindings.get(access.binding.bindingId);
    if (!record?.linked) fail('unbound');
    if (record.studentId !== access.binding.studentId || record.siteOrigin !== access.binding.siteOrigin ||
        record.workspaceId !== access.binding.workspaceId || record.workspacePath !== access.cwd ||
        record.updatedAt !== access.binding.updatedAt || (revisions.get(access.binding.bindingId) || 0) !== access.revision) fail('identity');
    return current;
  }
  async function identity(sessionId, signal, access) {
    const me = await requestSite(access.binding.siteOrigin, access.token, ACTIONS.me, signal);
    const observedAt = new Date().toISOString();
    const student = studentOf(me, access.token);
    if (student.id !== access.binding.studentId) fail('identity');
    await revalidate(sessionId, access, signal);
    return { ...access, me, student, observedAt };
  }
  async function saveProfile(access, student, observedAt) {
    const record = bindings.get(access.binding.bindingId);
    await bindings.put(access.binding.bindingId, { ...record, teachingProfile: { nickname: student.displayName,
      currentBookId: student.currentBookId || null, observedAt, credentialFingerprint: access.fingerprint } });
  }
  function learnerView(resolved, sources, verified = true, authorityRef = null) {
    const binding = resolved.binding;
    if (!binding && ![...bindings.entries()].some(([, record]) =>
      (process.platform === 'win32' ? record.workspacePath.toLowerCase() : record.workspacePath) === resolved.cwd)) fail('unbound');
    return structuredClone({ mode: 'family', workspaceRef: resolved.cwd, bindingRef: binding?.bindingId || null, authorityRef,
      identity: verified && binding ? { studentId: binding.studentId, siteOrigin: binding.siteOrigin, workspaceId: binding.workspaceId } : null,
      sources });
  }
  function unavailable(resolved, reason) {
    return learnerView(resolved, Object.fromEntries(FAMILY_SOURCES.map(source =>
      [source, { availability: 'unavailable', observedAt: null, reason }])), false);
  }
  async function rejectSources(sessionId, access, reason, signal) {
    return mutate(async () => {
      const resolved = await sessionBinding(sessionId, signal);
      const bindingId = resolved.binding?.bindingId;
      if (bindingId && (!access || bindingId === access.binding.bindingId &&
          resolved.binding.updatedAt === access.binding.updatedAt && (revisions.get(bindingId) || 0) === access.revision)) {
        const credential = await credentials.readRecord(credentialKey(name, bindingId));
        if (!access || !credential || credential.key === access.token) {
          snapshots.delete(bindingId);
          attempts.delete(bindingId);
          const revision = (revisions.get(bindingId) || 0) + 1;
          revisions.set(bindingId, revision);
          if (access) rejected.set(bindingId, { scope: scopeOf({ ...access, revision }), reason });
          const { teachingProfile, ...record } = bindings.get(bindingId);
          if (teachingProfile) await bindings.put(bindingId, record);
        }
      }
      return unavailable(await sessionBinding(sessionId, signal), reason);
    });
  }
  const service = {
    async withBindingLock(operation) { return mutate(operation); },
    async list(input = {}, signal) {
      fields(input, ['sessionId']);
      const resolved = input.sessionId === undefined ? null : await sessionBinding(sessionInput(input.sessionId), signal);
      const preferences = resolved ? await engine.call('experience', { cwd: resolved.cwd, action: 'read' }) : null;
      return { bindings: [...bindings.entries()].filter(([, record]) => record.linked).map(([key, value]) => publicBinding(key, value)),
        current: resolved?.binding || null, preferences,
        workspaces: workspaceRegistry.list().map(value => ({ workspaceId: value.id, title: value.title, path: value.path })) };
    },
    async verify(input, signal) {
      fields(input, ['siteOrigin', 'token', 'workspaceId'], ['siteOrigin', 'token']);
      const siteOrigin = origin(input.siteOrigin);
      const token = tokenValue(input.token);
      if (siteOrigin.includes(token)) fail('input');
      const workspaceId = input.workspaceId === undefined ? undefined : id(input.workspaceId);
      if (workspaceId && !workspaceRegistry.get(workspaceId)) fail('workspace');
      const student = studentOf(await requestSite(siteOrigin, token, ACTIONS.me, signal), token);
      for (const [key, value] of pending) if (value.expiresAt < Date.now()) pending.delete(key);
      const verificationId = randomUUID();
      const expiresAt = Date.now() + 5 * 60 * 1000;
      pending.set(verificationId, { siteOrigin, token, student, workspaceId, expiresAt });
      return { verificationId, siteOrigin, student, ...(workspaceId ? { workspaceId } : {}), expiresAt };
    },
    async confirm(input, signal) {
      fields(input, ['verificationId', 'expectedStudentId'], ['verificationId', 'expectedStudentId']);
      id(input.verificationId); id(input.expectedStudentId);
      return mutate(async () => {
        const draft = pending.get(input.verificationId);
        if (!draft || draft.expiresAt < Date.now()) { pending.delete(input.verificationId); fail('expired'); }
        if (draft.student.id !== input.expectedStudentId) fail('identity');
        const student = studentOf(await requestSite(draft.siteOrigin, draft.token, ACTIONS.me, signal), draft.token);
        const observedAt = new Date().toISOString();
        if (student.id !== draft.student.id) fail('identity');
        const bindingId = `student-${createHash('sha256').update(`${draft.siteOrigin}\0${student.id}`).digest('hex').slice(0, 32)}`;
        const existing = bindings.get(bindingId);
        if (existing && draft.workspaceId && existing.workspaceId !== draft.workspaceId) fail('conflict');
        let target;
        if (existing) target = await workspace(existing);
        else if (draft.workspaceId) target = workspaceRegistry.get(draft.workspaceId);
        else {
          if (typeof workspaceRoot !== 'string' || !path.isAbsolute(workspaceRoot)) fail('workspace');
          await mkdir(workspaceRoot, { recursive: true });
          const root = await canonical(workspaceRoot);
          const directory = path.join(root, bindingId);
          await mkdir(directory, { recursive: true });
          if (path.dirname(await canonical(directory)) !== root) fail('workspace');
          target = await workspaceRegistry.create(directory, student.displayName);
        }
        if (!target) fail('workspace');
        const workspacePath = await canonical(target.path);
        if ([...bindings.entries()].some(([key, value]) => key !== bindingId &&
          (value.workspaceId === target.id || value.workspacePath === workspacePath))) fail('conflict');
        const ownership = await engine.call('experience_workspace_owner', { cwd: workspacePath });
        if (ownership?.legacyBound !== false) fail('conflict');
        const now = new Date().toISOString();
        const legacyBookId = existing ? existing.currentBookId : student.currentBookId;
        const record = { siteOrigin: draft.siteOrigin, studentId: student.id, studentName: existing?.studentName || student.displayName,
          ...(legacyBookId ? { currentBookId: legacyBookId } : {}), workspaceId: target.id,
          workspacePath, linked: true, createdAt: existing?.createdAt || now, updatedAt: now,
          teachingProfile: { nickname: student.displayName, currentBookId: student.currentBookId || null, observedAt,
            credentialFingerprint: fingerprint(bindingId, draft.token) } };
        await credentials.modifyRecord(credentialKey(name, bindingId), async () => ({ kind: 'api-key', key: draft.token }));
        await bindings.put(bindingId, record);
        revisions.set(bindingId, (revisions.get(bindingId) || 0) + 1);
        snapshots.delete(bindingId);
        attempts.delete(bindingId);
        rejected.delete(bindingId);
        pending.delete(input.verificationId);
        return { binding: publicBinding(bindingId, record), reused: Boolean(existing) };
      });
    },
    async preferences(input, signal) {
      fields(input, ['sessionId', 'preferences'], ['sessionId']);
      const { cwd } = await inspect(sessionInput(input.sessionId), signal);
      return engine.call('experience', { cwd, action: input.preferences === undefined ? 'read' : 'save',
        ...(input.preferences === undefined ? {} : { preferences: input.preferences }) });
    },
    async speech(input, signal) {
      fields(input, ['sessionId', 'voice', 'text', 'rate'], ['sessionId', 'voice', 'text', 'rate']);
      await inspect(sessionInput(input.sessionId), signal);
      if (!onlineVoices.some(voice => voice.id === input.voice) || typeof input.text !== 'string' || !input.text.trim()
        || input.text.length > 2000 || !Number.isFinite(input.rate) || input.rate < 0.5 || input.rate > 2) fail('input');
      try {
        if (!synthesizeSpeech || signal?.aborted) fail('speech');
        return await synthesizeSpeech({ voice: input.voice, text: input.text, rate: input.rate }, signal);
      } catch { fail('speech'); }
    },
    async context(input, signal) {
      fields(input, ['sessionId'], ['sessionId']);
      const resolved = await sessionBinding(sessionInput(input.sessionId), signal);
      return { binding: resolved.binding, cwd: resolved.cwd,
        familyWorkspace: [...bindings.entries()].some(([, record]) =>
          (process.platform === 'win32' ? record.workspacePath.toLowerCase() : record.workspacePath) === resolved.cwd),
        preferences: await engine.call('experience', { cwd: resolved.cwd, action: 'read' }) };
    },
    async learnerContext(input, signal) {
      fields(input, ['sessionId', 'refresh'], ['sessionId']);
      const sessionId = sessionInput(input.sessionId);
      if (input.refresh !== undefined && typeof input.refresh !== 'boolean') fail('input');
      let access;
      try {
        access = await authorization(sessionId, signal);
        let verified;
        let failure;
        const fresh = {};
        const failures = {};
        const requested = {};
        if (input.refresh) {
          const attemptedAt = new Date().toISOString();
          for (const source of FAMILY_SOURCES) requested[source] = { attemptedAt };
          try {
            verified = await identity(sessionId, signal, access);
            const results = await Promise.allSettled(['mastery', 'gaps'].map(async source => {
              requested[source].attemptedAt = new Date().toISOString();
              const result = await requestSite(access.binding.siteOrigin, access.token, ACTIONS[source], signal);
              return { data: scrub(result.data, access.token), observedAt: new Date().toISOString() };
            }));
            for (const [index, source] of ['mastery', 'gaps'].entries()) {
              const result = results[index];
              if (result.status === 'fulfilled') fresh[source] = result.value;
              else if (result.reason?.code === 'k12/auth' || result.reason?.code === 'k12/identity') throw result.reason;
              else failures[source] = result.reason?.code === 'k12/network' ? 'network' : 'service';
            }
          } catch (error) {
            if (!['k12/network', 'k12/service'].includes(error.code)) throw error;
            failure = error.code.slice(4);
          }
          for (const source of FAMILY_SOURCES) {
            const reason = failures[source] || failure;
            if (reason) requested[source].reason = reason;
          }
        }
        return await mutate(async () => {
          const current = await revalidate(sessionId, access, signal);
          const scope = scopeOf(access);
          const denied = rejected.get(access.binding.bindingId);
          if (!input.refresh && denied?.scope === scope) return unavailable(current, denied.reason);
          const previous = snapshots.get(access.binding.bindingId);
          const cached = previous?.scope === scope ? previous.sources : {};
          const previousAttempts = attempts.get(access.binding.bindingId);
          const lastAttempts = { ...(previousAttempts?.scope === scope ? previousAttempts.sources : {}), ...requested };
          if (verified) {
            await saveProfile(access, verified.student, verified.observedAt);
            fresh.teachingProfile = { data: { nickname: verified.student.displayName,
              currentBookId: verified.student.currentBookId || null }, observedAt: verified.observedAt };
            rejected.delete(access.binding.bindingId);
          }
          const profile = bindings.get(access.binding.bindingId)?.teachingProfile;
          const teachingProfile = profile?.credentialFingerprint === access.fingerprint ?
            { data: { nickname: profile.nickname, currentBookId: profile.currentBookId }, observedAt: profile.observedAt } : undefined;
          const sources = Object.fromEntries(FAMILY_SOURCES.map(source => {
            if (fresh[source]) return [source, { availability: 'live', ...fresh[source] }];
            const snapshot = source === 'teachingProfile' ? teachingProfile : cached[source];
            const attempt = lastAttempts[source];
            return [source, { availability: snapshot ? 'cached' : attempt?.reason ? 'unavailable' : 'not_requested',
              ...(snapshot || { observedAt: null }), ...(attempt?.reason ? attempt : {}) }];
          }));
          await revalidate(sessionId, access, signal);
          if (input.refresh) rememberAttempts(access, requested);
          snapshots.set(access.binding.bindingId, { scope, sources: { ...cached,
            ...Object.fromEntries(Object.entries(fresh).filter(([source]) => source !== 'teachingProfile')) } });
          return learnerView(current, sources, true, createHash('sha256').update(scope).digest('hex'));
        });
      } catch (error) {
        if (!['k12/unbound', 'k12/auth', 'k12/identity'].includes(error.code)) throw error;
        return rejectSources(sessionId, access, error.code.slice(4), signal);
      }
    },
    async target(input, signal) {
      fields(input, ['bindingId', 'fromSessionId'], ['bindingId']);
      const bindingId = id(input.bindingId);
      if (input.fromSessionId !== undefined) {
        await inspect(sessionInput(input.fromSessionId), signal);
        await active(input.fromSessionId, signal);
      }
      const record = bindings.get(bindingId);
      if (!record?.linked) fail('unbound');
      const target = await workspace(record);
      const { items } = await sessionController.list({}, signal || new AbortController().signal);
      const archived = new Set(workspaceRegistry.archivedSessionIds);
      for (const item of items.filter(row => target.sessionIds.includes(row.sessionId) && !archived.has(row.sessionId))) {
        if (item.running || (await sessionActivity(item.sessionId)).length) fail('active');
      }
      const candidates = items.filter(item => target.sessionIds.includes(item.sessionId) && !archived.has(item.sessionId) &&
        !item.blank && !item.parentSessionId && item.origin !== 'subagent').sort((a, b) => b.updatedAt - a.updatedAt);
      let sessionId;
      for (const candidate of candidates) {
        const resolved = await inspect(candidate.sessionId, signal);
        if (resolved.cwd !== record.workspacePath) continue;
        if (candidate.running) fail('active');
        sessionId = candidate.sessionId;
        break;
      }
      return { binding: publicBinding(bindingId, record), workspaceId: target.id, workspacePath: record.workspacePath,
        ...(sessionId ? { sessionId } : {}) };
    },
    async unlink(input) {
      fields(input, ['bindingId'], ['bindingId']);
      const bindingId = id(input.bindingId);
      return mutate(async () => {
        const existing = bindings.get(bindingId);
        if (existing?.linked) {
          const { teachingProfile, ...record } = existing;
          await bindings.put(bindingId, { ...record, linked: false, updatedAt: new Date().toISOString() });
        }
        revisions.set(bindingId, (revisions.get(bindingId) || 0) + 1);
        snapshots.delete(bindingId);
        attempts.delete(bindingId);
        rejected.delete(bindingId);
        await credentials.deleteRecord(credentialKey(name, bindingId));
        return { unlinked: true, bindingId };
      });
    },
    async readStudy(input, signal) {
      fields(input, ['sessionId', 'action', 'resourceId', 'query', 'version', 'limit'], ['sessionId', 'action']);
      const sessionId = sessionInput(input.sessionId);
      if (typeof input.action !== 'string' || (!Object.hasOwn(ACTIONS, input.action) && !KNOWLEDGE_ACTIONS.includes(input.action))) fail('input');
      if (input.action === 'paper' || ['knowledge_node', 'knowledge_graph'].includes(input.action)) id(input.resourceId);
      else if (input.resourceId !== undefined) fail('input');
      if (input.action === 'knowledge_search') {
        if (typeof input.query !== 'string' || !input.query.trim() || [...input.query].length > 100
          || (input.limit !== undefined && (!Number.isInteger(input.limit) || input.limit < 1 || input.limit > 100))) fail('input');
      } else if (input.query !== undefined || input.limit !== undefined) fail('input');
      if (['knowledge_search', 'knowledge_node', 'knowledge_graph'].includes(input.action)) {
        if (input.version !== undefined && (typeof input.version !== 'string' || !SAFE_VERSION.test(input.version))) fail('input');
      } else if (input.version !== undefined) fail('input');
      let access;
      try {
        access = await authorization(sessionId, signal);
        const { binding, token, me, student, observedAt } = await identity(sessionId, signal, access);
        const attemptedAt = new Date().toISOString();
        let result;
        let failure;
        try {
          result = input.action === 'me' ? me : await requestSite(binding.siteOrigin, token,
            KNOWLEDGE_ACTIONS.includes(input.action) ? knowledgeResource(input)
              : ACTIONS[input.action] + (input.action === 'paper' ? encodeURIComponent(input.resourceId) : ''), signal);
          if (KNOWLEDGE_ACTIONS.includes(input.action)) knowledgeData(input, result.data);
        } catch (error) {
          if (!['k12/network', 'k12/service'].includes(error.code)) throw error;
          failure = error;
        }
        const resultObservedAt = new Date().toISOString();
        const value = await mutate(async () => {
          await revalidate(sessionId, access, signal);
          await saveProfile(access, student, observedAt);
          await revalidate(sessionId, access, signal);
          rejected.delete(binding.bindingId);
          rememberAttempts(access, { teachingProfile: { attemptedAt: observedAt },
            ...(['mastery', 'gaps'].includes(input.action) ? { [input.action]: { attemptedAt,
              ...(failure ? { reason: failure.code.slice(4) } : {}) } } : {}) });
          if (!failure && ['mastery', 'gaps'].includes(input.action)) {
            const scope = scopeOf(access);
            const previous = snapshots.get(binding.bindingId);
            snapshots.set(binding.bindingId, { scope, sources: { ...(previous?.scope === scope ? previous.sources : {}),
              [input.action]: { data: scrub(result.data, token), observedAt: resultObservedAt } } });
          }
          return scrub(result, token);
        });
        if (failure) throw failure;
        return value;
      } catch (error) {
        if (['k12/unbound', 'k12/auth', 'k12/identity'].includes(error.code)) {
          await rejectSources(sessionId, access, error.code.slice(4), signal);
        }
        throw error;
      }
    },
    async createPaper(input, signal) {
      fields(input, ['sessionId', 'title', 'questionIds', 'requestId'], ['sessionId', 'title', 'questionIds', 'requestId']);
      const sessionId = sessionInput(input.sessionId);
      const requestId = id(input.requestId);
      if (typeof input.title !== 'string' || !input.title.trim() || input.title.length > 200 ||
          !Array.isArray(input.questionIds) || input.questionIds.length < 1 || input.questionIds.length > 50) fail('input');
      const questionIds = input.questionIds.map(id);
      if (new Set(questionIds).size !== questionIds.length) fail('input');
      const body = { title: input.title, questionIds };
      const payloadHash = createHash('sha256').update(JSON.stringify(body)).digest('hex');
      let access;
      return mutate(async () => {
        access = await authorization(sessionId, signal);
        const { binding, token } = await identity(sessionId, signal, access);
        const key = createHash('sha256').update(`${binding.bindingId}\0${sessionId}\0${requestId}`).digest('hex');
        const fingerprint = createHash('sha256').update(`${binding.bindingId}\0${token}`).digest('hex');
        let recorded = requests.get(key);
        if (recorded && recorded.payloadHash !== payloadHash) fail('request');
        if (recorded?.status === 'complete') return { ok: true, data: paperReceipt(recorded.result, binding, token) };
        if (recorded && recorded.credentialFingerprint !== fingerprint) fail('request');
        if (!recorded) {
          for (const questionId of questionIds) {
            const question = (await requestSite(binding.siteOrigin, token, `/bank/questions/${encodeURIComponent(questionId)}`, signal)).data;
            if (question?.id !== questionId || question.retiredAt !== null) fail('service');
            if (!['single', 'multiple'].includes(question.type)) fail('unsupported');
          }
          recorded = { bindingId: binding.bindingId, sessionId, requestId, payloadHash,
            credentialFingerprint: fingerprint, status: 'pending' };
          await requests.put(key, recorded);
        }
        const result = await requestSite(binding.siteOrigin, token, ACTIONS.papers, signal, { key, body });
        const receipt = paperReceipt(result.data, binding, token);
        await requests.put(key, { ...recorded, status: 'complete', result: receipt });
        return scrub({ ok: true, data: receipt }, token);
      }).catch(async error => {
        if (['k12/unbound', 'k12/auth', 'k12/identity'].includes(error.code)) {
          await rejectSources(sessionId, access, error.code.slice(4), signal);
        }
        throw error;
      });
    },
  };
  return Object.fromEntries(Object.entries(service).map(([key, method]) => [key, async (...args) => {
    try { return await method(...args); }
    catch (error) { throw error instanceof ExperienceError ? error : new ExperienceError('internal'); }
  }]));
}

export function registerExperienceRoutes(ctx, service) {
  for (const endpoint of ['list', 'verify', 'confirm', 'preferences', 'speech', 'target', 'unlink', 'study']) {
    const method = `k12.experience.${endpoint}`;
    const routePath = `/api/${method}`;
    ctx.effect(() => ctx.connection.fetch.register({ path: routePath, methods: ['POST'], requestBody: 'buffered',
      fetch: async request => {
        let envelope;
        let result;
        try {
          if (request.method !== 'POST' || new URL(request.url).pathname !== routePath || new URL(request.url).search) fail('input');
          envelope = await request.json();
          fields(envelope, ['type', 'rpcId', 'method', 'payload'], ['type', 'rpcId', 'method', 'payload']);
          if (envelope.type !== 'client-request' || typeof envelope.rpcId !== 'string' || envelope.rpcId.length > 128 ||
              !envelope.rpcId || envelope.method !== method) fail('input');
          const value = await service[endpoint === 'study' ? 'readStudy' : endpoint](envelope.payload, request.signal);
          result = { ok: true, value };
        } catch (error) {
          result = { ok: false, error: { code: error instanceof ExperienceError ? error.code : 'k12/internal',
            message: error instanceof ExperienceError ? error.message : MESSAGES.internal, details: {} } };
        }
        return Response.json({ type: 'server-response', rpcId: typeof envelope?.rpcId === 'string' ?
          scrub(envelope.rpcId, typeof envelope?.payload?.token === 'string' ? envelope.payload.token : '') : 'invalid', result },
        { headers: { 'cache-control': 'no-store' } });
      } }));
  }
}

export async function apply(ctx, config = {}) {
  const workspaceRoot = config.workspaceRoot ?? path.join(ctx.profileContext.dir, 'k12-experience', 'workspaces');
  if (typeof workspaceRoot !== 'string' || !path.isAbsolute(workspaceRoot)) fail('workspace');
  const { credentialKey } = await import('@deepseek-ai/dsh-credentials');
  const domain = await openExperienceStore(ctx.storageDomain);
  const onlineSpeech = createOnlineSpeech();
  ctx.effect(() => () => onlineSpeech.dispose());
  ctx.effect(() => () => domain.close());
  const service = createExperienceService({ bindings: domain.table('bindings'), requests: domain.table('requests'), credentials: ctx.credentials, credentialKey,
    workspaceRegistry: ctx.workspaceRegistry, sessionController: ctx.sessionController, engine: ctx.k12Engine,
    workspaceRoot, synthesizeSpeech: onlineSpeech.synthesize,
    sessionActivity: sessionId => ctx.waterfall('workspace/session-activity', { sessionId }, () => Promise.resolve([])) });
  ctx.provide('k12Experience', service);
  registerExperienceRoutes(ctx, service);
}
