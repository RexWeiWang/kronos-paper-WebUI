# Kronos 预测与模拟盘

本地中文界面：A 股 ETF 模拟盘、历史回测、净值/持仓/订单，以及读取“个股研究”自选股进行单只或批量预测。个股预测不会向 ETF 账户下单。仅虚拟交易。

## 重新部署（Python 3.12）

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
mkdir -p vendor models
git clone https://github.com/shiyu-coder/Kronos.git vendor/Kronos
git -C vendor/Kronos checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a
cp model-revisions.json models/manifest.json
.venv/bin/python download_models.py
.venv/bin/python gui_control.py
```

模型从 Hugging Face 下载固定提交版本；首次安装需要联网和足够磁盘空间。默认支持 Apple MPS 或 CPU。浏览器地址及具体功能见《中文控制台使用说明.txt》。

跨平台可直接运行 Python 命令；`.command` 入口适用于 macOS。`research_connection.json` 配置个股研究地址。先启动个股研究，才能读取当前自选股。

## 记录与恢复

本仓库只托管代码、示例参数与版本清单。`.venv/`、`models/`、`vendor/`、`data/`、`reports/`、`state/`、`experiments/` 不上传。新机器默认创建新模拟盘，不包含原机器的持仓和历史；原机器记录需单独安全备份恢复。不要用空目录覆盖原记录。

`config.json` 是首版 ETF 基线参数，不包含账户凭据。使用界面另存新模拟盘，不覆盖原账户参数。

## 来源

上游 Kronos 的固定版本见 `SOURCE_PROVENANCE.json`，MIT 许可证见 `licenses/Kronos-LICENSE`；模型使用条款以对应 Hugging Face 模型卡为准。预测有效性未经未来实盘验证，历史回测不代表未来收益。
