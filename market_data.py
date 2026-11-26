"""Daily bars in CNY and shares; no silent replacement with synthetic data."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent


def completed_date():
    now = pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")))
    # A conservative daily-bar publication cutoff; no partial current-day bar.
    day = now.normalize().tz_localize(None)
    return day if now.hour >= 18 else day - pd.Timedelta(days=1)


def validate(df):
    cols = ["open", "high", "low", "close", "volume", "amount"]
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    if df["date"].duplicated().any():
        raise ValueError("Duplicate daily bars")
    df = df.sort_values("date").reset_index(drop=True)
    for col in cols:
        df[col] = pd.to_numeric(df[col], errors="raise")
    if not np.isfinite(df[cols].to_numpy()).all():
        raise ValueError("Non-finite market data")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Nonpositive prices")
    if ((df.high < df[["open", "close", "low"]].max(axis=1) - 1e-8) |
        (df.low > df[["open", "close", "high"]].min(axis=1) + 1e-8)).any():
        raise ValueError("Invalid OHLC ordering")
    if (df[["volume", "amount"]] < 0).any().any():
        raise ValueError("Negative turnover")
    return df


def fetch_all(config):
    import akshare as ak
    (ROOT / "data").mkdir(exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="fetch-", dir=ROOT/"data"))
    manifest = {"fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "adjustment": "raw", "volume_unit": "shares", "amount_unit": "CNY",
                "symbols": {}}
    for symbol in config["symbols"]:
        last_error = None
        for source in ("sina", "eastmoney"):
            try:
                if source == "sina":
                    frame = ak.fund_etf_hist_sina(symbol=("sh" if symbol.startswith("5") else "sz")+symbol)
                    source_name = "akshare.fund_etf_hist_sina"
                else:
                    frame = ak.fund_etf_hist_em(symbol=symbol, period="daily",
                        start_date=config["data_start"].replace("-", ""),
                        end_date=completed_date().strftime("%Y%m%d"), adjust="")
                    frame = frame.rename(columns={"日期":"date", "开盘":"open", "最高":"high",
                        "最低":"low", "收盘":"close", "成交量":"volume", "成交额":"amount"})
                    frame["volume"] = pd.to_numeric(frame["volume"]) * 100  # EM reports lots.
                    source_name = "akshare.fund_etf_hist_em"
                frame = frame[["date", "open", "high", "low", "close", "volume", "amount"]]
                frame = validate(frame)
                frame = frame[(frame.date <= completed_date()) & (frame.date >= pd.Timestamp(config["data_start"]))]
                if len(frame) < config["lookback"] + 10:
                    raise ValueError(f"Insufficient history for {symbol}: {len(frame)}")
                payload = frame.to_csv(index=False).encode()
                (target / f"{symbol}.csv").write_bytes(payload)
                manifest["symbols"][symbol] = {"rows":len(frame),
                    "last_date":str(frame.date.max().date()),
                    "sha256":hashlib.sha256(payload).hexdigest(), "source":source_name}
                print(f"{symbol}: {len(frame)} bars, last={frame.date.max().date()}, source={source}", flush=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                print(f"{symbol}: {source} failed, trying alternate source: {exc}", flush=True)
        if last_error:
            raise RuntimeError(f"Data fetch failed for {symbol}: {last_error}")
        dividends = ak.fund_open_fund_info_em(symbol=symbol, indicator="分红送配详情")
        events = []
        for _, row in dividends.iterrows():
            amount = re.search(r"派现金([0-9.]+)元", str(row["每10份分红"]))
            if amount is None:
                raise ValueError(f"Unrecognised dividend: {row.to_dict()}")
            events.append({"symbol":symbol, "record_date":str(row["权益登记日"]),
                "ex_date":str(row["除息日"]), "pay_date":str(row["分红发放日"]),
                "cash_per_share":float(amount.group(1)) / 10})
        splits = ak.fund_open_fund_info_em(symbol=symbol, indicator="拆分详情")
        # Refuse an unsupported split rather than invent a share adjustment.
        if not splits.empty:
            for _, row in splits.iterrows():
                split_date = pd.Timestamp(row["拆分折算日"])
                if pd.Timestamp(config["data_start"]) <= split_date <= completed_date():
                    raise ValueError(f"{symbol} has a split in the data window; implement it before use: {row.to_dict()}")
        (target / f"{symbol}_dividends.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
        manifest["symbols"][symbol]["dividends"] = len(events)
        manifest["symbols"][symbol]["dividends_sha256"] = hashlib.sha256(
            (target / f"{symbol}_dividends.json").read_bytes()).hexdigest()
        manifest["symbols"][symbol]["corporate_action_source"] = "akshare.fund_open_fund_info_em"
        time.sleep(0.5)
    published = ROOT / "data" / "bars"
    published.mkdir(exist_ok=True)
    for path in target.iterdir():
        os.replace(path, published/path.name)
    temp_manifest = ROOT / "data" / "manifest.tmp.json"
    temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    os.replace(temp_manifest, ROOT / "data" / "manifest.json")
    shutil.rmtree(target)


def load_bars(config):
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text())
    result = {}
    for s in config["symbols"]:
        path = ROOT / "data" / "bars" / f"{s}.csv"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["symbols"][s]["sha256"]:
            raise ValueError(f"{s}: data hash mismatch or incomplete refresh; fetch again")
        result[s] = validate(pd.read_csv(path)).set_index("date")
    return result


def sessions(start, end):
    import exchange_calendars as xcals
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cal = xcals.get_calendar("XSHG", start=f"{start.year}-01-01", end=f"{end.year}-12-31")
    dates = cal.sessions
    dates = dates.tz_localize(None) if dates.tz is not None else dates
    return dates[(dates >= start) & (dates <= end)]


def next_sessions(day, count):
    day = pd.Timestamp(day)
    for offset in (14, 28, 50):
        # Do not unnecessarily request an unannounced next-year holiday calendar.
        end = min(day + pd.Timedelta(days=offset), pd.Timestamp(f"{day.year}-12-31"))
        dates = sessions(day, end)
        future = dates[dates > day][:count]
        if len(future) == count:
            return future
    raise ValueError("Not enough verified future exchange sessions; update the holiday calendar")


def load_dividends(config):
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text())
    result = []
    for s in config["symbols"]:
        path = ROOT / "data" / "bars" / f"{s}_dividends.json"
        expected = manifest["symbols"][s].get("dividends_sha256")
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"{s}: dividend file hash mismatch; fetch again")
        result.extend(json.loads(path.read_text()))
    return result


def adjusted_window(bars, events, symbol, asof):
    """Cash-dividend adjustment using only events up to the prediction date."""
    history = bars.loc[:asof].copy()
    for event in events:
        ex = pd.Timestamp(event["ex_date"])
        if event["symbol"] == symbol and ex <= pd.Timestamp(asof):
            history.loc[history.index < ex, ["open", "high", "low", "close"]] -= event["cash_per_share"]
    return history
