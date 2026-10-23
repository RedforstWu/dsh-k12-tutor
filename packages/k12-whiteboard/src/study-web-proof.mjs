import { createHash } from 'node:crypto';

const metadata = new Set(['agent-preset/selected', 'model/selection', 'permission/preset', 'sandbox/mode', 'plan/mode', 'session/end-seed']);
function canonical(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  throw new Error('non-json event');
}
export function prefixHash(events) { return createHash('sha256').update(canonical(events)).digest('hex'); }

/** Strict Web classifier: no assistant output or model tool card is neutral. */
export function scanWebHistory(events, quiescent = true) {
  let complete = quiescent && Array.isArray(events) && events.length <= 100000;
  let reason = complete ? 'clean' : 'evidence_incomplete';
  if (!Array.isArray(events)) events = [];
  for (let index = 0; index < Math.min(events.length, 100000); index++) {
    const event = events[index];
    if (!event || event.seq !== index || typeof event.type !== 'string') { complete = false; reason = 'evidence_incomplete'; continue; }
    if (event.type.startsWith('assistant/') || event.type.startsWith('tool/') || event.type === 'deliverables/presented') {
      if (reason === 'clean') reason = 'assisted';
    } else if (event.type === 'user/message' || (event.type === 'agent/inbox/spliced' && event.data?.inserted?.length)) {
      if (reason === 'clean') reason = 'prior_input';
    } else if (event.type === 'approval/policy' && event.data && ['ask', 'never'].includes(event.data.policy)
      && (event.data.source === undefined || event.data.source === 'delegation')
      && Object.keys(event.data).every(key => ['policy', 'source'].includes(key))) {
      // Installed DSH log-only policy configuration; never model or learner content.
    } else if (!metadata.has(event.type)) {
      complete = false; reason = 'evidence_incomplete';
    }
  }
  let hash;
  try { hash = prefixHash(events); } catch { complete = false; reason = 'evidence_incomplete'; hash = prefixHash([]); }
  return { throughEventSeq: events.length - 1, complete, clean: complete && reason === 'clean', reason, hash };
}

export function frozenPrefixValid(events, proof) {
  if (!Array.isArray(events) || !Number.isInteger(proof.throughEventSeq) || events.length <= proof.throughEventSeq) return false;
  const prefix = events.slice(0, proof.throughEventSeq + 1);
  if (prefix.some((event, index) => event?.seq !== index)) return false;
  try { return prefixHash(prefix) === proof.sealHash; } catch { return false; }
}

function quiet(scope, sessionId) {
  const agent = scope.agents.get(sessionId);
  return !agent || (agent.status === 'idle' && agent.inbox.nextTurn.length === 0 && agent.inbox.nextStep.length === 0);
}

/** Flush through the public persistence barrier, then inspect the complete original log. */
export async function snapshotWebHistory(scope, sessionId, signal) {
  let quiescent = quiet(scope, sessionId);
  const live = scope.sessions.get(sessionId);
  if (live && !await scope.sessions.flush(live)) quiescent = false;
  if (typeof scope.sessionController.follow !== 'function') quiescent = false;
  else {
    const iterator = scope.sessionController.follow({ address: { kind: 'session', sessionId }, assistantStream: true }, signal)[Symbol.asyncIterator]();
    try {
      const first = await iterator.next();
      if (first.done || first.value?.type !== 'snapshot' || !first.value.assistantStream
        || first.value.assistantStream.activeAttempt) quiescent = false;
    } finally { await iterator.return?.(); }
  }
  const inspection = await scope.sessionController.inspect(sessionId, signal);
  signal?.throwIfAborted();
  const current = scope.sessions.get(sessionId);
  if (!quiet(scope, sessionId) || (current && current.seq !== inspection.events?.length)) quiescent = false;
  if (inspection.meta?.id !== sessionId) quiescent = false;
  return { inspection, scan: scanWebHistory(inspection.events, quiescent) };
}

export function webCertificate(baseline, snapshot, task) {
  const { hostWindow } = baseline;
  const { inspection, scan } = snapshot;
  let complete = scan.complete, clean = scan.clean && hostWindow.clean && !baseline.exposed;
  let reason = scan.reason;
  const prefix = inspection.events?.slice(0, hostWindow.fromEventSeq + 1);
  try {
    if (inspection.meta?.id !== hostWindow.sessionId || !Array.isArray(prefix)
      || prefix.length !== hostWindow.fromEventSeq + 1 || prefixHash(prefix) !== hostWindow.prefixHash) {
      complete = false; clean = false; reason = 'evidence_incomplete';
    }
  } catch { complete = false; clean = false; reason = 'evidence_incomplete'; }
  if (baseline.exposed && reason === 'clean') reason = 'assisted';
  if (!hostWindow.clean && reason === 'clean') reason = 'evidence_incomplete';
  if (!complete) { clean = false; reason = 'evidence_incomplete'; }
  return { submissionId: task.submission.submissionId, textHash: task.submission.textHash,
    snapshotHash: task.exercise.snapshotHash, sessionId: hostWindow.sessionId,
    fromEventSeq: hostWindow.fromEventSeq, throughEventSeq: scan.throughEventSeq,
    prefixHash: hostWindow.prefixHash, sealHash: scan.hash, complete, clean, reason };
}
