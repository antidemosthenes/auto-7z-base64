import unittest
from threading import Event, Lock

from auto_7z_base64.parallel import _parallel


class ParallelTests(unittest.TestCase):
    def test_slow_first_task_does_not_stall_later_tasks(self):
        progress = Event()
        lock = Lock()
        finished = []

        def action(index, cancel):
            if index == 0:
                if not progress.wait(5):
                    raise RuntimeError("Later tasks stalled behind the first task")
            with lock:
                finished.append(index)
                if len(finished) >= 6:
                    progress.set()

        _parallel(range(12), action, 2)
        self.assertEqual(sorted(finished), list(range(12)))

    def test_worker_failure_cancels_running_tasks(self):
        started = Event()
        stopped = Event()

        def action(index, cancel):
            if index == 0:
                started.set()
                if cancel.wait(5):
                    stopped.set()
                else:
                    raise RuntimeError("Cancellation did not reach the running task")
            elif started.wait(5):
                raise ValueError("worker failed")

        with self.assertRaisesRegex(ValueError, "worker failed"):
            _parallel(range(4), action, 2)
        self.assertTrue(stopped.is_set())
