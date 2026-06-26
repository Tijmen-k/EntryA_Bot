"""
Position sizing for Entry A.

Risk-based sizing:
  risk_amount     = account_balance * RISK_PER_TRADE_PCT
  position_notional = risk_amount / SL_PCT
  contracts         = position_notional / current_price   (linear perpetual PF_ETHUSD)

The notional is then capped by the margin available given the configured leverage.
"""
from loguru import logger
import config


class PositionSizer:

    def calculate(
        self,
        account_balance: float,
        current_price: float,
        sl_pct: float,
    ) -> float:
        """
        Return contract size (ETH) to trade.
        Returns 0.0 if sizing is not feasible.
        """
        if account_balance <= 0 or current_price <= 0 or sl_pct <= 0:
            logger.warning("Invalid inputs for position sizing")
            return 0.0

        risk_amount = account_balance * config.RISK_PER_TRADE_PCT
        position_notional = risk_amount / sl_pct

        # Cap by max notional from leverage + available balance
        max_notional = account_balance * config.LEVERAGE
        if position_notional > max_notional:
            logger.debug(f"Notional capped: {position_notional:.2f} → {max_notional:.2f}")
            position_notional = max_notional

        contracts = position_notional / current_price

        # Kraken minimum order size for PF_ETHUSD is typically 0.1 ETH
        min_size = 0.1
        if contracts < min_size:
            logger.warning(
                f"Calculated size {contracts:.4f} ETH below minimum {min_size} ETH. "
                f"Account balance may be too small for this leverage/risk setting."
            )
            return 0.0

        logger.debug(
            f"Sizing: balance={account_balance:.2f} risk={risk_amount:.2f} "
            f"notional={position_notional:.2f} contracts={contracts:.4f}"
        )
        return round(contracts, 4)
