import { randomUUID } from 'node:crypto';
import { snapshotWebHistory, webCertificate, frozenPrefixValid } from './study-web-proof.mjs';
import { studyWorkspaceAllowed } from '../../../ops/k12-tutor.mjs';
import { parseStudySkin, withStudySkin } from './study-skin.mjs';

export const launchPath = (session, taskId) => `/api/k12.study.open?${new URLSearchParams({ sessionId: session, taskId })}`;
const FAMILY_STUDY_GUIDANCE = '当前学生已连接家庭学习空间，请使用 study_data 查看学情或 study_paper_create 创建练习。';

function publicLinks(value, session, origin) {
  if (!value || typeof value !== 'object') return value;
  const result = { ...value };
  if (typeof result.taskId === 'string' && (typeof result.answerUrl === 'string' || typeof result.answerLink === 'string')) {
    result.answerUrl = new URL(launchPath(session, result.taskId), origin).href;
    if ('answerLink' in result) result.answerLink = result.answerUrl;
  }
  if (result.latestTask) result.latestTask = publicLinks(result.latestTask, session, origin);
  return result;
}

export function admitted(events, requestId) {
  const matches = message => message?.source?.kind === 'user' && message.source.rpcId === requestId;
  return events?.some(event => event.type === 'user/message' ? matches(event.data)
    : event.type === 'agent/inbox/spliced' && event.data.inserted?.some(matches)) === true;
}

/** Owner-authenticated RPCs; caller cannot select an endpoint, workspace, or command. */
export function studyBridge(ctx, worker, config) {
  let enabled = config.studyEnabled === true;
  let workspaces = config.studyWorkspaces;
  const configure = value => { enabled = value.studyEnabled === true; workspaces = value.studyWorkspaces; };
  let experience;
  ctx.inject(['k12Experience'], scope => {
    const service = scope.k12Experience;
    experience = service;
    scope.effect(() => () => { if (experience === service) experience = undefined; });
  });
  const familyBound = async (cwd, sessionId) => {
    if (!experience || !sessionId) return false;
    const value = await experience.context({ sessionId });
    if (!value.binding && value.familyWorkspace !== true) return false;
    if (!studyWorkspaceAllowed(cwd, [value.cwd])) throw new Error('session identity changed');
    return true;
  };
  const requireLegacyStudy = async args => {
    if (await familyBound(args.cwd, args.session)) throw new Error(FAMILY_STUDY_GUIDANCE);
  };
  const allowed = async (cwd, sessionId) => enabled && !(await familyBound(cwd, sessionId)) && (studyWorkspaceAllowed(cwd, workspaces)
    || (await worker.call('study_binding_context', { cwd })).enabled);
  const continuations = new Map();
  const admitContinuation = (key, action) => {
    if (continuations.has(key)) return continuations.get(key);
    const pending = action().finally(() => continuations.delete(key));
    continuations.set(key, pending);
    return pending;
  };
  let hostOrigin;
  ctx.inject(['webServer'], scope => { hostOrigin = () => scope.webServer.getOrigin(); });
  let resolveCredential = async () => undefined;
  ctx.inject(['credentials'], scope => {
    const resolve = ref => scope.credentials.resolve(ref);
    resolveCredential = resolve;
    scope.effect(() => () => {
      if (resolveCredential === resolve) resolveCredential = async () => undefined;
    });
  });
  const rawCall = async (op, args) => {
    if (op.startsWith('study_')) await requireLegacyStudy(args);
    let privateContext;
    if (['study_query', 'study_flush', 'study_practice', 'study_continuation', 'study_practice_open', 'study_mark_continued', 'study_web_assessment', 'study_web_read', 'study_web_certify', 'study_web_revoke'].includes(op)) {
      const binding = await worker.call('study_binding_context', { cwd: args.cwd });
      if (binding.credentialRef) {
        const resolved = await resolveCredential(binding.credentialRef);
        if (resolved) privateContext = { credentialRef: binding.credentialRef, bindingVersion: binding.bindingVersion, token: resolved.value };
      }
    }
    const value = await worker.call(op, args, privateContext);
    if (op.startsWith('study_')) await requireLegacyStudy(args);
    return publicLinks(value, args.session, hostOrigin?.());
  };
  let sealAssessment;
  let startAssessment;
  const call = async (op, args) => {
    if (op === 'study_practice' && ['read', 'grade'].includes(args.action)) {
      await requireLegacyStudy(args);
      const baseline = await worker.call('study_web_baseline', { cwd: args.cwd, session: args.session, taskId: args.taskId });
      if (baseline?.hostWindow) {
        if (!sealAssessment) throw new Error('assessment evidence service unavailable');
        await sealAssessment(args, baseline);
      }
    }
    const result = await rawCall(op, args);
    if (enabled && (op === 'study_query' || (op === 'whiteboard' && (result?.boardId || result?.boards?.length)))
      && !(await familyBound(args.cwd, args.session))) {
      const binding = await worker.call('study_binding_context', { cwd: args.cwd });
      if (binding.credentialRef) await worker.call('study_web_exposure', { cwd: args.cwd, session: args.session, kind: op === 'study_query' ? 'history' : 'whiteboard' });
    }
    return result;
  };
  let continueTask;
  let poll = async () => {};
  ctx.inject(['sessionController', 'sessions', 'workspaceRegistry', 'agents'], scope => {
    const abort = new AbortController();
    const inspect = async args => {
      abort.signal.throwIfAborted();
      if (scope.workspaceRegistry.archivedSessionIds.includes(args.session)) throw new Error('session archived');
      const value = await scope.sessionController.inspect(args.session, abort.signal);
      if (value?.meta?.id !== args.session || !studyWorkspaceAllowed(value.meta.cwd, [args.cwd])) throw new Error('session identity changed');
      if (!(await allowed(args.cwd, args.session))) throw new Error('workspace disabled');
      return value;
    };
    startAssessment = async (args, signal) => {
      await inspect(args);
      const snapshot = await snapshotWebHistory(scope, args.session, signal);
      const hostWindow = { version: 'web-host/v1', sessionId: args.session,
        fromEventSeq: snapshot.scan.throughEventSeq, prefixHash: snapshot.scan.hash, clean: snapshot.scan.clean };
      return rawCall('study_web_assessment', { ...args, hostWindow });
    };
    sealAssessment = async (args, baseline) => {
      await inspect(args);
      if (baseline.terminalDenied) return;
      const task = await rawCall('study_web_read', { cwd: args.cwd, session: args.session, taskId: args.taskId });
      if (!task.submission || task.submission.deletedAt) return;
      if (baseline.revocation) {
        await rawCall('study_web_revoke', { cwd: args.cwd, session: args.session, taskId: args.taskId, ...baseline.revocation });
        return;
      }
      const snapshot = await snapshotWebHistory(scope, args.session, abort.signal);
      if (baseline.proof && !frozenPrefixValid(snapshot.inspection.events, baseline.proof)) {
        await rawCall('study_web_revoke', { cwd: args.cwd, session: args.session, taskId: args.taskId,
          submissionId: baseline.proof.submissionId, reason: 'evidence_incomplete', sealHash: baseline.proof.sealHash });
        return;
      }
      const proof = baseline.proof || webCertificate(baseline, snapshot, task);
      await rawCall('study_web_certify', { cwd: args.cwd, session: args.session, taskId: args.taskId, proof });
    };
    const audit = async () => {
      const batch = await worker.call('study_web_audit_list', {});
      for (const item of (batch?.items || []).slice(0, 30)) {
        if (abort.signal.aborted) break;
        try {
          if (item.revocation && await allowed(item.cwd, item.session)) {
            await rawCall('study_web_revoke', { cwd: item.cwd, session: item.session, taskId: item.taskId, ...item.revocation });
            continue;
          }
          if (!(await allowed(item.cwd, item.session))) continue;
          const snapshot = await snapshotWebHistory(scope, item.session, abort.signal);
          if (snapshot.inspection.meta?.id !== item.session
            || !studyWorkspaceAllowed(snapshot.inspection.meta.cwd, [item.cwd])
            || !frozenPrefixValid(snapshot.inspection.events, item.proof)) {
            await rawCall('study_web_revoke', { cwd: item.cwd, session: item.session, taskId: item.taskId,
              submissionId: item.proof.submissionId, reason: 'evidence_incomplete', sealHash: item.proof.sealHash });
          }
        } catch { /* Retry the bounded trusted audit registry on the next tick. */ }
      }
    };
    const deliver = args => admitContinuation(`${args.session}:${args.taskId}`, async () => {
      if (await familyBound(args.cwd, args.session)) return false;
      const claim = await worker.call('study_watch_claim', args);
      if (!claim.claimed) return false;
      const leaseId = claim.leaseId;
      try {
      let inspection = await inspect(args);
      const value = await call('study_practice', { ...args, action: 'read' });
      if (value.state !== 'complete') throw new Error('submission unavailable');
      if (value.submission?.deletedAt) throw new Error('submission deleted');
      if (!value.submission) {
        await worker.call('study_watch_state', { ...args, leaseId, state: 'pending' });
        return false;
      }
      const receipt = await call('study_continuation', args);
      if (receipt.continued) {
        await worker.call('study_watch_state', { ...args, leaseId, state: 'delivered' });
        return true;
      }
      inspection = await inspect(args);
      if (!admitted(inspection.events, receipt.requestId)) {
        await scope.sessionController.prompt({ requestId: receipt.requestId, sessionId: args.session,
          mode: 'queue', content: [{ type: 'text', text: `学习网页任务 ${receipt.taskId} 已提交。请用 study_practice(action=read, taskId=${receipt.taskId}) 读取真实作答，再继续讲解。此通知不是学生答案，不得用 study_record 记录。` }],
          clientTimeZone: 'Asia/Shanghai' }, abort.signal);
        const live = scope.sessions.get(args.session);
        if (!live || !await scope.sessions.flush(live)) throw new Error('notification is not durable');
      } else {
        const live = scope.sessions.get(args.session);
        if (live && !await scope.sessions.flush(live)) throw new Error('notification is not durable');
      }
      abort.signal.throwIfAborted();
      await call('study_mark_continued', { ...args, leaseId, requestId: receipt.requestId, submissionId: receipt.submissionId });
      return true;
      } catch (error) {
        const reason = ['session archived', 'session identity changed', 'workspace disabled', 'notification is not durable', 'submission unavailable', 'submission deleted'].includes(error.message) ? error.message : 'original session or service unavailable';
        if (!abort.signal.aborted) await worker.call('study_watch_state', { ...args, leaseId, state: 'paused', reason, retryAfterSeconds: 30 });
        throw error;
      }
    });
    continueTask = deliver;
    let running;
    poll = () => running ??= (async () => {
      if (abort.signal.aborted) return;
      await audit();
      const batch = await worker.call('study_watch_list', {});
      for (const item of batch.items.slice(0, 30)) {
        if (abort.signal.aborted) break;
        const args = { cwd: item.cwd, session: item.session, taskId: item.taskId };
        try { await deliver(args); } catch { /* Per-task state was persisted by the lease owner. */ }
      }
    })().catch(() => {}).finally(() => { running = undefined; });
    scope.effect(() => {
      const timer = setInterval(() => { void poll(); }, 3000);
      timer.unref?.();
      return () => { clearInterval(timer); abort.abort(); continueTask = undefined; sealAssessment = undefined; startAssessment = undefined; };
    });
  });
  ctx.inject(['connection', 'sessionController', 'credentials', 'workspaceRegistry'], scope => {
    scope.effect(() => scope.connection.fetch.register({ path: '/api/k12.study.open', methods: ['GET'], requestBody: 'buffered', fetch: async request => {
      try {
        const url = new URL(request.url);
        if ([...url.searchParams.keys()].some(key => !['sessionId', 'taskId', 'dshSkin', 'dshAppearance'].includes(key))
          || url.searchParams.getAll('sessionId').length !== 1 || url.searchParams.getAll('taskId').length !== 1) throw new Error('invalid launch');
        const preference = parseStudySkin(url.searchParams);
        const session = url.searchParams.get('sessionId'), taskId = url.searchParams.get('taskId');
        if (!session || !taskId) throw new Error('invalid launch');
        if (scope.workspaceRegistry.archivedSessionIds.includes(session)) throw new Error('session archived');
        const inspection = await scope.sessionController.inspect(session, request.signal);
        if (inspection?.meta?.id !== session || !inspection.meta.cwd) throw new Error('session unavailable');
        await requireLegacyStudy({ cwd: inspection.meta.cwd, session });
        if (!(await allowed(inspection.meta.cwd, session))) throw new Error('session unavailable');
        const entry = await call('study_practice_open', { cwd: inspection.meta.cwd, session, taskId });
        return new Response(null, { status: 303, headers: { location: withStudySkin(entry.handoffUrl, preference), 'cache-control': 'no-store', 'referrer-policy': 'no-referrer' } });
      } catch (error) {
        return new Response(error.message === FAMILY_STUDY_GUIDANCE ? FAMILY_STUDY_GUIDANCE : '无法打开练习，请回到老师会话重试。', { status: 403, headers: { 'cache-control': 'no-store', 'content-type': 'text/plain; charset=utf-8' } });
      }
    } }));
    for (const action of ['status', 'connect', 'reconnect', 'sync', 'read', 'continue', 'assessment']) {
      scope.effect(() => scope.connection.fetch.register({ path: `/api/k12.study.${action}`,
        methods: ['POST'], requestBody: 'buffered', fetch: async request => {
          let envelope;
          let result;
          let createdCredential;
          let workspace;
          try {
            envelope = await request.json();
            if (envelope.type !== 'client-request' || typeof envelope.rpcId !== 'string'
              || envelope.method !== `k12.study.${action}`) throw new Error('invalid study envelope');
            const payload = envelope.payload;
            const fields = action === 'connect' ? ['sessionId', 'token'] : action === 'assessment' ? ['sessionId', 'templateId'] : ['sessionId'];
            if (!payload || typeof payload.sessionId !== 'string' || Object.keys(payload).some(key => !fields.includes(key))) throw new Error('invalid study fields');
            if (!enabled) throw new Error('学习入口尚未由管理员启用');
            const inspection = await scope.sessionController.inspect(payload.sessionId, request.signal);
            if (!inspection?.meta?.cwd || inspection.meta.id !== payload.sessionId) throw new Error('session unavailable');
            request.signal.throwIfAborted();
            const args = { cwd: inspection.meta.cwd, session: inspection.meta.id };
            await requireLegacyStudy(args);
            workspace = args.cwd;
            let value;
            if (action === 'assessment') {
              if (!startAssessment || typeof payload.templateId !== 'string') throw new Error('assessment unavailable');
              const latestTask = await startAssessment({ ...args, templateId: payload.templateId }, request.signal);
              value = { ...await call('study_status', args), latestTask };
            } else if (action === 'connect') {
              const connect = async () => {
                await requireLegacyStudy(args);
                if (typeof payload.token !== 'string' || !payload.token.trim() || payload.token.length > 4096) throw new Error('请输入学生令牌');
                const credentialRef = `K12_STUDY_${randomUUID().replaceAll('-', '_')}`;
                createdCredential = credentialRef;
                await scope.credentials.set(credentialRef, payload.token.trim());
                const resolved = await scope.credentials.resolve(credentialRef);
                if (!resolved) throw new Error('凭据保存失败');
                await requireLegacyStudy(args);
                const result = await worker.call('study_connect', args, { credentialRef, token: resolved.value });
                await requireLegacyStudy(args);
                return result;
              };
              value = experience?.withBindingLock ? await experience.withBindingLock(connect) : await connect();
            } else if (action === 'reconnect') {
              if (!(await allowed(args.cwd, args.session))) throw new Error('workspace disabled');
              const binding = await worker.call('study_binding_context', { cwd: args.cwd });
              if (!binding.credentialRef) throw new Error('workspace unbound');
              const resolved = await scope.credentials.resolve(binding.credentialRef);
              if (!resolved) throw new Error('credential unavailable');
              await requireLegacyStudy(args);
              value = await worker.call('study_connect', args, { credentialRef: binding.credentialRef, token: resolved.value });
              await requireLegacyStudy(args);
            } else if (action === 'status') {
              value = await call('study_status', args);
              value.enabled = await allowed(args.cwd, args.session);
            } else {
              if (!(await allowed(args.cwd, args.session))) throw new Error('当前工作区未启用学习记录');
              if (action === 'sync') {
                const sync = await call('study_flush', args);
                value = { ...await call('study_status', args), sync };
              } else if (action === 'read') {
                const current = await call('study_status', args);
                if (!current.latestTask?.taskId) throw new Error('task unavailable');
                const latestTask = await call('study_practice', { ...args, action: 'read', taskId: current.latestTask.taskId });
                if (latestTask.state && latestTask.state !== 'complete') throw new Error('submission read unavailable');
                value = { ...await call('study_status', args), latestTask };
              } else {
                const status = await call('study_status', args);
                if (!status.latestTask?.taskId || !continueTask) throw new Error('task unavailable');
                await continueTask({ ...args, taskId: status.latestTask.taskId });
                value = await call('study_status', args);
              }
            }
            result = { ok: true, value: publicLinks(value, args.session, hostOrigin?.()) };
          } catch (error) {
            if (createdCredential && workspace) {
              try {
                const active = await worker.call('study_binding_context', { cwd: workspace });
                if (active.credentialRef !== createdCredential) await scope.credentials.unset(createdCredential);
              } catch { /* Keep an ambiguous credential rather than delete a possibly active binding. */ }
            }
            // No raw credential provider, CLI, or submitted token errors cross the boundary.
            result = { ok: false, error: { code: 'k12/study', message: error.message === FAMILY_STUDY_GUIDANCE
              ? FAMILY_STUDY_GUIDANCE : '学习操作未完成，请检查连接、当前会话和任务状态后重试。', details: {} } };
          }
          return Response.json({ type: 'server-response', rpcId: envelope?.rpcId || 'invalid', result });
        } }));
    }
  });
  return { call, allowed, configure, poll: () => poll() };
}
