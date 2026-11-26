import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache" / "matplotlib"))
sys.path.insert(0, str(ROOT / "vendor" / "Kronos"))


class Forecaster:
    def __init__(self, config):
        import torch
        from model import Kronos, KronosTokenizer, KronosPredictor
        self.config = config
        torch.set_num_threads(4)
        device = config["device"]
        if device == "auto":
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        model = Kronos.from_pretrained(str(ROOT / "models" / "Kronos-base"))
        tokenizer = KronosTokenizer.from_pretrained(str(ROOT / "models" / "Kronos-Tokenizer-base"))
        model.eval()
        tokenizer.eval()
        self.predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        self.device = device
        self.parameters = {"model":sum(p.numel() for p in model.parameters()),
                           "tokenizer":sum(p.numel() for p in tokenizer.parameters())}
        self.identity = {"models":json.loads((ROOT / "models" / "manifest.json").read_text()),
            "code_commit":subprocess.check_output(["git", "-C", str(ROOT / "vendor" / "Kronos"),
                                                   "rev-parse", "HEAD"], text=True).strip(),
            "torch":torch.__version__, "device":device, "parameters":self.parameters}
        print(f"Loaded Kronos-base: {self.parameters}, device={device}", flush=True)

    def forecast(self, symbol, bars, asof, future):
        import torch
        asof = pd.Timestamp(asof)
        window = bars.loc[:asof].tail(self.config["lookback"])
        if len(window) != self.config["lookback"] or window.index[-1] != asof:
            raise ValueError(f"Missing full input window for {symbol} at {asof.date()}")
        options = {k:self.config[k] for k in ("lookback", "horizon", "seed", "top_k", "sample_count")}
        key_input = window.to_csv() + json.dumps(options, sort_keys=True) + json.dumps(self.identity, sort_keys=True) + str(list(future))
        digest = hashlib.sha256(key_input.encode()).hexdigest()
        folder = ROOT / "data" / "predictions"
        folder.mkdir(parents=True, exist_ok=True)
        cache = folder / f"{symbol}_{asof.date()}_{digest[:16]}.json"
        input_file = cache.with_suffix(".input.csv")
        if not input_file.exists():
            window.to_csv(input_file)
        if cache.exists():
            result = json.loads(cache.read_text())
            return result
        torch.manual_seed(self.config["seed"])
        np.random.seed(self.config["seed"])
        started = time.perf_counter()
        predicted = self.predictor.predict(window.reset_index(drop=True),
            pd.Series(window.index), pd.Series(future), pred_len=len(future),
            T=1.0, top_k=self.config["top_k"], top_p=0.9,
            sample_count=self.config["sample_count"], verbose=False)
        if not np.isfinite(predicted.to_numpy()).all() or (predicted.close <= 0).any():
            raise ValueError("Invalid model output")
        result = {"symbol":symbol, "asof":str(asof.date()),
            "predicted_return":float(predicted.close.iloc[-1] / window.close.iloc[-1] - 1),
            "input_last_close":float(window.close.iloc[-1]), "input_sha256":digest,
            "seconds":time.perf_counter()-started, "identity":self.identity, "options":options,
            "forecast":[{"date":str(d.date()), **{k:float(v) for k,v in row.items()}}
                        for d,row in predicted.iterrows()]}
        cache.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"{symbol} {asof.date()}: {result['predicted_return']:+.2%}, {result['seconds']:.2f}s", flush=True)
        return result


def select_targets(results, config):
    eligible = sorted([r for r in results if r["predicted_return"] > config["signal_threshold"]],
                      key=lambda r:(-r["predicted_return"], r["symbol"]))[:config["max_holdings"]]
    # Keep one slot in cash for each unfilled position.
    return {r["symbol"]:1 / config["max_holdings"] for r in eligible}
