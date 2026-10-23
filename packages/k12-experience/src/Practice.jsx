import React, { useEffect, useRef, useState } from 'react';

export function Practice({ sessionId, rpc }) {
  const [task, setTask] = useState(null);
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const taskRef = useRef(null);
  const live = useRef(true);
  const retry = useRef(null);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      if (taskRef.current) void rpc('practice_close', { sessionId, activityId: taskRef.current.activityId }).catch(() => {});
    };
  }, [sessionId]);
  const open = async () => {
    setBusy(true); setStatus('');
    try {
      const value = await rpc('practice_open', { sessionId });
      if (!live.current) { await rpc('practice_close', { sessionId, activityId: value.activityId }); return; }
      taskRef.current = value; setTask(value);
    } catch { if (live.current) setStatus('任务暂时无法打开，请重试。'); }
    finally { if (live.current) setBusy(false); }
  };
  const submit = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const answer = { prediction: Number(form.elements.prediction.value), relation: form.elements.relation.value };
    const serialized = JSON.stringify(answer);
    if (!retry.current || retry.current.serialized !== serialized) retry.current = { serialized, eventId: crypto.randomUUID() };
    setBusy(true); setStatus('');
    try {
      await rpc('practice_submit', { sessionId, activityId: task.activityId, eventId: retry.current.eventId, answer });
      if (live.current) setStatus('想法已记录。回到对话，和导师讨论你的等量关系。');
    } catch { if (live.current) setStatus('尚未确认保存，可以重试。'); }
    finally { if (live.current) setBusy(false); }
  };
  return <section className="k12-practice"><h3>资源变化小任务</h3>
    <p>本地辅导练习，不计入学习站点成绩。</p>
    {!task ? <button disabled={busy} onClick={open}>开始资源任务</button> : <form onSubmit={submit}>
      <p>A 站开始有 20 单位，每分钟增加 3 单位。B 站开始有 5 单位，每分钟增加 6 单位。</p>
      <fieldset disabled={busy}><label>预测几分钟后数量相同<input name="prediction" type="number" min="0" max="1440" step="any" required /></label>
        <label>写出你的等量关系<input name="relation" maxLength="256" autoComplete="off" /></label><button>记录我的想法</button></fieldset>
    </form>}{status && <p role="status">{status}</p>}
  </section>;
}
