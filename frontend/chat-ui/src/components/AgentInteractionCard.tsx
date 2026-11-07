import { useState } from 'react';
import { Check, FolderOpen, LoaderCircle, MessageCircle, ShieldCheck } from 'lucide-react';
import type { AgentInteraction, Dispatch } from '../bridge';
import './agent-interaction.css';

/** A live question is ephemeral. Answers are never queued or replayed. */
export function AgentInteractionCard({ interaction, dispatch }: { interaction: AgentInteraction; dispatch: Dispatch }) {
  const [text, setText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [answered, setAnswered] = useState(false);
  const [error, setError] = useState('');
  const resource = interaction.resource;
  const answer = (value: Record<string, unknown>) => {
    if (submitting || answered) return;
    setSubmitting(true); setError('');
    dispatch('answerAgentInteraction', { conversationId: interaction.conversation_id,
      interactionId: interaction.id, answer: value }, (result) => {
      setSubmitting(false);
      if (result.ok) setAnswered(true);
      else setError('问题已更新或连接暂不可用，请检查后重试。');
    });
  };
  return <section className="agent-interaction" data-testid="agent-interaction" aria-label={resource ? '文件访问确认' : 'Agent 问题'}>
    <div className="agent-interaction-heading">
      {answered ? <Check size={17} /> : resource ? <ShieldCheck size={17} /> : <MessageCircle size={17} />}
      <strong>{answered ? '已提交' : resource ? '需要你的授权' : '需要你确认'}</strong>
      {submitting && <LoaderCircle size={15} className="spin" aria-label="正在发送答复" />}
    </div>
    <p>{interaction.question}</p>
    {resource ? <>
      <div className="agent-interaction-resource"><FolderOpen size={17} aria-hidden="true" /><div>
        <span>{resource.access === 'write' ? '读写' : '只读'} · {resource.directory ? '文件夹' : '文件'}</span>
        <code>{resource.path}</code></div></div>
      <div className="agent-interaction-actions">
        <button type="button" disabled={submitting || answered} onClick={() => answer({ allowed: false })}>拒绝</button>
        <button type="button" className="agent-interaction-primary" data-testid="agent-allow-access"
          disabled={submitting || answered} onClick={() => answer({ allowed: true })}>允许这次访问</button>
      </div>
    </> : <>
      {interaction.options?.length ? <div className="agent-interaction-options">{interaction.options.map((option, index) =>
        <button key={`${index}:${option}`} type="button" disabled={submitting || answered}
          onClick={() => answer({ text: option })}>{option}</button>)}</div> : null}
      <form onSubmit={(event) => { event.preventDefault(); if (text.trim()) answer({ text: text.trim() }); }}>
        <label className="sr-only" htmlFor={`answer-${interaction.id}`}>补充回答</label>
        <input id={`answer-${interaction.id}`} data-testid="agent-answer-input" value={text} maxLength={8000}
          placeholder="补充回答…" disabled={submitting || answered} onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) event.preventDefault(); }} />
        <button type="submit" className="agent-interaction-primary" disabled={!text.trim() || submitting || answered}>继续</button>
      </form>
    </>}
    {error && <p className="agent-interaction-error" role="alert">{error}</p>}
  </section>;
}
