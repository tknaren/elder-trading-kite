"""
Elder Trading System - NSE Trade Charges Calculator
====================================================

Calculates all charges applicable to NSE equity trades:
- STT (Securities Transaction Tax)
- Exchange Transaction Charges
- Brokerage
- GST on brokerage and charges
- SEBI Charges
- Stamp Duty

All charges are calculated for delivery (CNC) trades.
Zerodha-specific: Zero brokerage on delivery trades.
"""

from dataclasses import dataclass
from typing import Dict


@dataclass
class NSECharges:
    """NSE trade charges breakdown"""
    # Transaction details
    buy_value: float
    sell_value: float
    quantity: int

    # Individual charges
    stt: float  # Securities Transaction Tax
    exchange_charges: float  # NSE transaction charges
    sebi_charges: float  # SEBI turnover fee
    stamp_duty: float  # Stamp duty (on buy side)
    gst: float  # GST on brokerage + exchange charges
    brokerage: float  # Broker commission

    # Totals
    total_charges: float
    total_buy_cost: float  # Buy value + charges on buy
    total_sell_proceeds: float  # Sell value - charges on sell


# NSE Charge Rates (as of 2024)
class NSEChargeRates:
    # STT (Securities Transaction Tax)
    # Delivery: 0.1% on both buy and sell
    # Intraday: 0.025% on sell side only
    STT_DELIVERY_BUY = 0.001  # 0.1%
    STT_DELIVERY_SELL = 0.001  # 0.1%
    STT_INTRADAY_SELL = 0.00025  # 0.025%

    # Exchange Transaction Charges
    # NSE: 0.00345% (₹345 per crore)
    NSE_TRANSACTION_CHARGE = 0.0000345  # 0.00345%

    # SEBI Turnover Fee
    # ₹10 per crore = 0.0001%
    SEBI_CHARGES = 0.000001  # 0.0001%

    # Stamp Duty (varies by state, using Maharashtra rates)
    # 0.015% on buy side for delivery
    # 0.003% for intraday
    STAMP_DUTY_DELIVERY = 0.00015  # 0.015%
    STAMP_DUTY_INTRADAY = 0.00003  # 0.003%

    # GST: 18% on (brokerage + exchange charges + SEBI charges)
    GST_RATE = 0.18  # 18%

    # Zerodha Brokerage
    # Delivery: FREE (₹0)
    # Intraday: ₹20 per executed order or 0.03%, whichever is lower
    ZERODHA_DELIVERY_BROKERAGE = 0  # Free
    ZERODHA_INTRADAY_BROKERAGE = 20  # ₹20 flat or 0.03%
    ZERODHA_INTRADAY_BROKERAGE_PCT = 0.0003  # 0.03%


def calculate_delivery_charges(
    buy_price: float,
    sell_price: float,
    quantity: int,
    brokerage_per_order: float = 0  # Zerodha: ₹0 for delivery
) -> NSECharges:
    """
    Calculate all charges for a delivery (CNC) trade

    Args:
        buy_price: Entry price per share
        sell_price: Exit price per share
        quantity: Number of shares
        brokerage_per_order: Brokerage per order (default 0 for Zerodha delivery)

    Returns:
        NSECharges object with full breakdown
    """
    buy_value = buy_price * quantity
    sell_value = sell_price * quantity
    turnover = buy_value + sell_value

    # STT: 0.1% on both buy and sell for delivery
    stt_buy = buy_value * NSEChargeRates.STT_DELIVERY_BUY
    stt_sell = sell_value * NSEChargeRates.STT_DELIVERY_SELL
    stt = stt_buy + stt_sell

    # Exchange Transaction Charges: 0.00345% on turnover
    exchange_charges = turnover * NSEChargeRates.NSE_TRANSACTION_CHARGE

    # SEBI Charges: ₹10 per crore (0.0001%)
    sebi_charges = turnover * NSEChargeRates.SEBI_CHARGES

    # Stamp Duty: 0.015% on buy side only
    stamp_duty = buy_value * NSEChargeRates.STAMP_DUTY_DELIVERY

    # Brokerage (Zerodha: ₹0 for delivery)
    brokerage = brokerage_per_order * 2  # Buy + Sell orders

    # GST: 18% on (brokerage + exchange charges + SEBI charges)
    gst_base = brokerage + exchange_charges + sebi_charges
    gst = gst_base * NSEChargeRates.GST_RATE

    # Total charges
    total_charges = stt + exchange_charges + sebi_charges + stamp_duty + gst + brokerage

    # Total buy cost (for position sizing)
    buy_side_charges = stt_buy + stamp_duty + (exchange_charges / 2) + (sebi_charges / 2) + (gst / 2) + (brokerage / 2)
    total_buy_cost = buy_value + buy_side_charges

    # Total sell proceeds
    sell_side_charges = stt_sell + (exchange_charges / 2) + (sebi_charges / 2) + (gst / 2) + (brokerage / 2)
    total_sell_proceeds = sell_value - sell_side_charges

    return NSECharges(
        buy_value=round(buy_value, 2),
        sell_value=round(sell_value, 2),
        quantity=quantity,
        stt=round(stt, 2),
        exchange_charges=round(exchange_charges, 2),
        sebi_charges=round(sebi_charges, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        brokerage=round(brokerage, 2),
        total_charges=round(total_charges, 2),
        total_buy_cost=round(total_buy_cost, 2),
        total_sell_proceeds=round(total_sell_proceeds, 2)
    )


def calculate_intraday_charges(
    buy_price: float,
    sell_price: float,
    quantity: int,
    brokerage_per_order: float = 20  # Zerodha: ₹20 for intraday
) -> NSECharges:
    """
    Calculate all charges for an intraday (MIS) trade

    Args:
        buy_price: Entry price per share
        sell_price: Exit price per share
        quantity: Number of shares
        brokerage_per_order: Brokerage per order (default ₹20 for Zerodha intraday)

    Returns:
        NSECharges object with full breakdown
    """
    buy_value = buy_price * quantity
    sell_value = sell_price * quantity
    turnover = buy_value + sell_value

    # STT: 0.025% on sell side only for intraday
    stt = sell_value * NSEChargeRates.STT_INTRADAY_SELL

    # Exchange Transaction Charges
    exchange_charges = turnover * NSEChargeRates.NSE_TRANSACTION_CHARGE

    # SEBI Charges
    sebi_charges = turnover * NSEChargeRates.SEBI_CHARGES

    # Stamp Duty: 0.003% on buy side for intraday
    stamp_duty = buy_value * NSEChargeRates.STAMP_DUTY_INTRADAY

    # Brokerage: ₹20 per order or 0.03%, whichever is lower
    brokerage_buy = min(brokerage_per_order, buy_value * NSEChargeRates.ZERODHA_INTRADAY_BROKERAGE_PCT)
    brokerage_sell = min(brokerage_per_order, sell_value * NSEChargeRates.ZERODHA_INTRADAY_BROKERAGE_PCT)
    brokerage = brokerage_buy + brokerage_sell

    # GST
    gst_base = brokerage + exchange_charges + sebi_charges
    gst = gst_base * NSEChargeRates.GST_RATE

    # Total charges
    total_charges = stt + exchange_charges + sebi_charges + stamp_duty + gst + brokerage

    return NSECharges(
        buy_value=round(buy_value, 2),
        sell_value=round(sell_value, 2),
        quantity=quantity,
        stt=round(stt, 2),
        exchange_charges=round(exchange_charges, 2),
        sebi_charges=round(sebi_charges, 2),
        stamp_duty=round(stamp_duty, 2),
        gst=round(gst, 2),
        brokerage=round(brokerage, 2),
        total_charges=round(total_charges, 2),
        total_buy_cost=round(buy_value + stamp_duty + brokerage_buy + (exchange_charges / 2) + (sebi_charges / 2) + (gst / 2), 2),
        total_sell_proceeds=round(sell_value - stt - brokerage_sell - (exchange_charges / 2) - (sebi_charges / 2) - (gst / 2), 2)
    )


def estimate_trade_charges(
    entry_price: float,
    stop_loss: float,
    target: float,
    quantity: int,
    is_intraday: bool = False
) -> Dict:
    """
    Estimate charges for a planned trade

    Returns both win and loss scenarios with net P/L after charges.
    """
    calc_fn = calculate_intraday_charges if is_intraday else calculate_delivery_charges

    # Win scenario (exit at target)
    win_charges = calc_fn(entry_price, target, quantity)
    gross_profit = (target - entry_price) * quantity
    net_profit = gross_profit - win_charges.total_charges

    # Loss scenario (exit at stop loss)
    loss_charges = calc_fn(entry_price, stop_loss, quantity)
    gross_loss = (stop_loss - entry_price) * quantity
    net_loss = gross_loss - loss_charges.total_charges

    return {
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'target': target,
        'quantity': quantity,
        'trade_type': 'Intraday' if is_intraday else 'Delivery',

        # Entry cost
        'buy_value': entry_price * quantity,
        'estimated_entry_charges': round(win_charges.total_buy_cost - (entry_price * quantity), 2),

        # Win scenario
        'win_scenario': {
            'exit_price': target,
            'gross_profit': round(gross_profit, 2),
            'charges': win_charges.total_charges,
            'net_profit': round(net_profit, 2),
            'charges_breakdown': {
                'stt': win_charges.stt,
                'exchange': win_charges.exchange_charges,
                'sebi': win_charges.sebi_charges,
                'stamp_duty': win_charges.stamp_duty,
                'gst': win_charges.gst,
                'brokerage': win_charges.brokerage
            }
        },

        # Loss scenario
        'loss_scenario': {
            'exit_price': stop_loss,
            'gross_loss': round(gross_loss, 2),
            'charges': loss_charges.total_charges,
            'net_loss': round(net_loss, 2),
            'charges_breakdown': {
                'stt': loss_charges.stt,
                'exchange': loss_charges.exchange_charges,
                'sebi': loss_charges.sebi_charges,
                'stamp_duty': loss_charges.stamp_duty,
                'gst': loss_charges.gst,
                'brokerage': loss_charges.brokerage
            }
        },

        # Adjusted R:R after charges
        'risk_after_charges': round(abs(net_loss), 2),
        'reward_after_charges': round(net_profit, 2),
        'rr_ratio_after_charges': round(net_profit / abs(net_loss), 2) if net_loss != 0 else 0
    }


def calculate_break_even(
    entry_price: float,
    quantity: int,
    is_intraday: bool = False
) -> float:
    """
    Calculate break-even price after all charges

    This is useful for setting minimum target.
    """
    # Calculate charges at entry price (as if we sell at same price)
    charges = calculate_delivery_charges(entry_price, entry_price, quantity) if not is_intraday \
        else calculate_intraday_charges(entry_price, entry_price, quantity)

    # Break-even = Entry + (Total Charges / Quantity)
    break_even = entry_price + (charges.total_charges / quantity)

    return round(break_even, 2)


# Example usage and test
if __name__ == "__main__":
    print("=" * 60)
    print("  NSE Trade Charges Calculator - Test")
    print("=" * 60)

    # Example trade
    entry = 2500.00
    stop = 2400.00
    target = 2700.00
    qty = 100

    print(f"\nTrade: Buy {qty} shares at ₹{entry}")
    print(f"Stop Loss: ₹{stop}, Target: ₹{target}")

    estimate = estimate_trade_charges(entry, stop, target, qty)

    print(f"\n📊 Delivery Trade Analysis:")
    print(f"  Buy Value: ₹{estimate['buy_value']:,.2f}")
    print(f"  Entry Charges: ₹{estimate['estimated_entry_charges']:,.2f}")

    print(f"\n✅ Win Scenario (Target ₹{target}):")
    win = estimate['win_scenario']
    print(f"  Gross Profit: ₹{win['gross_profit']:,.2f}")
    print(f"  Total Charges: ₹{win['charges']:,.2f}")
    print(f"  Net Profit: ₹{win['net_profit']:,.2f}")

    print(f"\n❌ Loss Scenario (Stop ₹{stop}):")
    loss = estimate['loss_scenario']
    print(f"  Gross Loss: ₹{loss['gross_loss']:,.2f}")
    print(f"  Total Charges: ₹{loss['charges']:,.2f}")
    print(f"  Net Loss: ₹{loss['net_loss']:,.2f}")

    print(f"\n📈 Adjusted R:R (after charges): 1:{estimate['rr_ratio_after_charges']:.2f}")

    be = calculate_break_even(entry, qty)
    print(f"\n💹 Break-even Price: ₹{be}")
