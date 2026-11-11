from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo
import math
import re
import time
import json
import uuid
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from collections import deque

TIMEFRAME_MINUTES = 15
STRIKE_INTERVAL = 50  # CRUDEOILM option strike spacing.
EXPIRY_ROLLOVER_DAYS = 2  # Switch to next month with this many calendar days left.
CHANNEL_LENGTH = 10
AVERAGE_LENGTH = 21
SIGNAL_LENGTH = 4
SIGNAL_MODE = 'wt1'  # WT1 crossing zero; 'difference' uses WT1 - WT2.
START_TIME = clock_time(9, 30)
END_TIME = clock_time(23, 0)
LOTS = 1
SL_POINTS = 50.0  # Distance below the option's buy price.
SL_BUFFER = 5.0  # Sell limit below the SL trigger.
last_trade_alert = None
saved_state = {}


def notify(api, message, category="error"):
    """Use main.py's Telegram callback; notification failures do not stop trading."""
    sender = getattr(api, 'notify', None)
    if not callable(sender):
        return False
    try:
        return bool(sender(message, category=category))
    except Exception:
        print('Telegram notification failed', flush=True)
        return False


def trade_notice(api, trade, event, details=''):
    """Send only entry, confirmed exit and SL-hit messages, once per trade event."""
    if event not in ('BUY submitted', 'EXIT filled', 'SL filled'):
        return
    side = 'Call' if trade['signal'] == 'LONG' else 'Put'
    strike = f"{float(trade['option']['strike']):g}"
    if event == 'BUY submitted':
        ball = '🟢' if side == 'Call' else '🔴'
        message = f'{ball} | Entered {side} | Strike {strike}'
    elif event == 'EXIT filled':
        message = f'❌ | Exit {side} | Strike {strike}'
    else:
        message = f'⚠️ | SL Hit | {side} | Strike {strike}'
    notices = trade.setdefault('notifications', {})
    if notices.get(event) == message:
        return
    if notify(api, message, category='trade'):
        notices[event] = message
        save_state(trade)


class StateError(RuntimeError):
    pass


def save_state(trade):
    """Keep trade records in memory for this run; do not create state files."""
    previous = saved_state.get('trade')
    if previous and trade and previous.get('buy_tag') != trade.get('buy_tag'):
        if previous.get('status') != 'CLOSED':
            raise StateError('An existing trade must be reconciled before replacing it')
        saved_state.setdefault('history', []).append(previous.copy())
    saved_state['trade'] = trade
    saved_state['last_trade_alert'] = last_trade_alert


def start_fresh(api):
    """Clear local records for a new run; leave broker orders and positions alone."""
    global saved_state, last_trade_alert
    saved_state = {'account': str(api.userid)}
    last_trade_alert = None
    save_state(None)


def recover_order_ids(api, trade):
    """Find a submitted order by its unique tag if its response was lost."""
    if not trade or trade.get('status') == 'CLOSED':
        return
    for id_field, tag_field, side in (
        ('order_id', 'buy_tag', 'B'),
        ('sl_order_id', 'sl_tag', 'S'),
        ('exit_order_id', 'exit_tag', 'S'),
    ):
        if trade.get(id_field) or not trade.get(tag_field):
            continue
        orders = api.get_order_book()
        if not isinstance(orders, list):
            raise ValueError('Order book unavailable; keeping the saved trade')
        matches = [row for row in orders if row.get('remarks') == trade[tag_field]]
        if len(matches) != 1:
            raise ValueError('Unconfirmed order could not be uniquely recovered; check broker orders')
        order = matches[0]
        if (order.get('tsym') != trade['option']['symbol'] or order.get('exch') != 'MCX'
                or order.get('prd') != trade['product'] or order.get('trantype') != side
                or int(order.get('qty', 0)) != trade['quantity'] or not order.get('norenordno')):
            raise ValueError('Recovered order does not match the saved trade')
        trade[id_field] = str(order['norenordno'])
        save_state(trade)
        print(f'Recovered {id_field}: {trade[id_field]}', flush=True)
        trade_notice(api, trade, 'Order recovered', f'{id_field}={trade[id_field]}')


def fetch_candles(api, token, timeframe=None):
    if timeframe is None:
        timeframe = TIMEFRAME_MINUTES

    now = datetime.now(ZoneInfo('Asia/Kolkata'))
    start = now - timedelta(days=14)

    try:
        candles = api.get_time_price_series(
            exchange='MCX',
            token=str(token),
            starttime=int(start.timestamp()),
            endtime=int(now.timestamp()),
            interval=timeframe,
        )
        return candles if isinstance(candles, list) else []
    except Exception as error:
        print('Candle fetch failed:', type(error).__name__)
        return []


class EMA:
    """EMA initialized with the first available value, matching Pine."""

    def __init__(self, length):
        if length < 1:
            raise ValueError('EMA length must be positive')
        self.alpha = 2.0 / (length + 1)
        self.value = None

    def update(self, value):
        if value is not None:
            self.value = (value if self.value is None else
                          self.value + self.alpha * (value - self.value))
        return self.value


class WaveTrend:
    """WaveTrend calculation adapted from LazyBear's TradingView indicator."""

    def __init__(self, channel_length=CHANNEL_LENGTH,
                 average_length=AVERAGE_LENGTH, signal_mode=SIGNAL_MODE):
        if channel_length < 1 or average_length < 1:
            raise ValueError('Channel and average lengths must be positive')
        if signal_mode not in ('difference', 'wt1'):
            raise ValueError("signal_mode must be 'difference' or 'wt1'")
        self.signal_mode = signal_mode
        self.esa = EMA(channel_length)
        self.deviation = EMA(channel_length)
        self.tci = EMA(average_length)
        self.wt1_values = deque(maxlen=SIGNAL_LENGTH)
        self.previous_signal = None

    def update(self, candle):
        ap = (float(candle['high']) + float(candle['low']) +
              float(candle['close'])) / 3.0
        esa = self.esa.update(ap)
        deviation = self.deviation.update(abs(ap - esa))
        ci = (ap - esa) / (0.015 * deviation) if deviation > 0 else None
        wt1 = self.tci.update(ci) if ci is not None else None
        if wt1 is not None:
            self.wt1_values.append(wt1)
        wt2 = (sum(self.wt1_values) / SIGNAL_LENGTH
               if len(self.wt1_values) == SIGNAL_LENGTH else None)
        difference = wt1 - wt2 if wt2 is not None else None
        signal_value = difference if self.signal_mode == 'difference' else wt1

        crossed = None
        if signal_value is not None and self.previous_signal is not None:
            if self.previous_signal <= 0 < signal_value:
                crossed = 'LONG'
            elif self.previous_signal >= 0 > signal_value:
                crossed = 'SHORT'
        if signal_value is not None:
            self.previous_signal = signal_value
        return {'wt1': wt1, 'wt2': wt2, 'difference': difference,
                'signal_value': signal_value, 'crossed': crossed}


def wt_cross(candles, timeframe=None):
    """Return WaveTrend values and the latest completed candle's signal."""
    if timeframe is None:
        timeframe = TIMEFRAME_MINUTES

    now = datetime.now(ZoneInfo('Asia/Kolkata'))
    closed = {}
    try:
        for candle in candles:
            if 'ssboe' in candle:
                stamp = datetime.fromtimestamp(int(candle['ssboe']), now.tzinfo)
            else:
                stamp = datetime.strptime(candle['time'], '%d-%m-%Y %H:%M:%S').replace(tzinfo=now.tzinfo)
            if stamp + timedelta(minutes=timeframe) <= now:
                closed[stamp] = candle

        if len(closed) < 100:
            return None

        indicator = WaveTrend()
        for stamp in sorted(closed):
            candle = closed[stamp]
            result = indicator.update({
                'high': float(candle['inth']),
                'low': float(candle['intl']),
                'close': float(candle['intc']),
            })
        return {'time': stamp.isoformat(), 'wt1': result['wt1'],
                'wt2': result['wt2'], 'signal': result['crossed']}
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        print('WaveTrend calculation failed:', type(error).__name__)
        return None


def find_atm_option(api, signal, price):
    """LONG buys the nearest ATM call; SHORT buys the nearest ATM put."""
    try:
        price = float(price)
        if signal not in ('LONG', 'SHORT') or not math.isfinite(price) or price <= 0:
            print('Use LONG or SHORT and a valid futures price')
            return None

        today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
        response = api.searchscrip(exchange='MCX', searchtext='CRUDEOILM')
        if not response or response.get('stat') != 'Ok':
            print('Option search failed')
            return None

        # Read the expiry, call/put type and strike from each symbol.
        options = []
        for row in response.get('values', []):
            symbol = row.get('tsym', '')
            match = re.fullmatch(r'CRUDEOILM(\d{2}[A-Z]{3}\d{2})([CP])(\d+(?:\.\d+)?)', symbol)
            if not match:
                continue

            expiry = datetime.strptime(match[1], '%d%b%y').date()
            if expiry < today:
                continue

            options.append({
                'symbol': symbol,
                'token': str(row['token']),
                'expiry': expiry,
                'kind': match[2],
                'strike': float(match[3]),
            })

        if not options:
            print('No CRUDEOILM options found')
            return None

        # Use next month's expiry when 2 calendar days or fewer remain.
        expiries = sorted({option['expiry'] for option in options})
        expiry = expiries[0]
        if (expiry - today).days <= EXPIRY_ROLLOVER_DAYS:
            next_month = (expiry.replace(day=28) + timedelta(days=4)).replace(day=1)
            expiry = None
            for date in expiries:
                if date.year == next_month.year and date.month == next_month.month:
                    expiry = date
                    break
            if expiry is None:
                print('Next-month options unavailable')
                return None

        # A broad search can omit nearby strikes. Search the exact ATM symbol
        # instead of choosing a distant contract from an incomplete result list.
        step = Decimal(str(STRIKE_INTERVAL))
        if not step.is_finite() or step <= 0:
            raise ValueError('Strike interval must be positive')
        strike = (Decimal(str(price)) / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        if strike <= 0:
            raise ValueError('Invalid ATM strike')
        kind = 'C' if signal == 'LONG' else 'P'
        symbol = f"CRUDEOILM{expiry.strftime('%d%b%y').upper()}{kind}{strike.normalize():f}"
        response = api.searchscrip(exchange='MCX', searchtext=symbol)
        if not response or response.get('stat') != 'Ok':
            print(f'ATM option unavailable: {symbol} | futures price={price}')
            return None
        matches = {str(row['token']) for row in response.get('values', [])
                   if row.get('tsym') == symbol and row.get('token')
                   and row.get('exch', 'MCX') == 'MCX'}
        if len(matches) != 1:
            print(f'Exact ATM option not uniquely found: {symbol} | futures price={price}')
            return None
        token = matches.pop()

        info = api.get_security_info(exchange='MCX', token=token)
        if not info or info.get('stat') != 'Ok':
            print('Option details unavailable')
            return None
        lot_size = int(info['ls'])
        if (info.get('tsym') != symbol or lot_size <= 0
                or info.get('exch', 'MCX') != 'MCX'
                or str(info.get('token', token)) != token):
            print('Option symbol or lot size mismatch')
            return None

        print(f'ATM selected: futures price={price} | strike={strike} | {symbol}', flush=True)
        return {
            'exchange': 'MCX',
            'symbol': symbol,
            'token': token,
            'strike': float(strike),
            'underlying_price': price,
            'expiry': expiry.isoformat(),
            'lot_size': lot_size,
        }
    except Exception as error:
        print('ATM option lookup failed:', type(error).__name__)
        return None


def take_trade(api, alert, price, lots=None, sl_points=None):
    global last_trade_alert

    if not trading_hours():
        return None
    if not alert or alert.get('signal') not in ('LONG', 'SHORT'):
        return None
    if lots is None:
        lots = LOTS
    if sl_points is None:
        sl_points = SL_POINTS
    if type(lots) is not int or lots < 1:
        print('Lots must be a positive whole number')
        return None
    if not math.isfinite(sl_points) or sl_points <= 0:
        print('SL points must be positive')
        return None

    alert_time = alert.get('time')
    if not alert_time or alert_time == last_trade_alert:
        return None

    option = find_atm_option(api, alert['signal'], price)
    if not option:
        notify(api, 'WT CRUDE entry skipped: exact ATM option lookup failed')
        return None

    quantity = lots * option['lot_size']
    last_trade_alert = alert_time
    trade = {
        'order_id': None,
        'buy_tag': 'wtcr_b_' + uuid.uuid4().hex[:12],
        'status': 'SUBMITTING',
        'signal': alert['signal'],
        'time': alert_time,
        'option': option,
        'quantity': quantity,
        'product': 'M',
        'sl_points': sl_points,
    }
    saved_state.setdefault('account', str(api.userid))
    save_state(trade)  # Save the intent BEFORE sending the buy.

    try:
        order = api.place_order(
            buy_or_sell='B',
            product_type='M',
            exchange='MCX',
            tradingsymbol=option['symbol'],
            quantity=quantity,
            price_type='MKT',
            price=0,
            trigger_price=0,
            retention='DAY',
            remarks=trade['buy_tag'],
        )
    except Exception as error:
        print('Buy response unavailable:', type(error).__name__, '| Saved for broker reconciliation.')
        trade_notice(api, trade, 'BUY unconfirmed', 'Response unavailable; reconciling with broker')
        return trade

    if not order or order.get('stat') != 'Ok' or not order.get('norenordno'):
        print('Buy not confirmed. Saved for reconciliation; no automatic retry.')
        trade_notice(api, trade, 'BUY unconfirmed', 'No confirmed order ID; reconciling with broker')
        return trade

    trade['order_id'] = str(order['norenordno'])
    trade['status'] = 'SUBMITTED'
    save_state(trade)
    print(f"BUY submitted: {option['symbol']} | qty={quantity} | ID={trade['order_id']}")
    trade_notice(api, trade, 'BUY submitted', f"Awaiting fill | futures price={price} | ATM strike={option['strike']}")
    apply_sl(api, trade)
    return trade


def apply_sl(api, trade, sl_points=None):
    if not trade or trade.get('sl_attempted'):
        return None
    if sl_points is None:
        sl_points = trade.get('sl_points', SL_POINTS)

    try:
        if not math.isfinite(sl_points) or sl_points <= 0:
            raise ValueError('SL points must be positive')
        if not math.isfinite(SL_BUFFER) or SL_BUFFER < 0:
            raise ValueError('SL buffer must not be negative')

        option = trade['option']
        fill_price = None

        # Wait up to 15 checks for the buy to fill completely.
        for attempt in range(15):
            history = api.single_order_history(orderno=trade['order_id'])
            if not history:
                break

            for order in history:
                if str(order.get('norenordno')) != trade['order_id']:
                    continue
                expected = (option['symbol'], 'MCX', 'B', trade['product'])
                actual = (order.get('tsym'), order.get('exch'), order.get('trantype'), order.get('prd'))
                if actual != expected:
                    raise ValueError('Buy order details do not match')
                if order.get('status') != 'COMPLETE':
                    continue
                if int(order.get('fillshares', 0)) == trade['quantity']:
                    fill_price = float(order['avgprc'])
                    break

            if fill_price is not None:
                break
            if history[0].get('status') in ('REJECTED', 'CANCELED', 'CANCELLED'):
                break
            if attempt < 14:
                time.sleep(1)

        if fill_price is None:
            trade['sl_status'] = 'WAITING_FOR_FULL_FILL'
            print('Buy not fully filled. Call apply_sl again later.')
            trade_notice(api, trade, 'BUY awaiting full fill', 'SL not placed yet')
            return None
        if not math.isfinite(fill_price) or fill_price <= 0:
            raise ValueError('Invalid buy price')
        trade['status'] = 'FILLED'
        trade['fill_price'] = fill_price
        trade_notice(api, trade, 'BUY filled', f'price={fill_price}')

        # Calculate the trigger and limit using the option's tick size.
        info = api.get_security_info(exchange='MCX', token=option['token'])
        if not info or info.get('stat') != 'Ok' or info.get('tsym') != option['symbol']:
            raise ValueError('Option details unavailable')
        tick = Decimal(str(info['ti']))
        if not tick.is_finite() or tick <= 0:
            raise ValueError('Invalid tick size')

        trigger = Decimal(str(fill_price)) - Decimal(str(sl_points))
        trigger = (trigger / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        limit = trigger - Decimal(str(SL_BUFFER))
        limit = (limit / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if limit <= 0:
            raise ValueError('SL distance exceeds option premium')

        # Record the attempt before sending, so a lost response cannot duplicate it.
        trade['sl_attempted'] = True
        trade['sl_status'] = 'UNCONFIRMED'
        trade['sl_trigger'] = float(trigger)
        trade['sl_limit'] = float(limit)
        trade['sl_tag'] = 'wtcr_s_' + uuid.uuid4().hex[:12]
        save_state(trade)

        order = api.place_order(
            buy_or_sell='S',
            product_type=trade['product'],
            exchange='MCX',
            tradingsymbol=option['symbol'],
            quantity=trade['quantity'],
            price_type='SL-LMT',
            price=float(limit),
            trigger_price=float(trigger),
            retention='DAY',
            remarks=trade['sl_tag'],
        )
        if not order or order.get('stat') != 'Ok' or not order.get('norenordno'):
            print('SL not confirmed. Check broker; position may be unprotected.')
            trade_notice(api, trade, 'SL unconfirmed', 'Check broker; position may be unprotected')
            return None

        trade['sl_order_id'] = str(order['norenordno'])
        trade['sl_status'] = 'SUBMITTED'
        save_state(trade)
        print(f"SL submitted: trigger={trigger} | limit={limit} | ID={trade['sl_order_id']}")
        trade_notice(api, trade, 'SL submitted', f"trigger={trigger} | limit={limit} | SL ID={trade['sl_order_id']}")
        return trade['sl_order_id']
    except StateError:
        raise
    except Exception as error:
        trade['sl_status'] = 'UNCONFIRMED' if trade.get('sl_attempted') else 'NOT_PLACED'
        print('SL failed:', type(error).__name__, '| Check broker; position may be unprotected.')
        trade_notice(api, trade, 'SL failed', f'{type(error).__name__}; check broker protection')
        return None
    finally:
        save_state(trade)


def get_trade_order(api, order_id, trade, side):
    """Read only the order belonging to this trade."""
    history = api.single_order_history(orderno=order_id)
    if not history:
        raise ValueError('Order history unavailable')

    for order in history:
        if str(order.get('norenordno')) != str(order_id):
            continue
        if (order.get('tsym') != trade['option']['symbol']
                or order.get('exch') != 'MCX'
                or order.get('prd') != trade['product']
                or order.get('trantype') != side):
            raise ValueError('Order does not match the recorded trade')
        return order  # Broker history is newest first.
    raise ValueError('Recorded order ID was not found')


def close_trade(api, trade, reason='Opposite WaveTrend alert'):
    """Cancel this trade's SL, then sell only the quantity bought by the script."""
    if trade.get('status') == 'CLOSED':
        return True

    try:
        # Confirm the original buy before sending any sell.
        buy = get_trade_order(api, trade['order_id'], trade, 'B')
        if buy.get('status') != 'COMPLETE' or int(buy.get('fillshares', 0)) != trade['quantity']:
            print('Original buy not fully filled. Reversal is waiting.')
            trade_notice(api, trade, 'Exit waiting', 'Original buy not fully filled')
            return False

        # Do not send a second sell if an earlier exit is still unresolved.
        if trade.get('exit_attempted'):
            if not trade.get('exit_order_id'):
                print('Exit response was lost. Check broker before continuing.')
                trade_notice(api, trade, 'Exit unconfirmed', 'Check broker before another sell')
                return False
        else:
            stop_id = trade.get('sl_order_id')
            if trade.get('sl_attempted') and not stop_id:
                print('SL submission is unresolved. Check broker before closing.')
                trade_notice(api, trade, 'Exit waiting', 'SL submission unresolved')
                return False

            if stop_id:
                stop = get_trade_order(api, stop_id, trade, 'S')
                if stop.get('status') not in ('COMPLETE', 'CANCELED', 'CANCELLED', 'REJECTED'):
                    if not trade.get('sl_cancel_attempted'):
                        trade['sl_cancel_attempted'] = True
                        save_state(trade)
                        api.cancel_order(orderno=stop_id)
                    for attempt in range(15):
                        stop = get_trade_order(api, stop_id, trade, 'S')
                        if stop.get('status') in ('COMPLETE', 'CANCELED', 'CANCELLED', 'REJECTED'):
                            break
                        if attempt < 14:
                            time.sleep(1)

                filled = int(stop.get('fillshares') or 0)
                if stop.get('status') == 'COMPLETE' and filled == trade['quantity']:
                    trade['status'] = 'CLOSED'
                    trade['exit_order_id'] = stop_id
                    trade['exit_reason'] = 'SL'
                    print('SL already closed the trade. No extra sell sent.')
                    trade_notice(api, trade, 'SL filled', f'SL ID={stop_id}; trade closed')
                    return True
                if stop.get('status') not in ('CANCELED', 'CANCELLED', 'REJECTED') or filled != 0:
                    print('SL is active or partially filled. Reversal is waiting.')
                    trade_notice(api, trade, 'Exit waiting', 'SL still active or partially filled')
                    return False
                trade['sl_status'] = stop['status']
                trade_notice(api, trade, 'SL terminal', f"status={stop['status']} | SL ID={stop_id}")

            # Manual additions are not ours to sell. Never exceed our original quantity.
            positions = api.get_positions()
            if not isinstance(positions, list):
                raise ValueError('Broker positions unavailable')
            available = 0
            for position in positions:
                if (position.get('tsym') == trade['option']['symbol']
                        and position.get('exch') == 'MCX'
                        and position.get('prd') == trade['product']):
                    available += int(position['netqty'])
            if available == 0:
                trade['status'] = 'CLOSED'
                trade['exit_reason'] = 'Already flat at broker'
                trade_notice(api, trade, 'Position closed', 'Broker confirms flat; no sell sent')
                return True
            if available < trade['quantity']:
                print('Position quantity changed. Check broker before closing.')
                trade_notice(api, trade, 'Exit waiting', 'Position quantity changed; check broker')
                return False

            trade['exit_attempted'] = True
            trade['status'] = 'EXIT_PENDING'
            trade['exit_tag'] = 'wtcr_e_' + uuid.uuid4().hex[:12]
            trade['exit_reason'] = reason
            save_state(trade)
            order = api.place_order(
                buy_or_sell='S',
                product_type=trade['product'],
                exchange='MCX',
                tradingsymbol=trade['option']['symbol'],
                quantity=trade['quantity'],
                price_type='MKT',
                price=0,
                trigger_price=0,
                retention='DAY',
                remarks=trade['exit_tag'],
            )
            if not order or order.get('stat') != 'Ok' or not order.get('norenordno'):
                print('Exit not confirmed. Check broker orders.')
                trade_notice(api, trade, 'Exit unconfirmed', 'Check broker orders')
                return False
            trade['exit_order_id'] = str(order['norenordno'])
            save_state(trade)
            print(f"EXIT submitted: {trade['option']['symbol']} | ID={trade['exit_order_id']}")
            trade_notice(api, trade, 'EXIT submitted', f"exit ID={trade['exit_order_id']} | {reason}")

        for attempt in range(15):
            exit_order = get_trade_order(api, trade['exit_order_id'], trade, 'S')
            if exit_order.get('status') == 'COMPLETE' and int(exit_order.get('fillshares', 0)) == trade['quantity']:
                trade['status'] = 'CLOSED'
                trade['exit_price'] = float(exit_order['avgprc'])
                trade['exit_reason'] = reason
                print(f"EXIT filled: {trade['option']['symbol']} | price={trade['exit_price']}")
                trade_notice(api, trade, 'EXIT filled', f"price={trade['exit_price']} | exit ID={trade['exit_order_id']} | {reason}")
                return True
            if exit_order.get('status') in ('REJECTED', 'CANCELED', 'CANCELLED'):
                break
            if attempt < 14:
                time.sleep(1)
        print('Exit not fully filled. New entry is waiting.')
        trade_notice(api, trade, 'Exit awaiting full fill', 'New entry is waiting')
    except StateError:
        raise
    except Exception as error:
        print('Exit check failed:', type(error).__name__, '| Check broker orders.')
        trade_notice(api, trade, 'Exit failed', f'{type(error).__name__}; check broker orders')
    finally:
        save_state(trade)
    return False


def handle_alert(api, alert, price, trade=None, lots=None, sl_points=None):
    """Close an opposite trade first, then open the new call or put."""
    now = datetime.now(ZoneInfo('Asia/Kolkata'))
    if now.time() >= END_TIME:
        return day_end_exit(api, trade, now)
    if not trading_hours(now):
        return trade
    if not alert or alert.get('signal') not in ('LONG', 'SHORT') or not alert.get('time'):
        return trade
    if trade and trade.get('status') != 'CLOSED':
        if trade['signal'] == alert['signal']:
            return trade
        if not close_trade(api, trade):
            return trade

    new_trade = take_trade(api, alert, price, lots=lots, sl_points=sl_points)
    return new_trade if new_trade else trade


def trading_hours(now=None):
    if now is None:
        now = datetime.now(ZoneInfo('Asia/Kolkata'))
    return START_TIME <= now.time() < END_TIME


def day_end_exit(api, trade, now=None):
    """Call on every poll, even when there is no new signal."""
    if now is None:
        now = datetime.now(ZoneInfo('Asia/Kolkata'))
    if trade and trade.get('status') != 'CLOSED' and now.time() >= END_TIME:
        close_trade(api, trade, reason='Day-end square-off')
    return trade


def find_futures(api):
    """Find mini futures for the month of the option expiry we will trade."""
    today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    response = api.searchscrip(exchange='MCX', searchtext='CRUDEOILM')
    if not response or response.get('stat') != 'Ok':
        raise ValueError('CRUDEOILM search failed')

    expiries = set()
    futures = []
    for row in response.get('values', []):
        symbol = row.get('tsym', '')
        option = re.fullmatch(r'CRUDEOILM(\d{2}[A-Z]{3}\d{2})[CP]\d+(?:\.\d+)?', symbol)
        future = re.fullmatch(r'CRUDEOILM(\d{2}[A-Z]{3}\d{2})(?:F)?', symbol)
        if option:
            expiry = datetime.strptime(option[1], '%d%b%y').date()
            if expiry >= today:
                expiries.add(expiry)
        if future:
            expiry = datetime.strptime(future[1], '%d%b%y').date()
            if expiry >= today:
                futures.append({'expiry': expiry, 'symbol': symbol, 'token': str(row['token'])})

    if not expiries:
        raise ValueError('No option expiry found')
    expiry = min(expiries)
    if (expiry - today).days <= EXPIRY_ROLLOVER_DAYS:
        next_month = (expiry.replace(day=28) + timedelta(days=4)).replace(day=1)
        later = sorted(d for d in expiries if d.year == next_month.year and d.month == next_month.month)
        if not later:
            raise ValueError('Next-month options unavailable')
        expiry = later[0]
    for future in sorted(futures, key=lambda item: item['expiry']):
        if future['expiry'].year == expiry.year and future['expiry'].month == expiry.month:
            return future
    raise ValueError('Matching CRUDEOILM futures unavailable')


def get_futures_price(api, future):
    """Use the quote for the selected mini futures contract, not option premium."""
    quote = api.get_quotes(exchange='MCX', token=future['token'])
    if not quote or quote.get('stat') != 'Ok':
        raise ValueError('Futures quote unavailable')
    if (quote.get('tsym') != future['symbol']
            or str(quote.get('token')) != str(future['token'])
            or quote.get('exch') != 'MCX'):
        raise ValueError('Futures quote does not match the selected contract')
    price = float(quote['lp'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid futures price')
    return price


def check_trade(api, trade):
    """Check outstanding buys, broker SL fills and manual closures each minute."""
    if not trade or trade.get('status') == 'CLOSED':
        return trade
    if trade.get('exit_attempted'):
        close_trade(api, trade, reason=trade.get('exit_reason', 'Opposite WaveTrend alert'))
        return trade

    buy = get_trade_order(api, trade['order_id'], trade, 'B')
    if buy.get('status') in ('REJECTED', 'CANCELED', 'CANCELLED'):
        if buy.get('fillshares') is not None and int(buy['fillshares']) == 0:
            trade['status'] = 'CLOSED'
            trade['exit_reason'] = 'Buy ended without a fill'
            trade_notice(api, trade, 'BUY ended without fill', f"status={buy['status']}")
            return trade
    if buy.get('status') != 'COMPLETE':
        print('Buy is pending or partially filled; no new entry.', flush=True)
        trade_notice(api, trade, 'BUY awaiting full fill', 'SL not placed yet')
        return trade
    if int(buy.get('fillshares', 0)) != trade['quantity']:
        raise ValueError('Recorded buy quantity differs from the broker fill')
    trade['status'] = 'FILLED'
    trade['fill_price'] = float(buy['avgprc'])
    trade_notice(api, trade, 'BUY filled', f"price={trade['fill_price']}")

    stop_id = trade.get('sl_order_id')
    if stop_id:
        stop = get_trade_order(api, stop_id, trade, 'S')
        if stop.get('status') == 'COMPLETE' and int(stop.get('fillshares', 0)) == trade['quantity']:
            trade['status'] = 'CLOSED'
            trade['exit_reason'] = 'SL filled'
            print('Broker SL filled; trade closed.', flush=True)
            trade_notice(api, trade, 'SL filled', f'SL ID={stop_id}; trade closed')
            return trade
        if stop.get('status') in ('REJECTED', 'CANCELED', 'CANCELLED'):
            print('Broker SL is not active. Check this position.', flush=True)
            trade_notice(api, trade, 'SL inactive', f"status={stop['status']} | SL ID={stop_id}")

    positions = api.get_positions()
    if not isinstance(positions, list):
        raise ValueError('Position check unavailable')
    quantity = 0
    for position in positions:
        if (position.get('tsym') == trade['option']['symbol']
                and position.get('exch') == 'MCX' and position.get('prd') == trade['product']):
            quantity += int(position['netqty'])
    if quantity == 0:
        close_trade(api, trade, reason='Manually closed')  # Cancel any leftover SL.
        return trade
    if quantity < trade['quantity']:
        raise ValueError('Position reduced manually; check SL quantity before continuing')
    if not trade.get('sl_attempted'):
        now = datetime.now(ZoneInfo('Asia/Kolkata'))
        if datetime.fromisoformat(trade['time']).date() == now.date() and now.time() < END_TIME:
            apply_sl(api, trade)
    return trade


def reconcile_trade(api, trade):
    if not trade or trade.get('status') == 'CLOSED':
        return trade
    try:
        recover_order_ids(api, trade)
        return check_trade(api, trade)
    finally:
        save_state(trade)


def run(api):
    """Use main.py's login; poll once a minute without logging in again."""
    start_fresh(api)
    trade = None
    future = None
    feed_day = None
    last_candle = None
    last_error = None
    print('wt_cr started: LIVE | CRUDEOILM | 09:30–23:00 IST', flush=True)
    print('Fresh start: local state cleared. Previous broker positions and orders are not tracked.', flush=True)

    while True:
        now = datetime.now(ZoneInfo('Asia/Kolkata'))
        try:
            trade = reconcile_trade(api, trade)
            if trade and trade.get('status') != 'CLOSED' and datetime.fromisoformat(trade['time']).date() < now.date():
                close_trade(api, trade, reason='Missed previous day square-off')
                if trade.get('status') != 'CLOSED':
                    raise ValueError('Previous day position remains unresolved')
            # Square-off must run even if candle fetching is unavailable.
            if now.time() >= END_TIME:
                trade = day_end_exit(api, trade, now)
                if not trade or trade.get('status') == 'CLOSED':
                    print('Trading day finished.', flush=True)
                    return
            else:
                if trading_hours(now) and now.weekday() < 5:
                    if future is None or feed_day != now.date():
                        future = find_futures(api)
                        feed_day = now.date()
                        print(f"Futures: {future['symbol']} | token={future['token']}", flush=True)

                    candles = fetch_candles(api, future['token'])
                    alert = wt_cross(candles)
                    if alert and (last_candle is None or alert['time'] > last_candle):
                        candle_end = datetime.fromisoformat(alert['time']) + timedelta(minutes=TIMEFRAME_MINUTES)
                        if candle_end.date() != now.date() or now - candle_end > timedelta(minutes=TIMEFRAME_MINUTES, seconds=120):
                            raise ValueError('Latest completed candle is stale; no entry')
                        price = get_futures_price(api, future)
                        last_candle = alert['time']
                        saved_state['last_candle'] = last_candle
                        save_state(trade)
                        print(f"{now:%H:%M} | {future['symbol']}={price} | WT1={alert['wt1']} | signal={alert['signal']}", flush=True)
                        trade = handle_alert(api, alert, price, trade)
            save_state(trade)
            last_error = None
            reset = getattr(api, 'reset_notification', None)
            if callable(reset):
                reset('poll_error')
                reset('error')
        except StateError:
            print('Conflicting trade records. Runner stopped; check broker positions.', flush=True)
            notify(api, 'WT CRUDE stopped: conflicting trade records; check broker positions')
            raise
        except Exception as error:
            # Do not expose broker responses containing authentication details.
            message = f'wt_cr poll failed ({type(error).__name__}); will check again next minute'
            print(message, flush=True)
            if message != last_error:
                if notify(api, message, category='poll_error'):
                    last_error = message
        time.sleep(max(0.1, 60 - time.time() % 60))


def check_connection(api):
    """Read broker data only: no orders, cancellations or trade-state updates."""
    future = find_futures(api)
    print(f"Futures: {future['symbol']} | token={future['token']}", flush=True)
    candles = fetch_candles(api, future['token'])
    if not candles:
        raise ValueError('Broker returned no candles')
    result = wt_cross(candles)
    if result is None:
        raise ValueError('Not enough valid completed candles for WaveTrend')
    price = get_futures_price(api, future)
    call = find_atm_option(api, 'LONG', price)
    put = find_atm_option(api, 'SHORT', price)
    print(json.dumps({
        'read_only': True,
        'timeframe': TIMEFRAME_MINUTES,
        'candles': len(candles),
        'latest_completed': result,
        'futures_price': price,
        'atm_call': call,
        'atm_put': put,
    }, indent=2), flush=True)
    if call is None or put is None:
        raise ValueError('ATM option selection failed')
    print('Check finished. No orders placed or canceled; trade state unchanged.', flush=True)
