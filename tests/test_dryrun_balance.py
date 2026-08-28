"""DRYRUN_BALANCE_CENTS: simulated capital, and its containment to dryun.

The override exists so a dryrun on a small real account still exercises the
strategy at its designed size. The safety property that matters is narrow and
absolute: it must be impossible for this setting to change the size of a real
order. These tests assert that from several directions, because a leak here
would spend money that isn't there.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
import trader  # noqa: E402


@pytest.fixture
def override(monkeypatch):
    """Set DRYRUN_BALANCE_CENTS on the live settings object."""

    def _apply(cents):
        monkeypatch.setattr(config.settings, "dryrun_balance_cents", cents)

    return _apply


REAL = 657  # the actual deployed balance that motivated this feature
SIM = 20000  # $200 simulated


# ---- the containment guarantee ---------------------------------------------


@pytest.mark.parametrize("active_mode", ["live", "off", "", None, "LIVE", "unknown"])
def test_override_ignored_outside_dryrun(override, active_mode):
    """Any mode that is not exactly "dryrun" must use the real balance."""
    override(SIM)
    assert trader._sizing_balance_cents(REAL, active_mode) == REAL


def test_live_mode_ignores_override_even_when_enormous(override):
    """A misconfigured override must not be able to inflate a real order."""
    override(10_000_000)
    assert trader._sizing_balance_cents(REAL, "live") == REAL


def test_live_mode_ignores_override_even_when_real_balance_is_zero(override):
    """A flat account stays flat in live mode."""
    override(SIM)
    assert trader._sizing_balance_cents(0, "live") == 0


# ---- dryrun behaviour ------------------------------------------------------


def test_dryrun_uses_override(override):
    override(SIM)
    assert trader._sizing_balance_cents(REAL, "dryrun") == SIM


def test_dryrun_without_override_uses_real_balance(override):
    """Default (0) must be a complete no-op, changing nothing."""
    override(0)
    assert trader._sizing_balance_cents(REAL, "dryrun") == REAL


def test_dryrun_override_can_be_smaller_than_real(override):
    """Simulating a SMALLER account is legitimate (stress-testing sizing)."""
    override(300)
    assert trader._sizing_balance_cents(REAL, "dryrun") == 300


def test_negative_override_is_not_applied(override):
    """problems() rejects it, but the sizer must not honour it regardless."""
    override(-500)
    assert trader._sizing_balance_cents(REAL, "dryrun") == REAL


def test_real_balance_of_none_passes_through(override):
    """Before the first reconcile the balance is None; that must survive.

    Downstream sizing treats None as "unknown" and skips balance caps. Turning
    it into a number here would silently enable sizing against a balance that
    was never fetched.
    """
    override(0)
    assert trader._sizing_balance_cents(None, "dryrun") is None
    assert trader._sizing_balance_cents(None, "live") is None


def test_override_replaces_none_in_dryrun(override):
    """With an override, dryrun need not wait for a real balance fetch."""
    override(SIM)
    assert trader._sizing_balance_cents(None, "dryrun") == SIM


# ---- validation ------------------------------------------------------------


def test_negative_override_is_a_problem():
    settings = config.Settings()
    settings.dryrun_balance_cents = -1
    assert any("DRYRUN_BALANCE_CENTS" in issue for issue in settings.problems())


def test_override_below_min_trade_cost_is_a_problem():
    """An override too small to afford any entry would record nothing."""
    settings = config.Settings()
    settings.dryrun_balance_cents = 100
    settings.min_cost_per_trade_cents = 500
    issues = [i for i in settings.problems() if "DRYRUN_BALANCE_CENTS" in i]
    assert len(issues) == 1
    assert "no entry could ever be afforded" in issues[0]


def test_override_at_min_trade_cost_is_accepted():
    """Exactly affordable is legal -- the guard is strictly below."""
    settings = config.Settings()
    settings.dryrun_balance_cents = 500
    settings.min_cost_per_trade_cents = 500
    assert not any("DRYRUN_BALANCE_CENTS" in i for i in settings.problems())


def test_zero_override_is_never_a_problem():
    """The default must never produce a validation issue."""
    settings = config.Settings()
    settings.dryrun_balance_cents = 0
    settings.min_cost_per_trade_cents = 500
    assert not any("DRYRUN_BALANCE_CENTS" in i for i in settings.problems())


# ---- disclosure ------------------------------------------------------------


def test_dryrun_override_raises_a_caution():
    """Simulated P&L must be labelled as hypothetical, not as a forecast."""
    settings = config.Settings()
    settings.trading_mode = "dryrun"
    settings.dryrun_balance_cents = SIM
    notes = [n for n in settings.cautions() if "SIMULATED" in n]
    assert len(notes) == 1
    assert "$200.00" in notes[0]


def test_live_with_override_set_warns_it_is_inert():
    """Better to say it's ignored than to leave the operator guessing."""
    settings = config.Settings()
    settings.trading_mode = "live"
    settings.dryrun_balance_cents = SIM
    assert any("no effect in live mode" in n for n in settings.cautions())


def test_no_caution_when_override_is_off():
    settings = config.Settings()
    settings.trading_mode = "dryrun"
    settings.dryrun_balance_cents = 0
    assert not any("SIMULATED" in n for n in settings.cautions())


def test_cautions_never_become_problems():
    """A simulated balance must not block startup."""
    settings = config.Settings()
    settings.trading_mode = "dryrun"
    settings.dryrun_balance_cents = SIM
    settings.min_cost_per_trade_cents = 500
    assert not any("DRYRUN_BALANCE_CENTS" in i for i in settings.problems())


# ---- status surface --------------------------------------------------------


def test_status_exposes_both_balances():
    """The dashboard must be able to show the true account value always."""
    for key in ("balance_cents", "exchange_balance_cents", "balance_is_simulated"):
        assert key in trader.status
