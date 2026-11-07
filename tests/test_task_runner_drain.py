"""Actual QRunnable completion, without relying on queued GUI delivery."""
import threading
import unittest

from PySide6.QtWidgets import QApplication
from desktop_pet.async_tasks import TaskRunner


class TaskRunnerDrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def test_shutdown_finalizer_waits_for_real_accepted_work_without_pumping_gui(self):
        runner = TaskRunner()
        entered, release, finalized = threading.Event(), threading.Event(), threading.Event()
        order, gui_callbacks = [], []
        gui_thread = threading.get_ident()
        def write():
            entered.set()
            if not release.wait(3): raise RuntimeError("Owned worker was not released")
            order.append("write-ended")
        def finish():
            order.append(("finalizer", threading.get_ident()))
            finalized.set()
        runner.submit(write, lambda *_: gui_callbacks.append(True))
        try:
            self.assertTrue(entered.wait(2))
            runner.shutdown(on_drained=finish)
            self.assertFalse(finalized.wait(.03), "Closing GUI callbacks did not complete the disk function")
        finally: release.set()
        self.assertTrue(finalized.wait(3))
        self.assertEqual("write-ended", order[0])
        self.assertNotEqual(gui_thread, order[1][1])
        self.assertEqual([], gui_callbacks)

    def test_empty_shutdown_finalizes_once_and_rejects_new_work(self):
        runner = TaskRunner(); called = []
        runner.shutdown(on_drained=lambda: called.append(True))
        runner.shutdown(on_drained=lambda: called.append(False))
        self.assertEqual([True], called)
        self.assertIsNone(runner.submit(lambda: self.fail("No new accepted work"), lambda *_: None))

    def test_failed_worker_still_completes_drain_and_does_not_need_gui_callback(self):
        runner = TaskRunner(); release, finalized = threading.Event(), threading.Event()
        def fail():
            release.wait(2)
            raise OSError("Owned synthetic write failure")
        runner.submit(fail, lambda *_: self.fail("Shutdown callback must be suppressed"))
        runner.shutdown(on_drained=finalized.set)
        release.set()
        self.assertTrue(finalized.wait(3))


if __name__ == "__main__": unittest.main()
