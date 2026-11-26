"""One isolated GUI job; shares immutable weights, preserves each run's outputs."""
import argparse
import json
import shutil
import types
from pathlib import Path

from gui_store import ROOT, account_path, config_hash, read, write


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--account",required=True)
    parser.add_argument("--job",required=True)
    args=parser.parse_args()
    from gui_store import checked_id
    checked_id(args.job)
    workspace=account_path(args.account)
    run=workspace/"reports"/"gui-runs"/args.job
    job=read(run/"job.json")
    config=read(workspace/"config.json")
    if job["config_sha256"]!=config_hash(config):
        raise ValueError("任务排队后配置已改变，停止执行以保留记录一致性")
    if job["action"]=="stock_predict":
        from watchlist_prediction import run_predictions
        run_predictions(job["options"],config,run)
        return
    import app
    import market_data
    import predictor
    app.ROOT=workspace
    market_data.ROOT=workspace
    (workspace/"reports").mkdir(exist_ok=True)
    (workspace/"data").mkdir(exist_ok=True)
    if job["action"]=="fetch":
        # Refresh the entire supported pool so other accounts retain their data.
        market_data.ROOT=ROOT
        base=read(ROOT/"config.json")
        from gui_store import ETF_POOL
        market_data.fetch_all({**base,"symbols":ETF_POOL})
        market_data.ROOT=workspace
    if workspace!=ROOT:
        shutil.copytree(ROOT/"data"/"bars",workspace/"data"/"bars",dirs_exist_ok=True)
        shutil.copy2(ROOT/"data"/"manifest.json",workspace/"data"/"manifest.json")
    if job["action"]=="fetch":
        write(run/"data_manifest.json",read(workspace/"data"/"manifest.json"))
        return
    if job["action"]=="predict":
        from market_data import adjusted_window,load_bars,load_dividends,next_sessions,completed_date
        import pandas as pd
        symbol=job["options"]["symbol"]
        bars=load_bars(config)[symbol]
        events=load_dividends(config)
        day=pd.Timestamp(job["options"].get("asof") or bars.index[-1])
        if day>completed_date():
            raise ValueError("预测日期必须是已确认的收盘日")
        forecaster=predictor.Forecaster(config)
        result=forecaster.forecast(symbol,adjusted_window(bars,events,symbol,day),day,next_sessions(day,config["horizon"]))
        # Include the actual trailing observations for the Chinese chart.
        result["history"]=[{"date":str(d.date()),"close":float(r.close)} for d,r in bars.loc[:day].tail(40).iterrows()]
        write(run/"prediction.json",result)
        pd.DataFrame(result["forecast"]).to_csv(run/"prediction.csv",index=False)
    elif job["action"]=="backtest":
        original_dump=app.dump
        def isolated_dump(path,value):
            try:
                relative=path.relative_to(workspace/"reports"/"backtest")
                path=run/"backtest"/relative
            except ValueError:
                pass
            original_dump(path,value)
        app.dump=isolated_dump
        original_report=app.report
        app.report=lambda folder,*a,**kw:original_report(run/"backtest",*a,**kw)
        app.backtest(types.SimpleNamespace(days=job["options"]["days"],end=job["options"].get("end")),config)
    elif job["action"]=="paper":
        # The active account advances; its before/after records are never overwritten.
        if (workspace/"state"/"paper.json").exists():
            shutil.copy2(workspace/"state"/"paper.json",run/"account-before.json")
        if (workspace/"reports"/"paper").exists():
            shutil.copytree(workspace/"reports"/"paper",run/"paper-before",dirs_exist_ok=True)
        app.paper(None,config)
        shutil.copy2(workspace/"state"/"paper.json",run/"account-after.json")
        shutil.copytree(workspace/"reports"/"paper",run/"paper",dirs_exist_ok=True)
    else:
        raise ValueError("未知任务")
    write(run/"config.json",config)
    write(run/"data_manifest.json",read(workspace/"data"/"manifest.json"))


if __name__=="__main__":
    main()
