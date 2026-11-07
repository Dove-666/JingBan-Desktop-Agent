"""Independent real SQLite/QRunnable drain review; owned temp data only."""
from contextlib import contextmanager
import os
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
import time

from PySide6.QtCore import QThreadPool,QRunnable
from PySide6.QtWidgets import QApplication
from desktop_pet.async_tasks import TaskRunner


@contextmanager
def owned_connection(path):
    connection=sqlite3.connect(path)
    try:
        with connection:yield connection
    finally:connection.close()


class StartAcceptedDrainReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def test_post_accept_start_exception_does_not_finalize_before_actual_sqlite_commit(self):
        with tempfile.TemporaryDirectory(prefix="accepted-drain-review-") as d:
            database=Path(d)/"owned.sqlite3"
            with owned_connection(database) as c:c.execute("CREATE TABLE committed(value TEXT)")
            entered,release,ended,finalized=(threading.Event() for _ in range(4))
            rows_seen=[];runner=TaskRunner();pool=QThreadPool.globalInstance();actual=pool.start
            def write():
                try:
                    with owned_connection(database) as c:
                        c.execute("INSERT INTO committed VALUES ('OWNED_COMMITTED')")
                        entered.set()
                        if not release.wait(3):raise RuntimeError("Controlled writer not released")
                finally:ended.set()
            def finalizer():
                with owned_connection(database) as c:rows_seen.append(c.execute("SELECT value FROM committed").fetchall())
                finalized.set()
            def start_then_raise(worker,*args):
                actual(worker,*args)
                if not entered.wait(2):raise AssertionError("Actual QRunnable did not enter")
                raise RuntimeError("Controlled host error after actual Qt acceptance")
            try:
                with patch.object(pool,"start",start_then_raise):
                    with self.assertRaises(RuntimeError):runner.submit(write,lambda *_:None)
                runner.shutdown(on_drained=finalizer)
                premature=finalized.wait(.03)
            finally:
                release.set();self.assertTrue(ended.wait(3));pool.waitForDone(3000)
            self.assertTrue(finalized.wait(2))
            self.assertFalse(premature,"Finalizer ran while the accepted SQLite transaction was still blocked")
            self.assertEqual([[('OWNED_COMMITTED',)]],rows_seen)

    def test_actual_queued_but_not_started_exception_cancels_function_and_completes_once(self):
        pool=QThreadPool();pool.setMaxThreadCount(1)
        occupied,release,forbidden=(threading.Event() for _ in range(3))
        class Blocker(QRunnable):
            def run(self):occupied.set();release.wait(3)
        pool.start(Blocker());self.assertTrue(occupied.wait(2))
        runner=TaskRunner();actual=pool.start;callbacks=[];finalized=[]
        def accepted_then_raise(worker,*args):
            actual(worker,*args)
            raise RuntimeError("Controlled queued acceptance then host error")
        try:
            with patch("desktop_pet.async_tasks.QThreadPool.globalInstance",return_value=pool),\
                 patch.object(pool,"start",accepted_then_raise),patch.object(runner._drain,"complete",wraps=runner._drain.complete) as complete:
                with self.assertRaises(RuntimeError):runner.submit(forbidden.set,lambda *_:callbacks.append(True))
                self.assertFalse(forbidden.is_set())
                runner.shutdown(on_drained=lambda:finalized.append(True));runner.shutdown(on_drained=lambda:finalized.append(False))
                self.assertEqual([True],finalized,"Exact function cancellation permits drain even if the no-op Qt wrapper is queued")
                release.set();self.assertTrue(pool.waitForDone(3000))
                self.assertFalse(forbidden.is_set());self.assertEqual(1,complete.call_count)
                self.assertEqual(0,runner._drain._pending)
            self.app.processEvents();self.assertEqual([],callbacks)
        finally:release.set();pool.waitForDone(3000)

    def test_before_accept_exception_cancels_exact_function_and_late_entry_cannot_double_complete(self):
        pool=QThreadPool();runner=TaskRunner();captured=[];executed=[];callbacks=[];finalized=[]
        def reject(worker,*args):captured.append(worker);raise RuntimeError("Controlled rejection before native acceptance")
        with patch("desktop_pet.async_tasks.QThreadPool.globalInstance",return_value=pool),patch.object(pool,"start",reject),\
             patch.object(runner._drain,"complete",wraps=runner._drain.complete) as complete:
            with self.assertRaises(RuntimeError):runner.submit(lambda:executed.append(True),lambda *_:callbacks.append(True))
            self.assertEqual(1,complete.call_count);self.assertEqual(0,runner._drain._pending)
            # This synthetic late entry is a negative control, not a claim Qt
            # accepted the rejected call. The exact function is already dead.
            captured[0].run()
            self.assertEqual(1,complete.call_count);self.assertEqual(0,runner._drain._pending)
            self.assertEqual([],executed);self.assertEqual([],callbacks)
            runner.shutdown(on_drained=lambda:finalized.append(True));self.assertEqual([True],finalized)


class ArchiveCommitDrainReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def pump(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.002)
        self.assertTrue(predicate())

    @contextmanager
    def owned_chat(self):
        from desktop_pet.chat_controller import ChatController
        from desktop_pet.conversations import ConversationStore
        from desktop_pet.draft_store import LocalDraftStore
        from desktop_pet.draft_owner import LocalDraftOwner
        from desktop_pet.ui.draft_state import archive_identity_reader
        from tests.test_application import isolated_settings,MemoryCredentialBackend
        with tempfile.TemporaryDirectory(prefix="archive-draft-commit-review-") as d:
            root=Path(d);archive=ConversationStore(root/"archive.sqlite3");cid=str(uuid4())
            archive.append_message(cid,"user","OWNED_ARCHIVED_MESSAGE","user",message_id=str(uuid4()))
            settings=isolated_settings(root/"settings.ini",MemoryCredentialBackend())
            with patch("desktop_pet.chat_controller.capture_execution_runtime",side_effect=AssertionError("No production execution config")),\
                 patch("desktop_pet.chat_controller.capture_office_runtime",side_effect=AssertionError("No production office config")):
                chat=ChatController(settings,store=archive,metadata_client_factory=lambda:None)
                self.assertTrue(chat.open_conversation(cid));self.pump(lambda:not chat.history_loading)
                self.pump(lambda:QThreadPool.globalInstance().activeThreadCount()==0 and not chat._runner._tasks)
                side=LocalDraftStore(root/"drafts.sqlite3");identity=archive_identity_reader(archive);threads=[]
                def reader(target):threads.append(threading.get_ident());return identity(target)
                owner=LocalDraftOwner(side,identity_reader=reader);chat._local_draft_owner=owner
                self.assertTrue(owner._loaded.wait(2));owner.set_text(cid,"OWNED_UNSENT_DRAFT")
                self.assertTrue(owner.wait(owner.flush(),2))
                try:yield root,archive,chat,owner,side,cid,threads
                finally:
                    chat.shutdown();QThreadPool.globalInstance().waitForDone(3000)
                    owner.shutdown(2);self.assertTrue(owner._stopped.is_set())
                    chat.deleteLater();self.app.processEvents()

    def boundary(self,action,after_commit):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            entered,release=threading.Event(),threading.Event();armed=[True]
            gui_thread=threading.get_ident();actual_connect=sqlite3.connect
            class CommitFault(sqlite3.Connection):
                target_mutation=False
                def execute(self,sql,parameters=(),*args,**kwargs):
                    normalized=" ".join(sql.upper().split())
                    expected="DELETE FROM MESSAGES WHERE CONVERSATION_ID" if action=="clear" else "DELETE FROM CONVERSATIONS WHERE ID"
                    if normalized.startswith(expected) and parameters and parameters[0]==cid:self.target_mutation=True
                    return super().execute(sql,parameters,*args,**kwargs)
                def __exit__(self,typ,value,traceback):
                    if self.target_mutation and armed[0] and typ is None:
                        armed[0]=False
                        if after_commit:super().__exit__(typ,value,traceback)
                        else:self.rollback()
                        entered.set()
                        if not release.wait(3):raise RuntimeError("Controlled commit fault not released")
                        raise sqlite3.OperationalError("OWNED_POSTCOMMIT_ERROR" if after_commit else "OWNED_COMMIT_REJECTED")
                    return super().__exit__(typ,value,traceback)
            def connect(path,*args,**kwargs):
                if str(path)==str(archive.path):kwargs["factory"]=CommitFault
                return actual_connect(path,*args,**kwargs)
            actual_shutdown=owner.shutdown;shutdown_calls=[]
            def shutdown(*args,**kwargs):shutdown_calls.append(threading.get_ident());return actual_shutdown(*args,**kwargs)
            with patch.object(sqlite3,"connect",connect),patch.object(owner,"shutdown",shutdown):
                try:
                    if action=="clear":chat.clear()
                    else:self.assertTrue(chat.delete_conversation(cid))
                    self.assertTrue(entered.wait(2))
                    # Deliberately no QApplication.processEvents from this
                    # point until real DB/drain/draft completion is asserted.
                    chat.shutdown();chat.shutdown()
                    self.assertFalse(owner._stopped.wait(.03))
                    release.set();self.assertTrue(owner._stopped.wait(3))
                    QThreadPool.globalInstance().waitForDone(3000)
                finally:release.set()
            self.assertEqual(1,len(shutdown_calls),"Repeated controller shutdown must not run the finalizer twice")
            self.assertTrue(threads and all(thread!=gui_thread for thread in threads),"Archive identity rollback checks performed GUI-thread IO")
            draft=side.lookup(cid)
            self.assertIsNotNone(draft)
            if after_commit:
                self.assertEqual("",draft.text,"Committed archive deletion/clear must never resurrect the previous draft")
                self.assertEqual([],archive.load_messages(cid))
            else:
                self.assertEqual("OWNED_UNSENT_DRAFT",draft.text,"Rejected SQLite commit must preserve the actual unsent draft")
                self.assertEqual(1,len(archive.load_messages(cid)))

    def test_clear_sqlite_commit_rejection_restores_draft_after_shutdown_without_gui_callbacks(self):
        self.boundary("clear",False)

    def test_delete_sqlite_commit_rejection_restores_draft_after_shutdown_without_gui_callbacks(self):
        self.boundary("delete",False)

    def test_clear_actual_commit_then_error_keeps_durable_empty_fence(self):
        self.boundary("clear",True)

    def test_delete_actual_commit_then_error_keeps_durable_empty_fence(self):
        self.boundary("delete",True)

    def test_postclear_readonly_metadata_failure_cannot_rollback_a_committed_draft_clear(self):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            read=archive.get_conversation;failed=threading.Event();gui_thread=threading.get_ident()
            def metadata(target):
                if target==cid and not archive.load_messages(cid):
                    failed.set();raise OSError("OWNED_READONLY_METADATA_FAILURE")
                return read(target)
            with patch.object(archive,"get_conversation",metadata):
                chat.clear();self.pump(lambda:not chat.history_loading)
            self.assertTrue(failed.is_set());self.assertEqual([],archive.load_messages(cid))
            self.assertEqual("",owner.snapshot(cid).text)
            self.assertTrue(owner.wait(owner.flush(),2));self.assertEqual("",side.lookup(cid).text)
            self.assertTrue(threads and all(thread!=gui_thread for thread in threads))

    def test_postclear_metadata_error_does_not_restore_messages_already_deleted_from_sqlite(self):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            self.assertEqual(1,len(chat.messages));read=archive.get_conversation
            def metadata(target):
                if target==cid and not archive.load_messages(cid):raise OSError("OWNED_READONLY_METADATA_FAILURE")
                return read(target)
            with patch.object(archive,"get_conversation",metadata):
                chat.clear();self.pump(lambda:not chat.history_loading)
            self.assertEqual([],archive.load_messages(cid))
            self.assertEqual([],chat.messages,"Read-only metadata failure must not resurrect the committed clear's old UI messages")

    def committed_error_gui(self,action):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            armed=[True];actual_connect=sqlite3.connect
            class CommitThenError(sqlite3.Connection):
                mutated=False
                def execute(self,sql,parameters=(),*args,**kwargs):
                    prefix="DELETE FROM MESSAGES WHERE CONVERSATION_ID" if action=="clear" else "DELETE FROM CONVERSATIONS WHERE ID"
                    if " ".join(sql.upper().split()).startswith(prefix) and parameters and parameters[0]==cid:self.mutated=True
                    return super().execute(sql,parameters,*args,**kwargs)
                def __exit__(self,typ,value,traceback):
                    answer=super().__exit__(typ,value,traceback)
                    if self.mutated and armed[0] and typ is None:
                        armed[0]=False;raise sqlite3.OperationalError("OWNED_COMMIT_APPLIED_ACK_FAILED")
                    return answer
            def connect(path,*args,**kwargs):
                if str(path)==str(archive.path):kwargs["factory"]=CommitThenError
                return actual_connect(path,*args,**kwargs)
            with patch.object(sqlite3,"connect",connect):
                if action=="clear":chat.clear()
                else:self.assertTrue(chat.delete_conversation(cid))
                self.pump(lambda:cid not in chat._invalidating_conversations)
            self.assertEqual([],archive.load_messages(cid))
            self.assertEqual("",owner.snapshot(cid).text)
            self.assertNotIn("OWNED_ARCHIVED_MESSAGE",[row.get("content") for row in chat.messages],
                "An uncertain mutator ACK cannot restore old rows already removed by the actual SQLite commit")
            self.assertTrue(chat.error,"This must remain an uncertain/error outcome rather than a false successful ACK")

    def test_clear_actual_commit_ack_error_with_gui_callbacks_never_restores_old_rows(self):self.committed_error_gui("clear")

    def test_delete_actual_commit_ack_error_with_gui_callbacks_never_restores_old_rows(self):self.committed_error_gui("delete")

    @contextmanager
    def held_postcommit_ack(self,archive,cid,action="clear"):
        entered,release=threading.Event(),threading.Event();armed=[True];actual=sqlite3.connect
        class Connection(sqlite3.Connection):
            mutated=False
            def execute(self,sql,parameters=(),*args,**kwargs):
                prefix="DELETE FROM MESSAGES WHERE CONVERSATION_ID" if action=="clear" else "DELETE FROM CONVERSATIONS WHERE ID"
                if " ".join(sql.upper().split()).startswith(prefix) and parameters and parameters[0]==cid:self.mutated=True
                return super().execute(sql,parameters,*args,**kwargs)
            def __exit__(self,typ,value,traceback):
                answer=super().__exit__(typ,value,traceback)
                if self.mutated and armed[0] and typ is None:
                    armed[0]=False;entered.set()
                    if not release.wait(4):raise RuntimeError("Controlled ACK gate not released")
                    raise sqlite3.OperationalError("OWNED_LATE_ACK_FAILED")
                return answer
        def connect(path,*args,**kwargs):
            if str(path)==str(archive.path):kwargs["factory"]=Connection
            return actual(path,*args,**kwargs)
        with patch.object(sqlite3,"connect",connect):
            try:yield entered,release
            finally:release.set()

    def test_same_cid_late_history_error_cannot_restore_rows_from_before_committed_clear(self):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            read=archive.get_conversation;load_entered,load_release=threading.Event(),threading.Event();load_thread=[]
            def held_history(target):
                if target==cid and (not load_thread or load_thread[0]==threading.get_ident()):
                    if not load_thread:load_thread.append(threading.get_ident())
                    load_entered.set()
                    if not load_release.wait(4):raise RuntimeError("Controlled history load not released")
                    raise OSError("OWNED_LATE_HISTORY_READ_FAILED")
                return read(target)
            with self.held_postcommit_ack(archive,cid) as (ack_entered,ack_release):
                try:
                    chat.clear();self.assertTrue(ack_entered.wait(2))
                    with patch.object(archive,"get_conversation",held_history):
                        self.assertTrue(chat.open_conversation(cid));self.assertTrue(load_entered.wait(2))
                        ack_release.set();self.pump(lambda:cid not in chat._invalidating_conversations)
                        load_release.set();self.pump(lambda:QThreadPool.globalInstance().activeThreadCount()==0)
                        self.app.processEvents()
                finally:ack_release.set();load_release.set()
            self.assertEqual([],archive.load_messages(cid))
            self.assertNotIn("OWNED_ARCHIVED_MESSAGE",[r.get("content") for r in chat.messages])
            self.assertFalse(chat.history_loading);self.assertTrue(chat.error)

    def switched_history(self,*,failed=False,action="clear"):
        with self.owned_chat() as (root,archive,chat,owner,side,cid,threads):
            other=str(uuid4());archive.append_message(other,"user","OWNED_OTHER_CONVERSATION","user",message_id=str(uuid4()))
            read=archive.get_conversation;entered,release=threading.Event(),threading.Event()
            def held_other(target):
                if target==other:
                    entered.set()
                    if not release.wait(4):raise RuntimeError("Controlled other history not released")
                    if failed:raise OSError("OWNED_OTHER_HISTORY_FAILED")
                return read(target)
            with self.held_postcommit_ack(archive,cid,action) as (ack_entered,ack_release):
                try:
                    if action=="clear":chat.clear()
                    else:self.assertTrue(chat.delete_conversation(cid))
                    self.assertTrue(ack_entered.wait(2))
                    with patch.object(archive,"get_conversation",held_other):
                        self.assertTrue(chat.open_conversation(other));self.assertTrue(entered.wait(2))
                        ack_release.set();self.pump(lambda:cid not in chat._invalidating_conversations)
                        self.assertEqual(other,chat.current_conversation_id)
                        self.assertTrue(chat.history_loading,"A's uncertain result cancelled unrelated B's pending load")
                        release.set();self.pump(lambda:not chat.history_loading)
                finally:ack_release.set();release.set()
            self.assertNotIn("OWNED_ARCHIVED_MESSAGE",[r.get("content") for r in chat.messages])
            if not failed:
                self.assertEqual(other,chat.current_conversation_id)
                self.assertEqual(["OWNED_OTHER_CONVERSATION"],[r["content"] for r in chat.messages])
            else:
                self.assertFalse(chat.messages,"A failed B load restored A's already-cleared pre-mutation fallback")
                self.assertTrue(chat.error)

    def test_other_cid_pending_load_is_not_cancelled_or_polluted_by_clear_or_delete_ack_error(self):
        for action in ("clear","delete"):
            with self.subTest(action=action):self.switched_history(action=action)

    def test_other_cid_failed_load_fallback_does_not_resurrect_cleared_history(self):self.switched_history(failed=True)


if __name__=="__main__":unittest.main()
