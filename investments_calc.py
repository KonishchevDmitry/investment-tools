#!/usr/bin/env python3
#
# requirements.txt: requests termcolor

"""Investments distribution calculator"""

import argparse
import logging
import math
import operator

from decimal import Decimal
from typing import List

import requests

from termcolor import colored

import pcli.log

log = logging.getLogger()


class Actions:
    SHOW = "show"
    REBALANCE = "rebalance"

    ALL = [SHOW, REBALANCE]


class Currency:
    USD = "usd"
    RUB = "rub"


class CommissionSpec:
    def __init__(self, *, minimum, percent=None, per_share=None, maximum_percent=None):
        self.__minimum = Decimal(minimum)
        self.__percent = None if percent is None else Decimal(percent)
        self.__per_share = None if per_share is None else Decimal(per_share)
        self.__maximum_percent = None if maximum_percent is None else Decimal(maximum_percent)

    def calculate(self, shares, price):
        commissions = Decimal()

        if self.__percent is not None:
            commissions += shares * price * self.__percent / 100

        if self.__per_share is not None:
            commissions += shares * self.__per_share

        if self.__maximum_percent is not None:
            commissions = min(commissions, shares * price * self.__maximum_percent / 100)

        return max(self.__minimum, commissions)


class Holding:
    def __init__(self, name, weight, ticker=None, shares=None, holdings=None):
        if (ticker is not None) == bool(holdings):
            raise Error("Invalid holding {!r}: either ticket or group's holdings must be specified.", name)

        if (ticker is None) != (shares is None):
            raise Error("Invalid holding {!r}: ticker must be specified with shares.", name)

        if ticker is not None:
            name = "{} ({})".format(name, ticker)

        self.ticker = ticker
        self.name = name

        self.expected_weight = Decimal(weight.rstrip("%")) / 100
        self.weight = self.expected_weight

        if self.is_group:
            self.holdings = holdings
        else:
            self.current_shares = shares
            self.shares = shares
            self.commission = 0

            self.price = None

        self.current_value = None
        self.value = None

        self.minimum_value = None
        self.selling_restricted = None
        self.sell_blocked = False

        self.maximum_value = None
        self.buying_restricted = None
        self.buy_blocked = False

    @property
    def short_name(self):
        return self.name if self.ticker is None else self.ticker

    @property
    def is_group(self):
        return self.ticker is None

    def restrict_selling(self, restrict=True):
        if self.is_group:
            apply_restriction(self.holdings, "selling_restricted", restrict)
        else:
            self.selling_restricted = restrict

        return self

    def restrict_buying(self, restrict=True):
        if self.is_group:
            apply_restriction(self.holdings, "buying_restricted", restrict)
        else:
            self.buying_restricted = restrict

        return self

    def change(self, reason, shares, commission_spec: CommissionSpec):
        if shares != self.shares:
            log.debug("%s shares: %s -> %s (%s).", self.short_name, self.shares, shares, reason)

        self.commission = self.commission_for(shares, commission_spec)
        self.shares = shares
        self.value = shares * self.price

    def set_weight(self, reason, weight):
        if weight != self.weight:
            log.debug("%s weight: %s -> %s (%s).", self.short_name, self.weight, weight, reason)

        self.weight = weight

    def on_buy_blocked(self, reason):
        log.debug("%s: buy blocked: %s.", self.short_name, reason)
        self.buy_blocked = True

    def on_sell_blocked(self, reason):
        log.debug("%s: sell blocked: %s.", self.short_name, reason)
        self.sell_blocked = True

    def commission_for(self, shares, commission_spec: CommissionSpec):
        return commission_spec.calculate(abs(shares - self.current_shares), self.price)


class Portfolio:
    def __init__(self, name, currency, commission_spec: CommissionSpec, *, holdings: List[Holding], free_assets,
                 min_free_assets, min_trade_volume=0, free_commissions=None):
        self.name = name
        self.currency = currency
        self.commission_spec = commission_spec
        self.holdings = holdings
        self.free_assets = Decimal(free_assets)
        self.min_free_assets = Decimal(min_free_assets)
        self.min_trade_volume = Decimal(min_trade_volume)
        self.free_commissions = None if free_commissions is None else Decimal(free_commissions)

    def restrict_selling(self, restrict=True):
        apply_restriction(self.holdings, "selling_restricted", restrict)
        return self

    def restrict_buying(self, restrict=True):
        apply_restriction(self.holdings, "buying_restricted", restrict)
        return self


class Error(Exception):
    def __init__(self, *args):
        message, args = args[0], args[1:]
        super().__init__(message.format(*args) if args else message)


class LogicalError(Error):
    def __init__(self):
        super().__init__("Logical error.")


def apply_restriction(holdings: List[Holding], name, value):
    for holding in holdings:
        if holding.is_group:
            apply_restriction(holding.holdings, name, value)
        else:
            if not hasattr(holding, name):
                raise LogicalError()

            if getattr(holding, name) is None:
                setattr(holding, name, value)


def calculate(portfolio: Portfolio, *, fake_prices=False):
    tickers = set()

    def process(name, holdings: List[Holding]):
        if holdings and sum(holding.expected_weight for holding in holdings) != 1:
            raise Error("Invalid weights for {!r}.", name)

        for holding in holdings:
            if holding.is_group:
                process(holding.name, holding.holdings)
            else:
                tickers.add(holding.ticker)

    process(portfolio.name, portfolio.holdings)

    if fake_prices:
        prices = {ticker: Decimal(1) for ticker in tickers}
    else:
        prices = get_prices(tickers)

    current_value = calculate_current_value(portfolio.holdings, prices)
    total_assets = current_value + portfolio.free_assets
    rebalance_to = total_assets - portfolio.min_free_assets

    if not fake_prices:
        calculate_restrictions(portfolio.holdings)
        correct_weights_for_buying_restriction(portfolio.holdings, rebalance_to)  # TODO: Display underuse?
        correct_weights_for_selling_restriction(portfolio.holdings, rebalance_to)  # TODO: Display overuse?

    rebalanced_value = rebalance(portfolio.holdings, rebalance_to, portfolio.commission_spec, portfolio.min_trade_volume)
    commissions = calculate_total_commissions(portfolio.holdings)
    free_assets = total_assets - rebalanced_value - commissions
    free_assets_to_distribute = free_assets - portfolio.min_free_assets

    for underfunded_only in (True, False):
        while free_assets_to_distribute > 0:
            free_assets_after_distribution = distribute_free_assets(
                portfolio.holdings, rebalanced_value, free_assets_to_distribute,
                portfolio.commission_spec, portfolio.min_trade_volume, underfunded_only)

            if free_assets_after_distribution == free_assets_to_distribute:
                break

            free_assets_to_distribute = free_assets_after_distribution

    free_assets = free_assets_to_distribute + portfolio.min_free_assets
    rebalanced_value = sum(holding.value for holding in portfolio.holdings)
    commissions = calculate_total_commissions(portfolio.holdings)

    return rebalanced_value, free_assets, commissions


def calculate_current_value(holdings: List[Holding], prices):
    total_value = Decimal()

    for holding in holdings:
        if holding.is_group:
            holding.value = holding.current_value = calculate_current_value(holding.holdings, prices)
        else:
            holding.price = prices[holding.ticker]
            holding.value = holding.current_value = holding.current_shares * holding.price

        total_value += holding.current_value

    return total_value


def calculate_restrictions(holdings: List[Holding]):
    total_minimum_value = None
    total_maximum_value = None

    maximum_values = []

    for holding in holdings:
        if holding.is_group:
            holding.minimum_value, holding.maximum_value = calculate_restrictions(holding.holdings)
        else:
            if holding.selling_restricted:
                holding.minimum_value = holding.current_value

            if holding.buying_restricted:
                holding.maximum_value = holding.current_value

        if holding.minimum_value is not None:
            if total_minimum_value is None:
                total_minimum_value = holding.minimum_value
            else:
                total_minimum_value += holding.minimum_value

        if holding.maximum_value is not None:
            maximum_values.append(holding.maximum_value)

    if len(maximum_values) == len(holdings):
        total_maximum_value = sum(maximum_values)

    return total_minimum_value, total_maximum_value


def correct_weights_for_selling_restriction(holdings: List[Holding], expected_total_value):
    while True:
        succeeded = True
        total_assets_overuse = Decimal()
        correctable_holdings = []

        for holding in holdings:
            expected_value = expected_total_value * holding.weight

            if holding.minimum_value is not None and expected_value <= holding.minimum_value:
                total_assets_overuse += holding.minimum_value - expected_value
            else:
                correctable_holdings.append(holding)

        if total_assets_overuse > 0:
            if correctable_holdings:
                expected_value = Decimal()
                for holding in correctable_holdings:
                    expected_value += expected_total_value * holding.weight

                correction_multiplicator = (expected_value - total_assets_overuse) / expected_value

                for holding in correctable_holdings:
                    corrected_weight = holding.weight * correction_multiplicator

                    if (
                        holding.minimum_value is not None and
                        expected_total_value * corrected_weight < holding.minimum_value
                    ):
                        corrected_weight = holding.minimum_value / expected_total_value
                        succeeded = False

                    holding.set_weight("selling restrictions", corrected_weight)
            else:
                succeeded = False

        if not succeeded and correctable_holdings:
            continue

        for holding in holdings:
            if holding.is_group:
                succeeded &= correct_weights_for_selling_restriction(
                    holding.holdings, expected_total_value * holding.weight)

        return succeeded


def correct_weights_for_buying_restriction(holdings: List[Holding], expected_total_value):
    while True:
        succeeded = True
        extra_assets = Decimal()
        correctable_holdings = []

        for holding in holdings:
            expected_value = expected_total_value * holding.weight

            if holding.maximum_value is not None and expected_value >= holding.maximum_value:
                extra_assets += expected_value - holding.maximum_value
            else:
                correctable_holdings.append(holding)

        if extra_assets:
            if correctable_holdings:
                expected_value = Decimal()
                for holding in correctable_holdings:
                    expected_value += expected_total_value * holding.weight

                correction_multiplicator = (expected_value + extra_assets) / expected_value

                for holding in correctable_holdings:
                    corrected_weight = holding.weight * correction_multiplicator

                    if (
                        holding.maximum_value is not None and
                        expected_total_value * corrected_weight > holding.maximum_value
                    ):
                        corrected_weight = holding.maximum_value / expected_total_value
                        succeeded = False

                    holding.set_weight("buying restrictions", corrected_weight)
            else:
                succeeded = False

        if not succeeded and correctable_holdings:
            continue

        for holding in holdings:
            if holding.is_group:
                succeeded &= correct_weights_for_buying_restriction(
                    holding.holdings, expected_total_value * holding.weight)

        return succeeded


def rebalance(holdings: List[Holding], expected_total_value, commission_spec: CommissionSpec, min_trade_volume):
    total_value = Decimal()

    for holding in holdings:
        expected_value = expected_total_value * holding.weight

        if holding.is_group:
            holding.value = rebalance(holding.holdings, expected_value, commission_spec, min_trade_volume)
        else:
            if holding.shares != holding.current_shares:
                raise LogicalError()

            current_weight = get_weight(expected_total_value, holding.current_value)

            if current_weight != holding.weight:
                rebalanced_shares = expected_value // holding.price

                if rebalanced_shares != holding.current_shares:
                    commission = holding.commission_for(rebalanced_shares, commission_spec)
                    rebalanced_shares = (expected_value - commission) // holding.price

                if rebalanced_shares != holding.current_shares:
                    if rebalanced_shares < holding.current_shares and holding.selling_restricted:
                        holding.on_sell_blocked("selling is restricted")
                    elif rebalanced_shares > holding.current_shares and holding.buying_restricted:
                        holding.on_buy_blocked("buying is restricted")
                    elif abs(rebalanced_shares - holding.current_shares) * holding.price < min_trade_volume:
                        if rebalanced_shares < holding.current_shares:
                            holding.on_sell_blocked("min trade volume restriction")
                        else:
                            holding.on_buy_blocked("min trade volume restriction")
                    else:
                        holding.change("rebalancing", rebalanced_shares, commission_spec)

        total_value += holding.value

    return total_value


def calculate_total_commissions(holdings: List[Holding]):
    commissions = 0

    for holding in holdings:
        if holding.is_group:
            commissions += calculate_total_commissions(holding.holdings)
        else:
            commissions += holding.commission

    return commissions


def distribute_free_assets(
    holdings: List[Holding], expected_total_value, free_assets, commission_spec: CommissionSpec, min_trade_volume,
    underfunded_only
):
    def difference_from_expected(holding: Holding):
        expected_value = expected_total_value * holding.expected_weight
        if expected_value == 0:
            return -holding.value
        else:
            return (expected_value - holding.value) / expected_value

    for holding in sorted(holdings, key=difference_from_expected, reverse=True):
        if free_assets <= 0:
            return free_assets

        expected_value = expected_total_value * holding.expected_weight

        if holding.is_group:
            free_assets = distribute_free_assets(
                holding.holdings, expected_value, free_assets, commission_spec, min_trade_volume, underfunded_only)
            holding.value = sum(holding.value for holding in holding.holdings)
        elif not underfunded_only or holding.value < expected_value:
            previous_commission = holding.commission
            extra_shares = (free_assets + previous_commission) // holding.price
            extra_shares = limit_extra_shares_to_minimum(holding, extra_shares, min_trade_volume)

            if extra_shares > 0:
                commission = commission_spec.calculate(
                    abs(holding.shares + extra_shares - holding.current_shares), holding.price)
                extra_shares = (free_assets + previous_commission - commission) // holding.price
                extra_shares = limit_extra_shares_to_minimum(holding, extra_shares, min_trade_volume)

            if extra_shares > 0:
                result_shares = holding.shares + extra_shares

                if not (
                    result_shares < holding.current_shares and holding.selling_restricted or
                    result_shares > holding.current_shares and holding.buying_restricted or
                    abs(result_shares - holding.current_shares) * holding.price < min_trade_volume
                ):
                    holding.change("free assets distribution", result_shares, commission_spec)
                    free_assets -= extra_shares * holding.price - (holding.commission - previous_commission)

    return free_assets


def limit_extra_shares_to_minimum(holding: Holding, extra_shares, min_trade_volume):
    min_trade_shares = math.ceil(min_trade_volume / holding.price)

    min_extra_shares = min_trade_shares
    if holding.shares > holding.current_shares:
        min_extra_shares -= holding.shares - holding.current_shares
    min_extra_shares = max(1, min_extra_shares)

    return min(extra_shares, min_extra_shares)


def flatify(holdings: List[Holding], expected_weight, weight):
    flat_holdings = []

    for holding in holdings:
        holding.expected_weight *= expected_weight
        holding.weight *= weight

        if holding.is_group:
            flat_holdings.extend(flatify(holding.holdings, holding.expected_weight, holding.weight))
        else:
            flat_holdings.append(holding)

    return flat_holdings


def show(action, portfolio: Portfolio, holdings: List[Holding], expected_total_value, *, depth=0):
    for holding in sorted(holdings, key=operator.attrgetter("weight"), reverse=True):
        title = "{indent}* {name}".format(indent="  " * depth, name=colorify_name(holding.name))
        expected_value = expected_total_value * holding.weight

        if action != Actions.SHOW:
            if holding.sell_blocked:
                title += colorify_freeze(" [sell blocked]")

            if holding.buy_blocked:
                title += colorify_freeze(" [buy blocked]")

            title += " -"

            if not holding.is_group:
                title += " " + format_shares(holding.current_shares)

            current_weight = get_weight(expected_total_value, holding.current_value)
            title += " {current_weight} ({current_value})".format(
                current_weight=format_weight(current_weight),
                current_value=format_assets(holding.current_value, portfolio.currency))

            if holding.value != holding.current_value:
                if not holding.is_group:
                    colorify_func = colorify_buy if holding.value > holding.current_value else colorify_sell
                    title += colorify_func(" {shares_change} ({value_change})".format(
                        shares_change=format_shares(holding.shares - holding.current_shares, sign=True),
                        value_change=format_assets(abs(holding.value - holding.current_value), portfolio.currency)))

                title += " → {result_weight} ({result_value})".format(
                    result_weight=format_weight(get_weight(expected_total_value, holding.value)),
                    result_value=format_assets(holding.value, portfolio.currency))

        title += " " + ("-" if action == Actions.SHOW else "/")
        title += " {expected_weight} ({expected_value})".format(
            expected_weight=format_weight(holding.expected_weight),
            expected_value=format_assets(expected_total_value * holding.expected_weight, portfolio.currency))

        if holding.is_group:
            title += ":"

        print(title)
        if holding.is_group:
            show(action, portfolio, holding.holdings, expected_value, depth=depth + 1)


def get_weight(assets, value):
    if assets == 0:
        return Decimal(1)
    else:
        return value / assets


def format_shares(shares, sign=False):
    format_string = "{"
    if sign:
        format_string += ":+"
    format_string += "}s"
    return format_string.format(shares)


def format_assets(assets, currency):
    string = str(int(assets))

    if currency == Currency.USD:
        string = "$" + string
    elif currency == Currency.RUB:
        string += "₽"
    else:
        raise LogicalError()

    return string


def format_weight(weight):
    return ("{:.1f}".format(weight * 100).rstrip("0").rstrip(".") or "0") + "%"


def colorify_name(string):
    return colored(string, attrs=["bold"])


def colorify_freeze(string):
    return colored(string, "blue")


def colorify_buy(string):
    return colored(string, "green")


def colorify_sell(string):
    return colored(string, "red")


def colorify_weight(string):
    return colored(string, "magenta", attrs=["bold"])


def colorify_warning(string):
    return colored(string, "red")


def get_prices(tickers):
    prices = {}
    if not tickers:
        return prices

    response = requests.get("https://www.alphavantage.co/query", params={
        "function": "BATCH_STOCK_QUOTES",
        "symbols": ",".join(tickers),
        "apikey": "api-key-stub"
    })
    response.raise_for_status()
    result = response.json()

    if "Error Message" in result:
        raise Error("Unable to get tickers info: {}", result["Error Message"])

    for quote in result["Stock Quotes"]:
        prices[quote["1. symbol"]] = Decimal(quote["2. price"])

    unknown_tickers = set(tickers) - set(prices)
    if not unknown_tickers:
        return prices

    # See http://iss.moex.com/iss/reference/
    # HTML output: https://iss.moex.com/iss/engines/stock/markets/shares/securities?securities=FXMM,FXRB
    response = requests.get("https://iss.moex.com/iss/engines/stock/markets/shares/securities.json", params={
        "securities": ",".join(unknown_tickers),
    })
    response.raise_for_status()
    result = response.json()

    market_data = result["marketdata"]
    columns = market_data["columns"]
    ticker_column_id = columns.index("SECID")
    board_column_id = columns.index("BOARDID")
    last_price_column_id = columns.index("LAST")
    last_current_price_column_id = columns.index("LCURRENTPRICE")

    for data in market_data["data"]:
        ticker = data[ticker_column_id]
        if ticker in unknown_tickers and data[board_column_id] == "TQTF":
            price = data[last_price_column_id]
            if price is None:
                price = data[last_current_price_column_id]
            prices[ticker] = Decimal(price)

    unknown_tickers = set(tickers) - set(prices)
    if unknown_tickers:
        raise Error("Unable to get info for the following tickers: {}.", ", ".join(unknown_tickers))

    return prices


def process_portfolio(action, portfolio: Portfolio, flat_view):
    print(colorify_name(portfolio.name + ":"))

    total_value, free_assets, commissions = calculate(portfolio, fake_prices=action == Actions.SHOW)
    if action == Actions.SHOW:
        total_value, free_assets, commissions = portfolio.free_assets, 0, 0

    if flat_view:
        portfolio.holdings = flatify(portfolio.holdings, Decimal(1), Decimal(1))
        portfolio.holdings.sort(key=lambda holding: holding.value, reverse=True)

    show(action, portfolio, portfolio.holdings, total_value)

    if action == Actions.REBALANCE:
        print()
        print(colorify_name("Total value: ") + format_assets(total_value, portfolio.currency))

        formatted_free_assets = format_assets(free_assets, portfolio.currency)
        if free_assets < portfolio.min_free_assets:
            formatted_free_assets = colorify_warning(formatted_free_assets)
        print(colorify_name("Free assets: ") + formatted_free_assets)

        formatted_comissions = format_assets(commissions, portfolio.currency)
        if portfolio.free_commissions is not None and commissions > portfolio.free_commissions:
            formatted_comissions = colorify_warning(formatted_comissions)
        print(colorify_name("Commissions: ") + formatted_comissions)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=Actions.ALL, help="action to process")
    parser.add_argument("--debug", action="store_true", help="debug mode")
    parser.add_argument("--flat", action="store_true", help="flat view")
    return parser.parse_args()


def main(portfolios):
    args = parse_args()
    pcli.log.setup(level=logging.DEBUG if args.debug else logging.CRITICAL)

    for portfolio_id, portfolio in enumerate(portfolios):
        if portfolio_id:
            print("\n")

        process_portfolio(args.action, portfolio, args.flat)
