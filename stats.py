"""stats.py — Running trade performance scorecard."""
from __future__ import annotations


class TradeStats:
    """Accumulates win/loss metrics across all closed positions."""

    def __init__(self) -> None:
        self.total_trades   = 0
        self.wins           = 0
        self.losses         = 0
        self.total_pnl      = 0.0
        self.total_staked   = 0.0
        self.stop_losses    = 0
        self.take_profits   = 0
        self.manual_exits   = 0
        self.largest_win    = 0.0
        self.largest_loss   = 0.0
        self._edge_sum      = 0.0

    def record(self, position) -> None:
        pnl = position.realised_pnl
        self.total_trades += 1
        self.total_pnl    += pnl
        self.total_staked += position.size_usdc
        self._edge_sum    += position.edge_at_entry

        if pnl > 0:
            self.wins        += 1
            self.largest_win  = max(self.largest_win, pnl)
        else:
            self.losses      += 1
            self.largest_loss = min(self.largest_loss, pnl)

        reason = position.exit_reason or ""
        if reason == "stop_loss":     self.stop_losses  += 1
        elif reason == "take_profit": self.take_profits += 1
        else:                         self.manual_exits += 1

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_staked * 100) if self.total_staked else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades else 0.0

    @property
    def avg_edge(self) -> float:
        return self._edge_sum / self.total_trades if self.total_trades else 0.0

    def summary_line(self) -> str:
        if not self.total_trades:
            return "No closed trades yet."
        sign = "+" if self.total_pnl >= 0 else ""
        return (
            f"{self.wins}W/{self.losses}L  win={self.win_rate*100:.0f}%  "
            f"P&L={sign}${self.total_pnl:.2f}  ROI={sign}{self.roi:.1f}%  "
            f"avg_edge={self.avg_edge*100:.1f}pp"
        )
