/** k12 tutor runtime: the tutor's tools and its structural safety rails, as one DSH preset plugin.
 *
 * Everything that decides lives in the Python engine (`engine/k12_engine`), reached through one
 * long-lived JSON-lines worker per DSH process; this file only adapts DSH events to engine ops.
 *
 *   tools.guard         only the tutor's tools may run: read stays inside readRoots, present only delivers
 *                       pages under <workspace>/activities/
 *   agent/pre-step      the student's message goes through the engine's input checks; the notes it
 *                       returns (learner summary, crisis / integrity / privacy rules, route state) join the step
 *   agent/turn-stopping the finished reply goes through the reply checks; a violation steers one correction
 *
 * One workspace directory is one student: the engine keeps `<cwd>/.k12/learner.json`, per-session
 * state, `guard.jsonl` and `alerts.jsonl` there.
 *
 * Paths default to this package's own layout (engine/, skills/, subjects/ next to ops/), so the same file
 * works from a source checkout and from an installed bundle. The engine runs on config.python, else
 * $K12_PYTHON, else `uv run` with the engine's dependencies (uv provisions them on first use).
 */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync, realpathSync } from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

export const name = 'k12-tutor';
export const inject = ['tools', 'agents', 'k12Engine'];

export const TOOLS = ['route_task', 'math_check', 'learner_profile', 'make_activity', 'whiteboard', 'study_record', 'study_data', 'study_paper_create', 'experience_practice_read', 'present', 'read'];
export const ENGINE_TOOLS = ['route_task', 'math_check', 'learner_profile', 'make_activity', 'whiteboard', 'study_record'];
export const STUDY_TOOLS = ['study_query', 'study_assessment', 'study_practice'];

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULTS = {
  engineDir: path.join(ROOT, 'engine'),
  readRoots: [path.join(ROOT, 'skills'), path.join(ROOT, 'subjects')],
};
const UV_ARGS = ['run', '--no-project', '--quiet', '--python', '3.12', '--with', 'sympy>=1.13', '--with', 'pyyaml>=6', '--with', 'jsonschema>=4', 'python'];

/** The command that starts the engine worker. */
export function engineCommand(python, environ = process.env) {
  const interpreter = python || environ.K12_PYTHON;
  const serve = ['-m', 'k12_engine', 'serve'];
  if (interpreter) return [interpreter, serve];
  const bundledUv = path.join(ROOT, 'bin', 'win32-x64', 'uv.exe');
  const uv = process.platform === 'win32' && process.arch === 'x64' && existsSync(bundledUv) ? bundledUv : 'uv';
  return [uv, [...UV_ARGS, ...serve]];
}

/** One engine worker; requests are serialized by id, a dead worker is restarted on the next call. */
export class Engine {
  constructor({ python, engineDir }) {
    this.python = python;
    this.engineDir = engineDir;
    this.child = undefined;
    this.pending = new Map();
    this.next = 1;
  }

  start() {
    const [command, args] = engineCommand(this.python);
    const child = spawn(command, args, {
      cwd: this.engineDir,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONPATH: this.engineDir, PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    readline.createInterface({ input: child.stdout }).on('line', line => {
      let msg;
      try { msg = JSON.parse(line); } catch { return; }
      const waiter = this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id);
      msg.ok ? waiter.resolve(msg.value) : waiter.reject(new Error(msg.error));
    });
    let stderr = '';
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.on('error', error => {
      if (this.child === child) this.child = undefined;
      const hint = command === 'uv' ? '没有找到 uv：安装 uv（https://docs.astral.sh/uv/），或在 preset 配置 python / 设置 K12_PYTHON' : '';
      for (const waiter of this.pending.values()) waiter.reject(new Error(`k12 engine could not start (${command}): ${error.message}. ${hint}`));
      this.pending.clear();
    });
    child.on('exit', code => {
      if (this.child === child) this.child = undefined;
      for (const waiter of this.pending.values()) waiter.reject(new Error(`k12 engine exited (${code}): ${stderr.trim().slice(-500)}`));
      this.pending.clear();
    });
    this.child = child;
  }

  call(op, args, transportContext) {
    if (!this.child) this.start();
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(JSON.stringify({ id, op, args, transportContext }) + '\n');
    });
  }

  stop() {
    this.child?.kill();
    this.child = undefined;
  }
}

const text = { type: 'string', minLength: 1 };
const object = (properties, required) => ({ type: 'object', properties, required, additionalProperties: false });
const exerciseSlot = { type: 'string', pattern: '^(diagnose|practice|transfer|review|reflection)-[1-9][0-9]{0,3}$' };
const scope = { oneOf: [object({ kind: { const: 'whole' } }, ['kind']), object({ kind: { const: 'reflection' } }, ['kind']), object({ kind: { const: 'step' }, stepKey: text }, ['kind', 'stepKey'])] };
const judgment = { oneOf: [
  object({ state: { const: 'pending' }, reason: { enum: ['technical', 'incomplete', 'needs_review'] } }, ['state', 'reason']),
  object({ state: { const: 'graded' }, correct: { type: 'boolean' }, errorCause: { enum: ['concept', 'method', 'calc', 'misread', 'careless', null] } }, ['state', 'correct', 'errorCause']),
] };
export const SCHEMAS = {
  study_paper_create: {
    type: 'object', additionalProperties: false, required: ['title', 'questionIds', 'requestId'],
    properties: { title: { type: 'string', minLength: 1, maxLength: 200 },
      questionIds: { type: 'array', minItems: 1, maxItems: 50, uniqueItems: true, items: text },
      requestId: { type: 'string', minLength: 1, maxLength: 128 } },
  },
  experience_practice_read: { type: 'object', additionalProperties: false, properties: {} },
  study_data: {
    type: 'object', additionalProperties: false, required: ['action'],
    properties: {
      action: { type: 'string', enum: ['me', 'mastery', 'gaps', 'papers', 'paper', 'bank', 'knowledge_manifest', 'knowledge_search', 'knowledge_node', 'knowledge_graph'] },
      resourceId: { type: 'string', minLength: 1, maxLength: 200, pattern: '^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$' },
      query: { type: 'string', minLength: 1, maxLength: 100 },
      version: { type: 'string', minLength: 1, maxLength: 128, pattern: '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$' },
      limit: { type: 'integer', minimum: 1, maximum: 100 },
    },
  },
  study_record: {
    type: 'object',
    oneOf: [
      object({ action: { const: 'open' }, taskRef: text, slot: exerciseSlot, question: object({ type: { enum: ['single', 'multiple', 'blank', 'free'] }, stem: text, options: { type: ['array', 'null'], items: object({ key: text, text }, ['key', 'text']) }, questionId: text, referenceAnswer: { type: ['string', 'null'] } }, ['type', 'stem']), purpose: { enum: ['diagnosis', 'practice', 'transfer', 'review'] }, follows: text, taughtBy: text }, ['action', 'taskRef', 'slot', 'question', 'purpose']),
      object({ action: { const: 'open' }, taskRef: text, slot: exerciseSlot, assessment: { oneOf: [
        object({ templateId: { const: 'linear-function-value/v1' }, params: object({ k: { type:'integer', minimum:-20, maximum:20, not:{const:0} }, b:{type:'integer',minimum:-20,maximum:20}, x:{type:'integer',minimum:-20,maximum:20} }, ['k','b','x']) }, ['templateId','params']),
        ...['linear-equation/v1','linear-inequality/v1'].map(templateId => object({ templateId: {const:templateId}, params: object({ a:{type:'integer',minimum:-20,maximum:20,not:{const:0}}, b:{type:'integer',minimum:-20,maximum:20}, c:{type:'integer',minimum:-20,maximum:20}, ...(templateId.includes('inequality') ? {op:{enum:['<','>']}} : {}) }, templateId.includes('inequality') ? ['a','b','c','op'] : ['a','b','c']) }, ['templateId','params']))
      ] }, purpose: { enum: ['diagnosis','practice','transfer','review'] }, follows:text, taughtBy:text }, ['action','taskRef','slot','assessment','purpose']),
      object({ action: { const: 'answer' }, exerciseId: text, responseRef: text, answerSlot: text, scope, judgment, reportedAssistance: { enum: ['none', 'hint', 'revealed', 'unknown'] } }, ['action', 'exerciseId', 'responseRef', 'answerSlot', 'scope', 'judgment']),
      object({ action: { const: 'revise' }, eventId: text, localRevision: { type: 'integer', minimum: 1 }, judgment, mappingVersion: {const:'tutor-math/v1'}, reason: text }, ['action', 'eventId', 'localRevision', 'reason']),
      object({ action: { const: 'skip' }, exerciseId: text, responseRef: text }, ['action', 'exerciseId', 'responseRef']),
      object({ action: { const: 'dismiss' }, responseRef: text, reason: { enum: ['self_report', 'not_answer', 'context', 'technical'] } }, ['action', 'responseRef', 'reason']),
    ],
  },
  study_query: object({ kind: { enum: ['recent', 'wrong', 'mastery', 'gaps'] }, limit: { type: 'integer', minimum: 1, maximum: 100 }, cursor: text, knowledgeId: text, exerciseId: text }, ['kind']),
  whiteboard: {
    type: 'object', additionalProperties: false, required: ['action'],
    properties: {
      action: { type: 'string', enum: ['open', 'append', 'pause', 'close', 'read', 'list'] },
      boardId: { type: 'string', description: 'Only for reopening a board returned by list.' },
      steps: { type: 'array', maxItems: 12, items: { type: 'object', additionalProperties: false,
        required: ['draw', 'say', 'holdMs'], properties: {
          say: { type: 'string', maxLength: 1000 }, holdMs: { type: 'integer', minimum: 0, maximum: 10000 },
          draw: { type: 'array', maxItems: 40, items: { type: 'object', additionalProperties: false,
            required: ['kind', 'id'], properties: {
              kind: { type: 'string', enum: ['text', 'rect', 'ellipse', 'line', 'arrow', 'remove'] },
              id: text, x: { type: 'number' }, y: { type: 'number' }, w: { type: 'number' }, h: { type: 'number' },
              dx: { type: 'number' }, dy: { type: 'number' }, text: { type: 'string', maxLength: 1000 },
            } } },
        } } },
    },
  },
  route_task: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['start', 'adapt', 'resume', 'finish', 'reveal'] },
      taskRef: text, exerciseId: text, goal: text, method: { type: ['string', 'null'], enum: ['guided-hints', 'worked-example', 'self-explanation', 'compare-contrast', 'representation', 'retrieval', null] },
      focus: { type: ['string', 'null'], maxLength: 500 }, nextObservation: { type: ['string', 'null'], maxLength: 500 },
      evidenceRefs: { type: 'array', maxItems: 20, items: text },
      playbook: { type: 'string', enum: ['solve', 'concept', 'diagnose', 'practice', 'review-plan', 'writing', 'inquiry', 'emotion', 'boundary', 'figure-it-out'] },
      subject: { type: 'string', description: 'math, chinese, english, physics, chemistry, biology, history, geography, politics, other' },
      grade: { type: 'integer', minimum: 1, maximum: 12, description: 'only when the student just told you; otherwise the profile grade is used' },
      topic: { type: 'string', description: 'what the task is about in a few words, e.g. 一元一次方程 去括号' },
      problem: { type: 'string', description: 'the problem text, transcribed from the photo if there is one' },
      knowledge_points: { type: 'array', items: text, description: 'knowledge point ids if you already know them' },
      assessment_context: { type: 'string', enum: ['none', 'homework', 'exam-prep', 'exam-live', 'unknown'] },
      reason: { type: 'string', description: 'adapt, finish or reveal: a short teaching reason, not internal reasoning' },
    },
    required: ['action'],
    additionalProperties: false,
  },
  math_check: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['solve', 'simplify', 'factor', 'expand', 'evaluate', 'equivalent', 'check_solution'] },
      expr: { type: 'string', description: 'one expression, equation or inequality in school notation, e.g. 3(x-2)=2x+1' },
      exprs: { type: 'array', items: text, description: 'several: a system to solve, or the two sides to compare for equivalent' },
      variables: { type: 'array', items: text },
      values: { type: 'object', additionalProperties: { type: 'string' }, description: 'evaluate: variable values' },
      answer: { type: 'object', additionalProperties: { type: 'string' }, description: 'check_solution: the student answer, e.g. {"x":"7"}' },
    },
    required: ['action'],
    additionalProperties: false,
  },
  make_activity: {
    type: 'object',
    properties: {
      kind: { type: 'string', enum: ['template', 'scheme', 'custom'], description: 'template first, then a scheme component, custom only when neither fits' },
      purpose: { type: 'string', minLength: 1, maxLength: 500, description: 'the concrete student action this page supports; create only when it adds a useful action' },
      template: { type: 'string', enum: ['resource-comparison', 'number-line', 'balance', 'kb-sliders', 'zero-product', 'inequality-line', 'substitution-table', 'stepwise'] },
      themeId: { type: 'string', enum: ['classic', 'voxel-builder', 'space-explorer'] },
      scheme: { type: 'string', description: 'interaction-scheme id, e.g. choice-text.text-single (listed in references/activities.md)' },
      style: { type: 'string', enum: ['cartoon', 'retro-industrial', 'urban-tech'] },
      params: { type: 'object', additionalProperties: true, description: 'template or scheme parameters; formats in references/activities.md' },
      html: { type: 'string', description: 'custom: a self-contained page (inline CSS/JS, no network, no iframes)' },
      title: { type: 'string' },
    },
    required: ['kind', 'purpose'],
    additionalProperties: false,
  },
  learner_profile: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['read', 'update'] },
      refresh: { type: 'boolean', description: 'read: explicitly refresh current verified sources; unavailable is not empty history' },
      updates: {
        type: 'array',
        items: {
          oneOf: [
            object({ op: { const: 'basics' }, grade: { type: 'integer', minimum: 1, maximum: 12 },
              textbooks: { type: 'object', additionalProperties: { type: 'string' } }, nickname: text }, ['op']),
            object({ op: { const: 'observation' }, kind: { enum: ['fact', 'hypothesis'] }, kp: text,
              text, evidenceRefs: { type: 'array', items: text, minItems: 1, maxItems: 10 } }, ['op', 'kind', 'text', 'evidenceRefs']),
            object({ op: { const: 'preference' }, key: text, value: text }, ['op', 'key', 'value']),
            object({ op: { const: 'summary' }, text }, ['op', 'text']),
          ],
        },
      },
    },
    required: ['action'],
    additionalProperties: false,
  },
};

SCHEMAS.study_record.oneOf[0].properties.assessmentContext = object({
  value: { enum: ['none','homework','exam-prep','exam-live','unknown'] }, responseRef: text,
}, ['value','responseRef']);

SCHEMAS.route_task.oneOf = [
  ['start', ['goal','playbook','method','subject','grade','topic','problem','knowledge_points','assessment_context'], ['action','playbook']],
  ['adapt', ['taskRef','playbook','method','focus','nextObservation','reason','evidenceRefs'], ['action','taskRef','reason']],
  ['resume', ['taskRef','exerciseId'], ['action','taskRef']],
  ['finish', ['taskRef','reason'], ['action','taskRef','reason']],
  ['reveal', ['taskRef','exerciseId','reason','evidenceRefs'], ['action','reason']],
].map(([action, keys, required]) => object({ action: { const: action },
  ...Object.fromEntries(keys.map(key => [key, SCHEMAS.route_task.properties[key]])) }, required));

// Closed public entry: no free task text, identity or proof fields.
SCHEMAS.study_assessment = object({
  assessment: SCHEMAS.study_record.oneOf[1].properties.assessment,
  slot: SCHEMAS.study_record.oneOf[1].properties.slot,
  purpose: SCHEMAS.study_record.oneOf[1].properties.purpose,
}, ['assessment','slot','purpose']);

SCHEMAS.study_practice = { type: 'object', oneOf: [
  object({ action: { const: 'issue' }, assessment: SCHEMAS.study_assessment.properties.assessment,
    slot: SCHEMAS.study_assessment.properties.slot, purpose: SCHEMAS.study_assessment.properties.purpose, taskRef: text }, ['action','assessment','slot','purpose']),
  object({ action: { const: 'read' }, taskId: text }, ['action','taskId']),
  object({ action: { const: 'grade' }, taskId: text, judgment }, ['action','taskId','judgment']),
] };
const DESCRIPTIONS = {
  study_practice: 'Issue a practice-only web task and share returned answerUrl verbatim. Wait for its host submission notification, then read and grade with that exact taskId (never recordId). Continue only if still on that task and exercise; otherwise save for later review without changing focus or playing a board. Save grading even when practice-only or evidence_incomplete; exclusion from mastery does not prohibit grading. Only a host-issued assessment with a server-verified certificate may qualify.',
  study_assessment: 'Start independent assessment directly before any read, route or explanation. Send returned deliveryText exactly, alone, then wait for the student answer.',
  study_record: 'Record a confirmed exercise or classify a captured student response. Use only responseRef returned by host capture. Never supply student identity or answer text.',
  study_query: 'Read bound student learning records. Unavailable history is not empty history.',
  whiteboard: 'When the student interrupts or asks to pause, use pause to keep the board visible. Use close only when explicitly asked to close it. append only queues new steps; open resumes a board and does not accept steps. Before describing visible content or playback progress, call read: steps/applied describe delivered board content; pending is not displayed. Stopping or resuming does not turn queued steps into displayed steps. Never claim the student heard the speech. Read references/whiteboard.md for shape fields. Reuse an id to replace a shape; remove deletes it. No HTML, URLs or scripts.',
  route_task: 'Start a sustained learning goal; ordinary short definitions need no task. Adapt changes guidance while preserving the exercise and its evidence. Method is optional; select a supported method from student needs and feedback. Resume a prior task or exercise; finish records why learning stops. Reveal requires this exercise’s recorded responseRefs and a teaching reason; homework and exam restrictions remain.',
  math_check: 'Exact math with sympy. Compute the right result before you explain, and check a student step or answer. Results are for you, not to paste to the student.',
  make_activity: 'Create a page only when a concrete student action benefits from it. State its purpose; task and exercise links derive from the current host state. Use conversation or the existing board when sufficient. Read references/activities.md for formats, then deliver the created page with present. Current exercise protections apply.',
  learner_profile: 'Read the same sourced learner context used in automatic guidance. Sources separately report live/cached/unavailable/absent/not_requested and observation times. Official facts retain the source meaning; historical local mastery is not official. Update local observations with ledger evidenceRefs, preferences or a short summary. Record answers and revisions only with study_record. Never store contact details or school.',
};

/** Denial reason for one tool call, or undefined. Never throws. */
export function studyWorkspaceAllowed(cwd, workspaces) {
  try {
    const canonical = value => {
      if (typeof value !== 'string' || !path.isAbsolute(value)) return null;
      const resolved = realpathSync(value);
      return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
    };
    const current = canonical(cwd);
    return current !== null && Array.isArray(workspaces) && workspaces.some(value => canonical(value) === current);
  } catch {
    return false;
  }
}

export function guardCall(execution, readRoots, studyEnabled = false, studyWorkspaces = []) {
  try {
    const tool = execution?.name;
    if (STUDY_TOOLS.includes(tool) && (!studyEnabled || !studyWorkspaceAllowed(execution.agent?.session?.header?.cwd, studyWorkspaces))) return 'K12_GUARD: 当前工作区未启用学习记录。';
    if (![...TOOLS, ...(studyEnabled ? STUDY_TOOLS : [])].includes(tool)) return `K12_GUARD: 导师只能使用 ${TOOLS.join('、')}。`;
    if (tool === 'present') return guardPresent(execution);
    if (tool !== 'read') return undefined;
    const target = execution.arguments?.file_path;
    if (typeof target !== 'string' || !path.isAbsolute(target)) return 'K12_GUARD: read 只接受导师资料库里的绝对路径（route_task 返回的路径）。';
    const resolved = path.resolve(target).toLowerCase();
    const inside = readRoots.some(root => {
      const base = path.resolve(root).toLowerCase();
      return resolved === base || resolved.startsWith(base + path.sep);
    });
    return inside ? undefined : 'K12_GUARD: 只能读取导师资料库（skills、subjects）里的文件。';
  } catch {
    return 'K12_GUARD: 无法检查这次调用，已拒绝。';
  }
}

/** present may only deliver activity pages the engine wrote into this workspace's activities/ folder. */
function guardPresent(execution) {
  const cwd = execution.agent?.session?.header?.cwd;
  const files = execution.arguments?.files;
  if (!cwd || !Array.isArray(files) || files.length === 0) return 'K12_GUARD: present 只用来发 make_activity 生成的互动网页。';
  const base = path.resolve(cwd, 'activities').toLowerCase();
  for (const file of files) {
    const target = typeof file?.path === 'string' ? path.resolve(cwd, file.path).toLowerCase() : '';
    if (!target.startsWith(base + path.sep) || !target.endsWith('.html')) {
      return 'K12_GUARD: present 只能发本工作区 activities/ 下由 make_activity 生成的页面。';
    }
  }
  return undefined;
}

function textOf(message) {
  return (message?.content ?? []).filter(block => block.type === 'text').map(block => block.text).join('\n');
}

function lastReply(session, turn) {
  for (let seq = session.seq - 1; seq >= 0; seq -= 1) {
    const event = session.eventAt(seq);
    if (event?.type === 'turn/start') break;
    if (event?.type === 'assistant/message' && event.data.turn === turn) return textOf(event.data.message);
  }
  return '';
}

const exactKeys = (value, keys) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...keys].sort().join(',');

// DSH displays raw tool arguments and results. Only this closed question-only contract is neutral.
function assessmentDelivery(raw) {
  try {
    if (typeof raw !== 'string') return null;
    const args = JSON.parse(raw);
    // Reject duplicate keys and escaped/extra content in the visible raw argument string.
    if (JSON.stringify(args) !== raw.replace(/\s/g, '') || !exactKeys(args, ['assessment','slot','purpose'])) return null;
    if (!new RegExp(exerciseSlot.pattern).test(args.slot)
      || !SCHEMAS.study_assessment.properties.purpose.enum.includes(args.purpose)
      || !exactKeys(args.assessment, ['templateId','params'])) return null;
    const {templateId, params:p} = args.assessment;
    const functionValue = templateId === 'linear-function-value/v1';
    const inequality = templateId === 'linear-inequality/v1';
    if (!functionValue && !inequality && templateId !== 'linear-equation/v1') return null;
    const numeric = functionValue ? ['k','b','x'] : ['a','b','c'];
    if (!exactKeys(p, [...numeric, ...(inequality ? ['op'] : [])])
      || numeric.some(key => !Number.isInteger(p[key]) || Math.abs(p[key]) > 20)
      || p[functionValue ? 'k' : 'a'] === 0 || (inequality && !['<','>'].includes(p.op))) return null;
    return functionValue ? `已知一次函数 y = ${p.k}x + (${p.b})，当 x = ${p.x} 时，求 y。`
      : `${inequality ? '解不等式' : '解方程'}：${p.a}x + (${p.b}) ${inequality ? p.op : '='} ${p.c}。`;
  } catch { return null; }
}

function safeAssessmentCall(call) {
  return call?.name === 'study_assessment' && typeof (call.callId ?? call.id) === 'string'
    && !!(call.callId ?? call.id)
    && (call.type === 'tool-call' ? exactKeys(call, ['type','id','name','arguments'])
      : exactKeys(call, ['turn','step','callId','name','arguments'])
        && Number.isInteger(call.turn) && call.turn >= 0 && Number.isInteger(call.step) && call.step >= 0)
    && assessmentDelivery(call.arguments) !== null;
}

function safeAssessmentResult(call, data) {
  try {
    const message = data?.message;
    if (!safeAssessmentCall(call) || !exactKeys(data, ['turn','step','message']) || data.turn !== call.turn || data.step !== call.step
      || message?.role !== 'tool' || message.toolCallId !== call.callId
      || (message.isError !== undefined && message.isError !== false)
      || !Array.isArray(message.content) || message.content.length !== 1
      || !exactKeys(message.content[0], ['type','text']) || message.content[0].type !== 'text') return false;
    const raw = message.content[0].text;
    const result = JSON.parse(raw);
    return JSON.stringify(result) === raw && exactKeys(result, ['exerciseId','answerSlots','deliveryText'])
      && typeof result.exerciseId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(result.exerciseId)
      && JSON.stringify(result.answerSlots) === '["whole","reflection","step-1"]'
      && result.deliveryText === assessmentDelivery(call.arguments);
  } catch { return false; }
}

// Complete scan of the durable event stream, not a contiguous-message assumption.
export function hostScan(session) {
  const count = Number.isInteger(session?.seq) && session.seq >= 0 ? session.seq : null;
  const neutral = new Set(['agent-preset/selected','agent/inbox/spliced','approval/asked','approval/decided','approval/policy',
    'developer/message','system/message','request/context','request/header','turn/start','turn/end','step/start','step/end',
    'model/selection','permission/preset','sandbox/mode','plan/mode','session/title','session/title-llm-request',
    'session-log-deepseek/delivery-accepted','session/end-seed','llm/retry','llm/retry-started','working-directory/change']);
  const hazards = [];
  let complete = count !== null && typeof session.eventAt === 'function' && count <= 100000;
  for (let index = 0; index < Math.min(count ?? 0,100000); index += 1) {
    let event;
    try { event = session.eventAt(index); } catch { complete = false; continue; }
    if (!event || event.seq !== index) { complete = false; continue; }
    if (event.type === 'user/message') continue;
    if (event.type === 'assistant/message') {
      if (event.data?.interrupted || (typeof event.data?.message?.id !== 'string' || !event.data.message.id) || !Array.isArray(event.data?.message?.content) || event.data.message.content.some(block => block.type !== 'text' && (block.type !== 'tool-call' || !safeAssessmentCall(block)))) hazards.push(index);
      continue;
    }
    if (event.type === 'tool/call') {
      if (!safeAssessmentCall(event.data)) hazards.push(index);
      continue;
    }
    if (event.type === 'tool/result') {
      const refs = event.sourceEventSeqs;
      let call;
      try { call = Array.isArray(refs) && refs.length === 1 && Number.isInteger(refs[0]) && refs[0] >= 0 && refs[0] < index ? session.eventAt(refs[0]) : null; } catch {}
      if (call?.seq !== refs?.[0] || call?.type !== 'tool/call' || !safeAssessmentResult(call.data, event.data)) hazards.push(index);
      continue;
    }
    if (!neutral.has(event.type)) hazards.push(index);
  }
  return {throughEventSeq:(count ?? 0)-1,complete,hazards};
}

export function hostEvidence(session, incoming = [], trustedNotifications = new Set()) {
  const envelopes = [];
  const count = Number.isInteger(session?.seq) && typeof session.eventAt === 'function' ? Math.min(session.seq,100000) : 0;
  for (let index = 0; index < count; index += 1) {
    let event;
    try { event = session.eventAt(index); } catch { continue; }
    const user = event?.type === 'user/message' && event.data?.source?.kind === 'user';
    const assistant = event?.type === 'assistant/message';
    if (!user && !assistant) continue;
    const message = user ? event.data : event.data.message;
    if (user && trustedNotifications.has(message?.source?.rpcId)) continue;
    if (typeof message?.id !== 'string' || !message.id) continue;
    if (assistant && Array.isArray(message.content) && message.content.length && message.content.every(block => block.type === 'tool-call')) continue;
    const eventSeq = Number.isInteger(event.seq) && event.seq >= 0 ? event.seq : null;
    const date = typeof event.time === 'number' ? new Date(event.time) : null;
    const attemptedAt = date && Number.isFinite(date.valueOf()) ? date.toISOString() : null;
    const interrupted = assistant && event.data.interrupted === true;
    const textComplete = Array.isArray(message.content) && message.content.every(block => block.type === 'text');
    envelopes.push({ messageId: message.id, text: textOf(message), role: user ? 'user' : 'assistant',
      eventSeq, attemptedAt, coverage: eventSeq !== null && attemptedAt !== null && !interrupted && textComplete ? 'complete' : 'incomplete', interrupted });
  }
  const captured = new Set(envelopes.map(envelope => envelope.messageId));
  for (const message of incoming) {
    if (message?.source?.kind !== 'user' || trustedNotifications.has(message.source.rpcId) || typeof message.id !== 'string' || !message.id || captured.has(message.id)) continue;
    envelopes.push({ messageId: message.id, text: textOf(message), role: 'user', eventSeq: null,
      attemptedAt: null, coverage: 'incomplete', interrupted: false, pendingCommit: true });
    captured.add(message.id);
  }
  return envelopes;
}

function note(text) {
  return Object.freeze({ id: randomUUID(), role: 'user', content: [{ type: 'text', text }], source: { kind: name, form: 'snapshot', sections: [{ name, text }] } });
}

const where = exec => ({ cwd: exec.agent?.session?.header?.cwd, session: exec.agent?.session?.id });

export function apply(ctx, config = {}) {
  const readRoots = config.readRoots?.length ? config.readRoots : DEFAULTS.readRoots;
  const engine = ctx.k12Engine;
  const engineDir = config.engineDir || DEFAULTS.engineDir;
  const studyEnabled = config.studyEnabled === true;
  const knowledgeActions = new Set(['knowledge_manifest', 'knowledge_search', 'knowledge_node', 'knowledge_graph']);
  const enabledFor = async session => studyEnabled && (engine?.studyAllowed
    ? await engine.studyAllowed(session?.header?.cwd, session?.id) : studyWorkspaceAllowed(session?.header?.cwd, config.studyWorkspaces));

  let experience;
  const registerPractice = service => ctx.tools.register({
    name: 'experience_practice_read', description: '读取本会话最近一次原生资源练习，仅作本地辅导证据，不作为正式成绩。',
    parameters: SCHEMAS.experience_practice_read,
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: async (args, exec) => {
      if (!args || Object.keys(args).length) throw new Error('unexpected practice field');
      exec.signal?.throwIfAborted();
      return { practice: await service.latest({ sessionId: where(exec).session }) };
    },
  });
  ctx.tools.register({
    name: 'study_data', description: '读取当前学生站点学情或共享知识图谱；图谱关系以学习站返回的版本为准。此工具不写成绩。',
    parameters: SCHEMAS.study_data,
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: async (args, exec) => {
      const session = exec.agent?.session;
      const action = args?.action;
      const accepted = {
        knowledge_manifest: ['action'], knowledge_search: ['action', 'query', 'version', 'limit'],
        knowledge_node: ['action', 'resourceId', 'version'], knowledge_graph: ['action', 'resourceId', 'version'],
        paper: ['action', 'resourceId'],
      };
      const required = { knowledge_search: ['query'], knowledge_node: ['resourceId'], knowledge_graph: ['resourceId'], paper: ['resourceId'] };
      const fields = accepted[action] || ['action'];
      if (!args || !SCHEMAS.study_data.properties.action.enum.includes(action)
        || Object.keys(args).some(key => !fields.includes(key)) || (required[action] || []).some(key => !(key in args))
        || action === 'paper' && (typeof args.resourceId !== 'string' || args.resourceId.length > 128)) {
        throw new Error('unexpected study field');
      }
      exec.signal?.throwIfAborted();
      const sessionId = where(exec).session;
      if (!experience) throw new Error('学习站点身份服务暂不可用，无法确认当前会话归属。');
      const access = await experience.context({ sessionId }, exec.signal);
      if (access.binding || access.familyWorkspace === true) {
        if (!studyWorkspaceAllowed(where(exec).cwd, [access.cwd])) throw new Error('会话身份已变化。');
        const value = await experience.readStudy({ ...args, sessionId }, exec.signal);
        if (!knowledgeActions.has(action)) await engine.call('learner_context_dirty', where(exec));
        return value;
      }
      if (!knowledgeActions.has(action)) throw new Error('当前会话尚未连接学习站点。');
      if (!await enabledFor(session)) {
        throw new Error('K12_GUARD: 当前工作区未启用共享知识图谱入口。');
      }
      return { ok: true, data: await engine.call('study_knowledge', { ...args, ...where(exec) }) };
    },
  });
  const registerExperience = service => {
    experience = service;
    ctx.tools.register({
      name: 'study_paper_create', description: '学生明确要做正式练习时，从站点已有题目创建试卷。仅站点真实提交和服务端批改才形成成绩。',
      parameters: SCHEMAS.study_paper_create,
      output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: async (args, exec) => {
        if (!args || Object.keys(args).some(key => !['title', 'questionIds', 'requestId'].includes(key))) throw new Error('unexpected paper field');
        exec.signal?.throwIfAborted();
        return service.createPaper({ ...args, sessionId: where(exec).session }, exec.signal);
      },
    });
  };
  if (ctx.inject) {
    ctx.inject(['k12Experience'], scope => { registerExperience(scope.k12Experience); });
    ctx.inject(['k12Practice'], scope => { registerPractice(scope.k12Practice); });
  } else {
    if (ctx.k12Experience) registerExperience(ctx.k12Experience);
    if (ctx.k12Practice) registerPractice(ctx.k12Practice);
  }

  engine?.configureStudy?.(config);
  const studyInstructions = `[学习记录已启用] 网页练习调用 study_practice(action=issue,assessment,slot,purpose)，将返回的宿主 answerUrl 原样作为链接给学生，不自行改写为学习站链接。发题后等待网页提交的宿主通知，无需让学生手动读取或点击继续；收到通知后 read 和 grade 都使用通知中的 taskId（不是 recordId），读取并批改同一网页提交；practice_only 或证据不完整只排除掌握度，仍须保存批改，当前仍在原任务与原题时才继续现有白板；已切换时保存待回顾，不切换焦点或播放。宿主通知不是学生作答，不能保存为聊天答案。普通网页练习不计掌握度；宿主界面发起的独立测评须由服务器核验证据后才可能合格，不能自行声称独立完成或另用 study_record 保存。独立评估直接调用 study_assessment({assessment:{templateId,params},slot,purpose})，之前不要 read、route_task、其他工具、开场白或提示；返回 deliveryText 后原样单独发给学生，等待真实回答。普通持续目标先 route_task(action=start)；同目标新题在向学生提问前调用 study_record(action=open)，新槽位如 practice-2，不重建任务。题目须在可记录作答前开立。保存 exerciseId；answer 只用真实输入的 responseRef、answerSlot=whole 或 step-N 及对应 scope、judgment={state:"graded",correct,errorCause}（正确时 errorCause=null），不确定时 judgment={state:"pending",reason:"needs_review"}。不能补开练习或伪造作答。dismiss 的 reason 仅可为 self_report、not_answer、context、technical。普通辅导详细说明见 ${path.join(path.dirname(engineDir), 'skills', 'tutor-mode', 'references', 'study-records.md')}。`;
  const localInstructions = '[本地题目账本] 需要记录作答时先建立持续任务。返回的 exercise 若 contextOnly=false，沿用其 exerciseId 记录后续真实作答；尚无具体题目身份或题意改变时，才在提问前 study_record(action=open,taskRef,slot,question,purpose)。原题换讲法用 adapt，回到旧题用 resume；不能为已收到的回答补开题。同一目标可用 practice-1、practice-2 等槽位；同槽位重试复用身份，改题用新槽位。answer 仅使用真实输入 responseRef；分步用 answerSlot=step-N 和 scope={kind:"step",stepKey:"step-N"}。同一步真实重答使用新 responseRef，改判用 revise；自述/闲聊/技术失败用 dismiss，不制造答错。这里的记录是 local_only，不是正式成绩，不启用远端绑定或上传。';
  const notificationIds = async session => {
    const value = await engine.call('exercise_notification_ids', { cwd: session.header.cwd, session: session.id });
    if (!Array.isArray(value?.requestIds) || value.requestIds.some(id => typeof id !== 'string' || !id)) throw new Error('K12_GUARD: 无法核验宿主通知。');
    return new Set(value.requestIds);
  };
  const capture = async (session, incoming = [], trustedNotifications, remote) => engine.call(
    (remote ?? await enabledFor(session)) ? 'study_capture' : 'exercise_capture', {
      cwd: session.header.cwd, session: session.id,
      envelopes: hostEvidence(session, incoming, trustedNotifications ?? await notificationIds(session)), scan: hostScan(session),
    });
  for (const tool of [...ENGINE_TOOLS, ...(studyEnabled ? STUDY_TOOLS : [])]) {
    ctx.tools.register({
      name: tool,
      description: tool === 'whiteboard' ? DESCRIPTIONS[tool].replace('references/whiteboard.md',
        path.join(path.dirname(engineDir), 'skills', 'tutor-mode', 'references', 'whiteboard.md')) : DESCRIPTIONS[tool],
      parameters: tool === 'study_record' ? { type: 'object', oneOf: SCHEMAS.study_record.oneOf.filter((_, index) => index !== 1) } : SCHEMAS[tool],
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: typeof value?.text === 'string'
          ? `${JSON.stringify({ ...value, text: undefined })}\n\n${value.text}` : JSON.stringify(value) }],
      },
      execute: async (args, exec) => {
        exec.signal?.throwIfAborted();
        const enabled = studyEnabled ? await enabledFor(exec.agent.session) : false;
        if (STUDY_TOOLS.includes(tool) && !enabled) throw new Error('K12_GUARD: 当前工作区未启用此学习记录入口。已连接家庭学习空间时请使用 study_data 或 study_paper_create。');
        if (tool === 'study_record' && args.assessment) throw new Error('K12_GUARD: 模板测评使用独立入口。');
        if (['study_record', 'study_assessment', 'study_practice', 'route_task'].includes(tool)) await capture(exec.agent.session);
        if (enabled && ['study_record', 'study_assessment'].includes(tool)) await engine.call('study_enable', where(exec));
        const result = await engine.call(tool === 'study_record' && !enabled ? 'exercise_record' : tool, { ...args, ...where(exec), ...(tool === 'whiteboard' ? { receipt: exec.callId } : {}) });
        if (exec.signal?.aborted) {
          await engine.call('whiteboard', { ...where(exec), action: 'invalidate' });
          exec.signal.throwIfAborted();
        }
        if (tool === 'whiteboard' && args.action === 'read' && result === null) return { board: null };
        return result;
      },
    });
  }

  // DSH guards are synchronous. This checks the tool surface and local path bounds;
  // every study execute checks the authoritative async workspace gate above before capture or transport.
  ctx.tools.guard(execution => guardCall(execution, readRoots, studyEnabled,
    studyEnabled ? [execution?.agent?.session?.header?.cwd] : []));

  ctx.on('agent/pre-step', async ({ agent, step, signal }, next) => {
    const decision = await next();
    if (decision.kind !== 'enter' || signal.aborted) return decision;
    const enabled = await enabledFor(agent.session);
    const trustedNotifications = await notificationIds(agent.session);
    const evidence = await capture(agent.session, decision.messages, trustedNotifications, enabled);
    const responseNote = evidence
      ? `${step === 1 ? (enabled ? studyInstructions : localInstructions) + '\n' : ''}真实输入引用：${JSON.stringify(evidence.responses ?? [])}。只使用对应输入的 responseRef，不能把自述或题目上下文记为作答。` : '';
    if (step !== 1) return responseNote ? { ...decision, messages: [...decision.messages, note(responseNote)] } : decision;
    const student = decision.messages.filter(m => m.source?.kind === 'user' && !trustedNotifications.has(m.source.rpcId));
    const { notes } = student.length ? await engine.call('turn_begin', {
      cwd: agent.session.header.cwd, session: agent.session.id, text: student.map(textOf).join('\n'),
    }) : { notes: [] };
    if (student.length && enabled) {
      const pending = await engine.call('study_pending_reviews', { cwd: agent.session.header.cwd, session: agent.session.id });
      if (pending.length) notes.push('[待回顾网页结果] ' + JSON.stringify(pending.map(item => ({
        taskId: item.taskId, taskRef: item.teacherTaskRef, exerciseId: item.teacherExerciseId, needsGrade: item.needsGrade,
      }))) + ' 适合回顾时按原 taskId 读取和批改，不切换当前任务或白板。专用独立测评继续其直接入口，稍后处理回顾。');
    }
    if (responseNote) notes.push(responseNote);
    return notes.length ? { ...decision, messages: [...decision.messages, note(notes.join('\n\n'))] } : decision;
  });

  ctx.on('agent/turn-stopping', async ({ agent, turn }) => {
    await capture(agent.session);
    if (await enabledFor(agent.session)) {
      await engine.call('study_flush', { cwd: agent.session.header.cwd, session: agent.session.id });
    }
    const reply = lastReply(agent.session, turn);
    if (!reply) return;
    const { steer } = await engine.call('turn_end', { cwd: agent.session.header.cwd, session: agent.session.id, reply, turn });
    if (steer) agent.steer(note(steer));
  });
}
