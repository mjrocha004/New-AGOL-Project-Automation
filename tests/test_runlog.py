"""The run log: what happened, in what order, and how long each step took.

Every diagnosis in this project so far has come back to two questions -- how long
did that take, and which item was it on -- reconstructed by hand from a terminal
scrollback. A view whose layers never arrived, an index that failed once and
worked on the next run, a process that sat for ninety minutes after it had
finished: all of them were timing questions answered from memory.
"""

import pytest

from agol_provision.runlog import RunLog


class Clock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


@pytest.fixture
def log(tmp_path):
    return RunLog(tmp_path / "companya-moline.log", clock=Clock(),
                  now=lambda: "2026-09-09T14:03:11+00:00")


def lines(log):
    return log.path.read_text().splitlines()


class TestWriting:
    def test_an_event_records_a_timestamp_and_a_label(self, log):
        log.event("provision", "CompanyA / Moline")
        assert lines(log) == [
            "2026-09-09T14:03:11+00:00              provision  CompanyA / Moline"
        ]

    def test_a_step_records_how_long_it_took(self, log):
        with log.step("master.copy", "CompanyA_Moline"):
            log._clock.advance(92.11)
        assert "92110ms  master.copy  CompanyA_Moline" in lines(log)[0]

    def test_a_step_can_name_its_result_once_it_knows_it(self, log):
        with log.step("master.indexes") as note:
            log._clock.advance(1.2)
            note.detail = "10 applied, 0 failed"
        assert lines(log)[0].endswith("master.indexes  10 applied, 0 failed")

    def test_appends_across_runs(self, log):
        log.event("provision", "first")
        log.event("provision", "second")
        assert len(lines(log)) == 2

    def test_creates_the_directory(self, tmp_path):
        target = tmp_path / "state" / "companya-moline.log"
        RunLog(target, now=lambda: "T").event("provision")
        assert target.exists()


class TestFailures:
    def test_a_failing_step_is_recorded_and_the_error_still_propagates(self, log):
        with pytest.raises(RuntimeError):
            with log.step("views.create", "CompanyA_Moline_QC"):
                log._clock.advance(0.5)
                raise RuntimeError("Invalid definition")

        line = lines(log)[0]
        assert "FAILED views.create" in line
        assert "Invalid definition" in line
        assert "500ms" in line

    def test_the_error_is_collapsed_onto_one_line(self, log):
        """AGOL returns four-line errors; a log with embedded newlines is unreadable."""
        with pytest.raises(RuntimeError):
            with log.step("master.indexes"):
                raise RuntimeError("Unable to add.\nInvalid definition\n(Error Code: 400)")
        assert len(lines(log)) == 1

    def test_a_stop_is_recorded_as_what_it_was(self, log):
        """`_fail` raises SystemExit, whose message is just the exit code."""
        with pytest.raises(SystemExit):
            with log.step("views.create"):
                raise SystemExit(1)
        assert "SystemExit" in lines(log)[0]


class TestNeverBreaksTheRun:
    def test_no_path_writes_nothing_and_raises_nothing(self):
        """--dry-run promises to write nothing at all, log included."""
        quiet = RunLog(None)
        quiet.event("provision")
        with quiet.step("master.copy"):
            pass

    def test_an_unwritable_path_is_swallowed(self, tmp_path):
        """A log that breaks the run it records is worse than no log."""
        blocked = tmp_path / "file"
        blocked.write_text("not a directory")
        log = RunLog(blocked / "companya.log")
        log.event("provision")  # must not raise

    def test_a_failing_write_still_lets_the_step_finish(self, tmp_path):
        blocked = tmp_path / "file"
        blocked.write_text("not a directory")
        log = RunLog(blocked / "companya.log")
        with log.step("master.copy"):
            pass
