"""strategy/signal.py — CryptoStrategy: evaluates markets and generates trade signals."""
from __future__ import annotations

import logging
import math
from typing import Optional

from models import Market, TradeSignal
from feeds.binance import BinanceFeed
from feeds.volatility import VolatilityFeed
from strategy.parser import ParsedMarket, parse_market_question
from strategy.pricer import binary_option_price, skew_adjusted_sigma, jump_risk_premium

log = logging.getLogger(__name__)

_MIN_DAYS     = 2      # minimum days to expiry
_MAX_SIGMAS   = 2.5    # reject outcomes requiring > 2.5σ move
_MAX_DAILY_MOVE_PCT = 8.0  # max plausible daily % move for crypto


def _kelly_fraction(edge: float, max_fraction: float = 0.25) -> float:
    """Half-Kelly sizing: f* = (edge / (1-edge)) * 0.5, capped at max_fraction."""
    if abs(edge) < 1e-6:
        return 0.0
    e = min(abs(edge), 0.99)
    return min((e / (1.0 - e)) * 0.5, max_fraction)


def _compute_fair_yes(parsed: ParsedMarket, spot: float, sigma: float) -> float:
    """Compute fair YES probability given parsed market, spot price, and IV."""
    if parsed.market_type == "between":
        p_low  = binary_option_price(spot=spot, strike=parsed.strike,      sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
        p_high = binary_option_price(spot=spot, strike=parsed.strike_high, sigma=sigma, days_to_expiry=parsed.days_to_expiry, is_call=True)
        return max(p_low - p_high, 0.0)
    return binary_option_price(
        spot=spot, strike=parsed.strike, sigma=sigma,
        days_to_expiry=parsed.days_to_expiry, is_call=parsed.is_call,
    )


class CryptoStrategy:
    """
    Evaluates Polymarket crypto price markets against Black-Scholes fair value.
    Returns a TradeSignal when edge exceeds min_edge, None otherwise.
    Single responsibility: signal generation only. Does not place orders.
    """

    def __init__(
        self,
        spot_feed: BinanceFeed,
        vol_feed:  VolatilityFeed,
        *,
        min_edge:           float = 0.05,
        min_volume:         float = 500.0,
        max_kelly_fraction: float = 0.25,
    ) -> None:
        self._spot      = spot_feed
        self._vol       = vol_feed
        self._min_edge  = min_edge
        self._min_vol   = min_volume
        self._max_kelly = max_kelly_fraction

    async def evaluate(self, market: Market) -> Optional[TradeSignal]:
        if market.volume_24h < self._min_vol:
            return None

        parsed = parse_market_question(market.question, market.end_date)
        if parsed is None:
            return None

        spot = self._spot.price(parsed.asset)
        if spot is None:
            return None

        if parsed.days_to_expiry < _MIN_DAYS:
            return None

        if parsed.market_type == "up_down":
            if parsed.pct_move <= 0:
                return None
            parsed.strike = spot * (1.0 + parsed.pct_move if parsed.is_call else 1.0 - parsed.pct_move)

        await self._vol.refresh()
        sigma = self._vol.iv(parsed.asset)

        fair_yes  = _compute_fair_yes(parsed, spot, sigma)
        yes_price = market.yes_price
        no_price  = market.no_price
        fair_no   = max(0.0, min(1.0, 1.0 - fair_yes))
        yes_edge  = fair_yes - yes_price

        sigma_adj = skew_adjusted_sigma(sigma, spot, parsed.strike, parsed.days_to_expiry)
        jump      = jump_risk_premium(parsed.days_to_expiry)

        log.info(
            "%-62s | spot=%-9.2f σ=%.0f%%→%.0f%% T=%.1fd jump=+%.3f | fair_yes=%.3f mkt=%.3f edge=%+.3f",
            market.question[:62], spot,
            sigma * 100, sigma_adj * 100, parsed.days_to_expiry, jump,
            fair_yes, yes_price, yes_edge,
        )

        if abs(yes_edge) < self._min_edge:
            return None

        # Reject deep OTM outcomes requiring > 2.5 sigma move
        if parsed.strike > 0 and parsed.days_to_expiry > 0:
            sigmas = abs(math.log(parsed.strike / spot)) / (sigma_adj * math.sqrt(parsed.days_to_expiry / 365.0))
            if sigmas > _MAX_SIGMAS:
                log.debug("Requires %.1fσ — rejecting", sigmas)
                return None

        # Reject physically implausible outcomes
        if parsed.strike > 0:
            required_pct = abs(parsed.strike / spot - 1.0) * 100
            if required_pct > _MAX_DAILY_MOVE_PCT * parsed.days_to_expiry:
                log.debug("Requires %.0f%% move — rejecting", required_pct)
                return None

        # Determine which token to buy
        if yes_edge > 0:
            held_outcome, token_id, market_price, fair_value = "YES", market.yes_token_id, yes_price, fair_yes
        else:
            held_outcome, token_id, market_price, fair_value = "NO",  market.no_token_id,  no_price,  fair_no

        edge = fair_value - market_price

        # Reject near-zero noise signals
        if fair_value < 0.05 and market_price < 0.05:
            return None

        return TradeSignal(
            market_question  = market.question,
            token_id         = token_id,
            held_outcome     = held_outcome,
            side             = "BUY",
            market_price     = market_price,
            fair_value       = fair_value,
            edge             = edge,
            market_yes_price = yes_price,
            market_no_price  = no_price,
            fair_yes_value   = fair_yes,
            fair_no_value    = fair_no,
            size_fraction    = _kelly_fraction(edge, self._max_kelly),
            neg_risk         = market.neg_risk,
            days_to_expiry   = parsed.days_to_expiry,
        )
