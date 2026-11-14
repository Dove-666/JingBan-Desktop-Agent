package com.aura.companion.ui

import android.os.Bundle
import android.os.Parcel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.compose.runtime.saveable.SaverScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestCoroutineScheduler
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.UUID

@OptIn(ExperimentalCoroutinesApi::class)
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class ConversationPersistenceQueueTest {
    private val scheduler = TestCoroutineScheduler()
    private val owners = ViewModelStore()
    private val stores = mutableListOf<SlowStore>()
    @Before fun main() { Dispatchers.setMain(StandardTestDispatcher(scheduler)) }
    @After fun close() {
        stores.forEach { it.release.countDown() }
        owners.clear()
        drain { stores.all { it.waiting.get() == 0 } }
        Dispatchers.resetMain()
    }

    private class SlowStore : DraftStore {
        override val changes = MutableStateFlow(0L)
        @Volatile var value = PersistedDrafts()
        override val committed get() = value
        @Volatile var block = false
        @Volatile var fail = false
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val writes = AtomicInteger()
        val waiting = AtomicInteger()
        @Volatile var diskThread: Long = 0
        override fun load(): PersistedDrafts { diskThread = Thread.currentThread().id; return value }
        override fun update(change: (PersistedDrafts) -> PersistedDrafts): PersistedDrafts {
            if (fail) throw java.io.IOException("owned recovery failure")
            diskThread = Thread.currentThread().id
            if (block) {
                waiting.incrementAndGet(); entered.countDown()
                check(release.await(5, TimeUnit.SECONDS)); waiting.decrementAndGet()
            }
            synchronized(this) {
                value = change(value); writes.incrementAndGet(); changes.value++
                return value
            }
        }
    }

    private fun model(store: SlowStore): ConversationDrafts {
        stores += store
        val factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T = ConversationDrafts(store) as T
        }
        return ViewModelProvider(owners, factory)[UUID.randomUUID().toString(), ConversationDrafts::class.java].also { drain { !it.saving } }
    }

    private fun drain(done: () -> Boolean) {
        val limit = System.nanoTime() + TimeUnit.SECONDS.toNanos(8)
        do { scheduler.runCurrent(); if (done()) return; Thread.sleep(5) } while (System.nanoTime() < limit)
        fail("Owned local persistence did not settle")
    }

    @Test fun blockedDiskDoesNotBlockTypingAndOneThousandEditsCoalesce() {
        val store = SlowStore(); val drafts = model(store)
        val uiThread = Thread.currentThread().id
        store.block = true
        drafts.edit("a", "first")
        scheduler.runCurrent(); assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        repeat(1000) { drafts.edit("a", "latest-$it") }
        assertEquals("latest-999", drafts.read("a").text)
        assertTrue(drafts.saving)
        assertNotEquals(uiThread, store.diskThread)
        store.release.countDown(); drain { !drafts.saving }
        assertEquals("latest-999", store.value.entries["a"]!!.text)
        assertTrue("Burst should need only first and final transaction", store.writes.get() <= 2)
        assertNull(drafts.persistenceError)
    }

    @Test fun acceptanceCommittingDuringANewerEditCannotEraseTheNewDraft() {
        val store = SlowStore(); val drafts = model(store)
        drafts.edit("a", "sent"); drain { !drafts.saving }
        val ticket = drafts.beginSubmission("a")
        store.block = true
        drafts.acknowledge(ticket, true)
        scheduler.runCurrent(); assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "new unsent message")
        store.release.countDown(); drain { !drafts.saving }
        assertEquals("new unsent message", drafts.read("a").text)
        assertEquals("new unsent message", store.value.entries["a"]!!.text)
    }

    @Test fun activityDestructionDrainsTheLastQueuedSnapshot() {
        val store = SlowStore(); val drafts = model(store)
        store.block = true
        drafts.edit("a", "first"); scheduler.runCurrent()
        assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "last before exit")
        drafts.edit("b", "another conversation")
        owners.clear()
        store.release.countDown()
        drain { store.value.entries["a"]?.text == "last before exit" && store.value.entries["b"]?.text == "another conversation" }
        assertNotNull(store.value.entries["a"])
    }

    @Test fun admissionTicketTracksItsDurableRevisionWithoutAcceptingLaterEdits() {
        val store = SlowStore(); val drafts = model(store)
        store.block = true
        drafts.edit("a", "original"); scheduler.runCurrent()
        assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        val ticket = drafts.beginSubmission("a")
        drafts.edit("a", "different"); drafts.edit("a", "original")
        store.release.countDown(); drain { !drafts.saving }
        assertFalse(drafts.isCurrent(ticket))
        drafts.acknowledge(ticket, true); drain { !drafts.saving }
        assertEquals("original", store.value.entries["a"]!!.text)
    }

    @Test fun admissionTicketCanSendAfterItsExactSnapshotHasCommitted() {
        val store = SlowStore(); val drafts = model(store)
        store.block = true
        drafts.edit("a", "original"); scheduler.runCurrent()
        assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        val ticket = drafts.beginSubmission("a")
        store.release.countDown(); drain { !drafts.saving }
        assertTrue(drafts.isCurrent(ticket))
        drafts.acknowledge(ticket, true); drain { !drafts.saving }
        assertEquals("", store.value.entries["a"]!!.text)
    }

    @Test fun anAcceptanceArrivingAfterActivityDestructionStillClearsOnlyItsOwnedDraft() {
        val store = SlowStore(); val drafts = model(store)
        drafts.edit("a", "sent before exit"); drain { !drafts.saving }
        val ticket = drafts.beginSubmission("a")
        owners.clear(); drain { !drafts.saving }
        drafts.acknowledge(ticket, true)
        drain { store.value.entries["a"]?.text == "" }
        assertEquals("", store.value.entries["a"]!!.text)
    }

    private fun save(drafts: ConversationDrafts): Bundle {
        val saved = with(CurrentDraftCursor.saver(drafts)) {
            SaverScope { true }.save(CurrentDraftCursor(drafts, "a"))!!
        }
        val parcel = Parcel.obtain()
        return try {
            saved.writeToParcel(parcel, 0)
            val bytes = parcel.marshall()
            parcel.unmarshall(bytes, 0, bytes.size); parcel.setDataPosition(0)
            Bundle.CREATOR.createFromParcel(parcel)
        } finally { parcel.recycle() }
    }

    @Test fun savedStateRecoversTheLatestTypingWhileAnOlderWriteIsStillBlocked() {
        val original = SlowStore(); val drafts = model(original)
        original.block = true
        drafts.edit("a", "first pending")
        val first = drafts.read("a")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "latest pending\n🙂")
        val saved = save(drafts)
        // A process can die either before the old write commits or immediately after it.
        for (disk in listOf(original.value, PersistedDrafts(99, mapOf("a" to first.copy(revision = 99))))) {
            val recoveredStore = SlowStore().apply { value = disk }
            val recovered = model(recoveredStore)
            CurrentDraftCursor.saver(recovered).restore(saved)
            drain { !recovered.saving }
            assertEquals("latest pending\n🙂", recovered.read("a").text)
            assertEquals("latest pending\n🙂", recoveredStore.value.entries["a"]!!.text)
        }
    }

    @Test fun pendingSavedStateCannotResurrectAcceptedOrDeletedDraftsOrOverwriteNewerEdits() {
        val original = SlowStore(); val drafts = model(original)
        original.block = true; drafts.edit("a", "pending")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        val saved = save(drafts)
        for (disk in listOf(
            PersistedDrafts(100, mapOf("a" to DraftSnapshot("", 100))),
            PersistedDrafts(100, mapOf("a" to DraftSnapshot("newer", 100, "another-owner"))),
            PersistedDrafts(100, mapOf("a" to DraftSnapshot("pending", 100, "same-text-new-owner"))),
            PersistedDrafts(scopes = mapOf("a" to DraftScope(null, true)))
        )) {
            val recoveredStore = SlowStore().apply { value = disk }; val recovered = model(recoveredStore)
            CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
            assertEquals(disk.entries["a"] ?: DraftSnapshot(), recovered.read("a"))
        }
    }

    @Test fun savedAcceptanceClearsItsOwnedDiskVersionAfterProcessLoss() {
        val original = SlowStore(); val drafts = model(original)
        drafts.edit("a", "already sent"); drain { !drafts.saving }
        original.block = true; drafts.acknowledge(drafts.beginSubmission("a"), true)
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        val saved = save(drafts)
        val recoveredStore = SlowStore().apply { value = original.value }; val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertEquals("", recovered.read("a").text)
        assertEquals("", recoveredStore.value.entries["a"]!!.text)
    }

    @Test fun discardBeforeThePendingWriteFinishesCannotBeRevivedBySavedState() {
        val original = SlowStore(); val drafts = model(original)
        drafts.edit("a", "previous"); drain { !drafts.saving }
        original.block = true; drafts.edit("a", "discard this pending edit")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        drafts.discard("a")
        val saved = save(drafts)
        val recoveredStore = SlowStore().apply { value = original.value }; val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertEquals("", recovered.read("a").text)
        assertEquals("", recoveredStore.value.entries["a"]!!.text)
    }

    @Test fun pendingRecoveryCannotCrossAReplacedConversationGeneration() {
        val original = SlowStore().apply { value = PersistedDrafts(scopes = mapOf("a" to DraftScope("first"))) }
        val drafts = model(original)
        original.block = true; drafts.edit("a", "old generation")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        val saved = save(drafts)
        val recoveredStore = SlowStore().apply { value = PersistedDrafts(scopes = mapOf("a" to DraftScope("replacement"))) }
        val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertEquals("", recovered.read("a").text)
    }

    @Test fun savedNewerTypingSurvivesWhenAnAcceptedOlderDraftIsClearedAfterSavingState() {
        val original = SlowStore(); val drafts = model(original)
        drafts.edit("a", "sent"); drain { !drafts.saving }
        original.block = true; drafts.acknowledge(drafts.beginSubmission("a"), true)
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "next unsent")
        val saved = save(drafts)
        val tombstone = saved.getBundle("pending")!!.getStringArrayList("ancestors")!!.last().removePrefix("id:")
        val recoveredStore = SlowStore().apply { value = PersistedDrafts(100, mapOf("a" to DraftSnapshot("", 100, tombstone))) }
        val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertEquals("next unsent", recovered.read("a").text)
    }

    @Test fun aQueuedDiscardCannotEraseAnEditMadeAfterTheDiscard() {
        val store = SlowStore(); val drafts = model(store)
        drafts.edit("a", "old"); drain { !drafts.saving }
        store.block = true; drafts.discard("a")
        scheduler.runCurrent(); assertTrue(store.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "new after discard")
        store.release.countDown(); drain { !drafts.saving }
        assertEquals("new after discard", drafts.read("a").text)
        assertEquals("new after discard", store.value.entries["a"]!!.text)
    }

    @Test fun storageFailureDuringRecoveryKeepsTheSavedBackupAndRetriesAfterRecovery() {
        val original = SlowStore(); val drafts = model(original)
        original.block = true; drafts.edit("a", "recover complete material")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        val saved = save(drafts)
        val recoveredStore = SlowStore().apply { fail = true }; val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertNotNull(recovered.persistenceError)
        assertEquals("recover complete material", save(recovered).getBundle("pending")!!.getString("text"))
        recoveredStore.fail = false; recoveredStore.changes.value++
        drain { recovered.read("a").text == "recover complete material" && !recovered.saving }
        assertNull(recovered.persistenceError)
    }

    @Test fun savedNewTypingAfterDiscardSurvivesEitherSideOfTheDiscardCommit() {
        val original = SlowStore(); val drafts = model(original)
        drafts.edit("a", "discarded"); drain { !drafts.saving }
        original.block = true; drafts.discard("a")
        scheduler.runCurrent(); assertTrue(original.entered.await(2, TimeUnit.SECONDS))
        drafts.edit("a", "new draft after discard")
        val saved = save(drafts)
        val tombstone = saved.getBundle("pending")!!.getStringArrayList("ancestors")!!.last().removePrefix("id:")
        for (disk in listOf(original.value, PersistedDrafts(100, mapOf("a" to DraftSnapshot("", 100, tombstone))))) {
            val recoveredStore = SlowStore().apply { value = disk }; val recovered = model(recoveredStore)
            CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
            assertEquals("new draft after discard", recovered.read("a").text)
        }
    }

    @Test fun discardingAnUnpersistedEditCannotReviveItsPreviousDiskValueAfterAStorageFailure() {
        val original = SlowStore(); val drafts = model(original)
        drafts.edit("a", "previous disk draft"); drain { !drafts.saving }
        original.fail = true
        drafts.edit("a", "never committed"); drain { !drafts.saving }
        drafts.discard("a"); drain { !drafts.saving }
        assertNotNull(drafts.persistenceError)
        val saved = save(drafts)
        val recoveredStore = SlowStore().apply { value = original.value }; val recovered = model(recoveredStore)
        CurrentDraftCursor.saver(recovered).restore(saved); drain { !recovered.saving }
        assertEquals("", recovered.read("a").text)
        original.fail = false; original.changes.value++
        drain { drafts.read("a").text == "" && !drafts.saving }
        assertEquals("", original.value.entries["a"]!!.text)
        assertNull(drafts.persistenceError)
    }
}
