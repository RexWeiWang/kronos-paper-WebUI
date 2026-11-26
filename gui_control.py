"""Start/open or stop the user's loopback dashboard."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT=Path(__file__).resolve().parent
URL="http://127.0.0.1:8766"


def request(path):
    with urllib.request.urlopen(URL+path,timeout=2) as response:
        return json.load(response)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop",action="store_true")
    parser.add_argument("--no-open",action="store_true")
    args=parser.parse_args()
    try:
        health=request("/api/health")
        if health.get("app")!="kronos-local-dashboard":
            raise RuntimeError("8766 端口被其他服务占用")
    except (OSError,ValueError):
        health=None
    if args.stop:
        if not health:
            print("中文控制台当前未运行。")
            return
        if request("/api/bootstrap")["active_jobs"]:
            raise SystemExit("仍有模型任务运行，请等待任务完成后再停止。")
        os.kill(health["pid"],signal.SIGINT)
        print("中文控制台已停止；模拟盘记录已保留。")
        return
    if not health:
        folder=ROOT/"state"/"gui"
        folder.mkdir(parents=True,exist_ok=True)
        with (folder/"server.log").open("a") as log:
            process=subprocess.Popen([str(ROOT/".venv/bin/python"),"-u",str(ROOT/"dashboard.py")],
                cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        for _ in range(60):
            try:
                health=request("/api/health")
                if health.get("app")=="kronos-local-dashboard":
                    break
            except (OSError,ValueError):
                pass
            if process.poll() is not None:
                raise SystemExit("启动失败，请查看 state/gui/server.log。")
            time.sleep(.2)
        else:
            raise SystemExit("启动超时，请查看 state/gui/server.log。")
    print(f"Kronos 中文控制台：{URL}")
    if not args.no_open:
        webbrowser.open(URL)


if __name__=="__main__":
    main()
