package com.aura.companion.ui

import androidx.compose.runtime.saveable.SaverScope
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ConversationDraftsTest {
    @Test fun conversationDraftsRemainIndependent() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "A draft"); drafts.edit("b", "B draft")
        assertEquals("A draft", drafts.read("a").text)
        assertEquals("B draft", drafts.read("b").text)
        assertEquals("", drafts.read("new").text)
    }

    @Test fun acceptedRequestClearsOnlyItsOriginalConversation() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "A request")
        val ticket = drafts.beginSubmission("a")
        drafts.cancelFirstMigration()
        drafts.edit("b", "B remains")
        drafts.acknowledge(ticket, true)
        assertEquals("", drafts.read("a").text)
        assertEquals("B remains", drafts.read("b").text)
    }

    @Test fun oldAcceptanceDoesNotClearANewerEditEvenIfTextMatchesAgain() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "original")
        val ticket = drafts.beginSubmission("a")
        drafts.edit("a", "editing"); drafts.edit("a", "original")
        drafts.acknowledge(ticket, true)
        assertEquals("original", drafts.read("a").text)
    }

    @Test fun rejectedRequestRetainsTheUnsentDraft() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "request")
        drafts.acknowledge(drafts.beginSubmission("a"), false)
        assertEquals("request", drafts.read("a").text)
    }

    @Test fun firstRequestAcceptedBeforeConversationCreationStaysCleared() {
        val drafts = ConversationDrafts()
        drafts.edit(null, "first request")
        drafts.acknowledge(drafts.beginSubmission(null), true)
        drafts.migrateFirstDraft("created")
        assertEquals("", drafts.read("created").text)
    }

    @Test fun firstRequestRejectedBeforeConversationCreationRetainsItsDraft() {
        val drafts = ConversationDrafts()
        drafts.edit(null, "first request")
        drafts.acknowledge(drafts.beginSubmission(null), false)
        assertTrue(drafts.preserveFirstConversation)
        drafts.migrateFirstDraft("created")
        assertEquals("first request", drafts.read("created").text)
    }

    @Test fun acceptedFirstRequestFollowsTheAssignedConversation() {
        val drafts = ConversationDrafts()
        drafts.edit(null, "first request")
        val ticket = drafts.beginSubmission(null)
        drafts.migrateFirstDraft("created")
        drafts.acknowledge(ticket, true)
        assertEquals("", drafts.read("created").text)
    }

    @Test fun cancelledFirstMigrationCannotClearAManuallySelectedConversation() {
        val drafts = ConversationDrafts()
        drafts.edit(null, "first request")
        val ticket = drafts.beginSubmission(null)
        drafts.cancelFirstMigration()
        drafts.edit("manual", "manual draft")
        drafts.acknowledge(ticket, true)
        assertEquals("manual draft", drafts.read("manual").text)
        assertEquals("first request", drafts.read(null).text)
    }

    @Test fun migrationDoesNotOverwriteANewerDestinationEdit() {
        val drafts = ConversationDrafts()
        drafts.edit(null, "before ID")
        val ticket = drafts.beginSubmission(null)
        drafts.edit("created", "newer draft")
        drafts.migrateFirstDraft("created")
        drafts.acknowledge(ticket, true)
        assertEquals("newer draft", drafts.read("created").text)
    }

    @Test fun restorationCannotResurrectAClearedDraftOrOverwriteNewerText() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "submitted")
        drafts.acknowledge(drafts.beginSubmission("a"), true)
        drafts.restoreCurrent("a", "submitted")
        drafts.edit("b", "newer")
        drafts.restoreCurrent("b", "older")
        assertEquals("", drafts.read("a").text)
        assertEquals("newer", drafts.read("b").text)
    }

    @Test fun savedStateContainsOnlyTheVisibleDraftAndRestoresAfterProcessLoss() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "private inactive draft".repeat(1000))
        drafts.edit("b", "current draft")
        val saver = CurrentDraftCursor.saver(drafts)
        val saved = with(saver) { SaverScope { true }.save(CurrentDraftCursor(drafts, "b")) }!!
        assertEquals(setOf("conversation", "text"), saved.keySet())
        assertEquals("b", saved.getString("conversation"))
        assertEquals("current draft", saved.getString("text"))
        val newProcess = ConversationDrafts()
        val cursor = CurrentDraftCursor.saver(newProcess).restore(saved)!!
        assertEquals("b", cursor.conversationId)
        assertEquals("current draft", newProcess.read("b").text)
        assertEquals("", newProcess.read("a").text)
    }

    @Test fun currentStateRestorationUsesTheRetainedCacheWithoutOverwritingIt() {
        val drafts = ConversationDrafts()
        drafts.edit("a", "old current")
        drafts.edit("b", "inactive remains")
        val saver = CurrentDraftCursor.saver(drafts)
        val saved = with(saver) { SaverScope { true }.save(CurrentDraftCursor(drafts, "a")) }!!
        drafts.edit("a", "new current")
        saver.restore(saved)
        assertEquals("new current", drafts.read("a").text)
        assertEquals("inactive remains", drafts.read("b").text)
    }
}
