"""Tests for durable state, written as attempts to break it.

The point of these is not that state round-trips -- that was never in doubt.
The point is what happens when the process is killed mid-write, when the file is
corrupt, when the disk refuses the write. Each of those had a silent
money-losing failure mode before.
"""

import json
import os

import pytest

import store


@pytest.fixture(autouse=True)
def clean_store():
    store.forget()
    store.errors.clear()
    yield
    store.forget()
    store.errors.clear()


def blank():
    return {"halted": False, "lots": {}}


# ---- the basics -------------------------------------------------------------


def test_missing_file_is_not_an_error(tmp_path):
    """First run. There is genuinely nothing to load, and nothing to report."""
    path = tmp_path / "state.json"
    assert store.read(path, blank) == blank()
    assert store.status()["healthy"] is True


def test_round_trip_and_that_reads_do_not_touch_the_disk(tmp_path):
    path = tmp_path / "state.json"
    store.write(path, {"halted": True, "lots": {"a": 1}})

    # Delete the file entirely; the cached copy must still answer, because the
    # exit path must never wait on a filesystem.
    os.unlink(path)
    assert store.read(path, blank)["halted"] is True


def test_the_write_is_visible_to_a_separate_reader(tmp_path):
    path = tmp_path / "state.json"
    store.write(path, {"halted": True, "lots": {}})
    assert json.loads(path.read_text())["halted"] is True


# ---- the failure that mattered most ----------------------------------------


def test_a_truncated_state_file_does_not_silently_clear_a_halt(tmp_path):
    """The defect this module exists to fix.

    A non-atomic write killed halfway leaves an unparseable file. The old reader
    caught the parse error and returned blank state -- so a latched daily-loss
    halt un-latched itself across a crash. A halt that clears itself on a crash
    is not a safety mechanism.
    """
    path = tmp_path / "risk.json"
    store.write(path, {"halted": True, "halt_reason": "daily loss limit", "lots": {}})
    store.forget()

    # Simulate the kill: primary truncated mid-write.
    path.write_text('{"halted": true, "halt_rea')

    recovered = store.read(path, blank)
    assert recovered["halted"] is True, "the halt must survive a torn write"
    assert recovered["halt_reason"] == "daily loss limit"
    # The incident is reported, and stays reported after the file is repaired.
    # "healthy" going back to True is correct -- state is writable again -- but
    # the fact that the process was killed mid-write must not be erased.
    status = store.status()
    assert status["recovered_from_corruption"] is True
    assert status["recoveries"]


def test_recovery_repairs_the_primary_so_it_does_not_recur(tmp_path):
    path = tmp_path / "risk.json"
    store.write(path, {"halted": True, "lots": {}})
    store.forget()
    path.write_text("not json at all")

    store.read(path, blank)
    store.forget()
    store.errors.clear()

    # The primary is valid again, and reads no longer need the backup.
    assert store.read(path, blank)["halted"] is True
    assert store.status()["healthy"] is True


def test_both_copies_unreadable_reports_loudly_instead_of_pretending(tmp_path):
    path = tmp_path / "state.json"
    store.write(path, {"halted": True, "lots": {}})
    store.forget()

    path.write_text("garbage")
    path.with_suffix(path.suffix + ".bak").write_text("also garbage")

    value = store.read(path, blank)
    # Blank is the only thing it can return -- but it must not be silent, and
    # the message must say how to recover.
    assert value == blank()
    assert store.status()["healthy"] is False
    message = next(iter(store.status()["errors"].values()))["message"]
    assert "reconcil" in message.lower()


def test_a_json_file_that_is_not_an_object_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert store.read(path, blank) == blank()
    assert store.status()["healthy"] is False


# ---- write failures --------------------------------------------------------


def test_a_failed_write_raises_instead_of_being_swallowed(tmp_path):
    """The old code did `except Exception: pass`.

    A read-only volume therefore discarded every state write while the bot went
    on trading as though it had saved -- and would only find out at the next
    restart, having forgotten everything since the first failure.
    """
    path = tmp_path / "nested" / "state.json"
    path.parent.mkdir()
    path.parent.chmod(0o500)  # readable, not writable
    try:
        with pytest.raises(Exception):
            store.write(path, {"halted": True})
        assert store.status()["healthy"] is False
    finally:
        path.parent.chmod(0o700)


def test_no_temp_files_are_left_behind_after_a_failed_write(tmp_path):
    path = tmp_path / "nested" / "state.json"
    path.parent.mkdir()

    class Unserializable:
        pass

    with pytest.raises(Exception):
        store.write(path, {"bad": Unserializable()})

    leftovers = [name for name in os.listdir(path.parent) if name.endswith(".tmp")]
    assert leftovers == []


def test_a_failed_write_does_not_destroy_the_previous_good_state(tmp_path):
    path = tmp_path / "state.json"
    store.write(path, {"halted": True, "lots": {"keep": 1}})

    class Unserializable:
        pass

    with pytest.raises(Exception):
        store.write(path, {"bad": Unserializable()})

    store.forget()
    recovered = store.read(path, blank)
    assert recovered.get("lots") == {"keep": 1}


# ---- integration with the modules that hold money -------------------------


def test_risk_halt_survives_a_simulated_crash(tmp_path, monkeypatch):
    """End to end: latch a halt, corrupt the file, confirm it is still halted."""
    import config
    import risk

    path = tmp_path / "risk_state.json"
    monkeypatch.setattr(type(config.settings), "risk_state_path", path, raising=False)
    store.forget()

    risk.halt("daily loss limit reached")
    assert risk.is_halted()

    store.forget()
    path.write_text('{"halted": tr')
    assert risk.is_halted(), "a torn write must not resume trading"


def test_scalp_lots_survive_a_simulated_crash(tmp_path, monkeypatch):
    import config
    import scalp

    path = tmp_path / "lots.json"
    monkeypatch.setattr(
        config.settings, "lots_path", lambda mode: path, raising=False
    )
    store.forget()

    scalp.record_entry("dryrun", "KXBTC15M-T", "yes", 3, 45, None, lane="dip", conviction=70)
    assert len(scalp.open_lots("dryrun")) == 1

    store.forget()
    path.write_text('{"lots": {"KXBTC15M-T|ye')

    lots = scalp.open_lots("dryrun")
    assert len(lots) == 1, "an open position must not be forgotten by a torn write"
    assert lots[0]["entry_price"] == 45
    assert lots[0]["lane"] == "dip"


# ---- configuration hazards -------------------------------------------------


def test_cautions_flag_a_daily_limit_that_one_trade_can_exhaust(monkeypatch):
    """Legal, but one bad trade would halt the day. It must not be silent."""
    import config

    monkeypatch.setattr(config.settings, "max_cost_per_trade_cents", 2000)
    monkeypatch.setattr(config.settings, "daily_loss_limit_cents", 2000)
    notes = " ".join(config.settings.cautions())
    assert "daily budget" in notes

    monkeypatch.setattr(config.settings, "daily_loss_limit_cents", 6000)
    notes = " ".join(config.settings.cautions())
    assert "daily budget" not in notes


def test_cautions_never_block_startup(monkeypatch):
    """A hazard is a judgment call, not a bug: problems() must stay clean."""
    import config

    monkeypatch.setattr(config.settings, "max_cost_per_trade_cents", 2000)
    monkeypatch.setattr(config.settings, "daily_loss_limit_cents", 2000)
    assert config.settings.cautions()
    assert config.settings.problems() == []


def test_cautions_flag_conviction_sizing_with_no_range(monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "conviction_sizing", 1)
    monkeypatch.setattr(config.settings, "min_cost_per_trade_cents", 2000)
    monkeypatch.setattr(config.settings, "max_cost_per_trade_cents", 2000)
    assert any("conviction has no effect" in note for note in config.settings.cautions())


def test_cautions_flag_a_tape_shorter_than_the_dip_lookback(monkeypatch):
    import config

    monkeypatch.setattr(config.settings, "dip_entry", 1)
    monkeypatch.setattr(config.settings, "tape_seconds", 30)
    monkeypatch.setattr(config.settings, "dip_lookback_seconds", 90)
    assert any("full lookback window" in note for note in config.settings.cautions())
