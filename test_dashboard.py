import json
import math
from pathlib import Path

import pytest

import gui_store
from dashboard import normalize_options


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    original=Path(__file__).resolve().parent
    config=json.loads((original/"config.json").read_text())
    (tmp_path/"config.json").write_text(json.dumps(config))
    for filename in ("app.py","broker.py","market_data.py","predictor.py"):
        (tmp_path/filename).write_text("# isolated fixture")
    (tmp_path/"state").mkdir()
    (tmp_path/"state"/"paper.json").write_text('{"sentinel":"original account"}')
    monkeypatch.setattr(gui_store,"ROOT",tmp_path)
    return tmp_path,config


def test_new_account_config_is_immutable_and_original_survives(workspace):
    root,config=workspace
    before=(root/"state/paper.json").read_bytes()
    edited={**config,"initial_cash":200000,"lookback":128}
    ident=gui_store.create_account("新实验",edited)
    assert gui_store.account_path(ident)!=root
    assert gui_store.read(gui_store.account_path(ident)/"config.json")["initial_cash"]==200000
    assert gui_store.read(root/"config.json")==config
    assert (root/"state/paper.json").read_bytes()==before
    assert not (gui_store.account_path(ident)/"state/paper.json").exists()
    assert gui_store.read(gui_store.account_path(ident)/"account_meta.json")["config_sha256"]==gui_store.config_hash(edited)


@pytest.mark.parametrize("patch",[{"initial_cash":float("nan")},{"horizon":2.5},{"commission_rate":-1},
    {"symbols":{"600000":"不是 ETF"}},{"symbols":{}},{"lookback":513},{"lot_size":1},
    {"sample_count":100},{"initial_cash":True},{"unknown_field":1}])
def test_unsafe_financial_configs_rejected(workspace,patch):
    _,config=workspace
    with pytest.raises(ValueError):
        gui_store.validate_config({**config,**patch})


@pytest.mark.parametrize("ident",["../state","original/../../","/tmp","a.b","",None])
def test_account_path_traversal_rejected(workspace,ident):
    with pytest.raises(ValueError):
        gui_store.account_path(ident)


def test_job_options_are_bounded(workspace):
    _,config=workspace
    assert normalize_options("backtest",{"days":63,"end":"2026-11-23"},config)=={"days":63,"end":"2026-11-23"}
    for options in ({"days":999999},{"days":True},{"days":2.5},{"end":"; rm -rf /"}):
        with pytest.raises((ValueError,TypeError)):
            normalize_options("backtest",options,config)
    with pytest.raises(ValueError):
        normalize_options("predict",{"symbol":"600000"},config)


def test_account_data_uses_ledger_date_for_holdings(workspace):
    root,config=workspace
    write=gui_store.write
    write(root/"state/paper.json",{"state":{"cash":99000,"positions":{"510300":100},"last_date":"2026-11-20"}})
    bar=root/"data/bars"
    bar.mkdir(parents=True)
    (bar/"510300.csv").write_text("date,close\n2026-11-20,10\n2026-11-23,99\n")
    d=gui_store.detail("original")
    assert d["holdings"][0]["value"]==1000


def test_newer_gui_prediction_takes_precedence(workspace):
    root,_=workspace
    gui_store.write(root/"reports/single_prediction.json",{"symbol":"510300","asof":"2026-11-19"})
    folder=root/"reports/gui-runs/run-abc"
    gui_store.write(folder/"job.json",{"id":"run-abc","action":"predict","status":"completed"})
    gui_store.write(folder/"prediction.json",{"symbol":"510300","asof":"2026-11-20","history":[{"date":"2026-11-20","close":10}]})
    assert gui_store.detail("original")["prediction"]["asof"]=="2026-11-20"


def test_run_list_uses_creation_time_not_random_uuid(workspace):
    root,_=workspace
    for ident,date in [("run-zzzz","2026-11-20T12:00:00+08:00"),("run-aaaa","2026-11-21T12:00:00+08:00")]:
        gui_store.write(root/"reports/gui-runs"/ident/"job.json",{"id":ident,"created_at":date})
    assert [r["id"] for r in gui_store.run_records("original")]==["run-aaaa","run-zzzz"]
