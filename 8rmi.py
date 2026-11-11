#!/usr/bin/env python3
"""RMI Trend Sniper strategy evaluated on closed candles.

Python adaptation of RMI Trend Sniper by TZack88.
Entries require a line touch followed by confirmation on the next candle.
Exits use SL or color changes.
"""

import argparse
from collections import deque
from datetime import datetime, time
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import execute, load_candles


# Original script: RMI Trend Sniper by TZack88, MPL-2.0.
RMI_LENGTH = 14
POSITIVE_LEVEL = 66.0
NEGATIVE_LEVEL = 30.0
SL_POINTS = 70.0
TRADE_START = time(9, 45)
TRADE_END = time(23, 12, 59)
TRADE_TIMEZONE = ZoneInfo("Asia/Kolkata")
SQUARE_OFF_INTRADAY = True


class RMA:
    """Wilder's moving average, seeded with an SMA."""

    def __init__(self, length: int):
        if length < 1:
            raise ValueError("RMA length must be positive")
        self.length = length
        self.values = []
        self.value = None

    def update(self, value):
        if value is None:
            return self.value

        if self.value is None:
            self.values.append(value)
            if len(self.values) < self.length:
                return None
            self.value = sum(self.values) / self.length
            self.values.clear()
        else:
            self.value += (value - self.value) / self.length

        return self.value


class EMA:
    """EMA initialized from the first input, matching Pine's ta.ema."""

    def __init__(self, length: int):
        self.alpha = 2.0 / (length + 1)
        self.value = None

    def update(self, value):
        self.value = (
            value if self.value is None
            else self.value + self.alpha * (value - self.value)
        )
        return self.value


class RMITrendSniper:
    def __init__(self, length=RMI_LENGTH,
                 positive_level=POSITIVE_LEVEL,
                 negative_level=NEGATIVE_LEVEL):
        if length < 1:
            raise ValueError("RMI length must be positive")
        if not 0 <= negative_level < positive_level <= 100:
            raise ValueError("Levels must satisfy 0 <= negative < positive <= 100")

        self.length = length
        self.positive_level = positive_level
        self.negative_level = negative_level

        self.up_rma = RMA(length)
        self.down_rma = RMA(length)
        self.atr_rma = RMA(30)
        self.ema5 = EMA(5)

        self.previous_close = None
        self.previous_hlc3 = None
        self.previous_ema5 = None
        self.previous_rsi_mfi = None
        self.positive = False
        self.negative = False
        self.pending_touch = None
        self.previous_day = None

        self.money_flows = deque(maxlen=length)
        self.band_values = deque(maxlen=21)
        self.range_history = deque(maxlen=39)

    def update(self, candle):
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        volume = float(candle["volume"])

        hlc3 = (high + low + close) / 3.0
        bar_range = high - low

        # RSI component.
        change = None if self.previous_close is None else close - self.previous_close
        up = None if change is None else max(change, 0.0)
        down = None if change is None else max(-change, 0.0)
        avg_up = self.up_rma.update(up)
        avg_down = self.down_rma.update(down)

        rsi = None
        if avg_up is not None and avg_down is not None:
            if avg_down == 0:
                rsi = 100.0
            elif avg_up == 0:
                rsi = 0.0
            else:
                rsi = 100.0 - 100.0 / (1.0 + avg_up / avg_down)

        # MFI component, calculated from typical-price direction and volume.
        if self.previous_hlc3 is not None:
            flow = hlc3 * volume
            self.money_flows.append(
                (flow, 0.0) if hlc3 > self.previous_hlc3 else
                (0.0, flow) if hlc3 < self.previous_hlc3 else
                (0.0, 0.0)
            )

        mfi = None
        if len(self.money_flows) == self.length:
            positive_flow = sum(item[0] for item in self.money_flows)
            negative_flow = sum(item[1] for item in self.money_flows)
            mfi = (
                100.0 if negative_flow == 0 else
                0.0 if positive_flow == 0 else
                100.0 - 100.0 / (1.0 + positive_flow / negative_flow)
            )

        rsi_mfi = (
            (rsi + mfi) / 2.0
            if rsi is not None and mfi is not None
            else None
        )

        # Pine: ta.change(ta.ema(close, 5)).
        ema5 = self.ema5.update(close)
        ema5_rising = self.previous_ema5 is not None and ema5 > self.previous_ema5
        ema5_falling = self.previous_ema5 is not None and ema5 < self.previous_ema5

        p_mom = (
            rsi_mfi is not None
            and self.previous_rsi_mfi is not None
            and self.previous_rsi_mfi < self.positive_level
            and rsi_mfi > self.positive_level
            and rsi_mfi > self.negative_level
            and ema5_rising
        )
        n_mom = (
            rsi_mfi is not None
            and rsi_mfi < self.negative_level
            and ema5_falling
        )

        was_positive = self.positive
        was_negative = self.negative

        if p_mom:
            self.positive = True
            self.negative = False
        if n_mom:
            self.positive = False
            self.negative = True

        crossed = None
        if self.positive and not was_positive:
            crossed = "LONG"
        elif self.negative and not was_negative:
            crossed = "SHORT"

        # Pine's band is min(ATR(30)*0.3, close*0.003), offset by 20 bars,
        # then divided by two and multiplied by eight.
        previous_close = self.previous_close
        true_range = (
            high - low if previous_close is None else
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
        atr = self.atr_rma.update(true_range)
        band_base = (
            min(atr * 0.3, close * 0.003)
            if atr is not None else None
        )
        self.band_values.append(band_base)
        band = (
            self.band_values[0] * 4.0
            if len(self.band_values) == 21 and self.band_values[0] is not None
            else None
        )

        # Range-weighted moving average, reproducing the script's rolling weights.
        self.range_history.append((bar_range, close))
        rwma = None
        if len(self.range_history) == 39:
            history = list(self.range_history)
            weighted_sum = 0.0
            weight_sum = 0.0
            for index in range(19, 39):
                window_sum = sum(history[j][0] for j in range(index - 19, index + 1))
                if window_sum > 0:
                    weight = history[index][0] / window_sum
                    weighted_sum += history[index][1] * weight
                    weight_sum += weight
            if weight_sum > 0:
                rwma = weighted_sum / weight_sum

        self.previous_close = close
        self.previous_hlc3 = hlc3
        self.previous_ema5 = ema5
        if rsi_mfi is not None:
            self.previous_rsi_mfi = rsi_mfi

        return {
            "crossed": crossed,
            "positive": self.positive,
            "negative": self.negative,
            "rwma": rwma,
            "band": band,
        }

    def logic(self, candle):
        dt = datetime.fromisoformat(candle["datetime"])
        dt = dt.replace(tzinfo=TRADE_TIMEZONE) if dt.tzinfo is None else dt.astimezone(TRADE_TIMEZONE)
        new_day = self.previous_day is not None and dt.date() != self.previous_day
        previous_side = "LONG" if self.positive else "SHORT" if self.negative else None
        self.previous_day = dt.date()
        if SQUARE_OFF_INTRADAY and new_day:
            self.pending_touch = None
        alert = self.candle_signal(candle)
        if SQUARE_OFF_INTRADAY and new_day and previous_side:
            # Intraday positions were squared off on the previous session's last
            # bar. An exit-side alert clears that side's SL lock in the existing
            # engine, including before today's entry window, without a new trade.
            alert = dict(alert or {})
            alert["exit"] = previous_side
            alert.setdefault("remark", "RMI intraday reset")
        return alert

    def candle_signal(self, candle):
        state = self.update(candle)
        color = "LONG" if state["positive"] else "SHORT" if state["negative"] else None
        pending_touch = self.pending_touch
        self.pending_touch = None
        if state["crossed"]:
            # A color-change candle may close a trade, but never open one.
            return {
                "exit": "SHORT" if state["crossed"] == "LONG" else "LONG",
                "remark": "RMI Trend Sniper state changed",
            }
        if color is None or state["rwma"] is None or state["band"] is None:
            return None

        line = state["rwma"] + (-state["band"] if color == "LONG" else state["band"])
        touches_line = float(candle["low"]) <= line <= float(candle["high"])
        close = float(candle["close"])
        confirmed = close > line if color == "LONG" else close < line
        if pending_touch == color and confirmed:
            return {
                "entry": color,
                "remark": "RMI entry: next candle closed above blue line" if color == "LONG"
                else "RMI entry: next candle closed below red line",
            }
        # A failed confirmation expires; a new touch can arm a fresh setup.
        if touches_line:
            self.pending_touch = color
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=Path(__file__).resolve().parents[2] / "res.json",
    )
    parser.add_argument("--length", type=int, default=RMI_LENGTH)
    parser.add_argument("--positive-level", type=float, default=POSITIVE_LEVEL)
    parser.add_argument("--negative-level", type=float, default=NEGATIVE_LEVEL)
    args = parser.parse_args()

    try:
        candles = load_candles(args.path)
        if any(candle.get("volume") is None for candle in candles):
            raise ValueError("RMI Trend Sniper requires a volume field in every candle")

        times = [datetime.fromisoformat(candle["datetime"]) for candle in candles]
        gaps = [
            (later - earlier).total_seconds() / 60
            for earlier, later in zip(times, times[1:])
            if earlier.date() == later.date() and later > earlier
        ]
        candle_minutes = min(gaps) if gaps else 0
        if not candle_minutes:
            raise ValueError("Need consecutive candles to determine closing timestamps")

        indicator = RMITrendSniper(
            args.length, args.positive_level, args.negative_level
        )
        print(
            f"RMI Trend Sniper: length={args.length}, "
            f"positive={args.positive_level:g}, negative={args.negative_level:g}"
        )
        execute(
            indicator.logic,
            args.path,
            sl_points=SL_POINTS,
            intraday=SQUARE_OFF_INTRADAY,
            trade_start=TRADE_START,
            trade_end=TRADE_END,
            timezone=TRADE_TIMEZONE,
            candle_minutes=candle_minutes,
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
