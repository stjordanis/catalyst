import re
from collections import defaultdict

import ccxt
import pandas as pd
import six
from catalyst.assets._assets import TradingPair
from redo import retry

from catalyst.algorithm import MarketOrder
from catalyst.constants import LOG_LEVEL
from catalyst.exchange.exchange import Exchange
from catalyst.exchange.exchange_bundle import ExchangeBundle
from catalyst.exchange.exchange_errors import InvalidHistoryFrequencyError, \
    ExchangeSymbolsNotFound, ExchangeRequestError, InvalidOrderStyle, \
    UnsupportedHistoryFrequencyError, \
    ExchangeNotFoundError, CreateOrderError, InvalidHistoryTimeframeError, \
    MarketsNotFoundError, InvalidMarketError
from catalyst.exchange.exchange_execution import ExchangeLimitOrder
from catalyst.exchange.utils.ccxt_utils import get_exchange_config
from catalyst.exchange.utils.datetime_utils import from_ms_timestamp, \
    get_epoch, \
    get_periods_range
from catalyst.exchange.utils.exchange_utils import get_catalyst_symbol
from catalyst.finance.order import Order, ORDER_STATUS
from catalyst.finance.transaction import Transaction
from ccxt import InvalidOrder, NetworkError, \
    ExchangeError
from logbook import Logger
from six import string_types

log = Logger('CCXT', level=LOG_LEVEL)

SUPPORTED_EXCHANGES = dict(
    binance=ccxt.binance,
    bitfinex=ccxt.bitfinex,
    bittrex=ccxt.bittrex,
    poloniex=ccxt.poloniex,
    bitmex=ccxt.bitmex,
    gdax=ccxt.gdax,
)


class CCXT(Exchange):
    def __init__(self, exchange_name, key,
                 secret, password, base_currency, config=None):
        log.debug(
            'finding {} in CCXT exchanges:\n{}'.format(
                exchange_name, ccxt.exchanges
            )
        )
        try:
            # Making instantiation as explicit as possible for code tracking.
            if exchange_name in SUPPORTED_EXCHANGES:
                exchange_attr = SUPPORTED_EXCHANGES[exchange_name]

            else:
                exchange_attr = getattr(ccxt, exchange_name)

            self.api = exchange_attr({
                'apiKey': key,
                'secret': secret,
                'password': password,
            })
            self.api.enableRateLimit = True
            self.has = self.api.has
            self.fees = self.api.fees

        except Exception:
            raise ExchangeNotFoundError(exchange_name=exchange_name)

        self._symbol_maps = [None, None]

        self.name = exchange_name
        self.assets = []

        self.base_currency = base_currency
        self.transactions = defaultdict(list)

        self.num_candles_limit = 2000
        self.max_requests_per_minute = 60
        self.low_balance_threshold = 0.1
        self.request_cpt = dict()
        self._common_symbols = dict()

        self.bundle = ExchangeBundle(self.name)
        self._is_init = False
        self._config = config

    def init(self):
        if self._is_init:
            return

        if self._config is None:
            self._config = get_exchange_config(self.name)
            log.debug(
                'got exchange config {}:\n{}'.format(
                    self.name, self._config
                )
            )

        self.load_assets()
        self._is_init = True

    def load_assets(self):
        if self._config is None:
            raise ValueError('Exchange config not available.')

        self.assets = []
        for asset_dict in self._config['assets']:
            asset = TradingPair(**asset_dict)
            self.assets.append(asset)

    def _fetch_markets(self):
        markets_symbols = self.api.load_markets()
        log.debug(
            'fetching {} markets:\n{}'.format(
                self.name, markets_symbols
            )
        )
        try:
            markets = self.api.fetch_markets()

        except NetworkError as e:
            raise ExchangeRequestError(error=e)

        if not markets:
            raise MarketsNotFoundError(
                exchange=self.name,
            )

        for market in markets:
            if 'id' not in market:
                raise InvalidMarketError(
                    exchange=self.name,
                    market=market,
                )
        return markets

    def create_exchange_config(self):
        config = dict(
            name=self.name,
            features=[feature for feature in self.has if self.has[feature]]
        )
        markets = retry(
            action=self._fetch_markets,
            attempts=5,
            sleeptime=5,
            retry_exceptions=(ExchangeRequestError,),
            cleanup=lambda: log.warn(
                'fetching markets again for {}'.format(self.name)
            ),
        )

        config['assets'] = []
        for market in markets:
            asset = self.create_trading_pair(market=market)
            config['assets'].append(asset)

        return config

    def create_trading_pair(self, market, start_dt=None, end_dt=None,
                            leverage=1, end_daily=None, end_minute=None):
        """
        Creating a TradingPair from market and asset data.

        Parameters
        ----------
        market: dict[str, Object]
        start_dt
        end_dt
        leverage
        end_daily
        end_minute

        Returns
        -------

        """
        params = dict(
            exchange=self.name,
            data_source='catalyst',
            exchange_symbol=market['id'],
            symbol=get_catalyst_symbol(market),
            start_date=start_dt,
            end_date=end_dt,
            leverage=leverage,
            asset_name=market['symbol'],
            end_daily=end_daily,
            end_minute=end_minute,
        )
        self.apply_conditional_market_params(params, market)

        return TradingPair(**params)

    def load_assets(self):
        if self._config is None or 'error' in self._config:
            raise ValueError('Exchange config not available.')

        self.assets = []
        for asset_dict in self._config['assets']:
            asset = TradingPair(**asset_dict)
            self.assets.append(asset)

    def account(self):
        return None

    def time_skew(self):
        return None

    def get_candle_frequencies(self, data_frequency=None):
        frequencies = []
        try:
            for timeframe in self.api.timeframes:
                freq = CCXT.get_frequency(timeframe, raise_error=False)

                # TODO: support all frequencies
                if data_frequency == 'minute' and not freq.endswith('T'):
                    continue

                elif data_frequency == 'daily' and not freq.endswith('D'):
                    continue

                frequencies.append(freq)

        except Exception as e:
            log.warn(
                'candle frequencies not available for exchange {}'.format(
                    self.name
                )
            )

        return frequencies

    def substitute_currency_code(self, currency, source='catalyst'):
        if source == 'catalyst':
            currency = currency.upper()

            key = self.api.common_currency_code(currency).lower()
            self._common_symbols[key] = currency.lower()
            return key

        else:
            if currency in self._common_symbols:
                return self._common_symbols[currency]

            else:
                return currency.lower()

    def get_symbol(self, asset_or_symbol, source='catalyst'):
        """
        The CCXT symbol.

        Parameters
        ----------
        asset_or_symbol
        source

        Returns
        -------

        """

        if source == 'ccxt':
            if isinstance(asset_or_symbol, string_types):
                parts = asset_or_symbol.split('/')
                return '{}_{}'.format(parts[0].lower(), parts[1].lower())

            else:
                return asset_or_symbol.symbol

        else:
            symbol = asset_or_symbol if isinstance(
                asset_or_symbol, string_types
            ) else asset_or_symbol.symbol

            parts = symbol.split('_')
            return '{}/{}'.format(parts[0].upper(), parts[1].upper())

    @staticmethod
    def map_frequency(value, source='ccxt', raise_error=True):
        """
        Map a frequency value between CCXT and Catalyst

        Parameters
        ----------
        value: str
        source: str
        raise_error: bool

        Returns
        -------

        Notes
        -----
        The Pandas offset aliases supported by Catalyst:
        Alias	Description
        W	weekly frequency
        M	month end frequency
        D	calendar day frequency
        H	hourly frequency
        T, min	minutely frequency

        The CCXT timeframes:
        '1m': '1minute',
        '1h': '1hour',
        '1d': '1day',
        '1w': '1week',
        '1M': '1month',
        '1y': '1year',
        """
        match = re.match(
            r'([0-9].*)?(m|M|d|D|h|H|T|w|W|min)', value, re.M | re.I
        )
        if match:
            candle_size = int(match.group(1)) \
                if match.group(1) else 1

            unit = match.group(2)

        else:
            raise ValueError('Unable to parse frequency or timeframe')

        if source == 'ccxt':
            if unit == 'd':
                result = '{}D'.format(candle_size)

            elif unit == 'm':
                result = '{}T'.format(candle_size)

            elif unit == 'h':
                result = '{}H'.format(candle_size)

            elif unit == 'w':
                result = '{}W'.format(candle_size)

            elif unit == 'M':
                result = '{}M'.format(candle_size)

            elif raise_error:
                raise InvalidHistoryTimeframeError(timeframe=value)

        else:
            if unit == 'D':
                result = '{}d'.format(candle_size)

            elif unit == 'min' or unit == 'T':
                result = '{}m'.format(candle_size)

            elif unit == 'H':
                result = '{}h'.format(candle_size)

            elif unit == 'W':
                result = '{}w'.format(candle_size)

            elif unit == 'M':
                result = '{}M'.format(candle_size)

            elif raise_error:
                raise InvalidHistoryFrequencyError(frequency=value)

        return result

    @staticmethod
    def get_timeframe(freq, raise_error=True):
        """
        The CCXT timeframe from the Catalyst frequency.

        Parameters
        ----------
        freq: str
            The Catalyst frequency (Pandas convention)

        Returns
        -------
        str

        """
        return CCXT.map_frequency(
            freq, source='catalyst', raise_error=raise_error
        )

    @staticmethod
    def get_frequency(timeframe, raise_error=True):
        """
        Test Catalyst frequency from the CCXT timeframe

        Catalyst uses the Pandas offset alias convention:
        http://pandas.pydata.org/pandas-docs/stable/timeseries.html#offset-aliases

        Parameters
        ----------
        timeframe

        Returns
        -------

        """
        return CCXT.map_frequency(
            timeframe, source='ccxt', raise_error=raise_error
        )

    def get_candles(self, freq, assets, bar_count=1, start_dt=None,
                    end_dt=None, floor_dates=True):
        is_single = (isinstance(assets, TradingPair))
        if is_single:
            assets = [assets]

        symbols = self.get_symbols(assets)
        timeframe = CCXT.get_timeframe(freq)

        if timeframe not in self.api.timeframes:
            freqs = [CCXT.get_frequency(t) for t in self.api.timeframes]
            raise UnsupportedHistoryFrequencyError(
                exchange=self.name,
                freq=freq,
                freqs=freqs,
            )

        if start_dt is not None and end_dt is not None:
            raise ValueError(
                'Please provide either start_dt or end_dt, not both.'
            )

        if start_dt is None:
            if end_dt is None:
                end_dt = pd.Timestamp.utcnow()

            dt_range = get_periods_range(
                end_dt=end_dt,
                periods=bar_count,
                freq=freq,
            )
            start_dt = dt_range[0]

        delta = start_dt - get_epoch()
        since = int(delta.total_seconds()) * 1000

        candles = dict()
        for index, asset in enumerate(assets):
            ohlcvs = self.api.fetch_ohlcv(
                symbol=symbols[index],
                timeframe=timeframe,
                since=since,
                limit=bar_count,
                params={}
            )

            candles[asset] = []
            for ohlcv in ohlcvs:
                dt = pd.to_datetime(ohlcv[0], unit='ms', utc=True)
                if floor_dates:
                    dt = dt.floor('1T')

                candles[asset].append(
                    dict(
                        last_traded=dt,
                        open=ohlcv[1],
                        high=ohlcv[2],
                        low=ohlcv[3],
                        close=ohlcv[4],
                        volume=ohlcv[5],
                    )
                )
            candles[asset] = sorted(
                candles[asset], key=lambda c: c['last_traded']
            )

        if is_single:
            return six.next(six.itervalues(candles))

        else:
            return candles

    def _fetch_symbol_map(self, is_local):
        try:
            return self.fetch_symbol_map(is_local)

        except ExchangeSymbolsNotFound:
            return None

    def apply_conditional_market_params(self, params, market):
        """
        Applies a CCXT market dict to parameters of TradingPair init.

        Parameters
        ----------
        params: dict[Object]
        market: dict[Object]

        Returns
        -------

        """
        # TODO: make this more externalized / configurable
        # Consider representing in some type of JSON structure
        if 'active' in market:
            params['trading_state'] = 1 if market['active'] else 0

        else:
            params['trading_state'] = 1

        if 'lot' in market:
            params['min_trade_size'] = market['lot']
            params['lot'] = market['lot']

        if self.name == 'bitfinex':
            params['maker'] = 0.001
            params['taker'] = 0.002

        elif 'maker' in market and 'taker' in market \
            and market['maker'] is not None \
            and market['taker'] is not None:
            params['maker'] = market['maker']
            params['taker'] = market['taker']

        else:
            # TODO: default commission, make configurable
            params['maker'] = 0.0015
            params['taker'] = 0.0025

        info = market['info'] if 'info' in market else None
        if info:
            if 'minimum_order_size' in info:
                params['min_trade_size'] = float(info['minimum_order_size'])

                if 'lot' not in params:
                    params['lot'] = params['min_trade_size']

    def get_balances(self):
        try:
            log.debug('retrieving wallets balances')
            balances = self.api.fetch_balance()

            balances_lower = dict()
            for key in balances:
                balances_lower[key.lower()] = balances[key]

        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to fetch balance {}: {}'.format(
                    self.name, e
                )
            )
            raise ExchangeRequestError(error=e)

        return balances_lower

    def _create_order(self, order_status):
        """
        Create a Catalyst order object from a CCXT order dictionary

        Parameters
        ----------
        order_status: dict[str, Object]
            The order dict from the CCXT api.

        Returns
        -------
        Order
            The Catalyst order object

        """
        order_id = order_status['id']
        symbol = self.get_symbol(order_status['symbol'], source='ccxt')
        asset = self.get_asset(symbol)

        s = order_status['status']
        amount = order_status['amount']
        filled = order_status['filled']

        if s == 'canceled' or (s == 'closed' and filled == 0):
            status = ORDER_STATUS.CANCELLED

        elif s == 'closed' and filled > 0:
            if filled < amount:
                log.warn(
                    'order {id} is executed but only partially filled:'
                    ' {filled} {symbol} out of {amount}'.format(
                        id=order_status['status'],
                        filled=order_status['filled'],
                        symbol=asset.symbol,
                        amount=order_status['amount'],
                    )
                )
            else:
                log.info(
                    'order {id} executed in full: {filled} {symbol}'.format(
                        id=order_id,
                        filled=filled,
                        symbol=asset.symbol,
                    )
                )

            status = ORDER_STATUS.FILLED

        elif s == 'open':
            status = ORDER_STATUS.OPEN

        elif filled > 0:
            log.info(
                'order {id} partially filled: {filled} {symbol} out of '
                '{amount}, waiting for complete execution'.format(
                    id=order_id,
                    filled=filled,
                    symbol=asset.symbol,
                    amount=amount,
                )
            )
            status = ORDER_STATUS.OPEN

        else:
            log.warn(
                'invalid state {} for order {}'.format(
                    s, order_id
                )
            )
            status = ORDER_STATUS.OPEN

        if order_status['side'] == 'sell':
            amount = -amount
            filled = -filled

        price = order_status['price']
        order_type = order_status['type']

        limit_price = price if order_type == 'limit' else None

        executed_price = order_status['cost'] / order_status['amount']
        commission = order_status['fee']
        date = from_ms_timestamp(order_status['timestamp'])

        order = Order(
            dt=date,
            asset=asset,
            amount=amount,
            stop=None,
            limit=limit_price,
            filled=filled,
            id=order_id,
            commission=commission
        )
        order.status = status

        return order, executed_price

    def create_order(self, asset, amount, is_buy, style):
        symbol = self.get_symbol(asset)

        if isinstance(style, ExchangeLimitOrder):
            price = style.get_limit_price(is_buy)
            order_type = 'limit'

        elif isinstance(style, MarketOrder):
            price = None
            order_type = 'market'

        else:
            raise InvalidOrderStyle(
                exchange=self.name,
                style=style.__class__.__name__
            )

        side = 'buy' if amount > 0 else 'sell'
        if hasattr(self.api, 'amount_to_lots'):
            adj_amount = self.api.amount_to_lots(
                symbol=symbol,
                amount=abs(amount),
            )
            if adj_amount != abs(amount):
                log.info(
                    'adjusted order amount {} to {} based on lot size'.format(
                        abs(amount), adj_amount,
                    )
                )

        else:
            adj_amount = round(abs(amount), asset.decimals)

        try:
            result = self.api.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=adj_amount,
                price=price
            )
        except InvalidOrder as e:
            log.warn('the exchange rejected the order: {}'.format(e))
            raise CreateOrderError(exchange=self.name, error=e)

        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to create order {} / {}: {}'.format(
                    self.name, symbol, e
                )
            )
            raise ExchangeRequestError(error=e)

        exchange_amount = None
        if 'amount' in result and result['amount'] != adj_amount:
            exchange_amount = result['amount']

        elif 'info' in result:
            if 'origQty' in result['info']:
                exchange_amount = float(result['info']['origQty'])

        if exchange_amount:
            log.info(
                'order amount adjusted by {} from {} to {}'.format(
                    self.name, adj_amount, exchange_amount
                )
            )
            adj_amount = exchange_amount

        if 'info' not in result:
            raise ValueError('cannot use order without info attribute')

        final_amount = adj_amount if side == 'buy' else -adj_amount
        order_id = result['id']
        order = Order(
            dt=pd.Timestamp.utcnow(),
            asset=asset,
            amount=final_amount,
            stop=style.get_stop_price(is_buy),
            limit=style.get_limit_price(is_buy),
            id=order_id
        )
        return order

    def get_open_orders(self, asset):
        try:
            symbol = self.get_symbol(asset)
            result = self.api.fetch_open_orders(
                symbol=symbol,
                since=None,
                limit=None,
                params=dict()
            )
        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to fetch open orders {} / {}: {}'.format(
                    self.name, asset.symbol, e
                )
            )
            raise ExchangeRequestError(error=e)

        orders = []
        for order_status in result:
            order, _ = self._create_order(order_status)
            if asset is None or asset == order.sid:
                orders.append(order)

        return orders

    def _process_order_fallback(self, order):
        """
        Fallback method for exchanges which do not play nice with
        fetch-my-trades. Apparently, about 60% of exchanges will return
        the correct executed values with this method. Others will support
        fetch-my-trades.

        Parameters
        ----------
        order: Order

        Returns
        -------
        float

        """
        exc_order, price = self.get_order(
            order.id, order.asset, return_price=True
        )
        order.status = exc_order.status
        order.commission = exc_order.commission
        order.filled = exc_order.amount

        transactions = []
        if exc_order.status == ORDER_STATUS.FILLED:
            if order.amount > exc_order.amount:
                log.warn(
                    'executed order amount {} differs '
                    'from original'.format(
                        exc_order.amount, order.amount
                    )
                )

            order.check_triggers(
                price=price,
                dt=exc_order.dt,
            )
            transaction = Transaction(
                asset=order.asset,
                amount=order.amount,
                dt=pd.Timestamp.utcnow(),
                price=price,
                order_id=order.id,
                commission=order.commission,
            )
            transactions.append(transaction)

        return transactions

    def process_order(self, order):
        # TODO: move to parent class after tracking features in the parent
        if not self.api.has['fetchMyTrades']:
            return self._process_order_fallback(order)

        try:
            all_trades = self.get_trades(order.asset)
        except ExchangeRequestError as e:
            log.warn(
                'unable to fetch account trades, trying an alternate '
                'method to find executed order {} / {}: {}'.format(
                    order.id, order.asset.symbol, e
                )
            )
            return self._process_order_fallback(order)

        transactions = []
        trades = [t for t in all_trades if t['order'] == order.id]
        if not trades:
            log.debug(
                'order {} / {} not found in trades'.format(
                    order.id, order.asset.symbol
                )
            )
            return transactions

        trades.sort(key=lambda t: t['timestamp'], reverse=False)
        order.filled = 0
        order.commission = 0
        for trade in trades:
            # status property will update automatically
            filled = trade['amount'] * order.direction
            order.filled += filled

            commission = 0
            if 'fee' in trade and 'cost' in trade['fee']:
                commission = trade['fee']['cost']
                order.commission += commission

            order.check_triggers(
                price=trade['price'],
                dt=pd.to_datetime(trade['timestamp'], unit='ms', utc=True),
            )
            transaction = Transaction(
                asset=order.asset,
                amount=filled,
                dt=pd.Timestamp.utcnow(),
                price=trade['price'],
                order_id=order.id,
                commission=commission
            )
            transactions.append(transaction)

        order.broker_order_id = ', '.join([t['id'] for t in trades])
        return transactions

    def get_order(self, order_id, asset_or_symbol=None, return_price=False):
        if asset_or_symbol is None:
            log.debug(
                'order not found in memory, the request might fail '
                'on some exchanges.'
            )
        try:
            symbol = self.get_symbol(asset_or_symbol) \
                if asset_or_symbol is not None else None
            order_status = self.api.fetch_order(id=order_id, symbol=symbol)
            order, executed_price = self._create_order(order_status)

            if return_price:
                return order, executed_price

            else:
                return order

        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to fetch order {} / {}: {}'.format(
                    self.name, order_id, e
                )
            )
            raise ExchangeRequestError(error=e)

    def cancel_order(self, order_param,
                     asset_or_symbol=None, params={}):
        order_id = order_param.id \
            if isinstance(order_param, Order) else order_param

        if asset_or_symbol is None:
            log.debug(
                'order not found in memory, cancelling order might fail '
                'on some exchanges.'
            )
        try:
            symbol = self.get_symbol(asset_or_symbol) \
                if asset_or_symbol is not None else None
            self.api.cancel_order(id=order_id,
                                  symbol=symbol, params=params)

        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to cancel order {} / {}: {}'.format(
                    self.name, order_id, e
                )
            )
            raise ExchangeRequestError(error=e)

    def tickers(self, assets, on_ticker_error='raise'):
        """
        Retrieve current tick data for the given assets

        Parameters
        ----------
        assets: list[TradingPair]

        Returns
        -------
        list[dict[str, float]

        """
        if len(assets) == 1:
            try:
                symbol = self.get_symbol(assets[0])
                log.debug('fetching single ticker: {}'.format(symbol))
                results = dict()
                results[symbol] = self.api.fetch_ticker(symbol=symbol)

            except (ExchangeError, NetworkError,) as e:
                log.warn(
                    'unable to fetch ticker {} / {}: {}'.format(
                        self.name, symbol, e
                    )
                )
                raise ExchangeRequestError(error=e)

        elif len(assets) > 1:
            symbols = self.get_symbols(assets)
            try:
                log.debug('fetching multiple tickers: {}'.format(symbols))
                results = self.api.fetch_tickers(symbols=symbols)

            except (ExchangeError, NetworkError) as e:
                log.warn(
                    'unable to fetch tickers {} / {}: {}'.format(
                        self.name, symbols, e
                    )
                )
                raise ExchangeRequestError(error=e)
        else:
            raise ValueError('Cannot request tickers with not assets.')

        tickers = dict()
        for asset in assets:
            symbol = self.get_symbol(asset)
            if symbol not in results:
                msg = 'ticker not found {} / {}'.format(
                    self.name, symbol
                )
                log.warn(msg)
                if on_ticker_error == 'warn':
                    continue
                else:
                    raise ExchangeRequestError(error=msg)

            ticker = results[symbol]
            ticker['last_traded'] = from_ms_timestamp(ticker['timestamp'])

            if 'last_price' not in ticker:
                # TODO: any more exceptions?
                ticker['last_price'] = ticker['last']

            if 'baseVolume' in ticker and ticker['baseVolume'] is not None:
                # Using the volume represented in the base currency
                ticker['volume'] = ticker['baseVolume']

            elif 'info' in ticker and 'bidQty' in ticker['info'] \
                and 'askQty' in ticker['info']:
                ticker['volume'] = float(ticker['info']['bidQty']) + \
                                   float(ticker['info']['askQty'])

            else:
                ticker['volume'] = 0

            tickers[asset] = ticker

        return tickers

    def get_account(self):
        return None

    def get_orderbook(self, asset, order_type='all', limit=None):
        ccxt_symbol = self.get_symbol(asset)

        params = dict()
        if limit is not None:
            params['depth'] = limit

        order_book = self.api.fetch_order_book(ccxt_symbol, params)

        order_types = ['bids', 'asks'] if order_type == 'all' else [order_type]
        result = dict(last_traded=from_ms_timestamp(order_book['timestamp']))
        for index, order_type in enumerate(order_types):
            if limit is not None and index > limit - 1:
                break

            result[order_type] = []
            for entry in order_book[order_type]:
                result[order_type].append(dict(
                    rate=float(entry[0]),
                    quantity=float(entry[1])
                ))

        return result

    def get_trades(self, asset, my_trades=True, start_dt=None, limit=100):
        # TODO: is it possible to sort this? Limit is useless otherwise.
        ccxt_symbol = self.get_symbol(asset)
        if start_dt:
            delta = start_dt - get_epoch()
            since = int(delta.total_seconds()) * 1000
        else:
            since = None

        try:
            if my_trades:
                trades = self.api.fetch_my_trades(
                    symbol=ccxt_symbol,
                    since=since,
                    limit=limit,
                )
            else:
                trades = self.api.fetch_trades(
                    symbol=ccxt_symbol,
                    since=since,
                    limit=limit,
                )
        except (ExchangeError, NetworkError) as e:
            log.warn(
                'unable to fetch trades {} / {}: {}'.format(
                    self.name, asset.symbol, e
                )
            )
            raise ExchangeRequestError(error=e)

        return trades
