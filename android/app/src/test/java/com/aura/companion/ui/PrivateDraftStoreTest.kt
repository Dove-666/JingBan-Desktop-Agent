package com.aura.companion.ui

import android.os.Bundle
import android.os.Looper
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.After
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.Config
import java.io.File
import java.io.IOException
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class PrivateDraftStoreTest {
    @get:Rule val directory = TemporaryFolder()
    private val owners = ViewModelStore()
    private fun file() = File(directory.root, "drafts.json")
    private fun model(store: DraftStore = PrivateDraftStore(file())): ConversationDrafts {
        val factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T = ConversationDrafts(store) as T
        }
        return ViewModelProvider(owners, factory)[UUID.randomUUID().toString(), ConversationDrafts::class.java]
    }
    @After fun closeModels() { owners.clear() }

    @Test fun freshInstancesRecoverEveryConversationAndNullDraftWithoutTruncation() {
        val first = model(); val full = "完整素材🙂\n".repeat(2100)
        first.edit("a", full); first.edit("b", "independent"); first.edit(null, "unassigned")
        val restored = model()
        assertTrue(restored.read("a").text == full)
        assertEquals("independent", restored.read("b").text)
        assertEquals("unassigned", restored.read(null).text)
        assertNull(restored.persistenceError)
    }

    @Test fun legacyBundleCannotResurrectAnAcknowledgedDraftOrOverwriteDisk() {
        val first = model(); first.edit("a", "submitted"); first.acknowledge(first.beginSubmission("a"), true)
        val disk = model(); disk.edit("b", "new on disk")
        val old = Bundle().apply { putString("conversation", "a"); putString("text", "stale saved value") }
        CurrentDraftCursor.saver(disk).restore(old)
        disk.restoreCurrent("b", "older bundle value")
        disk.restoreCurrent("foreign", "unowned bundle value")
        assertEquals("", disk.read("a").text); assertEquals("new on disk", disk.read("b").text)
        assertEquals("", disk.read("foreign").text)
    }

    @Test fun staleCallbackFromAnOldViewModelCannotClearNewDiskRevisionEvenWhenTextMatches() {
        val old = model(); old.edit("a", "original"); val ticket = old.beginSubmission("a")
        val current = model(); current.edit("a", "edited"); current.edit("a", "original")
        old.acknowledge(ticket, true)
        assertEquals("original", model().read("a").text)
        assertEquals("original", current.read("a").text)
    }

    @Test fun ticketIdentityDoesNotCollideAcrossModelInstances() {
        val old = model(); old.edit("a", "same"); val oldTicket = old.beginSubmission("a")
        val current = model(); val newTicket = current.beginSubmission("a")
        current.acknowledge(oldTicket, true)
        assertEquals("same", model().read("a").text)
        current.acknowledge(newTicket, true)
        assertEquals("", model().read("a").text)
    }

    @Test fun acceptedOriginalClearsOnlyItsOwnConversationAndSurvivesAnotherLaunch() {
        val first = model(); first.edit("a", "request"); first.edit("b", "keep b"); first.edit(null, "keep null")
        first.acknowledge(first.beginSubmission("a"), true)
        val next = model()
        assertEquals("", next.read("a").text); assertEquals("keep b", next.read("b").text)
        assertEquals("keep null", next.read(null).text)
    }

    @Test fun nullDraftMigrationPersistsAssignmentAndAnEmptySourceTombstone() {
        val first = model(); first.edit(null, "before id"); first.migrateFirstDraft("assigned")
        val next = model(); assertEquals("before id", next.read("assigned").text); assertEquals("", next.read(null).text)
        next.restoreCurrent(null, "old bundle"); assertEquals("", next.read(null).text)
    }

    @Test fun explicitDiscardCannotBeRevivedByAnOldBundleOrAcceptance() {
        val first = model(); first.edit("a", "request"); val ticket = first.beginSubmission("a")
        first.discard("a"); first.acknowledge(ticket, true)
        val next = model(); next.restoreCurrent("a", "old value")
        assertEquals("", next.read("a").text); assertTrue(next.read("a").revision > ticket.snapshot.revision)
    }

    @Test fun deletedScopeClearsOnlyItsDraftAndNotAnotherConversationOrNull() {
        val scopes = mutableMapOf("a" to DraftScope("generation-a"), "b" to DraftScope("generation-b"))
        val store = PrivateDraftStore(file()) { ids -> scopes.filterKeys { it in ids } }
        val first = model(store); first.edit("a", "remove"); first.edit("b", "keep"); first.edit(null, "unassigned")
        scopes["a"] = DraftScope(null, true); store.refreshScopes()
        val next = model(store)
        assertEquals("", next.read("a").text); assertEquals("keep", next.read("b").text)
        assertEquals("unassigned", next.read(null).text)
        next.restoreCurrent("a", "stale bundle"); assertEquals("", next.read("a").text)
    }

    @Test fun clearedOrReplacedGenerationCannotReuseAnOldDraft() {
        var generation: String? = "generation-a"
        val store = PrivateDraftStore(file()) { ids -> ids.associateWith { DraftScope(generation) } }
        val first = model(store); first.edit("a", "old task")
        generation = null; store.refreshScopes(); assertEquals("", model(store).read("a").text)
        generation = "new-generation"; val next = model(store); next.edit("a", "new task")
        generation = "replacement"; store.refreshScopes(); assertEquals("", model(store).read("a").text)
    }

    @Test fun corruptOrUnsupportedDiskNeverGetsOverwrittenByAFallbackBundle() {
        for (bytes in listOf(byteArrayOf(0xc3.toByte(), 0x28), "{\"version\":999}".toByteArray())) {
            file().writeBytes(bytes)
            val first = model(); assertNotNull(first.persistenceError)
            val full = "preserve complete in memory".repeat(1000)
            first.edit("a", full); first.restoreCurrent("a", "old saved state")
            assertTrue(first.read("a").text == full); assertArrayEquals(bytes, file().readBytes())
            assertNotNull(first.persistenceError)
        }
    }

    private class FailingStore : DraftStore {
        override val changes = MutableStateFlow(0L)
        var state = PersistedDrafts(); var fail = false
        override fun load() = state
        override fun update(change: (PersistedDrafts) -> PersistedDrafts): PersistedDrafts {
            if (fail) throw IOException("synthetic owned storage failure")
            state = change(state); return state
        }
    }

    @Test fun writeFailurePreservesCompleteMemoryTextAndNeverPretendsItWasSaved() {
        val store = FailingStore().apply { fail = true }; val first = model(store)
        val full = "complete text\n".repeat(1000); first.edit("a", full)
        assertTrue(first.read("a").text == full); assertNotNull(first.persistenceError)
        assertTrue(store.state.entries.isEmpty())
    }

    @Test fun cachedPriorCommitCannotHideACorruptDiskFromANewModel() {
        model().edit("a", "previous durable value")
        val broken = byteArrayOf(0xc3.toByte(), 0x28); file().writeBytes(broken)
        val restored = model()
        Shadows.shadowOf(Looper.getMainLooper()).idle()
        assertNotNull(restored.persistenceError)
        restored.edit("a", "complete new in-memory value")
        assertEquals("complete new in-memory value", restored.read("a").text)
        assertNotNull(restored.persistenceError); assertArrayEquals(broken, file().readBytes())
    }

    @Test fun deletionDoesNotResurrectAnUnpersistedDraftWhenStorageRecovers() {
        val store = FailingStore(); val first = model(store)
        first.edit("a", "original"); store.fail = true
        first.edit("a", "unsaved new a"); first.edit("b", "unsaved independent b")
        store.state = store.state.copy(revision = 3, entries = mapOf("a" to DraftSnapshot("", 3)),
            scopes = mapOf("a" to DraftScope(null, true)))
        store.changes.value++
        Shadows.shadowOf(Looper.getMainLooper()).idle()
        assertEquals("", first.read("a").text)
        assertEquals("unsaved independent b", first.read("b").text)
        store.fail = false; first.edit("b", "new b after recovery")
        assertEquals("", store.state.entries["a"]!!.text)
        assertEquals("new b after recovery", store.state.entries["b"]!!.text)
    }

    @Test fun clearingFailureAfterAcceptanceRetainsTextAndExplicitlyPreventsDuplicateSend() {
        val store = FailingStore(); val first = model(store)
        first.edit("a", "request"); val ticket = first.beginSubmission("a"); store.fail = true
        first.acknowledge(ticket, true)
        assertEquals("request", first.read("a").text)
        assertTrue(first.persistenceError!!.contains("请勿重复发送"))
        assertEquals("request", store.state.entries["a"]!!.text)
        store.changes.value++
        Shadows.shadowOf(Looper.getMainLooper()).idle()
        assertNotNull(first.persistenceError)
        assertEquals("request", first.read("a").text)
        store.fail = false; store.changes.value++
        Shadows.shadowOf(Looper.getMainLooper()).idle()
        assertEquals("", first.read("a").text)
        assertEquals("", store.state.entries["a"]!!.text)
        assertNull(first.persistenceError)
    }

    @Test(timeout = 10000) @Config(sdk = [35])
    fun replacementCannotReportSuccessWhenCommittedBytesDifferOrCauseANotificationLoop() {
        val store = PrivateDraftStore(file()); val first = model(store)
        first.edit("a", "first"); first.edit("a", "replacement")
        assertEquals("replacement", first.read("a").text)
        if (first.persistenceError == null) {
            assertEquals("replacement", store.load().entries["a"]!!.text)
            assertEquals(2L, store.changes.value)
        } else {
            assertEquals("first", store.load().entries["a"]!!.text)
            assertEquals(1L, store.changes.value)
        }
    }
}
