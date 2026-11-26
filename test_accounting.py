import copy
import json

import pandas as pd
import pytest

from app import ROOT, future_open, weekly_signal
from broker import Broker, metrics
from market_data import adjusted_window, next_sessions, validate
from predictor import select_targets


@pytest.fixture
def config():
    return json.loads((ROOT/"config.json").read_text())


def inputs(price=10, volume=10000000):
    return {"510300":{"open":price,"prior_volume":volume}}, {"510300":10}


def test_cash_fees_and_round_lots(config):
    b=Broker(config)
    rows,prev=inputs()
    b.rebalance("2026-11-19","2026-11-20",{"510300":1},rows,prev)
    t=b.state["trades"][0]
    assert t["shares"] % 100 == 0
    assert t["shares"] == 9900
    assert b.state["cash"] == pytest.approx(100000-t["notional"]-t["fee"])
    assert b.state["cash"] >= 0


def test_same_day_fill_rejected(config):
    b=Broker(config)
    with pytest.raises(ValueError):
        b.rebalance("2026-11-20","2026-11-20",{},*inputs())


def test_sell_then_buy_never_shorts(config):
    b=Broker(config)
    b.rebalance("2026-11-19","2026-11-20",{"510300":1},*inputs())
    b.rebalance("2026-11-20","2026-11-23",{},*inputs())
    assert b.state["positions"]["510300"] == 0
    assert len(b.state["trades"])==2
    assert b.state["cash"] < 100000


def test_liquidity_cap_uses_prior_volume(config):
    b=Broker(config)
    b.rebalance("2026-11-19","2026-11-20",{"510300":1},*inputs(volume=10000))
    assert b.state["positions"]["510300"]==100
    assert b.state["orders"][0]["status"]=="partial"


def test_limit_open_does_not_fill(config):
    b=Broker(config)
    b.rebalance("2026-11-19","2026-11-20",{"510300":1},*inputs(price=11))
    assert b.state["trades"]==[]
    assert b.state["orders"][0]["reason"]=="opening_price_limit"


def test_dividend_entitlement_receivable_and_pay_date(config):
    b=Broker(config)
    b.state["cash"]=90000
    b.state["positions"]={"510300":1000}
    e=[{"symbol":"510300","record_date":"2026-11-19","ex_date":"2026-11-20",
        "pay_date":"2026-11-24","cash_per_share":0.1}]
    b.close("2026-11-19",{"510300":10},e)
    b.corporate_actions("2026-11-20",e)
    assert b.state["cash"]==90000
    assert b.nav({"510300":9.9})==pytest.approx(100000)
    b.corporate_actions("2026-11-20",e)  # repeated action is idempotent
    assert len(b.state["receivables"])==1
    b.corporate_actions("2026-11-24",e)
    assert b.state["cash"]==90100
    assert b.state["receivables"]==[]


def test_future_dividend_does_not_change_past_prediction_input():
    bars=pd.DataFrame({k:[10.,11.,12.] for k in ["open","high","low","close"]},
                      index=pd.to_datetime(["2026-11-19","2026-11-20","2026-11-23"]))
    events=[{"symbol":"x","ex_date":"2026-11-23","cash_per_share":0.5}]
    before=adjusted_window(bars,events,"x","2026-11-20")
    pd.testing.assert_frame_equal(before,bars.iloc[:2])
    after=adjusted_window(bars,events,"x","2026-11-23")
    assert after.close.iloc[0]==9.5
    assert after.close.iloc[-1]==12


def test_bad_bars_rejected():
    row={"date":"2026-11-20","open":10,"close":10,"high":9,"low":8,"volume":100,"amount":1000}
    with pytest.raises(ValueError):
        validate(pd.DataFrame([row]))


def test_target_selection_has_cash_slots(config):
    targets=select_targets([{"symbol":"a","predicted_return":.1},
                            {"symbol":"b","predicted_return":-.1}],config)
    assert targets=={"a":1/3}


def test_real_calendar_and_weekly_schedule():
    assert next_sessions("2026-11-20",1)[0]==pd.Timestamp("2026-11-23")
    assert weekly_signal(pd.Timestamp("2026-11-20"))
    assert not weekly_signal(pd.Timestamp("2026-11-23"))
    assert next_sessions("2026-09-11",1)[0] == pd.Timestamp("2026-09-14")


def test_paper_never_backdates_an_open():
    assert future_open("2026-11-23","2026-11-24 12:00:00+08:00")==pd.Timestamp("2026-11-25")
    assert future_open("2026-11-23","2026-11-24 08:00:00+08:00")==pd.Timestamp("2026-11-24")


def test_drawdown_includes_initial_cash():
    m=metrics([{"equity":90000},{"equity":95000}],100000,[])
    assert m["max_drawdown"]==pytest.approx(-.1)


def test_forecast_excludes_future_bars_and_reuses_exact_cache(tmp_path,monkeypatch,config):
    import predictor
    monkeypatch.setattr(predictor,"ROOT",tmp_path)
    f=predictor.Forecaster.__new__(predictor.Forecaster)
    f.config={**config,"lookback":2,"horizon":1}
    f.identity={"test":"causal-window"}
    calls=[]
    class Stub:
        def predict(self,df,x_timestamp,y_timestamp,**kwargs):
            calls.append(df.copy())
            return pd.DataFrame({c:[float(df[c].iloc[-1])] for c in df},index=pd.DatetimeIndex(y_timestamp))
    f.predictor=Stub()
    idx=pd.to_datetime(["2026-11-19","2026-11-20","2026-11-23"])
    bars=pd.DataFrame({c:[10.,11.,1000.] for c in ["open","high","low","close","volume","amount"]},index=idx)
    first=f.forecast("x",bars,"2026-11-20",idx[-1:])
    bars.iloc[-1,:]=1e9
    second=f.forecast("x",bars,"2026-11-20",idx[-1:])
    assert first==second
    assert len(calls)==1
    assert calls[0].close.tolist()==[10,11]


def test_forward_account_idempotence_and_no_retroactive_signals(tmp_path,monkeypatch,config):
    import app
    import predictor
    monkeypatch.setattr(app,"ROOT",tmp_path)
    config={**config,"symbols":{"510300":"test"},"lookback":2}
    idx=pd.to_datetime(["2026-11-19","2026-11-20","2026-11-23","2026-11-24","2026-11-25"])
    bars={"510300":pd.DataFrame({"open":[10.]*5,"close":[10.]*5,"volume":[10000000.]*5},index=idx)}
    current=[pd.Timestamp("2026-11-23")]
    # Advance the simulated wall clock with the fixture sessions, independent of today's date.
    from types import SimpleNamespace
    monkeypatch.setattr(app,"datetime",SimpleNamespace(now=lambda tz:
        (current[0].tz_localize("Asia/Shanghai")+pd.Timedelta(hours=18)).to_pydatetime()))
    monkeypatch.setattr(app,"load_bars",lambda c:bars)
    monkeypatch.setattr(app,"load_dividends",lambda c:[])
    monkeypatch.setattr(app,"common_dates",lambda b,c:idx[idx<=current[0]])
    monkeypatch.setattr(app,"completed_date",lambda:current[0])
    monkeypatch.setattr(app,"metadata",lambda f:{})
    monkeypatch.setattr(app,"report",lambda *a,**k:None)
    monkeypatch.setattr(app,"weekly_signal",lambda d:False)
    monkeypatch.setattr(app,"future_open",lambda d:pd.Timestamp("2026-11-25"))
    class Stub:
        identity={"test":"paper"}
        def __init__(self,c):pass
    monkeypatch.setattr(predictor,"Forecaster",Stub)
    generated=[]
    def pool(*args):
        generated.append(True)
        return [{"symbol":"510300","predicted_return":.1,"asof":"2026-11-23"}]
    monkeypatch.setattr(app,"predict_pool",pool)
    app.paper(None,config)
    state_path=tmp_path/"state"/"paper.json"
    initial=json.loads(state_path.read_text())
    app.paper(None,config)
    assert initial==json.loads(state_path.read_text())
    current[0]=pd.Timestamp("2026-11-25")
    app.paper(None,config)
    executed=json.loads(state_path.read_text())
    assert len(executed["state"]["trades"])==1
    assert executed["state"]["trades"][0]["date"]=="2026-11-25"
    assert executed["state"]["pending"] is None
    app.paper(None,config)
    assert executed==json.loads(state_path.read_text())
    assert len(generated)==1
