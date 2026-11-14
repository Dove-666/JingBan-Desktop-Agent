import type { AgentArtifact, ChatMessage } from './bridge';

/** A delivery stays with its answer as later chat and perception messages arrive. */
export function conversationArtifacts(messages: ChatMessage[], artifacts: AgentArtifact[] = []) {
  const answers = messages.filter(message => message.role === 'assistant'
    && !['proactive_screen', 'proactive_camera', 'stream_preview'].includes(message.source ?? '')
    && !message.screen_event);
  const answerIds = new Set(answers.map(message => message.id));
  const byMessage = new Map<string, AgentArtifact[]>();
  const unassigned: AgentArtifact[] = [];
  const seen = new Set<string>();
  for (const artifact of artifacts) {
    if (seen.has(artifact.reference)) continue;
    seen.add(artifact.reference);
    let owner = artifact.message_id && answerIds.has(artifact.message_id) ? artifact.message_id : undefined;
    if (!artifact.message_id) {
      // Old desktops did not persist ownership. Only use concrete, unambiguous
      // transcript evidence; never assume the last answer generated the file.
      owner = answers.find(message => message.content.includes(artifact.reference))?.id;
      if (!owner && artifact.name.length >= 3) {
        const matches = answers.filter(message => message.content.includes(artifact.name));
        if (matches.length === 1) owner = matches[0].id;
      }
    }
    if (owner) byMessage.set(owner, [...(byMessage.get(owner) ?? []), artifact]);
    else unassigned.push(artifact);
  }
  return { byMessage, unassigned };
}
