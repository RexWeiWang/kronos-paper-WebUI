from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import gui_store
import watchlist_prediction as wp
from dashboard import normalize_options


def raw_frame():
    return pd.DataFrame([dict(date='2026-11-20',open=10,high=11,low=9,close=10,volume=100,amount=1000),
        dict(date='2026-11-23',open=5,high=6,low=4,close=5,volume=200,amount=1000),
        dict(date='2026-11-24',open=5,high=6,low=4,close=5,volume=200,amount=1000)])


def test_adjustment_excludes_future_events_and_preserves_raw_units():
    factors=pd.DataFrame([dict(date='2026-01-01',hfq_factor=1),dict(date='2026-11-23',hfq_factor=2),
                          dict(date='2026-11-25',hfq_factor=100)])
    bars,used=wp.adjusted_stock_window(raw_frame(),factors,'2026-11-24',3)
    assert bars.close.tolist()==[5,5,5]
    assert bars.volume.tolist()==[100,200,200] and bars.amount.tolist()==[1000]*3
    assert used.hfq_factor.tolist()==[1,2,2]


@pytest.mark.parametrize('problem',['future_only_factor','missing_bar','short_window','negative_factor','duplicate_factor'])
def test_missing_or_unsafe_inputs_fail_without_silent_raw_price_fallback(problem):
    raw=raw_frame();factors=pd.DataFrame([dict(date='2026-01-01',hfq_factor=1)])
    if problem=='future_only_factor':factors['date']='2026-11-25'
    if problem=='missing_bar':raw=raw.iloc[:-1]
    if problem=='short_window':raw=raw.tail(2)
    if problem=='negative_factor':factors['hfq_factor']=-1
    if problem=='duplicate_factor':factors=pd.concat([factors,factors])
    with pytest.raises(ValueError):wp.adjusted_stock_window(raw,factors,'2026-11-24',3)


def test_live_membership_is_frozen_and_not_taken_from_history(monkeypatch):
    snapshot=dict(items=[dict(code='600900',name='长江电力')],skipped=[],retrieved_at='captured')
    monkeypatch.setattr(wp,'current_watchlist',lambda:deepcopy(snapshot))
    out=normalize_options('stock_predict',{},{});snapshot['items'].clear()
    assert out['symbols']==['600900'] and out['watchlist']['items']
    with pytest.raises(ValueError):normalize_options('stock_predict',{'symbols':['002602']},{})
    with pytest.raises(ValueError):normalize_options('stock_predict',{'asof':'2020-01-01'},{})


@pytest.mark.parametrize('code',['../600900','510300','sh000001','000001.SH','AAPL','920001','600900;exit'])
def test_invalid_or_wrong_market_codes_are_rejected(code):
    with pytest.raises(ValueError):wp.stock_code(code)


def test_aliases_and_optional_names_do_not_modify_membership(monkeypatch):
    def request(path):
        if path.endswith('watchlist'):return {'stock_codes':['600900.SH','sz002602','AAPL']}
        return {'items':[{'stock_code':'600900','stock_name':'长江电力'}, {'stock_code':'600671','stock_name':'非自选'}]}
    monkeypatch.setattr(wp,'_research_json',request)
    snapshot=wp.current_watchlist()
    assert snapshot['items']==[{'code':'600900','name':'长江电力'},{'code':'002602','name':'002602'}]
    assert snapshot['skipped']==['AAPL']


@pytest.mark.parametrize("bad_ohlc",[False,True])
def test_batch_keeps_per_stock_failure_and_never_changes_existing_account(tmp_path,monkeypatch,bad_ohlc):
    import predictor
    import market_data
    raw=raw_frame();factors=pd.DataFrame([dict(date='2026-01-01',hfq_factor=1)])
    bars,_=wp.adjusted_stock_window(raw,factors,'2026-11-24',3)
    account=tmp_path/'paper.json';account.write_text('{"cash":100000,"orders":[]}')
    before=account.read_bytes()
    monkeypatch.setattr(wp,'completed_date',lambda:pd.Timestamp('2026-11-24'))
    def load(code,*args):
        if code=='002602':raise ValueError('missing history')
        return bars,{'source':'test','files':{}}
    monkeypatch.setattr(wp,'load_stock_inputs',load)
    forecaster=Mock();forecaster.forecast.return_value={'symbol':'600900','asof':'2026-11-24','forecast':[
        dict(date='2026-11-25',open=5,high=6,low=4,close=5,volume=100,amount=500)]}
    if bad_ohlc:forecaster.forecast.return_value['forecast'][0]['high']=4
    monkeypatch.setattr(predictor,'Forecaster',lambda cfg:forecaster)
    folder=tmp_path/'run';folder.mkdir()
    wp.run_predictions({'symbols':['600900','002602'],'watchlist':{'items':[{'code':'600900','name':'长江电力'},{'code':'002602','name':'世纪华通'}]}},{'horizon':1},folder)
    batch=json.loads((folder/'stock_predictions.json').read_text())
    assert [i['status'] for i in batch['items']]==['invalid' if bad_ohlc else 'completed','failed']
    if bad_ohlc:
        assert batch['items'][0]['prediction']['forecast'][0]['high']==4
        assert batch['valid_count']==0 and batch['invalid_count']==1
    assert batch['items'][0]['prediction']['prediction_only']
    assert account.read_bytes()==before


def test_connection_refuses_external_hosts(tmp_path,monkeypatch):
    monkeypatch.setattr(wp,'ROOT',tmp_path)
    (tmp_path/'research_connection.json').write_text('{"base_url":"http://example.com"}')
    with pytest.raises(ValueError):wp._research_json('/api/v1/stocks/watchlist')



def test_double_click_deduplicates_even_when_watchlist_capture_time_changes(tmp_path,monkeypatch):
    import dashboard
    config=json.loads((Path(__file__).parent/'config.json').read_text())
    (tmp_path/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(gui_store,'ROOT',tmp_path)
    monkeypatch.setattr(dashboard,'ACTIVE',{})
    executor=Mock();monkeypatch.setattr(dashboard,'EXECUTOR',executor)
    counter=[]
    def snapshot():
        counter.append(1)
        return dict(items=[dict(code='600900',name='长江电力')],retrieved_at=str(len(counter)),skipped=[])
    monkeypatch.setattr(wp,'current_watchlist',snapshot)
    first=dashboard.submit('original','stock_predict',{})
    second=dashboard.submit('original','stock_predict',{})
    assert first['id']==second['id'] and executor.submit.call_count==1
    assert first['options']['watchlist']['retrieved_at']=='1'
