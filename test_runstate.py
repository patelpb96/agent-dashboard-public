import tempfile
import unittest
from pathlib import Path

import runstate


class RunStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.now = [1000.0]

    def make(self, pid=4242):
        return runstate.RunState(8799, pid=pid, state_dir=self.dir, clock=lambda: self.now[0])

    def status(self, alive):
        return runstate.status(8799, lambda pid: alive, state_dir=self.dir)

    def test_no_record(self):
        code, text = self.status(alive=False)
        self.assertEqual(code, 3)
        self.assertIn("No record", text)

    def test_running_while_pid_alive(self):
        self.make().start()
        code, text = self.status(alive=True)
        self.assertEqual(code, 0)
        self.assertIn("pid 4242", text)

    def test_running_record_with_dead_pid_is_an_unclean_end(self):
        self.make().start()
        code, text = self.status(alive=False)
        self.assertEqual(code, 2)
        self.assertIn("without a clean stop", text)

    def test_window_close_is_recorded_and_first_reason_wins(self):
        run = self.make()
        run.start()
        self.now[0] = 2000.0
        self.assertFalse(run.console_event(2))   # let Windows / Python's own handler carry on
        self.assertFalse(run.stop("exited"))     # the finally block must not overwrite the reason
        data = runstate.read(8799, self.dir)
        self.assertEqual((data["state"], data["reason"], data["stoppedAt"]), ("stopped", "window closed", 2000.0))
        code, text = self.status(alive=False)
        self.assertEqual(code, 1)
        self.assertIn("window closed", text)

    def test_console_events_map_to_reasons(self):
        for event, reason in [(0, "Ctrl+C"), (1, "Ctrl+Break"), (2, "window closed"),
                              (5, "user signed out"), (6, "Windows shut down"), (9, "console event 9")]:
            run = self.make()
            run.console_event(event)
            self.assertEqual(runstate.read(8799, self.dir)["reason"], reason)

    def test_restart_replaces_a_stopped_record(self):
        old = self.make()
        old.start()
        old.stop("Ctrl+C")
        self.make(pid=5555).start()
        code, text = self.status(alive=True)
        self.assertEqual(code, 0)
        self.assertIn("pid 5555", text)

    def test_corrupt_record_reads_as_none(self):
        runstate.record_path(8799, self.dir).write_text("{not json", encoding="utf-8")
        self.assertIsNone(runstate.read(8799, self.dir))
        self.assertEqual(self.status(alive=True)[0], 3)


if __name__ == "__main__":
    unittest.main()
