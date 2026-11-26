"""Immutable experiment configurations and append-only GUI run records."""
import csv
import hashlib
import json
import math
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ETF_POOL = {"510300":"沪深300ETF", "510500":"中证500ETF", "159915":"创业板ETF",
            "588000":"科创50ETF", "512100":"中证1000ETF"}
ID = re.compile(r"^[a-z0-9-]{1,80}$")


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
    temp.replace(path)


def checked_id(value):
    if not isinstance(value,str) or not ID.fullmatch(value):
        raise ValueError("记录编号无效")
    return value


def account_path(account):
    checked_id(account)
    if account == "original":
        return ROOT
    path = ROOT/"experiments"/account
    if not (path/"account_meta.json").is_file():
        raise ValueError("模拟盘不存在")
    return path


def validate_config(raw):
    base = read(ROOT/"config.json")
    if not isinstance(raw,dict) or set(raw)-set(base):
        raise ValueError("模拟盘配置字段无效")
    config = {**base, **raw}
    if not isinstance(config["symbols"],dict) or not config["symbols"] or set(config["symbols"])-set(ETF_POOL):
        raise ValueError("请从已验证的五只 ETF 中选择标的")
    config["symbols"] = {s:ETF_POOL[s] for s in config["symbols"]}
    ranges = {"initial_cash":(1000,100000000),"lookback":(32,512),"horizon":(1,10),
        "max_holdings":(1,len(config["symbols"])),"signal_threshold":(-0.5,0.5),
        "seed":(0,2147483647),"top_k":(1,1024),"sample_count":(1,10),
        "commission_rate":(0,0.01),"minimum_commission":(0,1000),"slippage_bps":(0,100),
        "max_volume_participation":(0.0001,0.1)}
    integers = {"lookback","horizon","max_holdings","seed","top_k","sample_count"}
    for key,(low,high) in ranges.items():
        value = config[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
            raise ValueError(f"参数 {key} 应在 {low} 至 {high} 之间")
        if key in integers:
            if int(value)!=value:
                raise ValueError(f"参数 {key} 必须为整数")
            config[key] = int(value)
    if config["device"] not in ("auto","mps","cpu"):
        raise ValueError("计算设备无效")
    if config["lot_size"] != 100 or config["data_start"] != base["data_start"]:
        raise ValueError("本界面固定使用 100 份交易单位及现有数据起始日期")
    return config


def config_hash(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()


def create_account(name,config):
    if not isinstance(name,str) or not 1<=len(name.strip())<=40:
        raise ValueError("模拟盘名称需为 1 至 40 个字符")
    config = validate_config(config)
    ident = datetime.now().strftime("exp-%Y%m%d-%H%M%S-")+uuid.uuid4().hex[:6]
    path = ROOT/"experiments"/ident
    path.mkdir(parents=True)
    write(path/"config.json",config)
    write(path/"account_meta.json",{"id":ident,"name":name.strip(),"created_at":now(),
                                    "config_sha256":config_hash(config)})
    for name in ("app.py","broker.py","market_data.py","predictor.py"):
        (path/name).symlink_to(ROOT/name)
    return ident


def accounts():
    entries = [{"id":"original","name":"原始模拟盘","created_at":None}]
    for meta in sorted((ROOT/"experiments").glob("*/account_meta.json"),reverse=True):
        entries.append(read(meta))
    for entry in entries:
        path = account_path(entry["id"])
        config = read(path/"config.json")
        state = read(path/"state"/"paper.json",{})
        entry.update(config=config,initialized=bool(state),
            last_date=state.get("state",{}).get("last_date"))
    return entries


def run_records(account):
    path = account_path(account)
    records=[read(p) for p in (path/"reports"/"gui-runs").glob("*/job.json")]
    return sorted(records,key=lambda r:(r.get("created_at",""),r["id"]),reverse=True)[:150]


def detail(account,run_id=None):
    path = account_path(account)
    config = read(path/"config.json")
    snapshot = read(path/"state"/"paper.json",{})
    state = snapshot.get("state",{})
    manifest = read(path/"data"/"manifest.json",read(ROOT/"data"/"manifest.json",{}))
    paper_summary = read(path/"reports"/"paper"/"summary.json",{})
    run = None
    folder = path/"reports"/"backtest"
    prediction = read(path/"reports"/"single_prediction.json")
    if run_id:
        checked_id(run_id)
        run_folder = path/"reports"/"gui-runs"/run_id
        run = read(run_folder/"job.json")
        if not run:
            raise ValueError("运行记录不存在")
        folder = run_folder/"backtest"
        prediction = read(run_folder/"prediction.json")
    else:
        for item in run_records(account):
            if item["action"]=="predict" and item["status"]=="completed":
                prediction=read(path/"reports"/"gui-runs"/item["id"]/"prediction.json")
                break
    if prediction and not prediction.get("history"):
        source=path/"data"/"bars"/f"{prediction['symbol']}.csv"
        if not source.exists():
            source=ROOT/"data"/"bars"/f"{prediction['symbol']}.csv"
        if source.exists():
            with source.open() as stream:
                history=[r for r in csv.DictReader(stream) if r["date"]<=prediction["asof"]][-40:]
            prediction["history"]=[{"date":r["date"],"close":float(r["close"])} for r in history]
    stock_predictions = None
    if run_id:
        stock_predictions=read(path/"reports"/"gui-runs"/run_id/"stock_predictions.json")
    else:
        for item in run_records(account):
            if item["action"]=="stock_predict" and item["status"]=="completed":
                stock_predictions=read(path/"reports"/"gui-runs"/item["id"]/"stock_predictions.json")
                break
    equity = state.get("equity",[])
    closes = {}
    for symbol in config["symbols"]:
        barfile = path/"data"/"bars"/f"{symbol}.csv"
        if not barfile.exists():
            barfile = ROOT/"data"/"bars"/f"{symbol}.csv"
        if barfile.exists():
            with barfile.open() as stream:
                rows=list(csv.DictReader(stream))
            # Mark against the last account date, never mix a future close into NAV.
            rows=[r for r in rows if not state.get("last_date") or r["date"]<=state["last_date"]]
            if rows:
                closes[symbol]=float(rows[-1]["close"])
    holdings = [{"symbol":s,"name":config["symbols"].get(s,s),"shares":qty,
        "close":closes.get(s),"value":qty*closes[s] if s in closes else None}
        for s,qty in state.get("positions",{}).items() if qty]
    def csv_rows(name):
        file=folder/name
        if not file.exists() or not file.stat().st_size:
            return []
        with file.open() as stream:
            return list(csv.DictReader(stream))
    return {"config":config,"state":state,"holdings":holdings,"paper_summary":paper_summary,
        "snapshot":{k:v for k,v in snapshot.items() if k!="state"},"data":manifest,
        "backtest_summary":read(folder/"summary.json",{}),"backtest_equity":csv_rows("equity.csv"),
        "baseline_equity":csv_rows("baseline_equity.csv"),"backtest_trades":csv_rows("trades.csv"),
        "backtest_orders":csv_rows("orders.csv"),"prediction":prediction,"stock_predictions":stock_predictions,"run":run}
