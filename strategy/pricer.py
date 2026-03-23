"""strategy/pricer.py — Black-Scholes binary option pricer with skew and jump adjustments."""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — Abramowitz & Stegun approximation, error < 1.5e-7."""
    t    = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
                + t * (-1.821255978 + t * 1.330274429))))
    cdf  = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def skew_adjusted_sigma(sigma: float, spot: float, strike: float, days: float) -> float:
    """
    Adjust flat IV for the volatility smile.
    OTM strikes trade at higher IV — using flat IV understates extreme move probabilities.
    Capped at 3x flat sigma to prevent absurd values on deep OTM strikes.
    """
    if spot <= 0 or strike <= 0:
        return sigma
    T           = max(days / 365.0, 1e-6)
    log_moneyness = math.log(strike / spot)
    skew_factor = 0.15 * (log_moneyness ** 2) / math.sqrt(T)
    return min(sigma * (1.0 + skew_factor), sigma * 3.0)


def jump_risk_premium(days: float, base: float = 0.08) -> float:
    """
    Jump risk premium for gap risk (exchange hacks, macro shocks).
    Decays with time — most relevant on short-dated markets.
    Only applied when base_prob > 2% to avoid creating fake floor on deep OTM.
    At T=12d: 0.08/√12 ≈ 0.023. At T=30d: ≈ 0.015. At T=60d: ≈ 0.010.
    """
    return base / math.sqrt(max(days, 1.0))


def binary_option_price(
    *,
    spot:           float,
    strike:         float,
    sigma:          float,
    days_to_expiry: float,
    is_call:        bool,
    risk_free:      float = 0.0,
) -> float:
    """
    Risk-neutral probability that spot will be above (call) or below (put) strike at expiry.
    Includes volatility skew correction and time-decaying jump risk premium.
    Returns 0.5 as a safe fallback on degenerate inputs.
    """
    T = days_to_expiry / 365.0
    if T <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    try:
        sigma_adj  = skew_adjusted_sigma(sigma, spot, strike, days_to_expiry)
        d2         = (math.log(spot / strike) + (risk_free - 0.5 * sigma_adj ** 2) * T) \
                     / (sigma_adj * math.sqrt(T))
        base_prob  = _norm_cdf(d2) if is_call else _norm_cdf(-d2)
        if base_prob > 0.02:
            base_prob += jump_risk_premium(days_to_expiry)
        return min(base_prob, 0.999)
    except (ValueError, ZeroDivisionError):
        return 0.5
