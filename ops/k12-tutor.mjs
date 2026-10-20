/** k12 tutor runtime: the tutor's tools and its structural safety rails, as one DSH preset plugin.
 *
 * Everything that decides lives in the Python engine (`engine/k12_engine`), reached through one
 * long-lived JSON-lines worker per DSH process; this file only adapts DSH events to engine ops.
 *
 *   tools.guard         only route_task, math_check, learner_profile and read (inside readRoots) may run
 *   agent/pre-step      the student's message goes through the engine's input checks; the notes it
 *                       returns (learner summary, crisis / integrity / privacy rules, route state) join the step
 *   agent/turn-stopping the finished reply goes through the reply checks; a violation steers one correction
 *
 * One workspace directory is one student: the engine keeps `<cwd>/.k12/learner.json`, per-session
 * state, `guard.jsonl` and `alerts.jsonl` there.
 *
 * Paths default to this package's own layout (engine/, skills/, subjects/ next to ops/), so the same file
 * works from a source checkout and from an installed bundle. The engine runs on config.python, else
 * $K12_PYTHON, else `uv run` with the engine's two dependencies (uv provisions them on first use).
 */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

export const name = 'k12-tutor';
export const inject = ['tools', 'agents'];

export const TOOLS = ['route_task', 'math_check', 'learner_profile', 'read'];

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const DEFAULTS = {
  engineDir: path.join(ROOT, 'engine'),
  readRoots: [path.join(ROOT, 'skills'), path.join(ROOT, 'subjects')],
};
const UV_ARGS = ['run', '--no-project', '--quiet', '--with', 'sympy>=1.13', '--with', 'pyyaml>=6', 'python'];

/** The command that starts the engine worker. */
export function engineCommand(python, environ = process.env) {
  const interpreter = python || environ.K12_PYTHON;
  const serve = ['-m', 'k12_engine', 'serve'];
  return interpreter ? [interpreter, serve] : ['uv', [...UV_ARGS, ...serve]];
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

  call(op, args) {
    if (!this.child) this.start();
    const id = this.next++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.child.stdin.write(JSON.stringify({ id, op, args }) + '\n');
    });
  }

  stop() {
    this.child?.kill();
    this.child = undefined;
  }
}

const text = { type: 'string', minLength: 1 };
export const SCHEMAS = {
  route_task: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['start', 'reveal'], description: 'start: a new learning task; reveal: ask to reveal the answer of the open task' },
      playbook: { type: 'string', enum: ['solve', 'concept', 'diagnose', 'practice', 'review-plan', 'writing', 'inquiry', 'emotion', 'boundary', 'figure-it-out'] },
      subject: { type: 'string', description: 'math, chinese, english, physics, chemistry, biology, history, geography, politics, other' },
      grade: { type: 'integer', minimum: 1, maximum: 12, description: 'only when the student just told you; otherwise the profile grade is used' },
      topic: { type: 'string', description: 'what the task is about in a few words, e.g. 一元一次方程 去括号' },
      problem: { type: 'string', description: 'the problem text, transcribed from the photo if there is one' },
      knowledge_points: { type: 'array', items: text, description: 'knowledge point ids if you already know them' },
      assessment_context: { type: 'string', enum: ['none', 'homework', 'exam-prep', 'exam-live', 'unknown'] },
      reason: { type: 'string', description: 'reveal only: why revealing now helps the student learn' },
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
  learner_profile: {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ['read', 'update'] },
      updates: {
        type: 'array',
        items: {
          type: 'object',
          properties: {
            op: { type: 'string', enum: ['basics', 'observe', 'misconception', 'preference', 'summary'] },
            grade: { type: 'integer' }, textbooks: { type: 'object', additionalProperties: { type: 'string' } }, nickname: { type: 'string' },
            kp: { type: 'string', description: 'observe: knowledge point id, e.g. math/junior:linear-eq-solve' },
            correct: { type: 'boolean' }, independent: { type: 'boolean', description: 'observe: answered with no hint and no reveal' },
            id: { type: 'string', description: 'misconception id' }, status: { type: 'string', enum: ['active', 'resolved'] },
            key: { type: 'string' }, value: { type: 'string' }, evidence: { type: 'string' }, text: { type: 'string' },
          },
          required: ["op"],
          additionalProperties: false,
        },
      },
    },
    required: ['action'],
    additionalProperties: false,
  },
};

const DESCRIPTIONS = {
  route_task: 'Start a learning task (once per task, not per message): returns the playbook, the knowledge points with their misconceptions, and the answer policy. action=reveal asks whether the answer of the open task may be revealed now.',
  math_check: 'Exact math with sympy. Compute the right result before you explain, and check a student step or answer. Results are for you, not to paste to the student.',
  learner_profile: 'Read or update the structured learner profile. Record evidence, not verdicts: observe each answer (correct, independent) and the profile computes mastered / not mastered / uncertain. Also misconceptions, preferences, short learning summaries. Never store names, contact details or school.',
};

/** Denial reason for one tool call, or undefined. Never throws. */
export function guardCall(execution, readRoots) {
  try {
    const tool = execution?.name;
    if (!TOOLS.includes(tool)) return `K12_GUARD: 导师只能使用 ${TOOLS.join('、')}。`;
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

function note(text) {
  return Object.freeze({ id: randomUUID(), role: 'user', content: [{ type: 'text', text }], source: { kind: name, form: 'snapshot', sections: [{ name, text }] } });
}

const where = exec => ({ cwd: exec.agent?.session?.header?.cwd, session: exec.agent?.session?.id });

export function apply(ctx, config = {}) {
  const engineDir = config.engineDir || DEFAULTS.engineDir;
  const readRoots = config.readRoots?.length ? config.readRoots : DEFAULTS.readRoots;
  const engine = new Engine({ python: config.python, engineDir });
  ctx.effect(() => () => engine.stop(), 'k12-engine');

  for (const tool of ['route_task', 'math_check', 'learner_profile']) {
    ctx.tools.register({
      name: tool,
      description: DESCRIPTIONS[tool],
      parameters: SCHEMAS[tool],
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: typeof value?.text === 'string'
          ? `${JSON.stringify({ ...value, text: undefined })}\n\n${value.text}` : JSON.stringify(value) }],
      },
      execute: (args, exec) => engine.call(tool, { ...args, ...where(exec) }),
    });
  }

  ctx.tools.guard(execution => guardCall(execution, readRoots));

  ctx.on('agent/pre-step', async ({ agent, step, signal }, next) => {
    const decision = await next();
    if (decision.kind !== 'enter' || signal.aborted || step !== 1) return decision;
    const student = decision.messages.filter(m => m.source?.kind === 'user');
    if (student.length === 0) return decision;
    const { notes } = await engine.call('turn_begin', {
      cwd: agent.session.header.cwd, session: agent.session.id, text: student.map(textOf).join('\n'),
    });
    return notes.length ? { ...decision, messages: [...decision.messages, note(notes.join('\n\n'))] } : decision;
  });

  ctx.on('agent/turn-stopping', async ({ agent, turn }) => {
    const reply = lastReply(agent.session, turn);
    if (!reply) return;
    const { steer } = await engine.call('turn_end', { cwd: agent.session.header.cwd, session: agent.session.id, reply, turn });
    if (steer) agent.steer(note(steer));
  });
}
