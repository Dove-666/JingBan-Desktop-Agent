import { useState } from 'react';
import { Download, FileText } from 'lucide-react';
import type { AgentArtifact, Dispatch } from '../bridge';
import './agent-interaction.css';

export function AgentArtifactCards({ artifacts, conversationId, enabled, dispatch }: {
  artifacts: AgentArtifact[]; conversationId: string; enabled: boolean; dispatch: Dispatch;
}) {
  const [saving, setSaving] = useState('');
  const [lastOutcome, setLastOutcome] = useState<{ reference: string; status: 'saved' | 'failed' }>();
  return <div className="agent-artifacts">{artifacts.map((artifact) =>
    <div className="agent-artifact" data-testid="agent-artifact" key={artifact.reference}>
      <FileText size={21} aria-hidden="true" /><div><strong>{artifact.name}</strong>
        <small>{artifact.bytes < 1024 ? `${artifact.bytes} B` : `${Math.ceil(artifact.bytes / 1024)} KB`}</small>
        {lastOutcome?.reference === artifact.reference && <small data-testid="agent-artifact-result"
          role={lastOutcome.status === 'failed' ? 'alert' : 'status'}>
          {lastOutcome.status === 'saved' ? '已保存' : '保存失败，请检查保存位置或重新生成文件后再试。'}</small>}</div>
      <button type="button" disabled={!enabled || !!saving} aria-label={`保存文件：${artifact.name}`}
        onClick={() => {
          setSaving(artifact.reference);
          setLastOutcome(undefined);
          dispatch('openArtifact', { conversationId, reference: artifact.reference }, (result) => {
            setSaving('');
            const status = result.status === 'cancelled' ? undefined
              : result.status === 'failed' ? 'failed'
              : result.status === 'saved' || result.ok ? 'saved'
              : typeof result.error === 'string' && result.error ? 'failed' : undefined;
            if (status) setLastOutcome({ reference: artifact.reference, status });
          });
        }}>
        <Download size={16} aria-hidden="true" />{saving === artifact.reference ? '保存中…'
          : lastOutcome?.reference === artifact.reference && lastOutcome.status === 'failed' ? '重试' : '保存'}</button>
    </div>)}</div>;
}
