"""Loopback-only Chinese dashboard. No third-party web packages or remote assets."""
import argparse
import fcntl
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlencode

from gui_store import ROOT, account_path, accounts, checked_id, config_hash, create_account, detail, now, read, run_records, write

TOKEN=secrets.token_urlsafe(32)
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix="kronos-job")
MUTEX=threading.Lock()
ACTIVE={}
ACTIONS={"fetch","predict","backtest","paper","stock_predict"}


def normalize_options(action,options,config):
    from datetime import date
    if not isinstance(options,dict):
        raise ValueError("任务参数无效")
    if action=="stock_predict":
        from watchlist_prediction import prediction_options
        return prediction_options(options)
    result={}
    if action=="predict":
        symbol=options.get("symbol",next(iter(config["symbols"])))
        if symbol not in config["symbols"]:
            raise ValueError("预测标的必须属于当前模拟盘")
        result["symbol"]=symbol
    if action=="backtest":
        days=options.get("days",63)
        if isinstance(days,bool) or not isinstance(days,int) or not 2<=days<=756:
            raise ValueError("回测长度应为 2 至 756 个交易日")
        result["days"]=days
    for key in ("asof",) if action=="predict" else (("end",) if action=="backtest" else ()):
        if options.get(key):
            result[key]=date.fromisoformat(options[key]).isoformat()
    return result


def execute_job(account,job):
    path=account_path(account)/"reports"/"gui-runs"/job["id"]
    try:
        job.update(status="running",started_at=now())
        write(path/"job.json",job)
        with (path/"output.log").open("w") as output:
            result=subprocess.run([str(ROOT/".venv"/"bin"/"python"),"-u",str(ROOT/"gui_worker.py"),
                "--account",account,"--job",job["id"]],cwd=ROOT,stdout=output,stderr=subprocess.STDOUT)
        job.update(status="completed" if result.returncode==0 else "failed",returncode=result.returncode)
        if result.returncode==0 and job["action"]=="stock_predict":
            batch=read(path/"stock_predictions.json",{})
            job["result_summary"]={key:batch.get(key,0) for key in ("valid_count","invalid_count","failed_count")}
        if result.returncode:
            lines=(path/"output.log").read_text(errors="replace").strip().splitlines()
            job["error"]=lines[-1] if lines else "任务执行失败，请查看日志"
    except Exception as exc:
        job.update(status="failed",error=str(exc))
        (path/"output.log").write_text(traceback.format_exc())
    finally:
        job["finished_at"]=now()
        write(path/"job.json",job)
        with MUTEX:
            ACTIVE.pop(job["id"],None)


def submit(account,action,options):
    workspace=account_path(account)
    if action not in ACTIONS:
        raise ValueError("不支持的任务类型")
    config=read(workspace/"config.json")
    options=normalize_options(action,options,config)
    with MUTEX:
        if len(ACTIVE)>=8:
            raise ValueError("排队任务较多，请等待当前任务完成")
        # A double click or browser retry must not submit the same action twice.
        for job in ACTIVE.values():
            same_options=(job["options"].get("symbols")==options.get("symbols")) if action=="stock_predict" else job["options"]==options
            if job["account"]==account and job["action"]==action and same_options:
                return dict(job)
        ident="run-"+uuid.uuid4().hex
        job={"id":ident,"account":account,"action":action,"options":options,
            "config_sha256":config_hash(config),"status":"queued","created_at":now()}
        write(workspace/"reports"/"gui-runs"/ident/"job.json",job)
        ACTIVE[ident]=job
        EXECUTOR.submit(execute_job,account,job)
        return dict(job)


class Handler(BaseHTTPRequestHandler):
    server_version="KronosLocal/1.0"

    def send(self,code,data,content_type="application/json; charset=utf-8"):
        if isinstance(data,(dict,list)):
            data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        elif isinstance(data,str):
            data=data.encode()
        self.send_response(code)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.valid_host():
            return self.send(403,{"error":"仅允许本机访问"})
        try:
            parsed=urlparse(self.path)
            query=parse_qs(parsed.query)
            get=lambda key,default=None:query.get(key,[default])[0]
            if parsed.path=="/api/health":
                return self.send(200,{"app":"kronos-local-dashboard","pid":os.getpid()})
            if parsed.path=="/api/bootstrap":
                with MUTEX:
                    active=[dict(j) for j in ACTIVE.values()]
                return self.send(200,{"token":TOKEN,"accounts":accounts(),"active_jobs":active})
            if parsed.path=="/api/watchlist":
                from watchlist_prediction import current_watchlist
                return self.send(200,current_watchlist())
            if parsed.path=="/api/account":
                return self.send(200,detail(get("account","original"),get("run")))
            if parsed.path=="/api/runs":
                return self.send(200,run_records(get("account","original")))
            if parsed.path=="/api/job":
                account,ident=get("account","original"),checked_id(get("id"))
                path=account_path(account)/"reports"/"gui-runs"/ident
                job=read(path/"job.json")
                if not job:
                    return self.send(404,{"error":"任务不存在"})
                logfile=path/"output.log"
                job["log"]=logfile.read_text(errors="replace")[-16000:] if logfile.exists() else "等待执行…"
                return self.send(200,job)
            if parsed.path=="/download":
                workspace=account_path(get("account","original"))
                ident=get("run")
                base=workspace/"reports"/("gui-runs/"+checked_id(ident) if ident else "backtest")
                requested=get("file","")
                allowed={"report.html","equity.png","summary.json","trades.csv","orders.csv","equity.csv",
                    "backtest/report.html","backtest/equity.png","backtest/summary.json","backtest/trades.csv",
                    "backtest/orders.csv","backtest/equity.csv","prediction.json","prediction.csv","output.log",
                    "account.json","backtest/account.json","stock_predictions.json","watchlist.json"}
                if requested not in allowed:
                    raise ValueError("不支持的文件")
                path=base/requested
                if not path.is_file():
                    return self.send(404,{"error":"该文件尚未生成"})
                data=path.read_bytes()
                if path.suffix==".html":
                    content=data.decode()
                    prefix="backtest/" if requested.startswith("backtest/") else ""
                    for filename in ("equity.png","summary.json","trades.csv","orders.csv","equity.csv","account.json"):
                        params={"account":get("account","original"),"file":prefix+filename}
                        if ident:
                            params["run"]=ident
                        content=content.replace(f'="{filename}"',f'="/download?{urlencode(params)}"')
                    data=content.encode()
                return self.send(200,data,mimetypes.guess_type(path.name)[0] or "text/plain; charset=utf-8")
            assets={"/":"index.html","/dashboard.js":"dashboard.js","/dashboard.css":"dashboard.css"}
            if parsed.path in assets:
                path=ROOT/"web"/assets[parsed.path]
                return self.send(200,path.read_bytes(),mimetypes.guess_type(path.name)[0]+"; charset=utf-8")
            return self.send(404,{"error":"页面不存在"})
        except (ValueError,KeyError,TypeError) as exc:
            self.send(400,{"error":str(exc)})
        except Exception:
            traceback.print_exc()
            self.send(500,{"error":"读取记录失败，请查看本地服务日志"})

    def do_POST(self):
        origin=self.headers.get("Origin")
        allowed={f"http://127.0.0.1:{self.server.server_port}",f"http://localhost:{self.server.server_port}"}
        if not self.valid_host() or origin not in allowed or self.headers.get("X-Kronos-Token")!=TOKEN:
            return self.send(403,{"error":"请求校验失败，请刷新本地页面"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=32768:
                raise ValueError("请求内容过大或为空")
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):
                raise ValueError("请求内容必须是对象")
            if self.path=="/api/accounts":
                ident=create_account(body.get("name"),body.get("config"))
                return self.send(201,{"id":ident})
            if self.path=="/api/jobs":
                return self.send(202,submit(body.get("account"),body.get("action"),body.get("options",{})))
            return self.send(404,{"error":"操作不存在"})
        except (ValueError,TypeError,KeyError) as exc:
            self.send(400,{"error":str(exc)})

    def log_message(self,fmt,*args):
        if "/api/job?" not in str(args[0]):
            super().log_message(fmt,*args)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8766)
    args=parser.parse_args()
    path=ROOT/"state"/"gui"
    path.mkdir(parents=True,exist_ok=True)
    lock=(path/"server.lock").open("w")
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    # Never auto-replay an interrupted trade or quietly lose a queued job.
    for workspace in [ROOT,*[p.parent for p in (ROOT/"experiments").glob("*/account_meta.json")]]:
        for file in (workspace/"reports"/"gui-runs").glob("*/job.json"):
            job=read(file)
            if job["status"] in ("queued","running"):
                job.update(status="interrupted",finished_at=now(),error="上次服务中断；请检查账户记录后重新运行")
                write(file,job)
    server=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    write(path/"server.json",{"pid":os.getpid(),"port":args.port,"started_at":now()})
    print(f"Kronos 中文控制台：http://127.0.0.1:{args.port}",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        EXECUTOR.shutdown(wait=True)


if __name__=="__main__":
    main()
