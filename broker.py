"""Long-only ETF simulation, next-open execution and a durable paper ledger."""
import math

import pandas as pd


class Broker:
    def __init__(self, config, state=None):
        self.config = config
        self.state = state or {"cash":float(config["initial_cash"]), "positions":{},
            "receivables":[], "entitlements":{}, "applied_actions":[], "trades":[],
            "orders":[], "equity":[], "last_date":None, "pending":None}

    def nav(self, prices):
        return (self.state["cash"] + sum(self.state["positions"].get(s,0)*p for s,p in prices.items())
                + sum(item["cash"] for item in self.state["receivables"]))

    def corporate_actions(self, date, events):
        day = str(pd.Timestamp(date).date())
        for event in events:
            key = f"{event['symbol']}:{event['ex_date']}"
            if event["ex_date"] == day and key not in self.state["applied_actions"]:
                shares = self.state["entitlements"].pop(key, 0)
                self.state["receivables"].append({"symbol":event["symbol"],
                    "cash":shares*event["cash_per_share"], "pay_date":event["pay_date"]})
                self.state["applied_actions"].append(key)
        remaining = []
        for claim in self.state["receivables"]:
            if claim["pay_date"] <= day:
                self.state["cash"] += claim["cash"]
            else:
                remaining.append(claim)
        self.state["receivables"] = remaining

    def record_entitlements(self, date, events):
        day = str(pd.Timestamp(date).date())
        for event in events:
            if event["record_date"] == day:
                key = f"{event['symbol']}:{event['ex_date']}"
                self.state["entitlements"][key] = self.state["positions"].get(event["symbol"],0)

    def rebalance(self, signal_date, date, targets, rows, previous_close):
        signal_date, date = pd.Timestamp(signal_date), pd.Timestamp(date)
        if date <= signal_date:
            raise ValueError("Execution must be later than the signal")
        opens = {s:float(r["open"]) for s,r in rows.items()}
        equity = self.nav(opens)
        lot = self.config["lot_size"]
        desired = {s:int(equity*targets.get(s,0)/p//lot)*lot for s,p in opens.items()}
        deltas = {s:desired[s]-self.state["positions"].get(s,0) for s in opens}
        # Sell first; all available inventory was held at the previous close (T+1).
        for symbol, delta in sorted(deltas.items(), key=lambda x:(x[1] > 0, x[0])):
            if not delta:
                continue
            row, price = rows[symbol], opens[symbol]
            order = {"signal_date":str(signal_date.date()), "date":str(date.date()),
                     "symbol":symbol, "requested_shares":int(delta)}
            prior = previous_close[symbol]
            # Conservative at an opening limit: skip the entire order. Never use
            # that day's final volume/range to decide whether a morning order fills.
            limit = 0.20 if symbol in ("159915", "588000") else 0.10
            upper = math.floor(prior*(1+limit)*1000+0.5)/1000
            lower = math.floor(prior*(1-limit)*1000+0.5)/1000
            if (delta > 0 and price >= upper-0.0005) or (delta < 0 and price <= lower+0.0005):
                order.update(status="skipped", reason="opening_price_limit")
                self.state["orders"].append(order)
                continue
            # rows contain a prior-day liquidity cap; current day volume is unknown.
            cap = int(float(row["prior_volume"])*self.config["max_volume_participation"]//lot)*lot
            qty = min(abs(delta), cap)
            side = 1 if delta > 0 else -1
            fill_price = price*(1+side*self.config["slippage_bps"]/10000)
            if side == 1:
                while qty > 0:
                    fee = max(self.config["minimum_commission"], qty*fill_price*self.config["commission_rate"])
                    if qty*fill_price+fee <= self.state["cash"]+1e-8:
                        break
                    qty -= lot
            else:
                qty = min(qty, self.state["positions"].get(symbol,0))
            if qty <= 0:
                order.update(status="skipped", reason="cash_or_prior_liquidity")
                self.state["orders"].append(order)
                continue
            fee = max(self.config["minimum_commission"], qty*fill_price*self.config["commission_rate"])
            self.state["cash"] -= side*qty*fill_price+fee
            self.state["positions"][symbol] = self.state["positions"].get(symbol,0)+side*qty
            assert self.state["cash"] >= -1e-6
            assert self.state["positions"][symbol] >= 0
            trade = {**order, "shares":side*qty, "price":fill_price, "fee":fee,
                     "notional":qty*fill_price, "cash_after":self.state["cash"]}
            self.state["trades"].append(trade)
            self.state["orders"].append({**order, "status":"filled" if qty==abs(delta) else "partial",
                                          "filled_shares":side*qty})

    def close(self, date, prices, events):
        self.record_entitlements(date, events)
        value = self.nav(prices)
        self.state["equity"].append({"date":str(pd.Timestamp(date).date()), "equity":value,
            "cash":self.state["cash"], "receivables":sum(c["cash"] for c in self.state["receivables"]),
            "positions":dict(self.state["positions"])})
        self.state["last_date"] = str(pd.Timestamp(date).date())


def metrics(equity, initial_cash, trades):
    series = pd.Series([initial_cash]+[r["equity"] for r in equity], dtype=float)
    returns = series.pct_change().dropna()
    drawdown = series/series.cummax()-1
    count = len(returns)
    return {"sessions":count, "ending_equity":float(series.iloc[-1]),
        "total_return":float(series.iloc[-1]/initial_cash-1),
        "annualized_return":float((series.iloc[-1]/initial_cash)**(252/count)-1) if count else None,
        "max_drawdown":float(drawdown.min()),
        "sharpe_zero_rf":float(returns.mean()/returns.std(ddof=1)*252**0.5) if count>1 and returns.std(ddof=1)>0 else None,
        "fills":len(trades), "total_commission":sum(t["fee"] for t in trades),
        "gross_traded_notional":sum(t["notional"] for t in trades),
        "turnover_over_initial_cash":sum(t["notional"] for t in trades)/initial_cash}
