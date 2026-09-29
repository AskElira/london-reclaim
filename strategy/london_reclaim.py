"""London range raid at the NY open — reclaim or expansion, three entry triggers.

Both models share the same skeleton: build the London high/low (LH/LL) between
02:00 and 08:30 ET, freeze it, then work the 09:30-11:00 ET window.

  model="reclaim"  (failed breakout / mean reversion)
      A sweep alone is NOT a setup. Price must prove the break failed by
      CLOSING back inside the range -- close > LL after an LL sweep for a long.
      Only then is a trigger armed. The stop sits at the sweep extreme.

  model="breakout" (pro-trend expansion)
      The break is treated as a real expansion leg. A displacement above LH
      arms a long on the pullback. The stop sits at the displacement base.

  trigger="fvg"    limit at the 3-bar imbalance left by the reclaim/displacement
                   leg. `fvg_entry_level`: "top" (edge nearest price), "ce"
                   (midpoint), "bottom" (far edge).
  trigger="ob"     limit at the last opposite-close candle before the push.
                   `ob_entry_level`: "open" | "high" | "ce" | "low" (mirrored
                   for shorts). Stop from the OB itself or the sweep wick via
                   `ob_sl_source`.
  trigger="swing"  stop-market one tick beyond the local swing printed during
                   the sweep (reclaim) or the breakout extreme after a pullback
                   forms (breakout). Momentum-confirmed, no resting-limit miss.

Swings are fractals: a bar whose extreme is the most extreme of `swing_bars`
bars either side, and therefore only confirmed `swing_bars` bars later. Nothing
is read before it is confirmed.

Order-placement honesty guards, which the spec's fill-rate table assumes:

  * A buy limit is only submitted BELOW the current close (sell limit above),
    otherwise it is not a retest -- it is a market order wearing a limit's name.
  * A buy stop is only submitted ABOVE the current close (sell stop below), so
    a "breakout" trigger cannot fill on a level price already passed.
  Setups failing these guards are discarded, which is exactly the "missed fill"
  the swing trigger is supposed to avoid, and it is measured rather than assumed.

Exit management is common to both models:
  * Stop covers 100% of the position and shares an OCO group with TP1, so a
    same-bar touch of both resolves against the trader (stop wins).
  * TP1 takes `tp1_percent` at `tp1_rr` R, optionally clamped to the opposing
    London level when that level is nearer (`tp1_clamp_to_opposing`).
  * On TP1 fill the engine cancels the OCO stop; the breakeven runner stop is
    submitted on the same bar and is live from the next one.
  * The runner exits on a structural break (close beyond the last confirmed
    swing against it) when `mss_exit_enabled`, else it holds to the EOD flat.
  * Everything is flat at `exit_eod_time`.

One trade per direction per session, `max_trades_per_day` total. An opposing
raid cancels a resting order. Unfilled orders die at the end of the NY window.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..data.contracts import get_contract, round_to_tick
from ..engine.clock import trading_date
from ..engine.event import Bar, Order, OrderSide, OrderStatus, OrderType, TimeInForce
from .base import BaseStrategy

ET = ZoneInfo("America/New_York")


def _parse_session(spec: str) -> tuple[time, time]:
    a, b = spec.split("-")
    return time(int(a[:2]), int(a[2:])), time(int(b[:2]), int(b[2:]))


def _parse_hhmm(spec: str) -> time:
    return time(int(spec[:2]), int(spec[2:]))


def _in_session(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end


@dataclass
class _Ex:
    key: datetime
    open: float
    high: float
    low: float
    close: float
    symbol: str
    volume: float = 0.0
    finalized: bool = False


@dataclass
class LondonReclaimConfig:
    # --- product -------------------------------------------------------
    contract: str = "MNQ"  # selects tick size + point multiplier
    threshold_units: str = "points"  # points | ticks | atr  (see module docstring)
    # --- model ---------------------------------------------------------
    model: str = "reclaim"  # "reclaim" | "breakout"
    trigger: str = "swing"  # "swing" | "fvg" | "ob"
    # --- timing --------------------------------------------------------
    bar_minutes: int = 1
    london_session: str = "0200-0830"
    premarket_session: str = "0800-0930"
    am_session: str = "0930-1200"  # builds AM High / AM Low for the PM window
    reference_levels: tuple = ("london_extremes",)
    # + "premarket_extremes", "pdh_pdl", "am_extremes"
    ny_exec_window: str = "0930-1100"
    pm_exec_window: str = ""  # e.g. "1330-1530"; empty disables the PM session
    pm_require_intact: bool = False  # PM only trades levels the morning never broke
    exit_eod_time: str = "1555"
    exit_eod_window_mins: int = 10
    # --- structure -----------------------------------------------------
    swing_bars: int = 3
    fvg_min_pts: float = 6.0
    fvg_entry_level: str = "top"  # top | ce | bottom
    ob_entry_level: str = "open"  # open | high | ce | low
    ob_sl_source: str = "ob"  # ob | sweep
    displacement_min_pts: float = 6.0  # breakout model: size of the expansion FVG
    breakout_entry_level: str = "fvg"  # fvg | level  (trigger="fvg" only)
    # --- order lifetime -------------------------------------------------
    max_wait_bars: int = 12
    # --- risk ----------------------------------------------------------
    max_sl_pts: float = 45.0
    risk_per_trade_usd: float = 300.0
    # True compounding: risk this PERCENT of present account equity per trade.
    # 0 = off (flat dollar risk). Takes precedence over the equity-cushion
    # ladder below, which exists for prop-firm trailing-drawdown survival, not
    # for growth. Falls back to risk_per_trade_usd until the loop publishes
    # equity on the first bar.
    risk_pct_equity: float = 0.0
    # Equity-cushion sizing: risk grows as the run banks a buffer against the
    # trailing drawdown. PnL is measured from the equity at the run's first bar,
    # so a phase-2 reset puts sizing back in survival mode automatically.
    dyn_risk_enabled: bool = False
    dyn_tier1_max_pnl: float = 1000.0
    dyn_tier1_risk: float = 100.0
    dyn_tier2_max_pnl: float = 2000.0
    dyn_tier2_risk: float = 175.0
    dyn_tier3_risk: float = 250.0
    # Phase 2 starts with a fresh trailing-drawdown buffer, so it need not crawl
    # back through tier 1. Set p2_* > 0 to decouple; 0 reuses the phase-1 ladder.
    p2_tier1_risk: float = 0.0
    p2_tier1_max_pnl: float = 1000.0
    p2_tier2_risk: float = 0.0
    # ADR compression: only trade days whose London range is a small fraction of
    # recent daily range. Compression precedes expansion.
    adr_filter_enabled: bool = False
    adr_days: int = 10
    adr_max_pct: float = 0.40
    max_contracts: int = 10
    sl_offset_ticks: float = 1.0
    # --- targets / exits ------------------------------------------------
    exit_model: str = "scale"  # scale | fixed | swing_trail | chandelier
    tp1_rr: float = 2.0
    tp1_percent: float = 60.0
    tp1_clamp_to_opposing: bool = True
    # Where TP1 sits: "rr" = tp1_rr multiples of risk; "range_mid" = the midpoint
    # of the swept reference range (auction value); "vwap" = session VWAP. The
    # latter two fall back to rr when the level is not on the profitable side.
    tp1_target: str = "rr"
    move_to_be_on_tp1: bool = True
    be_offset_ticks: float = 1.0
    trail_swing_bars: int = 3  # swing_trail: fractal half-width
    atr_len: int = 14  # chandelier: ATR period (Wilder)
    atr_mult: float = 3.0  # chandelier: multiple of ATR
    # --- runner --------------------------------------------------------
    mss_exit_enabled: bool = True
    # --- regime filters -------------------------------------------------
    blackout_weekdays: tuple = ()  # 0=Mon .. 4=Fri; days the model sits out
    blackout_dates: tuple = ()  # explicit "YYYY-MM-DD" list (news calendar)
    skip_first_friday: bool = False  # NFP proxy: first Friday of each month
    # --- limits --------------------------------------------------------
    max_trades_per_day: int = 2


class LondonReclaimStrategy(BaseStrategy):
    """London-range raid traded either as a failed break or as an expansion."""

    def __init__(self, config: LondonReclaimConfig | None = None):
        self.cfg = config or LondonReclaimConfig()
        if self.cfg.model not in ("reclaim", "breakout"):
            raise ValueError(f"model must be reclaim|breakout, got {self.cfg.model}")
        if self.cfg.trigger not in ("swing", "fvg", "ob"):
            raise ValueError(f"trigger must be swing|fvg|ob, got {self.cfg.trigger}")
        self.spec = get_contract(self.cfg.contract)
        if self.cfg.tp1_target not in ("rr", "range_mid", "vwap"):
            raise ValueError(f"tp1_target must be rr|range_mid|vwap, got {self.cfg.tp1_target}")
        if self.cfg.threshold_units not in ("points", "ticks", "atr"):
            raise ValueError(
                f"threshold_units must be points|ticks|atr, got {self.cfg.threshold_units}"
            )
        if self.cfg.exit_model not in ("scale", "fixed", "swing_trail", "chandelier"):
            raise ValueError(
                f"exit_model must be scale|fixed|swing_trail|chandelier, got {self.cfg.exit_model}"
            )
        self._london = _parse_session(self.cfg.london_session)
        self._premarket = _parse_session(self.cfg.premarket_session)
        self._am = _parse_session(self.cfg.am_session)
        self._pm = _parse_session(self.cfg.pm_exec_window) if self.cfg.pm_exec_window else None
        srcs = tuple(self.cfg.reference_levels)
        known = ("london_extremes", "premarket_extremes", "pdh_pdl", "am_extremes")
        for x in srcs:
            if x not in known:
                raise ValueError(f"reference_levels entries must be in {known}, got {x}")
        if not srcs:
            raise ValueError("reference_levels must name at least one source")
        self._srcs = srcs
        self._ny = _parse_session(self.cfg.ny_exec_window)
        eod = _parse_hhmm(self.cfg.exit_eod_time)
        eod_end = (
            datetime(2000, 1, 1, eod.hour, eod.minute)
            + timedelta(minutes=self.cfg.exit_eod_window_mins)
        ).time()
        self._eod = (eod, eod_end)
        self.reset()

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._agg: _Ex | None = None
        self._symbol: str | None = None
        self._bars: deque = deque(maxlen=256)
        self._oco_seq = 0
        self._today: date | None = None
        self._prev_in_london = False
        self._prev_in_pm = False
        self._prev_in_am = False
        self._prev_in_ny = False
        self._prev_in_pmwin = False
        self._atr: float | None = None
        self._atr_seed: list[float] = []
        self._prev_close: float | None = None
        self._pdh: float | None = None
        self._pdl: float | None = None
        self._phase: int = 1
        self._blackout_today = False
        self._vwap: float | None = None
        self._vwap_pv = 0.0
        self._vwap_v = 0.0
        self._day_ranges: deque = deque(maxlen=64)
        self._compressed = True
        self._equity0: float | None = None
        self._equity: float | None = None
        self._td: object | None = None
        self._td_hi: float | None = None
        self._td_lo: float | None = None
        self._trade_src: str | None = None
        self.stats: dict[str, int] = {}
        self._reset_day()
        self._flatten_state()

    def _bump(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    @staticmethod
    def _blank_src() -> dict:
        return {
            "hi": None,
            "lo": None,
            "locked": False,
            "swept_low": False,
            "swept_high": False,
            "sweep_low_px": None,
            "sweep_high_px": None,
            "arm_long": None,
            "arm_short": None,
            "reclaimed_long": False,
            "reclaimed_short": False,
            "traded_long": False,
            "traded_short": False,
            "broken_am": False,
        }

    def _reset_day(self) -> None:
        self._compressed = True
        self._src: dict[str, dict] = {n: self._blank_src() for n in self._srcs}
        if "pdh_pdl" in self._src and self._pdh is not None and self._pdl is not None:
            # Prior-day extremes are already known when the day starts.
            self._src["pdh_pdl"].update(hi=self._pdh, lo=self._pdl, locked=True)
        self._trades_today = 0

    def _reset_window(self) -> None:
        """Clear only the raid state; levels and per-day trade flags survive."""
        for st in self._src.values():
            st.update(
                swept_low=False,
                swept_high=False,
                sweep_low_px=None,
                sweep_high_px=None,
                arm_long=None,
                arm_short=None,
                reclaimed_long=False,
                reclaimed_short=False,
            )

    def _flatten_state(self) -> None:
        self._side: str | None = None
        self._pos_qty = 0
        self._entry_px: float | None = None
        self._sl_px: float | None = None
        self._tp1_px: float | None = None
        self._tp1_qty = 0
        self._tp1_hit = False
        self._bracket_placed = False
        self._need_be = False
        self._closing = False
        self._rest_bars = 0
        self._trail_px: float | None = None
        self._mfe_high: float | None = None
        self._mfe_low: float | None = None
        self._orders: dict[str, Order] = {}
        self._seen: dict[str, OrderStatus] = {}
        self._self_canceled: set[str] = set()

    # ------------------------------------------------------------------
    def _mk(
        self,
        ts: datetime,
        symbol: str,
        key: str,
        side: OrderSide,
        qty: int,
        otype: OrderType,
        price: float | None = None,
        oco: int = 0,
    ) -> Order:
        o = Order(
            id=0,
            ts_submitted=ts,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=otype,
            price=None if price is None else round_to_tick(price, self.spec.tick_size),
            tif=TimeInForce.GTC,
            tag=f"lrc_{key}",
            oco_group=oco,
        )
        self._orders[key] = o
        self._seen[key] = o.status
        return o

    def _cancel(self, key: str) -> None:
        o = self._orders.get(key)
        if o is not None and o.is_active():
            o.status = OrderStatus.CANCELED
            self._self_canceled.add(key)

    def _reconcile(self) -> bool:
        external = False
        for key, o in list(self._orders.items()):
            prev = self._seen.get(key, OrderStatus.PENDING)
            if o.status == prev:
                continue
            self._seen[key] = o.status
            if o.status == OrderStatus.FILLED:
                self._pos_qty += o.qty if o.side == OrderSide.BUY else -o.qty
                if key == "entry":
                    self._trades_today += 1
                    self._bump("entries_filled")
                    st = self._src.get(self._trade_src or "")
                    if st is not None:
                        st["traded_long" if self._side == "long" else "traded_short"] = True
                elif key == "tp1":
                    self._tp1_hit = True
                    self._need_be = True
                    self._bump("tp1_filled")
                elif key == "sl":
                    self._bump("stopped_out")
            elif o.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                if key in self._self_canceled:
                    if key == "entry":
                        self._bump("entries_expired")
                    continue
                if o.status == OrderStatus.CANCELED and self._oco_sibling_filled(o):
                    continue
                if key == "entry":
                    self._bump("entries_expired")
                    continue
                external = True
        return external

    def _oco_sibling_filled(self, o: Order) -> bool:
        if o.oco_group == 0:
            return False
        return any(
            s is not o and s.oco_group == o.oco_group and s.status == OrderStatus.FILLED
            for s in self._orders.values()
        )

    def _entry_resting(self) -> bool:
        o = self._orders.get("entry")
        return o is not None and o.is_active()

    # ------------------------------------------------------------------
    def on_bar(self, state, bar: Bar) -> list[Order]:
        if self._symbol is not None and bar.symbol != self._symbol:
            self._bars.clear()
            self._agg = None
            self._reset_day()
            self._flatten_state()
        self._symbol = bar.symbol

        ph = getattr(state, "phase", 1)
        if ph != self._phase:
            # Phase reset: the account starts over with a fresh buffer, so the
            # sizing baseline must start over with it.
            self._phase = ph
            self._equity0 = getattr(state, "equity", None)
        eq = getattr(state, "equity", None)
        if eq is not None:
            if self._equity0 is None:
                self._equity0 = eq
            self._equity = eq

        if self._reconcile():
            self._flatten_state()
        if self._pos_qty == 0:
            self._closing = False
            self._need_be = False
            if self._bracket_placed:
                for k in ("sl", "tp1", "be"):
                    self._cancel(k)
                self._bracket_placed = False

        orders: list[Order] = []
        m = max(1, self.cfg.bar_minutes)
        key = bar.ts.replace(minute=(bar.ts.minute // m) * m, second=0, microsecond=0)

        if self._agg is not None and self._agg.key != key:
            if not self._agg.finalized:
                orders.extend(self._on_exec_bar(self._agg))
            self._agg = None
        if self._agg is None:
            self._agg = _Ex(key, bar.open, bar.high, bar.low, bar.close, bar.symbol, bar.volume)
        else:
            a = self._agg
            a.high = max(a.high, bar.high)
            a.low = min(a.low, bar.low)
            a.close = bar.close

        if bar.ts.astimezone(ET).minute % m == m - 1 and not self._agg.finalized:
            orders.extend(self._on_exec_bar(self._agg))
        return orders

    # ------------------------------------------------------------------
    def _on_exec_bar(self, a: _Ex) -> list[Order]:
        a.finalized = True
        cfg = self.cfg
        orders: list[Order] = []

        et = a.key.astimezone(ET)
        t, d = et.time(), et.date()
        in_london = _in_session(t, *self._london)
        in_ny = _in_session(t, *self._ny)
        in_eod = _in_session(t, *self._eod)

        if d != self._today:
            self._today = d
            self._reset_day()
            self._blackout_today = self._is_blacked_out(d)

        self._update_atr(a)

        in_pm = _in_session(t, *self._premarket)
        in_am = _in_session(t, *self._am)
        self._build_levels(a, in_london, in_pm, in_am)

        in_pmwin = _in_session(t, *self._pm) if self._pm is not None else False
        in_exec = in_ny or in_pmwin

        if in_ny and not self._prev_in_ny:
            self._reset_window()
        if self._prev_in_ny and not in_ny:
            # Remember what the morning already took out before clearing state.
            for st in self._src.values():
                st["broken_am"] = st["swept_low"] or st["swept_high"]
            self._cancel("entry")
        if in_pmwin and not self._prev_in_pmwin:
            self._reset_window()
        if self._prev_in_pmwin and not in_pmwin:
            self._cancel("entry")

        if in_exec:
            self._track_levels(a)

        self._bars.append(a)

        entry = self._orders.get("entry")
        if (
            entry is not None
            and entry.status == OrderStatus.FILLED
            and not self._bracket_placed
            and not self._closing
            and self._pos_qty != 0
        ):
            orders.extend(self._place_bracket(a))

        if self._entry_resting():
            self._rest_bars += 1
            if self._rest_bars > cfg.max_wait_bars:
                self._cancel("entry")

        if (
            in_exec
            and self._pos_qty == 0
            and not self._entry_resting()
            and self._trades_today < cfg.max_trades_per_day
        ):
            orders.extend(self._scan(a, in_pmwin))

        if self._need_be and self._pos_qty != 0 and cfg.move_to_be_on_tp1:
            self._need_be = False
            orders.extend(self._breakeven(a))
        elif self._need_be:
            self._need_be = False

        if self._pos_qty != 0 and cfg.exit_model in ("swing_trail", "chandelier"):
            orders.extend(self._update_trail(a))

        if self._pos_qty != 0 and self._tp1_hit and cfg.mss_exit_enabled and self._mss_broken():
            orders.extend(self._close_all(a, "mss"))

        if in_eod and self._pos_qty != 0:
            orders.extend(self._close_all(a, "eod"))

        self._prev_in_london = in_london
        self._prev_in_pm = in_pm
        self._prev_in_am = in_am
        self._prev_in_ny = in_ny
        self._prev_in_pmwin = in_pmwin
        return orders

    # ------------------------------------------------------------------
    def _build_levels(self, a: _Ex, in_london: bool, in_pm: bool, in_am: bool) -> None:
        """Accumulate every enabled reference range and lock it when its window ends."""
        # Prior-day extremes, on the engine's 18:00 ET trading-day boundary.
        td = trading_date(a.key)
        if self._td is None:
            self._td, self._td_hi, self._td_lo = td, a.high, a.low
        elif td != self._td:
            self._vwap_pv = 0.0
            self._vwap_v = 0.0
            self._pdh, self._pdl = self._td_hi, self._td_lo
            if self._td_hi is not None and self._td_lo is not None:
                self._day_ranges.append(self._td_hi - self._td_lo)
            self._td, self._td_hi, self._td_lo = td, a.high, a.low
            st = self._src.get("pdh_pdl")
            if st is not None and self._pdh is not None:
                st.update(hi=self._pdh, lo=self._pdl, locked=True)
        else:
            self._td_hi = max(self._td_hi, a.high)
            self._td_lo = min(self._td_lo, a.low)

        typical = (a.high + a.low + a.close) / 3.0
        self._vwap_pv += typical * a.volume
        self._vwap_v += a.volume
        self._vwap = (self._vwap_pv / self._vwap_v) if self._vwap_v > 0 else None

        for name, in_win, was_in in (
            ("london_extremes", in_london, self._prev_in_london),
            ("premarket_extremes", in_pm, self._prev_in_pm),
            ("am_extremes", in_am, self._prev_in_am),
        ):
            st = self._src.get(name)
            if st is None:
                continue
            if in_win:
                if not was_in:
                    st["hi"], st["lo"] = a.high, a.low
                else:
                    st["hi"] = max(st["hi"] if st["hi"] is not None else a.high, a.high)
                    st["lo"] = min(st["lo"] if st["lo"] is not None else a.low, a.low)
            elif was_in:
                st["locked"] = st["hi"] is not None and st["lo"] is not None
                if name == "london_extremes" and st["locked"]:
                    self._compressed = self._check_compression(st["hi"] - st["lo"])

    def _is_blacked_out(self, d: date) -> bool:
        """True when the model must sit out this session entirely."""
        cfg = self.cfg
        if d.weekday() in tuple(cfg.blackout_weekdays):
            return True
        if d.isoformat() in tuple(cfg.blackout_dates):
            return True
        if cfg.skip_first_friday and d.weekday() == 4 and d.day <= 7:
            return True
        return False

    def _check_compression(self, london_range: float) -> bool:
        """London range small relative to the recent average daily range."""
        if not self.cfg.adr_filter_enabled:
            return True
        n = self.cfg.adr_days
        if len(self._day_ranges) < n:
            return False  # no ADR yet: refuse rather than guess
        adr = sum(list(self._day_ranges)[-n:]) / float(n)
        if adr <= 0:
            return False
        ok = london_range <= self.cfg.adr_max_pct * adr
        self._bump("adr_pass" if ok else "adr_reject")
        return ok

    def _thr(self, value: float) -> float | None:
        """Convert a configured threshold into a price distance for this product.

        points -> as written (MNQ-native, not portable)
        ticks  -> value * tick_size
        atr    -> value * ATR(atr_len) on execution bars; None until ATR exists,
                  which makes thresholds comparable across products of wildly
                  different tick value and volatility.
        """
        u = self.cfg.threshold_units
        if u == "points":
            return value
        if u == "ticks":
            return value * self.spec.tick_size
        if self._atr is None or self._atr <= 0:
            return None
        return value * self._atr

    def _current_risk(self) -> float:
        """Risk per trade for the account's present cushion."""
        cfg = self.cfg
        if cfg.risk_pct_equity > 0:
            eq = self._equity if self._equity is not None else self._equity0
            if eq is not None and eq > 0:
                return eq * cfg.risk_pct_equity / 100.0
            return cfg.risk_per_trade_usd
        if not cfg.dyn_risk_enabled:
            return cfg.risk_per_trade_usd
        if self._equity is None or self._equity0 is None:
            return cfg.dyn_tier1_risk
        pnl = self._equity - self._equity0
        if self._phase == 2 and cfg.p2_tier1_risk > 0:
            if pnl < cfg.p2_tier1_max_pnl:
                return cfg.p2_tier1_risk
            return cfg.p2_tier2_risk if cfg.p2_tier2_risk > 0 else cfg.p2_tier1_risk
        if pnl < cfg.dyn_tier1_max_pnl:
            return cfg.dyn_tier1_risk
        if pnl < cfg.dyn_tier2_max_pnl:
            return cfg.dyn_tier2_risk
        return cfg.dyn_tier3_risk

    # ------------------------------------------------------------------
    def _track_levels(self, a: _Ex) -> None:
        """Sweeps, breaks and (reclaim model) the close back inside each range."""
        for name, st in self._src.items():
            if not st["locked"] or st["hi"] is None or st["lo"] is None:
                continue
            lo, hi = st["lo"], st["hi"]

            if a.low < lo:
                if not st["swept_low"]:
                    st["swept_low"] = True
                    self._bump(f"sweep_low:{name}")
                    if self._entry_resting() and self._side == "short":
                        self._cancel("entry")
                if st["sweep_low_px"] is None or a.low < st["sweep_low_px"]:
                    st["sweep_low_px"] = a.low
            if a.high > hi:
                if not st["swept_high"]:
                    st["swept_high"] = True
                    self._bump(f"sweep_high:{name}")
                    if self._entry_resting() and self._side == "long":
                        self._cancel("entry")
                if st["sweep_high_px"] is None or a.high > st["sweep_high_px"]:
                    st["sweep_high_px"] = a.high

            if self.cfg.model == "reclaim":
                # The break must FAIL: a close back inside the range arms the setup.
                if st["swept_low"] and not st["reclaimed_long"] and a.close > lo:
                    st["reclaimed_long"] = True
                    st["arm_long"] = a.key
                    self._bump("armed_long")
                if st["swept_high"] and not st["reclaimed_short"] and a.close < hi:
                    st["reclaimed_short"] = True
                    st["arm_short"] = a.key
                    self._bump("armed_short")
            else:
                # Expansion: the break itself arms the setup, traded with the move.
                if st["swept_high"] and st["arm_long"] is None:
                    st["arm_long"] = a.key
                    self._bump("armed_long")
                if st["swept_low"] and st["arm_short"] is None:
                    st["arm_short"] = a.key
                    self._bump("armed_short")

    # ------------------------------------------------------------------
    def _swing(self, kind: str, after: datetime | None) -> _Ex | None:
        """Most recent CONFIRMED fractal swing at or after `after`."""
        k = self.cfg.swing_bars
        bars = list(self._bars)
        if len(bars) < 2 * k + 1:
            return None
        for i in range(len(bars) - k - 1, k - 1, -1):
            piv = bars[i]
            if after is not None and piv.key < after:
                return None
            w = bars[i - k : i + k + 1]
            if kind == "high" and all(piv.high >= b.high for b in w):
                return piv
            if kind == "low" and all(piv.low <= b.low for b in w):
                return piv
        return None

    def _fvg(self, side: str) -> tuple[float, float] | None:
        """(zone_lo, zone_hi) of the 3-bar imbalance ending on the latest bar."""
        bars = list(self._bars)
        if len(bars) < 3:
            return None
        b2, _b1, b0 = bars[-3], bars[-2], bars[-1]
        if side == "long":
            floor_ = self._thr(self.cfg.fvg_min_pts)
            if floor_ is None:
                return None
            gap = b0.low - b2.high
            return (b2.high, b0.low) if gap >= floor_ else None
        floor_ = self._thr(self.cfg.fvg_min_pts)
        if floor_ is None:
            return None
        gap = b2.low - b0.high
        return (b0.high, b2.low) if gap >= floor_ else None

    def _order_block(self, side: str, after: datetime | None) -> _Ex | None:
        """Last opposite-close candle before the push, searched back from now."""
        bars = list(self._bars)
        limit = 40
        for b in reversed(bars[-limit:]):
            if after is not None and b.key < after and len(bars) > 3:
                # allow the OB to predate the arming bar -- that is its definition
                pass
            if side == "long" and b.close < b.open:
                return b
            if side == "short" and b.close > b.open:
                return b
        return None

    # ------------------------------------------------------------------
    def _scan(self, a: _Ex, in_pmwin: bool = False) -> list[Order]:
        """First valid setup across every enabled reference level wins the slot."""
        for name in self._srcs:
            st = self._src.get(name)
            if st is None:
                continue
            if self._blackout_today:
                continue  # news / weekday blackout
            if not self._compressed:
                continue  # ADR filter: this day's range is not compressed
            if in_pmwin and self.cfg.pm_require_intact and st["broken_am"]:
                continue  # the morning already took this level out
            for side in ("long", "short"):
                if st[f"traded_{side}"]:
                    continue
                armed = st["arm_long"] if side == "long" else st["arm_short"]
                if armed is None:
                    continue
                built = self._build(side, a, armed, name, st)
                if built is not None:
                    return built
        return []

    def _build(
        self, side: str, a: _Ex, armed: datetime | None, src: str, st: dict
    ) -> list[Order] | None:
        cfg = self.cfg
        off = cfg.sl_offset_ticks * self.spec.tick_size
        otype = OrderType.LIMIT
        entry: float | None = None
        sl: float | None = None

        if cfg.trigger == "swing":
            otype = OrderType.STOP
            if cfg.model == "reclaim":
                piv = self._swing("high" if side == "long" else "low", armed)
                if piv is None:
                    return None
                entry = (piv.high + self.spec.tick_size) if side == "long" else (piv.low - self.spec.tick_size)
                base = st["sweep_low_px"] if side == "long" else st["sweep_high_px"]
                if base is None:
                    return None
                sl = base - off if side == "long" else base + off
            else:
                # Breakout: stop beyond the break extreme once a pullback prints.
                pull = self._swing("low" if side == "long" else "high", armed)
                if pull is None:
                    return None
                ext = st["sweep_high_px"] if side == "long" else st["sweep_low_px"]
                if ext is None:
                    return None
                entry = (ext + self.spec.tick_size) if side == "long" else (ext - self.spec.tick_size)
                sl = pull.low - off if side == "long" else pull.high + off

        elif cfg.trigger == "fvg":
            zone = self._fvg(side)
            if zone is None:
                return None
            lo, hi = zone
            if cfg.model == "breakout":
                lvl = st["hi"] if side == "long" else st["lo"]
                if lvl is None:
                    return None
                # the imbalance must sit beyond the broken level
                if side == "long" and lo < lvl:
                    return None
                if side == "short" and hi > lvl:
                    return None
                disp = self._thr(cfg.displacement_min_pts)
                if disp is None or hi - lo < disp:
                    return None
                if cfg.breakout_entry_level == "level":
                    entry = lvl
                else:
                    entry = hi if side == "long" else lo
                imp = list(self._bars)[-2]
                sl = imp.low - off if side == "long" else imp.high + off
            else:
                if cfg.fvg_entry_level == "ce":
                    entry = (lo + hi) / 2.0
                elif cfg.fvg_entry_level == "top":
                    entry = hi if side == "long" else lo
                else:
                    entry = lo if side == "long" else hi
                base = st["sweep_low_px"] if side == "long" else st["sweep_high_px"]
                if base is None:
                    return None
                sl = base - off if side == "long" else base + off

        else:  # order block
            ob = self._order_block(side, armed)
            if ob is None:
                return None
            lvl = cfg.ob_entry_level
            if lvl == "ce":
                entry = (ob.open + ob.close) / 2.0
            elif lvl == "open":
                entry = ob.open
            elif lvl == "high":
                entry = ob.high if side == "long" else ob.low
            else:
                entry = ob.low if side == "long" else ob.high
            if cfg.ob_sl_source == "ob":
                sl = ob.low - off if side == "long" else ob.high + off
            else:
                base = st["sweep_low_px"] if side == "long" else st["sweep_high_px"]
                if base is None:
                    return None
                sl = base - off if side == "long" else base + off

        if entry is None or sl is None:
            return None
        entry = round_to_tick(entry, self.spec.tick_size)
        sl = round_to_tick(sl, self.spec.tick_size)

        # Placement honesty: a limit must rest on the far side of price, a stop
        # beyond it. Anything else is a market order in disguise.
        if otype == OrderType.LIMIT:
            if side == "long" and entry >= a.close:
                self._bump("skip_limit_not_below")
                return None
            if side == "short" and entry <= a.close:
                self._bump("skip_limit_not_above")
                return None
        else:
            if side == "long" and entry <= a.close:
                self._bump("skip_stop_not_above")
                return None
            if side == "short" and entry >= a.close:
                self._bump("skip_stop_not_below")
                return None

        risk = entry - sl if side == "long" else sl - entry
        if risk <= 0:
            self._bump("skip_bad_risk")
            return None
        max_sl = self._thr(cfg.max_sl_pts)
        if max_sl is None or risk > max_sl:
            self._bump("skip_sl_too_wide")
            return None

        risk_usd = self._current_risk()
        qty = int(risk_usd / (risk * self.spec.multiplier))
        if qty < 1:
            self._bump("skip_qty_zero")
            return None
        qty = min(qty, cfg.max_contracts)

        if side == "long":
            rr_tp = entry + risk * cfg.tp1_rr
            opp = st["hi"]
            tp1 = min(rr_tp, opp) if cfg.tp1_clamp_to_opposing and opp and opp > entry else rr_tp
        else:
            rr_tp = entry - risk * cfg.tp1_rr
            opp = st["lo"]
            tp1 = max(rr_tp, opp) if cfg.tp1_clamp_to_opposing and opp and opp < entry else rr_tp

        if cfg.tp1_target != "rr":
            if cfg.tp1_target == "vwap":
                alt = self._vwap
            elif st["hi"] is not None and st["lo"] is not None:
                alt = (st["hi"] + st["lo"]) / 2.0
            else:
                alt = None
            # Only usable when it sits on the profitable side of the entry.
            if alt is not None and (
                (side == "long" and alt > entry) or (side == "short" and alt < entry)
            ):
                tp1 = alt
                self._bump(f"target:{cfg.tp1_target}")
            else:
                self._bump("target:rr_fallback")

        tp1 = round_to_tick(tp1, self.spec.tick_size)

        if cfg.exit_model == "fixed" or cfg.tp1_percent >= 100.0:
            tp1_qty = qty  # all-or-nothing: no runner is left behind
        elif qty >= 2:
            tp1_qty = max(1, min(int(round(qty * cfg.tp1_percent / 100.0)), qty - 1))
        else:
            tp1_qty = qty

        self._side = side
        self._entry_px = entry
        self._sl_px = sl
        self._tp1_px = tp1
        self._tp1_qty = tp1_qty
        self._tp1_hit = False
        self._bracket_placed = False
        self._rest_bars = 0
        self._self_canceled = set()
        self._trade_src = src
        self._bump("entries_placed")
        self._bump(f"placed:{src}")

        return [
            self._mk(
                a.key,
                a.symbol,
                "entry",
                OrderSide.BUY if side == "long" else OrderSide.SELL,
                qty,
                otype,
                entry,
            )
        ]

    # ------------------------------------------------------------------
    def _place_bracket(self, a: _Ex) -> list[Order]:
        self._bracket_placed = True
        self._oco_seq += 1
        g = self._oco_seq
        exit_side = OrderSide.SELL if self._side == "long" else OrderSide.BUY
        qty = abs(self._pos_qty)
        self._mfe_high = a.high
        self._mfe_low = a.low
        self._trail_px = self._sl_px

        if self.cfg.exit_model in ("swing_trail", "chandelier"):
            # No profit target at all: the stop does the work.
            return [
                self._mk(a.key, a.symbol, "sl", exit_side, qty, OrderType.STOP, self._sl_px)
            ]

        tp1_qty = min(self._tp1_qty, qty)
        return [
            self._mk(a.key, a.symbol, "sl", exit_side, qty, OrderType.STOP, self._sl_px, oco=g),
            self._mk(
                a.key, a.symbol, "tp1", exit_side, tp1_qty, OrderType.LIMIT, self._tp1_px, oco=g
            ),
        ]

    def _breakeven(self, a: _Ex) -> list[Order]:
        if self._entry_px is None or self._side is None:
            return []
        remaining = abs(self._pos_qty)
        if remaining <= 0:
            return []
        self._cancel("sl")
        self._cancel("be")
        off = self.cfg.be_offset_ticks * self.spec.tick_size
        px = self._entry_px + off if self._side == "long" else self._entry_px - off
        side = OrderSide.SELL if self._side == "long" else OrderSide.BUY
        return [self._mk(a.key, a.symbol, "be", side, remaining, OrderType.STOP, px)]

    def _update_atr(self, a: _Ex) -> None:
        """Wilder ATR over execution bars, seeded by a simple mean."""
        pc = self._prev_close
        tr = a.high - a.low if pc is None else max(a.high - a.low, abs(a.high - pc), abs(a.low - pc))
        n = max(1, self.cfg.atr_len)
        if self._atr is None:
            self._atr_seed.append(tr)
            if len(self._atr_seed) >= n:
                self._atr = sum(self._atr_seed) / float(n)
        else:
            self._atr = (self._atr * (n - 1) + tr) / float(n)
        self._prev_close = a.close

    def _update_trail(self, a: _Ex) -> list[Order]:
        """Ratchet the stop. It may only ever move in the trade's favour."""
        if self._side is None or self._trail_px is None:
            return []
        cfg = self.cfg
        long = self._side == "long"
        self._mfe_high = a.high if self._mfe_high is None else max(self._mfe_high, a.high)
        self._mfe_low = a.low if self._mfe_low is None else min(self._mfe_low, a.low)

        want: float | None = None
        if cfg.exit_model == "chandelier":
            if self._atr is None:
                return []
            band = cfg.atr_mult * self._atr
            want = (self._mfe_high - band) if long else (self._mfe_low + band)
        else:  # swing_trail
            k = cfg.trail_swing_bars
            bars = list(self._bars)
            if len(bars) < 2 * k + 1:
                return []
            piv = None
            for i in range(len(bars) - k - 1, k - 1, -1):
                p = bars[i]
                w = bars[i - k : i + k + 1]
                if long and all(p.low <= b.low for b in w):
                    piv = p
                    break
                if not long and all(p.high >= b.high for b in w):
                    piv = p
                    break
            if piv is None:
                return []
            off = cfg.sl_offset_ticks * self.spec.tick_size
            want = (piv.low - off) if long else (piv.high + off)

        if want is None:
            return []
        want = round_to_tick(want, self.spec.tick_size)
        # Never loosen, and never place a stop already through the market.
        if long and (want <= self._trail_px or want >= a.close):
            return []
        if not long and (want >= self._trail_px or want <= a.close):
            return []

        self._trail_px = want
        self._cancel("sl")
        self._cancel("be")
        self._bump("trail_moves")
        side = OrderSide.SELL if long else OrderSide.BUY
        return [
            self._mk(a.key, a.symbol, "sl", side, abs(self._pos_qty), OrderType.STOP, want)
        ]

    def _mss_broken(self) -> bool:
        piv = self._swing("low" if self._side == "long" else "high", None)
        if piv is None:
            return False
        last = list(self._bars)[-1]
        if self._side == "long":
            return last.close < piv.low
        return last.close > piv.high

    def _close_all(self, a: _Ex, why: str) -> list[Order]:
        qty = abs(self._pos_qty)
        side = OrderSide.SELL if self._pos_qty > 0 else OrderSide.BUY
        for k in ("entry", "sl", "tp1", "be"):
            self._cancel(k)
        self._bracket_placed = False
        self._closing = True
        self._bump(f"flat_{why}")
        return [self._mk(a.key, a.symbol, f"flat_{why}", side, qty, OrderType.MARKET)]
