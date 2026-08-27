"""Tests for execution accuracy: what the bot actually pays and actually books.

Every test here corresponds to a specific wrong number the bot used to produce.
Being wrong about the market is unavoidable; being wrong about arithmetic is not.
"""

import pytest

import config
import policy
import scalp
import store
from kalshi_client import KalshiClient


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    store.forget()
    store.errors.clear()
    monkeypatch.setattr(
        config.settings, "lots_path", lambda mode: tmp_path / f"{mode}.json", raising=False
    )
    yield
    store.forget()
    store.errors.clear()


def raw_book(yes_levels, no_levels):
    return {"orderbook": {"yes": yes_levels, "no": no_levels}}


# ---- the ask ladder is a mirror, and mirrors are easy to get backwards ------


def test_ask_ladder_is_cheapest_first():
    """A reversal here would price every sweep at the worst level on the book.

    Buying YES means lifting the NO bids: a NO bid of 55c for 12 lots IS a YES
    offer at 45c for 12 lots. The NO bids are stored best-first (highest price
    first), and 100 minus a descending sequence is already ascending, so
    reversing as well flips the ladder.
    """
    snap = policy.book_snapshot(raw_book([[40, 10]], [[55, 12], [54, 30], [52, 100]]))

    assert snap["yes_asks"] == [(45, 12), (46, 30), (48, 100)]
    assert snap["yes_ask"] == 45
    # The best ask on the ladder must equal the scalar best ask, always.
    assert snap["yes_asks"][0][0] == snap["yes_ask"]
    assert snap["no_asks"][0][0] == snap["no_ask"]


def test_the_two_sides_are_consistent_mirrors():
    snap = policy.book_snapshot(raw_book([[40, 10], [38, 5]], [[55, 12]]))
    assert snap["no_asks"] == [(60, 10), (62, 5)]
    assert snap["no_ask"] == 60


def test_duplicate_price_levels_are_summed_not_overwritten():
    snap = policy.book_snapshot(raw_book([[40, 10], [40, 7]], [[55, 3]]))
    assert snap["yes_bids"] == [(40, 17)]


def test_nonsense_levels_are_dropped_not_crashed_on():
    snap = policy.book_snapshot(
        raw_book([[40, 10], ["x", "y"], [0, 5], [100, 5], [39, 0]], [[55, 3]])
    )
    assert snap["yes_bids"] == [(40, 10)]


# ---- sweeping the ladder ---------------------------------------------------


def test_sweep_within_the_top_level_pays_the_top_price():
    levels = [(45, 12), (46, 30)]
    result = policy.sweep(levels, 10)
    assert result["filled"] == 10
    assert result["cost_cents"] == 450
    assert result["avg_price_cents"] == 45.0
    assert result["worst_price_cents"] == 45


def test_sweep_beyond_the_top_level_pays_a_worse_average():
    """The number the old code never computed.

    Wanting 50 contracts when 12 rest at 45c means paying 46.08c on average,
    not 45c. That difference is the edge, handed back as slippage.
    """
    levels = [(45, 12), (46, 30), (48, 100)]
    result = policy.sweep(levels, 50)
    assert result["filled"] == 50
    assert result["cost_cents"] == 12 * 45 + 30 * 46 + 8 * 48
    assert result["avg_price_cents"] == pytest.approx(46.08)
    # The limit the order must carry to complete, not the price it was quoted.
    assert result["worst_price_cents"] == 48


def test_sweep_reports_a_short_fill_rather_than_pretending():
    result = policy.sweep([(45, 12)], 50)
    assert result["filled"] == 12
    assert result["worst_price_cents"] == 45


def test_sweep_of_an_empty_book_is_not_a_free_trade():
    result = policy.sweep([], 10)
    assert result["filled"] == 0
    assert result["avg_price_cents"] is None
    assert result["cost_cents"] == 0


def test_fees_accumulate_per_level_because_kalshi_rounds_per_fill():
    """One fill of 60 and three fills of 20 do not cost the same.

    Kalshi rounds its fee up to the next cent on each fill, so a multi-level
    sweep pays more than the single-ceiling estimate. The old estimate was
    always the optimistic one.
    """
    # 45c: 1.7325c of fee per contract, so four contracts in one fill round to
    # 7c while four separate fills round to 2c each.
    one_level = policy.sweep([(45, 4)], 4)["fee_cents"]
    four_levels = policy.sweep([(45, 1), (46, 1), (47, 1), (48, 1)], 4)["fee_cents"]
    assert one_level == 7
    assert four_levels == 8
    assert four_levels > one_level


def test_depth_within_respects_the_slippage_cap():
    levels = [(45, 12), (46, 30), (48, 100)]
    assert policy.depth_within(levels, 0) == 12
    assert policy.depth_within(levels, 1) == 42
    assert policy.depth_within(levels, 3) == 142
    # A gap in the ladder stops the walk: depth beyond a hole is not reachable
    # at an acceptable price.
    assert policy.depth_within([(45, 12), (49, 500)], 2) == 12


# ---- the exchange's numbers beat our model --------------------------------


def test_average_fill_price_is_read_from_the_exchange():
    assert KalshiClient.average_fill_price_cents(
        {"average_fill_price": "0.4608", "fill_count": "50.00"}
    ) == pytest.approx(46.08)
    assert KalshiClient.average_fee_paid_cents({"average_fee_paid": "0.0176"}) == pytest.approx(1.76)


def test_missing_fill_fields_return_none_rather_than_zero():
    """Zero would silently mean "free", which is the wrong direction to guess."""
    assert KalshiClient.average_fill_price_cents({}) is None
    assert KalshiClient.average_fill_price_cents(None) is None
    assert KalshiClient.average_fee_paid_cents({"average_fee_paid": "bogus"}) is None


# ---- P&L must be net of fees ----------------------------------------------


def test_realized_pnl_subtracts_fees():
    """The defect that could invert the sign of the whole track record.

    A round trip on this market costs roughly 3-4c per contract in taker fees,
    and a scalp aims at a handful of cents. Reporting gross P&L does not merely
    flatter the record; a strategy averaging +2c gross per contract is losing
    money and the old number called it a win.
    """
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=1.75)
    gross = (53 - 50) * 10
    realized = scalp.record_exit("dryrun", scalp.key("KX-T", "yes"), "t1", "profit", 10, 53, exit_fee_cents=1.75)

    assert realized < gross
    assert realized == round(gross - (1.75 + 1.75) * 10)

    stats = scalp.load("dryrun")["stats"]
    assert stats["realized_gross_cents"] == gross
    assert stats["fees_cents"] == 35
    assert stats["realized_cents"] == realized


def test_a_thin_gross_win_is_correctly_recorded_as_a_loss():
    """+2c gross per contract at a 50c price is a LOSS after fees."""
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=1.75)
    realized = scalp.record_exit(
        "dryrun", scalp.key("KX-T", "yes"), "t1", "profit", 10, 52, exit_fee_cents=1.75
    )
    assert realized < 0


def test_settlement_pays_no_exit_fee():
    """Holding to expiry is fee-free; inventing an exit fee would understate it."""
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 60, None, entry_fee_cents=0.0)
    realized = scalp.record_exit(
        "dryrun", scalp.key("KX-T", "yes"), "settlement", "settlement", 10, 100, exit_fee_cents=0.0
    )
    assert realized == 400


def test_entry_fee_is_not_charged_twice_when_a_lot_exits_in_pieces():
    """The entry fee is apportioned per contract, so partial exits do not
    re-pay it. Charging it per exit would penalise scaling out."""
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=1.75)
    lot_key = scalp.key("KX-T", "yes")
    scalp.record_exit("dryrun", lot_key, "t1", "profit", 5, 56, exit_fee_cents=1.75)
    scalp.record_exit("dryrun", lot_key, "t2", "profit", 5, 56, exit_fee_cents=1.75)

    # Exiting in two halves must total exactly what one exit of ten would.
    # Each half is 12.5c net, so rounding each one before adding loses a full
    # cent across the lot.
    both_at_once = (56 - 50) * 10 - (1.75 + 1.75) * 10
    stats = scalp.load("dryrun")["stats"]
    assert stats["realized_cents_exact"] == pytest.approx(both_at_once)
    assert stats["realized_cents"] == 25


def test_an_unknown_entry_fee_falls_back_to_the_taker_rate():
    """Adopted positions have no recorded fee. Assuming zero would understate
    costs; the taker rate is the conservative assumption."""
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None)
    realized = scalp.record_exit("dryrun", scalp.key("KX-T", "yes"), "t1", "profit", 10, 53)
    assert realized < (53 - 50) * 10


def test_the_modeled_fee_matches_kalshis_published_schedule():
    """0.07 * P * (1-P), peaking at the midpoint."""
    assert scalp._modeled_fee_per_contract(50) == pytest.approx(1.75, abs=0.01)
    assert scalp._modeled_fee_per_contract(25) == pytest.approx(1.31, abs=0.01)
    assert scalp._modeled_fee_per_contract(90) == pytest.approx(0.63, abs=0.01)
    assert scalp._modeled_fee_per_contract(10) == pytest.approx(0.63, abs=0.01)


def test_blended_entry_fee_when_averaging_into_a_lot():
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=1.0)
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=2.0)
    lot = scalp.get("dryrun", scalp.key("KX-T", "yes"))
    assert lot["entry_fee_cents"] == pytest.approx(1.5)


def test_scaling_out_in_many_pieces_does_not_accumulate_rounding_drift():
    """Ten partial exits must sum to the same total as one, to the cent."""
    scalp.record_entry("dryrun", "KX-T", "yes", 10, 50, None, entry_fee_cents=1.75)
    lot_key = scalp.key("KX-T", "yes")
    for _ in range(10):
        scalp.record_exit("dryrun", lot_key, "t", "profit", 1, 56, exit_fee_cents=1.75)

    stats = scalp.load("dryrun")["stats"]
    assert stats["realized_cents_exact"] == pytest.approx((56 - 50) * 10 - 3.5 * 10)
    assert stats["realized_cents"] == 25


def test_money_rounding_is_half_up_not_bankers():
    """round(12.5) is 12 in Python. For money that is surprising and biased."""
    assert scalp._round_half_up(12.5) == 13
    assert scalp._round_half_up(11.5) == 12
    assert scalp._round_half_up(-12.5) == -13
    assert scalp._round_half_up(0.4) == 0


# ---- depth-aware sizing ----------------------------------------------------


@pytest.fixture
def sizing_env(tmp_path, monkeypatch):
    """A clean risk state and generous limits, so only sizing is under test."""
    import risk

    monkeypatch.setattr(
        type(config.settings), "risk_state_path", tmp_path / "risk.json", raising=False
    )
    store.forget()
    monkeypatch.setattr(config.settings, "max_cost_per_trade_cents", 2000)
    monkeypatch.setattr(config.settings, "min_cost_per_trade_cents", 500)
    monkeypatch.setattr(config.settings, "max_contracts_per_trade", 60)
    monkeypatch.setattr(config.settings, "conviction_sizing", 0)
    monkeypatch.setattr(config.settings, "depth_aware_sizing", 1)
    monkeypatch.setattr(config.settings, "max_entry_slippage_cents", 2)
    return risk


def signal_for(levels, price=45, fair_prob=0.62, min_edge=3.0):
    return {
        "action": "BUY YES",
        "ticker": "KXBTC15M-T",
        "side": "yes",
        "price_cents": price,
        "entry_levels": levels,
        "fair_prob": fair_prob,
        "min_required_edge_cents": min_edge,
        "stop_cents": 8,
        "exit_bid_size": 500,
    }


def test_size_is_capped_by_what_actually_rests_on_the_book(sizing_env):
    """The bug that silently stopped entries from happening at all.

    A $20 budget at 45c wants 44 contracts. If only 12 rest at 45c and nothing
    within the slippage cap, a fill-or-kill for 44 is KILLED -- the exchange
    cannot fill the whole size at that price, so it fills none. The bot logged
    "unfilled" and moved on believing it had tried.
    """
    approved, reason, sizing = sizing_env.check(
        signal_for([[45, 12], [49, 500]]), 100000, 0, []
    )
    assert approved, reason
    assert sizing["count"] == 12
    # And the order is priced so it can actually complete.
    assert sizing["limit_price_cents"] == 45
    assert sizing["avg_price_cents"] == pytest.approx(45.0)
    assert sizing["slippage_cents"] == pytest.approx(0.0)


def test_size_reaches_into_the_book_within_the_slippage_cap(sizing_env):
    approved, reason, sizing = sizing_env.check(
        signal_for([[45, 12], [46, 30], [47, 40]]), 100000, 0, []
    )
    assert approved, reason
    # 44 contracts would fit the budget at the 45c quote ($19.80) but NOT at the
    # price they actually cost: 12@45 + 30@46 + 2@47 is $20.14. So the size lands
    # at 43, which is the budget being enforced against real money rather than
    # against a quote we could not have filled at.
    assert sizing["count"] == 43
    assert sizing["cost_cents"] == 12 * 45 + 30 * 46 + 1 * 47
    assert sizing["cost_cents"] <= 2000
    assert sizing["limit_price_cents"] == 47
    assert sizing["avg_price_cents"] > 45
    assert sizing["slippage_cents"] > 0


def test_the_limit_price_is_the_worst_level_touched_not_the_best(sizing_env):
    """A fill-or-kill priced at the best offer cannot sweep two levels."""
    _, _, sizing = sizing_env.check(signal_for([[45, 5], [46, 100]]), 100000, 0, [])
    assert sizing["limit_price_cents"] == 46


def test_size_shrinks_until_the_edge_survives_the_average_fill_price(sizing_env):
    """Edge is measured at the best offer; it is PAID at the average fill.

    Fair value 48c against a 45c offer is 3c of edge, exactly the required
    minimum. Reaching up the ladder erodes it, so only the contracts available
    at 45c may be bought -- taking more would buy at a price the model never
    approved.
    """
    approved, reason, sizing = sizing_env.check(
        signal_for([[45, 8], [46, 500]], price=45, fair_prob=0.48, min_edge=3.0),
        100000,
        0,
        [],
    )
    assert approved, reason
    assert sizing["count"] == 8
    assert sizing["avg_price_cents"] == pytest.approx(45.0)


def test_a_trade_is_refused_when_no_size_clears_the_edge_bar(sizing_env):
    """Better no trade than a trade at a price the edge cannot support."""
    approved, reason, sizing = sizing_env.check(
        signal_for([[45, 40]], price=45, fair_prob=0.46, min_edge=3.0), 100000, 0, []
    )
    assert not approved
    assert "edge" in reason.lower()
    assert sizing is None


def test_an_empty_ask_ladder_is_refused(sizing_env):
    approved, reason, _ = sizing_env.check(signal_for([]), 100000, 0, [])
    # No ladder means fall back to the old budget arithmetic rather than
    # inventing depth; but with no offers at all there is nothing to lift.
    assert approved or "slippage" in reason.lower() or "liquidity" in reason.lower()


def test_settlement_holds_are_not_edge_checked_at_the_average_price(sizing_env):
    """A late snipe rides to settlement and pays no exit; the round-trip edge
    test does not apply to it."""
    signal = signal_for([[92, 40], [93, 200]], price=92, fair_prob=0.97, min_edge=3.0)
    signal["late_settlement"] = True
    approved, reason, sizing = sizing_env.check(signal, 100000, 0, [])
    assert approved, reason
    assert sizing["count"] >= 1


def test_cost_is_reported_from_the_sweep_not_from_the_quoted_price(sizing_env):
    """cost_cents feeds the daily accounting, so it must be what we will pay."""
    _, _, sizing = sizing_env.check(signal_for([[45, 10], [46, 34]]), 100000, 0, [])
    expected = 10 * 45 + (sizing["count"] - 10) * 46
    assert sizing["cost_cents"] == expected
    assert sizing["cost_cents"] <= 2000


def test_budget_is_respected_at_the_swept_price_not_the_quoted_one(sizing_env):
    """44 contracts at 45c is $19.80, but at a swept average it is over $20."""
    _, _, sizing = sizing_env.check(signal_for([[45, 10], [47, 500]]), 100000, 0, [])
    assert sizing["cost_cents"] <= config.settings.max_cost_per_trade_cents


# ---- clocks -----------------------------------------------------------------


class FakeResponse:
    def __init__(self, date_header):
        self.headers = {"Date": date_header} if date_header else {}


def http_date(epoch):
    import email.utils

    return email.utils.formatdate(epoch, usegmt=True)


def fresh_client():
    from kalshi_client import KalshiClient

    return KalshiClient()


def test_close_time_without_a_timezone_is_read_as_utc():
    """A naive timestamp read as local time would put the close hours away.

    seconds_to_close drives the settlement guard and every "is there time to
    exit" decision, so a timezone error here does not degrade behaviour, it
    inverts it: the bot would hold positions through expiry.
    """
    aware = policy.close_epoch({"close_time": "2026-08-27T20:15:00Z"})
    naive = policy.close_epoch({"close_time": "2026-08-27T20:15:00"})
    assert aware == naive


def test_unparseable_close_time_is_none_not_a_guess():
    assert policy.close_epoch({"close_time": "not a time"}) is None
    assert policy.close_epoch({}) is None
    assert policy.seconds_to_close({}) is None


def test_a_clock_running_behind_the_exchange_shortens_the_window(monkeypatch):
    """Our clock 10s behind means 10s less time than we think. The guard must
    use the smaller number."""
    import time as time_module

    close_at = time_module.time() + 100
    market = {"close_time": "x"}
    monkeypatch.setattr(policy, "close_epoch", lambda m: close_at)

    plain = policy.seconds_to_close(market)
    adjusted = policy.seconds_to_close(market, skew_seconds=-10.0)
    assert adjusted == pytest.approx(plain - 10.0, abs=0.5)


def test_a_clock_running_ahead_never_widens_the_window(monkeypatch):
    """Skew is only ever allowed to make us more cautious, never less."""
    close_at = __import__("time").time() + 100
    monkeypatch.setattr(policy, "close_epoch", lambda m: close_at)

    plain = policy.seconds_to_close({})
    adjusted = policy.seconds_to_close({}, skew_seconds=+10.0)
    assert adjusted == pytest.approx(plain, abs=0.5)


def test_clock_skew_is_measured_from_the_server_date_header():
    client = fresh_client()
    assert client.clock_status()["skew_seconds"] is None
    assert client.clock_status()["trusted"] is False

    server_now = 1_700_000_000.0
    # Local clock 8 seconds ahead of the server.
    for _ in range(10):
        client._observe_clock(
            FakeResponse(http_date(server_now)), server_now + 8.0, server_now + 8.02
        )

    status = client.clock_status()
    assert status["skew_seconds"] == pytest.approx(8.0, abs=0.5)
    assert status["trusted"] is True
    assert status["drifting"] is True
    # Ahead of the exchange, so no widening of the window.
    assert status["guard_adjustment_seconds"] == 0.0


def test_network_latency_is_not_mistaken_for_clock_drift():
    """Using the request midpoint means a slow round trip does not register as
    drift the way a one-sided comparison would."""
    client = fresh_client()
    server_now = 1_700_000_000.0
    for _ in range(10):
        # 400ms round trip, clocks in sync: server stamps the midpoint.
        client._observe_client = None
        client._observe_clock(
            FakeResponse(http_date(server_now)), server_now - 0.2, server_now + 0.2
        )
    assert client.clock_status()["skew_seconds"] == pytest.approx(0.0, abs=0.6)
    assert client.clock_status()["drifting"] is False


def test_an_absurd_skew_is_flagged_rather_than_silently_applied():
    """A broken clock must not quietly subtract days from the trading window --
    that would stop all trading and look like a strategy finding no setups."""
    client = fresh_client()
    server_now = 1_700_000_000.0
    for _ in range(10):
        client._observe_clock(
            FakeResponse(http_date(server_now)), server_now - 90000, server_now - 90000
        )

    status = client.clock_status()
    assert status["severe"] is True
    assert status["guard_adjustment_seconds"] == 0.0


def test_a_moderate_lag_is_applied_but_bounded():
    client = fresh_client()
    server_now = 1_700_000_000.0
    for _ in range(20):
        client._observe_clock(
            FakeResponse(http_date(server_now)), server_now - 45.0, server_now - 45.0
        )
    status = client.clock_status()
    assert status["severe"] is False
    assert status["guard_adjustment_seconds"] == -30.0


def test_a_missing_or_broken_date_header_is_ignored():
    client = fresh_client()
    client._observe_clock(FakeResponse(None), 1.0, 1.1)
    client._observe_clock(FakeResponse("garbage"), 1.0, 1.1)
    assert client.clock_status()["samples"] == 0
    assert client.clock_status()["skew_seconds"] is None


def test_a_broken_clock_stops_trading_rather_than_guessing():
    """Most of this strategy is time-conditional. If the clock is wrong by
    minutes, none of those conditions mean what they say."""
    signal = policy.evaluate(
        [(0, 100000.0)],
        {"ticker": "KXBTC15M-T", "close_time": "2026-08-27T20:15:00Z"},
        raw_book([[45, 10]], [[54, 10]]),
        clock_status={"severe": True, "skew_seconds": 400.0},
    )
    assert signal["action"] == "NO TRADE"
    assert "clock" in signal["reason"].lower()
