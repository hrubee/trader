"""
Binance Exchange Adapter — supports spot and futures (USDT-M) trading.
Uses CCXT for all API interactions.

Supports paper (public API only, no credentials) and
live (real orders on Binance, API credentials required) modes.

Demo/Testnet mode can be toggled via BINANCE_SANDBOX=1.
"""

import os
import sys
import math
import time
from typing import Tuple
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'shared_tools'))

import ccxt
import pandas as pd


class BinanceExchangeAdapter:
    """
    Exchange adapter for Binance — spot and futures.

    Paper mode:  no credentials needed; uses live Binance prices for simulation.
    Live mode:   requires BINANCE_LIVE_API_KEY, BINANCE_LIVE_API_SECRET.
    """

    def __init__(self):
        sandbox = os.environ.get("BINANCE_SANDBOX", "") == "1"
        
        if sandbox:
            api_key = os.environ.get("BINANCE_DEMO_API_KEY", "")
            api_secret = os.environ.get("BINANCE_DEMO_API_SECRET", "")
        else:
            api_key = os.environ.get("BINANCE_LIVE_API_KEY", "")
            api_secret = os.environ.get("BINANCE_LIVE_API_SECRET", "")

        config = {
            "enableRateLimit": True,
            "options": {
                "fetchCurrencies": False,
            }
        }
        
        # Determine if we are in live mode based on presence of credentials
        self._is_live = bool(api_key and api_secret)
        
        if self._is_live:
            config["apiKey"] = api_key
            config["secret"] = api_secret

        self._exchange = ccxt.binance(config)
        
        # Public instance for market data to avoid auth errors on public endpoints
        self._public_exchange = ccxt.binance({"enableRateLimit": True})
        
        if sandbox:
            # Enable CCXT's unified demo mode for Binance
            if hasattr(self._exchange, 'enable_demo_trading'):
                self._exchange.enable_demo_trading(True)
            else:
                # Fallback for older CCXT
                self._exchange.set_sandbox_mode(True)
            self._exchange.urls['api']['v1'] = 'https://demo-api.binance.com/api/v1'
            self._exchange.urls['api']['v3'] = 'https://demo-api.binance.com/api/v3'
            self._exchange.urls['api']['sapi'] = 'https://demo-api.binance.com/api' # sapi might not exist but avoid live host
            self._exchange.urls['api']['sapiV2'] = 'https://demo-api.binance.com/api'
            self._exchange.urls['api']['sapiV3'] = 'https://demo-api.binance.com/api'
            
            # For Futures Demo Mode, use demo-fapi.binance.com
            self._exchange.urls['api']['fapiPublic'] = 'https://demo-fapi.binance.com/fapi/v1'
            self._exchange.urls['api']['fapiPrivate'] = 'https://demo-fapi.binance.com/fapi/v1'
            self._exchange.urls['api']['fapiPublicV2'] = 'https://demo-fapi.binance.com/fapi/v2'
            self._exchange.urls['api']['fapiPrivateV2'] = 'https://demo-fapi.binance.com/fapi/v2'
            self._exchange.urls['api']['fapiPublicV3'] = 'https://demo-fapi.binance.com/fapi/v3'
            self._exchange.urls['api']['fapiPrivateV3'] = 'https://demo-fapi.binance.com/fapi/v3'

            self._exchange.options['enableDemoTrading'] = True
            self._exchange.options['fetchCurrencies'] = False
            self._exchange.options['fetchMargins'] = False

        self._markets_loaded = False

    @property
    def is_live(self) -> bool:
        """True if API credentials are provided (live mode)."""
        return self._is_live

    @property
    def mode(self) -> str:
        """'live' or 'paper'."""
        return "live" if self.is_live else "paper"

    @property
    def name(self) -> str:
        return "binance"

    # ─────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────

    def _load_markets(self):
        """Load and cache markets from Binance."""
        if not self._markets_loaded:
            self._exchange.load_markets()
            self._markets_loaded = True

    def _format_symbol(self, symbol: str, inst_type: str = "spot") -> str:
        """Format symbol for CCXT (e.g. BTC -> BTC/USDT or BTC/USDT:USDT)."""
        if not symbol:
            return ""
        pair = symbol if "/" in symbol else f"{symbol}/USDT"
        if inst_type == "futures" and ":" not in pair:
            pair = f"{pair}:USDT"
        return pair

    def get_spot_price(self, symbol: str) -> float:
        """Get current spot price for a coin (e.g. 'BTC')."""
        if not symbol:
            return 0.0
        pair = self._format_symbol(symbol, "spot")
        try:
            ticker = self._public_exchange.fetch_ticker(pair)
            price = ticker.get("last") or 0
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        return 0.0

    def get_perp_price(self, symbol: str) -> float:
        """Get current last price for a perpetual swap (e.g. 'BTC')."""
        if not symbol:
            return 0.0
        pair = self._format_symbol(symbol, "futures")
        try:
            ticker = self._public_exchange.fetch_ticker(pair)
            price = ticker.get("last") or 0
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        return 0.0

    def get_ohlcv(self, symbol: str, interval: str = "1h", limit: int = 200, inst_type: str = "spot") -> list:
        """
        Fetch OHLCV candles from Binance.
        """
        if not symbol:
            return []
        pair = self._format_symbol(symbol, inst_type)
        try:
            # Use authenticated client first if possible for higher API rate limits
            candles = self._exchange.fetch_ohlcv(pair, interval, limit=limit)
            return candles
        except Exception:
            try:
                # Fallback to public client in case of credentials/rate errors on authenticated client
                candles = self._public_exchange.fetch_ohlcv(pair, interval, limit=limit)
                return candles
            except Exception:
                return []

    def floor_size(self, symbol: str, sz: float) -> float:
        """Floor order size to Binance precision."""
        if sz <= 0:
            return 0.0
        try:
            self._load_markets()
            # Check both spot and futures if symbol is ambiguous
            pair = self._format_symbol(symbol, "spot")
            if pair not in self._exchange.markets:
                pair = self._format_symbol(symbol, "futures")
            
            # Use CCXT's built-in precision helper
            return float(self._exchange.amount_to_precision(pair, sz))
        except Exception:
            return sz

    # ─────────────────────────────────────────────
    # Order execution (live mode only)
    # ─────────────────────────────────────────────

    def fetch_open_positions(self) -> list:
        """Return every open perpetual swap position on the account."""
        if not self._is_live:
            raise RuntimeError("fetch_open_positions requires live mode (BINANCE_LIVE_API_KEY/SECRET)")
        
        return self._exchange.fetch_positions(params={"type": "future"}) or []

    def market_open(self, symbol: str, is_buy: bool, size: float, inst_type: str = "spot", reduce_only: bool = False) -> dict:
        """Place a market order."""
        if not self._is_live:
            raise RuntimeError("market_open requires live mode")
        
        self._load_markets()
        side = "buy" if is_buy else "sell"
        pair = self._format_symbol(symbol, inst_type)
        
        # Apply precision
        amount = float(self._exchange.amount_to_precision(pair, size))
        
        if inst_type == "futures":
            params = {"type": "future"}
            if reduce_only:
                params["reduceOnly"] = True
            return self._exchange.create_market_order(pair, side, amount, params=params)
        else:
            return self._exchange.create_market_order(pair, side, amount)

    def market_stop_loss(self, symbol: str, is_buy: bool, size: float, stop_price: float, inst_type: str = "spot") -> dict:
        """Place a stop-loss or take-profit order."""
        if not self._is_live:
            raise RuntimeError("market_stop_loss requires live mode")
        
        self._load_markets()
        side = "buy" if is_buy else "sell"
        pair = self._format_symbol(symbol, inst_type)
        
        # Apply precision
        amount_str = self._exchange.amount_to_precision(pair, size)
        if amount_str is None:
             raise ValueError(f"Could not format amount {size} for {pair}")
        amount = float(amount_str)

        px_raw = self._exchange.price_to_precision(pair, stop_price)
        
        if px_raw is None:
             px_raw = str(stop_price)
             
        px = float(px_raw)
        
        # Get current price to determine if this is a TP or SL
        current_price = self.get_spot_price(symbol) if inst_type == "spot" else self.get_perp_price(symbol)
        
        # For buy orders (closing a short): TP if stop_price < current_price, SL if stop_price > current_price
        # For sell orders (closing a long): TP if stop_price > current_price, SL if stop_price < current_price
        is_tp = False
        if is_buy:
            if px < current_price:
                is_tp = True
        else:
            if px > current_price:
                is_tp = True

        if inst_type == "futures":
            # For futures, we use MARKET stops with reduceOnly.
            order_type = "TAKE_PROFIT_MARKET" if is_tp else "STOP_MARKET"
            params = {
                "type": "future",
                "stopPrice": px,
                "reduceOnly": True,
            }
            print(f"DEBUG: Placing {order_type} {side} {amount} {pair} at stop {px} (reduceOnly=True)", file=sys.stderr)
            return self._exchange.create_order(pair, order_type, side, amount, price=None, params=params)
        else:
            # For spot, we use LIMIT stops.
            order_type = "TAKE_PROFIT_LIMIT" if is_tp else "STOP_LOSS_LIMIT"
            params = {
                "stopPrice": px,
            }
            # For TP/SL limit, we need a limit price as well. 
            # Slightly worse than stopPrice to ensure execution.
            limit_px = px * 0.99 if side == "sell" else px * 1.01
            limit_px = float(self._exchange.price_to_precision(pair, limit_px))
            
            return self._exchange.create_order(pair, order_type, side, amount, price=limit_px, params=params)

    def market_close(self, symbol: str, sz: float | None = None) -> dict:
        """Close an open perpetual swap position for a symbol (reduce-only)."""
        if not self._is_live:
            raise RuntimeError("market_close requires live mode")
            
        pair = self._format_symbol(symbol, "futures")
        positions = self._exchange.fetch_positions([pair], params={"type": "future"})
        results = []
        for pos in positions:
            amount = abs(float(pos.get("contracts", 0) or pos.get("amount", 0) or 0))
            if amount > 0:
                pos_side = pos.get("side", "")
                close_side = "sell" if pos_side in ("long", "buy") else "buy"
                
                close_sz = amount
                if sz is not None:
                    close_sz = min(float(sz), amount)
                
                if close_sz <= 0:
                    continue
                    
                results.append(self._exchange.create_market_order(
                    pair, close_side, close_sz,
                    params={"type": "future", "reduceOnly": True}
                ))
        return results[0] if results else {}

    def tiered_stop_loss(self, symbol: str, inst_type: str, side: str, prices: list[float], size: float) -> tuple[list[str], list[str]]:
        """Place multiple reduce-only stop/TP orders."""
        oids = []
        errors = []
        
        # Protection side is opposite of the entry side
        is_buy_protection = (side.lower() in ("sell", "short"))
        
        # Split size across tiers
        tier_size = size / len(prices) if prices else 0
        
        for px in prices:
            try:
                # market_stop_loss(symbol, is_buy, size, stop_price, inst_type)
                order = self.market_stop_loss(symbol, is_buy_protection, tier_size, px, inst_type)
                oids.append(str(order.get("id", "")))
                errors.append("")
            except Exception as e:
                oids.append("")
                errors.append(str(e))
                
        return oids, errors

    def cancel_orders(self, symbol: str, inst_type: str, oids: list[str]):
        """Cancel multiple orders by ID.

        Futures STOP_MARKET / TAKE_PROFIT_MARKET orders are placed on Binance's
        CONDITIONAL (algo) order system and carry an algoId, not a regular
        orderId — they MUST be cancelled with trigger=True or Binance returns
        -2011 "Unknown order sent". (#bug: this previously failed silently,
        which combined with a broken verify path let stop orders stack up.)"""
        pair = self._format_symbol(symbol, inst_type)
        params = {}
        if inst_type == "futures":
            params["type"] = "future"
            params["trigger"] = True

        for oid in oids:
            if not oid or str(oid) == "0":
                continue
            try:
                self._exchange.cancel_order(str(oid), pair, params=params)
            except Exception as e:
                print(f"WARNING: Failed to cancel order {oid}: {e}", file=sys.stderr)

    def list_open_stop_orders(self, symbol: str, inst_type: str) -> list:
        """Return open protective (stop/take-profit) orders for a symbol as
        [{id, side, trigger_price, reduce_only, qty}]. For futures these are
        CONDITIONAL orders, fetched with trigger=True (they do NOT appear in a
        normal open-orders query); for spot they are ordinary stop orders."""
        pair = self._format_symbol(symbol, inst_type)
        params = {"trigger": True} if inst_type == "futures" else {}
        if inst_type == "futures":
            params["type"] = "future"
        out = []
        try:
            for o in self._exchange.fetch_open_orders(pair, params=params) or []:
                info = o.get("info") or {}
                trig = o.get("stopPrice") or o.get("triggerPrice") or info.get("triggerPrice") or info.get("stopPrice")
                try:
                    trig = float(trig) if trig is not None else None
                except (TypeError, ValueError):
                    trig = None
                ro = o.get("reduceOnly")
                if ro is None:
                    ro = str(info.get("reduceOnly")).lower() == "true"
                out.append({
                    "id": str(o.get("id") or info.get("algoId") or ""),
                    "side": (o.get("side") or info.get("side") or "").lower(),
                    "trigger_price": trig,
                    "reduce_only": bool(ro),
                    "qty": o.get("amount") or info.get("quantity"),
                })
        except Exception as e:
            print(f"WARNING: list_open_stop_orders failed for {symbol}: {e}", file=sys.stderr)
        return out

    def get_futures_balance(self) -> dict:
        """Fetch raw futures balance from Binance."""
        if not self._is_live:
            raise RuntimeError("get_futures_balance requires live mode")
        return self._exchange.fapiPrivate_get_account()

    def get_futures_positions(self) -> list:
        """Fetch raw futures positions from Binance."""
        if not self._is_live:
            raise RuntimeError("get_futures_positions requires live mode")
        acct = self._exchange.fapiPrivate_get_account()
        return acct.get("positions") or []

    def get_account_balance(self, inst_type: str = "spot") -> float:
        """Return total USDT-denominated account value."""
        if not self._is_live:
            raise RuntimeError("get_account_balance requires live mode")
            
        params = {"type": "future"} if inst_type == "futures" else {}
        bal = self._exchange.fetch_balance(params=params)
        total = bal.get("total") or {}
        try:
            return float(total.get("USDT") or 0.0)
        except (TypeError, ValueError):
            return 0.0
