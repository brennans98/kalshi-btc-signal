"""Tests for the reworked strategy: contract tape, dip lanes, runner exits, sizing.

These are unit tests over synthetic data, and it is worth being precise about
what that does and does not establish. They prove the new code computes what it
claims to compute -- that a manufactured 12-cent drop is measured as a 12-cent
drop, that a dip signal fires on it and not on a flat book, that the trailing
stop widens with the peak and never sits inside the spread, that conviction
sizing stays inside the configured dollar bounds.

They prove nothing whatsoever about profitability. A backtest on real recorded
book data would be the next honest step, and even that would not settle it.
"""

import time

import pytest

import booktape
import config
import policy
import risk
import scalp


# ---- helpers ---------------------------------------------------------------


def raw_book(yes_bid, yes_ask, yes_size=50, no_size=50):
    """A Kalshi-shaped raw book. NO levels are the mirror of the YES levels."""
    return {
        "orderbook": {
            "yes": [[yes_bid, yes_size]],
            "no": [[100 - yes_ask, no_size]],
        }
    }


def book(yes_bid, yes_ask, yes_size=50, no_size=50):
    """The normalized top-of-book shape the tape and policy consume."""
    return policy.book_snapshot(raw_book(yes_bid, yes_ask, yes_size, no_size))


def fill_tape(ticker, prices, start=None, step=1.0, size=50):
    """Write a synthetic YES-bid path onto a contract's tape."""
    booktape.forget(ticker)
    now = start or time.time()
    base = now - step * len(prices)
    for index, bid in enumerate(prices):
        booktape.record(ticker, book(bid, bid + 2, size, size), now=base + index * step)
    return base + step * (len(prices) - 1)


@pytest.fixture(autouse=True)
def clean_tape():
    booktape._tapes.clear()
    yield
    booktape._tapes.clear()


# ---- the contract tape -----------------------------------------------------


def test_tape_needs_a_minimum_history_before_it_will_speak():
    """A two-sample tape must return None, not a confident read of noise."""
    fill_tape("KXBTC15M-A", [50, 51])
    assert booktape.analyze("KXBTC15M-A", "yes") is None


def test_tape_measures_a_drop_from_the_window_high():
    now = time.time()
    # Rises to 60, then falls to 48: a 12-cent drop off the high.
    path = [50, 51, 52, 55, 58, 60, 59, 58, 55, 53, 51, 50, 49, 48, 47, 48]
    last = fill_tape("KXBTC15M-B", path, start=now, step=0.5)

    read = booktape.analyze("KXBTC15M-B", "yes", now=last + 0.1)
    assert read is not None
    # Mid of a (bid, bid+2) book at the 60 high is 61.
    assert read["high_mid"] == pytest.approx(61.0)
    assert read["low_mid"] == pytest.approx(48.0)
    assert read["drop_cents"] == pytest.approx(12.0, abs=1.0)
    assert read["bid"] == 48
    assert read["seconds_since_high"] > 0


def test_no_side_is_the_mirror_of_the_yes_side():
    """A YES collapse is a NO rally; the tape must read it that way."""
    now = time.time()
    path = [60, 58, 55, 50, 45, 40, 38, 36, 35, 34, 33, 32, 31, 30, 29, 28]
    last = fill_tape("KXBTC15M-C", path, start=now, step=0.5)

    yes_read = booktape.analyze("KXBTC15M-C", "yes", now=last + 0.1)
    no_read = booktape.analyze("KXBTC15M-C", "no", now=last + 0.1)

    assert yes_read["drop_cents"] > 20
    assert no_read["rise_cents"] > 20
    assert no_read["drop_cents"] == pytest.approx(0.0, abs=1.0)


def test_unchanged_books_do_not_pad_the_tape():
    """Duplicate snapshots must not be counted as samples."""
    booktape.forget("KXBTC15M-D")
    now = time.time()
    for index in range(30):
        booktape.record("KXBTC15M-D", book(50, 52), now=now + index)
    assert len(booktape._tapes["KXBTC15M-D"]) == 1


# ---- the dip lane ----------------------------------------------------------


def market_dict(ticker, seconds_to_close=420, strike=100000.0):
    close = time.gmtime(time.time() + seconds_to_close)
    return {
        "ticker": ticker,
        "title": "BTC above X",
        "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", close),
        "status": "active",
        # At-the-money against the flat spot tape below, so fair value sits
        # near 50c and the contract's own dip is the only thing moving.
        "floor_strike": strike,
    }


def flat_spot(price=100000.0, seconds=300, hz=4):
    """A spot tape that is going nowhere, so the dip is contract-only.

    Deterministic sub-basis-point jitter, not a constant: realized volatility
    of exactly zero is rejected upstream (correctly -- a frozen tape means the
    feed is broken, not that the market is calm), so a literally flat series
    would never reach the lane under test.
    """
    now = time.time()
    # The jitter alternates once per WHOLE SECOND, not per sample. Volatility
    # upstream is measured on one-second buckets, so sub-second jitter can be
    # sampled away entirely -- and whether it is depends on where the current
    # clock happens to fall, which made an earlier version of this helper
    # intermittently produce a zero-volatility tape and a spurious failure.
    return [
        (now - seconds + index / hz, price + (1.0 if (index // hz) % 2 else -1.0))
        for index in range(seconds * hz)
    ]


def healthy_spot_status():
    return {
        "fresh_sources": 3,
        "divergence_bps": 1.0,
        "connected": True,
    }


def test_dip_lane_fires_on_a_contract_dip_with_a_steady_underlying():
    """The case the bot used to be blind to.

    The contract has sold off 15 cents while BTC has not moved, and the fall has
    stalled. Nothing in the momentum gates would ever produce this entry -- they
    are looking for a trend in the underlying, and there isn't one. This is the
    shape the manual trading was exploiting and the bot could not see, because
    it kept no record of the contract's own price.
    """
    ticker = "KXBTC15M-DIP"
    now = time.time()
    # 60 down to 45 over twelve seconds, then flat for the last four. The flat
    # tail is the deceleration the lane requires: it will not buy a knife that
    # is still falling.
    path = [60, 59, 58, 57, 56, 54, 52, 50, 49, 48, 47, 46, 45]
    for offset, bid in enumerate(path):
        booktape.record(ticker, book(bid, bid + 2), now=now - 16 + offset)

    read = booktape.analyze(ticker, "yes", now=now)
    assert read["drop_cents"] == pytest.approx(15.0)
    assert read["velocity_fast"] == pytest.approx(0.0), "the fall has stalled"
    assert read["decelerating"] is True

    signal = policy.evaluate(
        flat_spot(),
        market_dict(ticker),
        raw_book(45, 47),
        spot_status=healthy_spot_status(),
    )

    assert signal["action"] == "BUY YES"
    assert signal["lane"] == "dip_dislocation"
    assert signal["dip"] is True
    assert 0 < signal["conviction"] <= 100
    # A dip entry trades a wider stop than a momentum entry: the premise is
    # that the price is temporarily below fair, so a few more cents against us
    # is expected rather than disconfirming.
    assert signal["stop_cents"] > config.settings.stop_cents
    assert abs(signal["spot_move_bps"]) <= config.settings.dip_max_spot_move_bps


def test_dip_lane_refuses_to_catch_a_knife_still_falling():
    """Same drop, no stabilization. The lane must wait."""
    ticker = "KXBTC15M-KNIFE"
    now = time.time()
    # Still printing roughly -1c/s right up to the present moment.
    path = [60, 58, 56, 54, 52, 50, 48, 46, 44, 42, 40, 38, 36]
    for offset, bid in enumerate(path):
        booktape.record(ticker, book(bid, bid + 2), now=now - 12 + offset)

    read = booktape.analyze(ticker, "yes", now=now)
    assert read["decelerating"] is False

    signal = policy.evaluate(
        flat_spot(),
        market_dict(ticker),
        raw_book(36, 38),
        spot_status=healthy_spot_status(),
    )
    assert signal.get("lane") != "dip_dislocation"


def test_a_dip_matched_by_the_underlying_is_not_a_dislocation():
    """If BTC really fell, the contract is repriced, not dislocated.

    This is the distinction the whole lane rests on. Buying a contract that
    dropped because the underlying dropped is not buying a discount -- it is
    taking the wrong side of a real move.
    """
    ticker = "KXBTC15M-REAL"
    now = time.time()
    path = [60, 59, 58, 57, 56, 54, 52, 50, 49, 48, 47, 46, 45]
    for offset, bid in enumerate(path):
        booktape.record(ticker, book(bid, bid + 2), now=now - 16 + offset)

    # Spot slides ~60bps over the same window: a genuine move, far beyond
    # DIP_MAX_SPOT_MOVE_BPS.
    falling_spot = [
        (now - 300 + index / 4.0, 100600.0 - index * 0.5) for index in range(1200)
    ]

    signal = policy.evaluate(
        falling_spot,
        market_dict(ticker),
        raw_book(45, 47),
        spot_status=healthy_spot_status(),
    )
    assert signal.get("lane") != "dip_dislocation"


def test_dip_lane_stays_out_of_a_flat_book():
    """No drop, no dip entry. The lane must not manufacture a reason."""
    ticker = "KXBTC15M-FLAT"
    # Oscillates by a cent: noise, not a dislocation.
    path = [50, 51] * 12
    fill_tape(ticker, path, step=0.5)

    signal = policy.evaluate(
        flat_spot(),
        market_dict(ticker),
        raw_book(50, 52),
        spot_status=healthy_spot_status(),
    )
    assert not (signal.get("lane") or "").startswith("dip")


def test_divergence_between_spot_venues_vetoes_everything():
    """When the exchanges disagree, one of them is wrong and we cannot tell which."""
    ticker = "KXBTC15M-DIV"
    fill_tape(ticker, [62, 61, 60, 58, 56, 54, 52, 50, 49, 48, 47, 46, 47, 47], step=0.5)

    signal = policy.evaluate(
        flat_spot(),
        market_dict(ticker),
        raw_book(47, 49),
        spot_status={
            "fresh_sources": 3,
            "divergence_bps": config.settings.spot_divergence_bps + 20,
            "connected": True,
        },
    )
    assert signal["action"] == "NO TRADE"
    assert "disagree" in signal["reason"].lower()


def test_a_single_surviving_venue_vetoes_everything():
    ticker = "KXBTC15M-ONE"
    fill_tape(ticker, [62, 61, 60, 58, 56, 54, 52, 50, 49, 48, 47, 46, 47, 47], step=0.5)

    signal = policy.evaluate(
        flat_spot(),
        market_dict(ticker),
        raw_book(47, 49),
        spot_status={"fresh_sources": 1, "divergence_bps": None, "connected": True},
    )
    assert signal["action"] == "NO TRADE"


# ---- runner exits ----------------------------------------------------------


def test_trail_is_disarmed_until_the_position_has_actually_moved():
    cfg = config.settings
    assert scalp.runner_trail_cents(cfg, 0) is None
    assert scalp.runner_trail_cents(cfg, cfg.runner_arm_cents - 1) is None
    assert scalp.runner_trail_cents(cfg, cfg.runner_arm_cents) is not None


def test_trail_widens_with_the_peak_but_stays_inside_its_bounds():
    """This is the property that lets a winner run.

    A fixed 3-cent take-profit caps every trade at 3 cents. A trail that is a
    fraction of the peak gives the position room proportional to how far it has
    already come, so a 30-cent move is not cut at 3.
    """
    cfg = config.settings
    trails = [scalp.runner_trail_cents(cfg, peak) for peak in range(5, 80, 5)]
    trails = [value for value in trails if value is not None]

    assert trails == sorted(trails), "trail must never tighten as the peak grows"
    assert min(trails) >= cfg.runner_trail_min_cents
    assert max(trails) <= cfg.runner_trail_max_cents
    # And the floor must sit outside the spread, or the position gets stopped
    # out by a quote flicker rather than by an adverse move.
    assert cfg.runner_trail_min_cents > cfg.max_spread_cents


def test_trail_tightens_when_the_chart_turns_against_the_position():
    cfg = config.settings
    peak = 20
    assert scalp.runner_trail_cents(cfg, peak, opposed=True) < scalp.runner_trail_cents(
        cfg, peak
    )


# ---- conviction sizing -----------------------------------------------------


def sizing_for(conviction, price=40):
    signal = {
        "ticker": "KXBTC15M-SIZE",
        "price_cents": price,
        "stop_cents": 8,
        "conviction": conviction,
    }
    approved, reason, sizing = risk.check(
        signal, balance_cents=100000, open_position_count=0, open_tickers=[]
    )
    return approved, reason, sizing


def test_conviction_sizing_stays_within_the_configured_dollar_bounds(monkeypatch):
    """$5 at the bottom, $20 at the top, and never a cent outside either."""
    cfg = config.settings
    monkeypatch.setattr(risk, "load_state", lambda: risk._blank_state())

    low = sizing_for(0)[2]
    high = sizing_for(100)[2]
    mid = sizing_for(50)[2]

    assert low["budget_cents"] == cfg.min_cost_per_trade_cents
    assert high["budget_cents"] == cfg.max_cost_per_trade_cents
    assert low["budget_cents"] < mid["budget_cents"] < high["budget_cents"]

    for sizing in (low, mid, high):
        assert sizing["cost_cents"] <= cfg.max_cost_per_trade_cents
        assert sizing["count"] >= 1


def test_a_twenty_dollar_budget_actually_buys_twenty_dollars(monkeypatch):
    """Regression: a 6-contract cap silently turned $20 into $2.40."""
    monkeypatch.setattr(risk, "load_state", lambda: risk._blank_state())
    sizing = sizing_for(100, price=40)[2]
    # 2000c budget at 40c a contract is 50 contracts, not 6.
    assert sizing["cost_cents"] >= 1500


def test_conviction_can_be_absent_without_breaking_sizing(monkeypatch):
    monkeypatch.setattr(risk, "load_state", lambda: risk._blank_state())
    approved, _, sizing = risk.check(
        {"ticker": "T", "price_cents": 40, "stop_cents": 8},
        balance_cents=100000,
        open_position_count=0,
        open_tickers=[],
    )
    assert approved
    assert sizing["count"] >= 1


# ---- lane-specific pacing --------------------------------------------------


def test_dip_lane_gets_its_own_re_entry_allowance(monkeypatch):
    """Re-entering a dip several times in one market is the strategy, not a bug.

    The trend lane's 2-entry cap exists to stop momentum re-chasing. Applying
    it to dips would forbid exactly the behaviour that worked manually.
    """
    cfg = config.settings
    state = risk._blank_state()
    # Three entries already taken on this market: over the trend cap, under
    # the dip cap.
    state["ticker_attempts"] = {"KXBTC15M-RE": 3}
    monkeypatch.setattr(risk, "load_state", lambda: state)

    assert cfg.dip_max_entries_per_market > cfg.max_entries_per_market

    trend_ok, trend_reason, _ = risk.check(
        {"ticker": "KXBTC15M-RE", "price_cents": 40, "stop_cents": 8},
        balance_cents=100000,
        open_position_count=0,
        open_tickers=[],
    )
    dip_ok, _, _ = risk.check(
        {
            "ticker": "KXBTC15M-RE",
            "price_cents": 40,
            "stop_cents": 8,
            "dip": True,
            "lane": "dip_dislocation",
            "conviction": 70,
        },
        balance_cents=100000,
        open_position_count=0,
        open_tickers=[],
    )

    assert not trend_ok and "attempt cap" in trend_reason
    assert dip_ok


def test_adding_to_a_live_position_is_still_refused(monkeypatch):
    """Re-entry means after an exit. Averaging into an open lot is not allowed."""
    monkeypatch.setattr(risk, "load_state", lambda: risk._blank_state())
    approved, reason, _ = risk.check(
        {"ticker": "KXBTC15M-OPEN", "price_cents": 40, "stop_cents": 8, "dip": True},
        balance_cents=100000,
        open_position_count=1,
        open_tickers=["KXBTC15M-OPEN"],
    )
    assert not approved
    assert "already holding" in reason.lower()


# ---- configuration ---------------------------------------------------------


def test_the_shipped_configuration_validates():
    assert config.settings.problems() == []


def test_size_bounds_are_the_five_to_twenty_dollar_range_the_user_trades():
    cfg = config.settings
    assert cfg.min_cost_per_trade_cents == 500
    assert cfg.max_cost_per_trade_cents == 2000
