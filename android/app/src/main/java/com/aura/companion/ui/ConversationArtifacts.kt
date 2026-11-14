package com.aura.companion.ui

internal data class ConversationArtifacts(
    val byMessage: Map<String, List<UiAgentArtifact>>,
    val unassigned: List<UiAgentArtifact>,
)

/** Deliveries belong to their originating answer, never to the newest turn. */
internal fun conversationArtifacts(messages: List<UiMessage>, artifacts: List<UiAgentArtifact>): ConversationArtifacts {
    val answers = messages.filter { it.role == MessageRole.ASSISTANT }
    val answerIds = answers.mapTo(mutableSetOf()) { it.id }
    val grouped = linkedMapOf<String, MutableList<UiAgentArtifact>>()
    val unassigned = mutableListOf<UiAgentArtifact>()
    artifacts.distinctBy { it.reference }.forEach { file ->
        val owner = file.messageId?.takeIf { it in answerIds }
            ?: if (file.messageId == null) {
                // Compatibility with older desktops: use an explicit delivery
                // reference or an unambiguous filename mentioned in one answer.
                answers.firstOrNull { file.reference in it.content }?.id
                    ?: answers.filter { file.name.length >= 3 && file.name in it.content }.singleOrNull()?.id
            } else null
        if (owner != null) grouped.getOrPut(owner) { mutableListOf() }.add(file)
        else unassigned.add(file)
    }
    return ConversationArtifacts(grouped, unassigned)
}
