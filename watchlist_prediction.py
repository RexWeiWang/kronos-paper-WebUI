"""Read-only research watchlist bridge and isolated A-share forecasting."""
import hashlib
import json
import re
import urllib.request
from urllib.parse import urlsplit

import numpy as np
import pandas as pd

from gui_store import ROOT, now, read, write
from market_data import completed_date, next_sessions, sessions, validate


def stock_code(raw):
    text = str(raw).strip().upper()
    match = re.fullmatch(r'(?:(SH|SZ))?((?:00|30|60|68)\d{4})(?:\.(SH|SZ|SS))?', text)
    if not match:
        raise ValueError(f'暂不支持的 A 股代码：{text}')
    prefix, code, suffix = match.groups()
    expected = 'SH' if code.startswith('6') else 'SZ'
    if any(x and x.replace('SS', 'SH') != expected for x in (prefix, suffix)):
        raise ValueError(f'股票代码与交易所不匹配：{text}')
    return code


def _research_json(path):
    base = read(ROOT/'research_connection.json', {}).get('base_url', '')
    url = urlsplit(base)
    if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'} or url.username or url.password or url.query or url.fragment:
        raise ValueError('请在 research_connection.json 配置本机个股研究 base_url')
    try:
        with urllib.request.urlopen(base.rstrip('/')+path, timeout=5) as response:
            content = response.read(512*1024+1)
    except OSError as exc:
        raise ValueError('无法连接个股研究平台，请先打开个股研究桌面入口') from exc
    if len(content) > 512*1024:
        raise ValueError('个股平台响应过大')
    return json.loads(content)


def current_watchlist():
    """Always refresh from the source; never silently reuse an old watchlist."""
    response = _research_json('/api/v1/stocks/watchlist')
    codes = response.get('stock_codes')
    if not isinstance(codes, list) or len(codes) > 100:
        raise ValueError('个股平台自选列表无效')
    names = {}
    try:
        for item in _research_json('/api/v1/history/stocks').get('items', []):
            try:
                names[stock_code(item['stock_code'])] = item.get('stock_name') or item['stock_code']
            except (ValueError, KeyError):
                continue
    except (OSError, ValueError):
        pass  # Names are optional; membership never comes from report history.
    items, skipped, seen = [], [], set()
    for raw in codes:
        try:
            code = stock_code(raw)
            if code not in seen:
                items.append(dict(code=code, name=names.get(code, code)))
                seen.add(code)
        except ValueError:
            skipped.append(str(raw))
    return dict(items=items, skipped=skipped, retrieved_at=now(), source='个股研究实时自选列表')


def prediction_options(options):
    if set(options)-{'symbols'}:
        raise ValueError('个股预测只使用最新已确认日线，不支持历史信号回填')
    snapshot = current_watchlist()
    selected = options.get('symbols')
    all_codes = [v['code'] for v in snapshot['items']]
    if selected is None:
        selected = all_codes
    if not isinstance(selected, list) or not selected or len(selected) > 30 or len(set(selected)) != len(selected):
        raise ValueError('请选择 1 至 30 只不重复的当前自选股')
    if any(code not in all_codes for code in selected):
        raise ValueError('预测股票已不在当前自选，请刷新后重新选择')
    return dict(symbols=selected, watchlist=snapshot)


def adjusted_stock_window(raw, factors, day, lookback):
    """Scale historical HFQ factors to the as-of raw price; ignore future events."""
    raw = validate(raw)
    day = pd.Timestamp(day)
    if (raw.date > day).any():
        raw = raw.loc[raw.date <= day].copy()
    if raw.empty or raw.date.iloc[-1] != day:
        raise ValueError('股票行情未覆盖最新完整交易日，可能停牌或数据延迟')
    f = factors[['date', 'hfq_factor']].copy()
    f['date'] = pd.to_datetime(f['date']).dt.normalize()
    f['hfq_factor'] = pd.to_numeric(f['hfq_factor'], errors='raise')
    f = f.loc[f.date <= day].sort_values('date')
    if f.empty or f.date.duplicated().any() or not np.isfinite(f.hfq_factor).all() or (f.hfq_factor <= 0).any():
        raise ValueError('复权因子缺失或无效，不以不复权数据静默替代')
    window = raw.tail(lookback).copy()
    if len(window) != lookback:
        raise ValueError(f'需要 {lookback} 根完整日线，当前仅 {len(window)} 根')
    joined = pd.merge_asof(window, f, on='date', direction='backward')
    if joined.hfq_factor.isna().any():
        raise ValueError('历史复权因子不足，禁止用未来因子向前填充')
    prices = ['open', 'high', 'low', 'close']
    adjusted = joined.copy()
    adjusted[prices] = adjusted[prices].mul(joined.hfq_factor/joined.hfq_factor.iloc[-1], axis=0)
    # Volume/amount retain source shares/CNY units, matching existing Kronos inputs.
    return validate(adjusted[['date', *prices, 'volume', 'amount']]).set_index('date'), joined


def load_stock_inputs(code, day, config, folder):
    import akshare as ak
    ak.set_timeout((6, 15))  # Isolated GUI worker only, never the ETF server process.
    symbol = ('sh' if code.startswith('6') else 'sz')+code
    since = (pd.Timestamp(day)-pd.Timedelta(days=config['lookback']*3+90)).strftime('%Y%m%d')
    raw = ak.stock_zh_a_hist_sina(symbol=symbol, start_date=since, end_date=pd.Timestamp(day).strftime('%Y%m%d'), adjust='')
    factors = ak.stock_zh_a_hist_sina(symbol=symbol, adjust='hfq-factor')
    adjusted, used = adjusted_stock_window(raw, factors, day, config['lookback'])
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, frame in [('raw.csv', raw), ('factors.csv', factors), ('used.csv', used), ('model-input.csv', adjusted)]:
        content = frame.to_csv(index=name == 'model-input.csv').encode()
        (folder/name).write_bytes(content)
        files[name] = hashlib.sha256(content).hexdigest()
    evidence = dict(symbol=code, asof=str(pd.Timestamp(day).date()), captured_at=now(), source='akshare.stock_zh_a_hist_sina',
        adjustment='historical_hfq_factor_divided_by_asof_factor', volume_unit='shares', amount_unit='CNY',
        files=files, prediction_only=True)
    write(folder/'manifest.json', evidence)
    return adjusted, evidence


def run_predictions(options, config, folder):
    from predictor import Forecaster
    cutoff = completed_date()
    day = sessions(cutoff-pd.Timedelta(days=30), cutoff)[-1]
    names = {v['code']:v['name'] for v in options['watchlist']['items']}
    forecaster = None
    items = []
    write(folder/'watchlist.json', options['watchlist'])
    for code in options['symbols']:
        print(f'Stock prediction started: {code}', flush=True)
        try:
            bars, evidence = load_stock_inputs(code, day, config, folder/'stock-inputs'/code)
            if forecaster is None:
                forecaster = Forecaster(config)
            future = next_sessions(day, config['horizon'])
            result = forecaster.forecast(code, bars, day, future)
            quality_error = None
            try:
                validate(pd.DataFrame(result['forecast']))
                if [pd.Timestamp(r['date']) for r in result['forecast']] != list(future):
                    raise ValueError('预测日期与交易日历不一致')
            except ValueError as exc:
                quality_error = '模型开高低收或输出日期未通过校验，已隔离：'+str(exc)
            result = dict(result, stock_name=names[code], asset_type='stock', prediction_only=True, evidence=evidence,
                generated_at=now(), quality=dict(valid=quality_error is None, error=quality_error),
                history=[dict(date=str(d.date()), close=float(r.close)) for d,r in bars.tail(40).iterrows()])
            write(folder/'stock-results'/f'{code}.json', result)
            pd.DataFrame(result['forecast']).to_csv(folder/'stock-results'/f'{code}.csv', index=False)
            items.append(dict(code=code, name=names[code], status='invalid' if quality_error else 'completed', prediction=result, error=quality_error))
            print(f'Stock prediction completed: {code}', flush=True)
        except Exception as exc:
            items.append(dict(code=code, name=names[code], status='failed', error=f'{type(exc).__name__}: {exc}'))
            print(f'Stock prediction failed: {code}: {exc}', flush=True)
        write(folder/'stock_predictions.json', dict(asof=str(day.date()), generated_at=now(), items=items, prediction_only=True, valid_count=sum(i['status']=='completed' for i in items),
            invalid_count=sum(i['status']=='invalid' for i in items), failed_count=sum(i['status']=='failed' for i in items)))
    if not any(i.get('prediction') for i in items):
        raise ValueError('所有股票预测失败，请查看各股错误及日志')
    write(folder/'config.json', config)
