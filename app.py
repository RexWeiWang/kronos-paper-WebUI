"""Local Kronos prediction, ETF historical simulation and forward paper account."""
import argparse
import fcntl
import hashlib
import html
import json
import os
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from broker import Broker, metrics
from market_data import (ROOT, adjusted_window, completed_date, fetch_all,
                         load_bars, load_dividends, next_sessions, sessions)


def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(tmp, path)


def common_dates(bars, config):
    start = max(frame.index[config["lookback"]-1] for frame in bars.values())
    end = min(frame.index[-1] for frame in bars.values())
    dates = sessions(start, end)
    for symbol, frame in bars.items():
        missing = sessions(frame.index[0], end).difference(frame.index)
        if len(missing):
            raise ValueError(f"{symbol}: missing {list(missing)}; refresh or review suspension")
    return dates


def weekly_signal(day):
    nxt = next_sessions(day, 1)[0]
    return pd.Timestamp(day).isocalendar()[:2] != nxt.isocalendar()[:2]


def future_open(asof, now=None):
    now = pd.Timestamp(now or datetime.now(ZoneInfo("Asia/Shanghai")))
    if now.tzinfo is None:
        now = now.tz_localize("Asia/Shanghai")
    day = pd.Timestamp(asof)
    for _ in range(8):
        day = next_sessions(day, 1)[0]
        opening = day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9,minutes=30)
        if opening > now:
            return day
    raise ValueError("No verified future opening in the calendar")


def predict_pool(forecaster, bars, events, day, config):
    future = next_sessions(day, config["horizon"])
    return [forecaster.forecast(s, adjusted_window(b, events, s, day), day, future)
            for s,b in bars.items()]


def execution_inputs(bars, day, events):
    rows, previous = {}, {}
    for s, frame in bars.items():
        prev = frame.loc[frame.index < day].iloc[-1]
        rows[s] = {"open":float(frame.loc[day,"open"]), "prior_volume":float(prev.volume)}
        # Ex-dividend reference adjusts the prior close for limit-price checks.
        cash = sum(e["cash_per_share"] for e in events if e["symbol"]==s and e["ex_date"]==str(day.date()))
        previous[s] = float(prev.close)-cash
    return rows, previous


def report(folder, broker, config, extra, baseline=None):
    folder.mkdir(parents=True, exist_ok=True)
    # Preserve the exact raw input files used by this report, even after refresh.
    shutil.copytree(ROOT/"data"/"bars",folder/"input_bars",dirs_exist_ok=True)
    source_hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT/"app.py", ROOT/"broker.py", ROOT/"market_data.py",ROOT/"predictor.py")}
    state = broker.state
    result = {"generated_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "config":config, "source_sha256":source_hashes,
        "metrics":metrics(state["equity"], config["initial_cash"], state["trades"]),
        "limitations":["历史回放尚未经独立复现或审计。",
            "预训练数据覆盖时间未完整审计；历史结果不能证明模型在未见数据上的表现。",
            "开盘成交采用近似：前日成交量限额、不利滑点、开盘涨跌停拒单；未模拟盘口排队。",
            "固定五只 ETF 的选择有偏差，结果不能推广到所有 A 股 ETF。",
            "现金分红按登记、除息、发放日记账；如发现数据范围内的份额拆分则中止更新，需先实现相应规则。",
            "佣金和滑点为实验假设。预测期限从信号日收盘起算 5 个交易日，成交采用其后的可执行开盘。",
            "首次盘中建立账户时只排队到未来开盘，不补记已经发生的成交。日线确认截止时间为北京时间 18:00。"],
        **extra}
    if baseline:
        result["baseline"] = metrics(baseline.state["equity"],config["initial_cash"],baseline.state["trades"])
    dump(folder/"summary.json", result)
    dump(folder/"account.json", state)
    for name in ("trades", "orders", "equity"):
        pd.DataFrame(state[name]).to_csv(folder/f"{name}.csv",index=False)
    if baseline:
        pd.DataFrame(baseline.state["equity"]).to_csv(folder/"baseline_equity.csv",index=False)
    draw_chart(folder, state, baseline)
    display = [("ending_equity","期末资产","money"),("total_return","累计收益","pct"),
        ("max_drawdown","最大回撤","pct"),("sharpe_zero_rf","夏普（零无风险利率）","num"),
        ("fills","成交笔数","int"),("total_commission","累计佣金","money"),
        ("turnover_over_initial_cash","累计双边成交额 / 初始资金","num")]
    def fmt(value, kind):
        if value is None:
            return "—"
        return {"money":f"{value:,.2f} 元","pct":f"{value:.2%}","num":f"{value:.3f}","int":str(int(value))}[kind]
    rows = "<tr><th>指标</th><th>模型策略 / 纸面账户</th>" + ("<th>等权买入持有</th>" if baseline else "") + "</tr>"
    for key,label,kind in display:
        rows += f"<tr><td>{label}</td><td>{fmt(result['metrics'][key],kind)}</td>"
        if baseline:
            rows += f"<td>{fmt(result['baseline'][key],kind)}</td>"
        rows += "</tr>"
    limits = "".join(f"<li>{html.escape(s)}</li>" for s in result["limitations"])
    context = f"<p>回放区间：{html.escape(extra['start'])} 至 {html.escape(extra['end'])}</p>" if "start" in extra else ""
    pending = extra.get("pending_order")
    if pending:
        targets = "、".join(f"{s} {html.escape(config['symbols'][s])} {w:.1%}" for s,w in pending["targets"].items()) or "全部现金"
        context += (f"<p>信号截至：{pending['asof']}；待模拟成交开盘：{pending['execute_date']}。</p>"
                    f"<p>目标配置：{targets}。只有更新账户并获得该日完整行情后才会记账。</p>")
    text = f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>Kronos ETF 虚拟交易</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#203046}}table{{border-collapse:collapse}}td{{padding:7px 20px;border-bottom:1px solid #ddd}}img{{width:100%}}code{{background:#eee;padding:3px}}li{{margin:8px}}</style>
<h1>Kronos-base · A 股 ETF 虚拟交易</h1><p>{html.escape(result['generated_at'])}</p>
<p>实验结果；费用后净值。历史回放与前向纸面交易分别记账。交易日数：{result['metrics']['sessions']}。</p>
{context}
<img src="equity.png" alt="Equity curve"><table>{rows}</table>
<h2>实验范围与限制</h2><ul>{limits}</ul><p>完整配置、模型版本和数据校验值：<a href="summary.json">summary.json</a>。</p>
<p><a href="trades.csv">成交记录</a> · <a href="orders.csv">订单记录</a> · <a href="equity.csv">每日净值</a> · <a href="account.json">账户状态</a></p></html>'''
    (folder/"report.html").write_text(text)
    print(json.dumps(result["metrics"],ensure_ascii=False,indent=2),flush=True)
    return result


def draw_chart(folder, state, baseline):
    os.environ.setdefault("MPLCONFIGDIR", str(ROOT/".cache"/"matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11,4))
    values = state["equity"]
    if values:
        ax.plot(pd.to_datetime([r["date"] for r in values]),[r["equity"] for r in values],label="Kronos / paper NAV",color="#2468c9")
    if baseline:
        items = baseline.state["equity"]
        ax.plot(pd.to_datetime([r["date"] for r in items]),[r["equity"] for r in items],label="Equal-weight buy & hold",color="#e49c32")
    ax.set_ylabel("Account value (CNY)")
    ax.grid(alpha=.2)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(folder/"equity.png",dpi=150)
    plt.close(fig)


def metadata(forecaster):
    return {"model":forecaster.identity, "data":json.loads((ROOT/"data"/"manifest.json").read_text()),
            "python":platform.python_version(), "platform":platform.platform()}


def backtest(args, config):
    from predictor import Forecaster, select_targets
    bars, events = load_bars(config), load_dividends(config)
    dates = common_dates(bars, config)
    dates = dates[dates <= min(completed_date(), pd.Timestamp(args.end) if args.end else completed_date())]
    days = dates[-(args.days+1):]
    if len(days) < args.days+1:
        raise ValueError("Insufficient history for requested backtest")
    forecaster = Forecaster(config)
    broker, baseline = Broker(config), Broker(config)
    results, pending = [], None
    # The first date is a signal-only warmup; the next opening is the first fill.
    for index, day in enumerate(days):
        if index:
            rows, previous = execution_inputs(bars, day, events)
            broker.corporate_actions(day, events)
            baseline.corporate_actions(day, events)
            if index == 1:
                baseline.rebalance(days[0],day,{s:1/len(bars) for s in bars},rows,previous)
            if pending:
                broker.rebalance(pending["asof"],day,pending["targets"],rows,previous)
                pending = None
            prices = {s:float(b.loc[day,"close"]) for s,b in bars.items()}
            broker.close(day,prices,events)
            baseline.close(day,prices,events)
        if index < len(days)-1 and (index == 0 or weekly_signal(day)):
            pool = predict_pool(forecaster,bars,events,day,config)
            results.extend(pool)
            pending = {"asof":str(day.date()),"targets":select_targets(pool,config)}
    dump(ROOT/"reports"/"backtest"/"signals.json",results)
    report(ROOT/"reports"/"backtest",broker,config,{"mode":"historical_replay",
        "start":str(days[1].date()), "end":str(days[-1].date()),**metadata(forecaster)},baseline)


def paper(args, config):
    from predictor import Forecaster, select_targets
    folder = ROOT/"state"
    folder.mkdir(exist_ok=True)
    with (folder/"paper.lock").open("w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        bars, events = load_bars(config), load_dividends(config)
        dates = common_dates(bars,config)
        latest = dates[-1]
        if latest != sessions(latest,completed_date())[-1]:
            raise ValueError("Data is stale; refresh before forward paper trading")
        path = folder/"paper.json"
        fresh = not path.exists()
        snapshot = json.loads(path.read_text()) if not fresh else None
        cfg_hash = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        if snapshot and snapshot["config_sha256"] != cfg_hash:
            raise ValueError("Paper account config changed; preserve it and create a new account explicitly")
        forecaster = Forecaster(config)
        if snapshot and snapshot["model_identity"] != forecaster.identity:
            raise ValueError("Model/device/version changed; existing paper account must retain its identity")
        broker = Broker(config, snapshot["state"] if snapshot else None)
        if not fresh:
            last = pd.Timestamp(broker.state["last_date"])
            new_days = dates[dates > last]
            for day in new_days:
                broker.corporate_actions(day,events)
                pending = broker.state["pending"]
                if pending and pd.Timestamp(pending["execute_date"]) == day:
                    opening = day.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9,minutes=30)
                    if pd.Timestamp(pending["created_at"]) >= opening:
                        raise ValueError("Paper signal was created after execution; refusing a backdated fill")
                    rows, previous = execution_inputs(bars,day,events)
                    broker.rebalance(pending["asof"],day,pending["targets"],rows,previous)
                    broker.state["pending"] = None
                elif pending and pd.Timestamp(pending["execute_date"]) < day:
                    raise ValueError("Missed execution session; review missing data")
                broker.close(day,{s:float(b.loc[day,"close"]) for s,b in bars.items()},events)
        else:
            broker.close(latest,{s:float(b.loc[latest,"close"]) for s,b in bars.items()},events)
        signals = [] if not snapshot else snapshot.get("last_signals",[])
        # Do not fabricate historical signals when several daily runs were missed.
        if (fresh or weekly_signal(latest)) and (fresh or snapshot.get("last_signal_date") != str(latest.date())):
            signals = predict_pool(forecaster,bars,events,latest,config)
            broker.state["pending"] = {"asof":str(latest.date()),
                "execute_date":str(future_open(latest).date()),
                "created_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "targets":select_targets(signals,config)}
        payload = {"config_sha256":cfg_hash,"model_identity":forecaster.identity,
            "initialized_at":snapshot.get("initialized_at", (snapshot["state"].get("pending") or {}).get("created_at")) if snapshot else datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "state":broker.state,"last_signals":signals,
            "last_signal_date":signals[0]["asof"] if signals else None}
        dump(path,payload)
        report(ROOT/"reports"/"paper",broker,config,{"mode":"forward_paper",
            "pending_order":broker.state["pending"],"last_signals":signals, **metadata(forecaster)})
        print("Paper pending:",json.dumps(broker.state["pending"],ensure_ascii=False),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default=str(ROOT/"config.json"))
    parser.add_argument("--device",choices=["auto","cpu","mps"])
    sub = parser.add_subparsers(dest="command",required=True)
    sub.add_parser("fetch")
    pred = sub.add_parser("predict")
    pred.add_argument("--symbol",default="510300")
    pred.add_argument("--asof")
    bt = sub.add_parser("backtest")
    bt.add_argument("--days",type=int,default=63)
    bt.add_argument("--end")
    sub.add_parser("paper")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.device:
        config["device"] = args.device
    for path in ("reports",".cache"):
        (ROOT/path).mkdir(exist_ok=True)
    if args.command == "fetch":
        fetch_all(config)
    elif args.command == "predict":
        from predictor import Forecaster
        bars, events = load_bars(config), load_dividends(config)
        symbol = args.symbol
        day = pd.Timestamp(args.asof) if args.asof else bars[symbol].index[-1]
        if day > completed_date():
            raise ValueError("Prediction cutoff must be a completed daily bar")
        f = Forecaster(config)
        result = f.forecast(symbol,adjusted_window(bars[symbol],events,symbol,day),day,next_sessions(day,config["horizon"]))
        dump(ROOT/"reports"/"single_prediction.json",result)
        pd.DataFrame(result["forecast"]).to_csv(ROOT/"reports"/"single_prediction.csv",index=False)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command == "backtest":
        if args.days < 2:
            parser.error("--days must be at least 2")
        backtest(args,config)
    elif args.command == "paper":
        paper(args,config)


if __name__ == "__main__":
    main()
