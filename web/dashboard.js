"use strict";
const $=id=>document.getElementById(id);
const esc=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=v=>v==null?"—":Number(v).toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct=v=>v==null?"—":`${(Number(v)*100).toFixed(2)}%`;
const dateTime=v=>v?new Date(v).toLocaleString("zh-CN",{hour12:false}):"—";
const actionNames={fetch:"更新行情",predict:"单标的预测",backtest:"历史回测",paper:"更新模拟盘",stock_predict:"自选股预测"};
const statusNames={queued:"排队中",running:"运行中",completed:"已完成",failed:"失败",interrupted:"已中断"};
const titles={overview:["模拟盘概览","把预测变成可追踪的实验，让每一次交易都有记录。"],predict:["模型预测","查看 ETF 或当前自选股的量价预测，股票预测独立保存。"],backtest:["历史回测","在同一组行情上，比较模型策略与等权买入持有。"],settings:["参数与新模拟盘","从现有配置出发，创建保留独立记录的新实验。"]};
const pool={"510300":"沪深300ETF","510500":"中证500ETF","159915":"创业板ETF","588000":"科创50ETF","512100":"中证1000ETF"};
let store={account:localStorage.getItem("kronos-account")||"original",accounts:[],detail:null,runs:[],backtest:null,backtestRun:"",tab:"overview",accountView:"holdings",token:"",watchlist:null,watched:new Map()};

async function api(path,body){
  const response=await fetch(path,{method:body?"POST":"GET",headers:body?{"Content-Type":"application/json","X-Kronos-Token":store.token}:{},body:body?JSON.stringify(body):undefined});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||"本地服务请求失败");
  return data;
}
function notice(message,error=false){$("notice").hidden=!message;$("notice").textContent=message;$("notice").className="notice"+(error?" error":"");}
function safe(fn){return async event=>{try{await fn(event);}catch(error){notice(error.message,true);}};}
function changeTab(tab){
  store.tab=tab;
  document.querySelectorAll(".view").forEach(v=>v.hidden=v.id!==`view-${tab}`);
  document.querySelectorAll(".nav").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));
  $("page-title").textContent=titles[tab][0];$("breadcrumb").textContent=titles[tab][0];$("page-description").textContent=titles[tab][1];
  window.scrollTo({top:0,behavior:"instant"});
  if(store.detail){if(tab==="backtest")renderBacktest();if(tab==="predict")renderPrediction();if(tab==="overview")renderAccount();}
}
function table(columns,rows,emptyTitle="暂无记录",emptyNote="运行相应任务后，记录会显示在这里。"){
  if(!rows?.length)return `<div class="empty"><strong>${esc(emptyTitle)}</strong>${esc(emptyNote)}</div>`;
  let content=`<table><thead><tr>${columns.map(c=>`<th>${esc(c[0])}</th>`).join("")}</tr></thead><tbody>`;
  content+=rows.slice(-500).reverse().map(row=>`<tr>${columns.map(c=>`<td>${c[1](row)}</td>`).join("")}</tr>`).join("");
  content+="</tbody></table>";
  if(rows.length>500)content+=`<p class="record-limit">显示最近 500 条；完整记录保存在本地报告中。</p>`;
  return content;
}
function metrics(node,items){$(node).innerHTML=items.map(i=>`<div class="metric"><div class="metric-label">${esc(i[0])}</div><div class="metric-value ${i[3]||""}">${esc(i[1])}</div><div class="metric-note">${esc(i[2])}</div></div>`).join("");}
function drawChart(node,series,kind="money",empty="暂无净值记录。初始化模拟盘后，随着行情更新积累曲线。"){
  const host=$(node),valid=series.flatMap(s=>s.points.filter(p=>Number.isFinite(Number(p.value))));
  if(!valid.length){host.innerHTML=`<div class="empty"><strong>等待实验数据</strong>${esc(empty)}</div>`;return;}
  const dates=[...new Set(valid.map(p=>p.date))].sort(),all=valid.map(p=>Number(p.value));
  let low=Math.min(...all),high=Math.max(...all),range=high-low;
  const padding=range*.14||Math.abs(high)*.015||1;low-=padding;high+=padding;range=high-low;
  const W=Math.max(280,host.clientWidth||600),H=W<500?260:300,L=W<500?61:76,R=18,T=20,B=40,plotW=W-L-R,plotH=H-T-B;
  const time=d=>Date.parse(`${d.slice(0,10)}T00:00:00Z`),min=time(dates[0]),max=time(dates.at(-1));
  const x=d=>max===min?L+plotW/2:L+(time(d)-min)/(max-min)*plotW;
  const y=v=>T+(high-Number(v))/range*plotH;
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${kind==="money"?"账户净值曲线":"历史价格与预测曲线"}">`;
  for(let i=0;i<5;i++){const val=low+range*i/4,Y=y(val);svg+=`<line x1="${L}" x2="${W-R}" y1="${Y}" y2="${Y}" stroke="#eaf0f6"/><text x="${L-12}" y="${Y+4}" text-anchor="end">${kind==="money"?Number(val).toLocaleString("en",{maximumFractionDigits:0}):val.toFixed(3)}</text>`;}
  const ticks=[...new Set(W<500?[0,Math.floor((dates.length-1)/2),dates.length-1]:[0,Math.floor((dates.length-1)/3),Math.floor((dates.length-1)*2/3),dates.length-1])];
  ticks.forEach(i=>{svg+=`<text x="${x(dates[i])}" y="${H-11}" text-anchor="middle">${esc(dates[i].slice(5))}</text>`;});
  series.forEach((s,idx)=>{
    const points=s.points.filter(p=>Number.isFinite(Number(p.value)));
    if(!points.length)return;
    const path=points.map((p,i)=>`${i?"L":"M"}${x(p.date).toFixed(2)},${y(p.value).toFixed(2)}`).join(" ");
    if(idx===0&&points.length>1&&!s.dashed)svg+=`<path d="${path} L${x(points.at(-1).date)},${H-B} L${x(points[0].date)},${H-B} Z" fill="#3264dc08"/>`;
    svg+=`<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2.3" ${s.dashed?'stroke-dasharray="6 5"':""}/>`;
    if(points.length===1)svg+=`<circle cx="${x(points[0].date)}" cy="${y(points[0].value)}" r="4" fill="${s.color}"/>`;
  });
  svg+=`<line data-crosshair x1="0" x2="0" y1="${T}" y2="${H-B}" stroke="#a7b7d4" stroke-dasharray="4 4" visibility="hidden"/><rect x="${L}" y="${T}" width="${plotW}" height="${plotH}" fill="transparent" data-hover/></svg>`;
  host.innerHTML=svg;
  const canvas=host.querySelector("svg"),overlay=host.querySelector("[data-hover]"),cross=host.querySelector("[data-crosshair]");
  overlay.addEventListener("pointermove",event=>{
    const box=canvas.getBoundingClientRect(),point=(event.clientX-box.left)/box.width*W;
    const closest=dates.reduce((a,b)=>Math.abs(x(b)-point)<Math.abs(x(a)-point)?b:a);
    cross.setAttribute("x1",x(closest));cross.setAttribute("x2",x(closest));cross.setAttribute("visibility","visible");
    const text=[closest];series.forEach(s=>{const p=s.points.find(p=>p.date===closest);if(p)text.push(`${s.name}：${kind==="money"?money(p.value)+" 元":Number(p.value).toFixed(3)+" 元"}`);});
    $("tooltip").textContent=text.join("\n");$("tooltip").hidden=false;$("tooltip").style.left=Math.min(event.clientX+14,innerWidth-230)+"px";$("tooltip").style.top=Math.min(event.clientY+12,innerHeight-120)+"px";
  });
  overlay.addEventListener("pointerleave",()=>{$("tooltip").hidden=true;cross.setAttribute("visibility","hidden");});
}
function renderAccount(){
  const d=store.detail,c=d.config,s=d.state,last=s.equity?.at(-1),nav=last?.equity??c.initial_cash;
  const ret=nav/c.initial_cash-1,dd=d.paper_summary.metrics?.max_drawdown;
  $("account-status").textContent=s.last_date?"已初始化":"待初始化";
  $("account-status").className="badge"+(s.last_date?"":" subtle");
  const dates=Object.values(d.data.symbols||{}).map(x=>x.last_date).sort();
  $("data-date").textContent=dates.length?`缓存日线截至 ${dates[0]}`:"尚无行情缓存";
  metrics("paper-metrics",[["总资产（元）",money(nav),s.last_date?`账本截至 ${s.last_date}`:"初始资金 · 尚未记账"],
    ["可用现金（元）",money(s.cash??c.initial_cash),`应收分红 ${money(last?.receivables||0)} 元`],
    ["累计收益",pct(ret),`初始资金 ${money(c.initial_cash)} 元`,ret>0?"positive":ret<0?"negative":""],
    ["最大回撤",pct(dd),s.last_date?"前向模拟盘记录":"初始化后开始统计"]]);
  $("paper-period").textContent=s.equity?.length?`${s.equity[0].date} 至 ${s.last_date} · ${s.equity.length} 条净值记录`:"前向纸面交易 · 等待初始化";
  drawChart("paper-chart",[{name:"模拟盘资产",color:"#3264dc",points:(s.equity||[]).map(r=>({date:r.date,value:r.equity}))}]);
  $("strategy-summary").innerHTML=`<dl><dt>模型</dt><dd>Kronos-base</dd><dt>标的池</dt><dd>${Object.keys(c.symbols).length} 只 ETF</dd><dt>历史 / 预测窗口</dt><dd>${c.lookback} / ${c.horizon} 日</dd><dt>最大持仓</dt><dd>${c.max_holdings} 只</dd><dt>调仓频率</dt><dd>每周</dd><dt>信号门槛</dt><dd>${pct(c.signal_threshold)}</dd><dt>单边滑点</dt><dd>${c.slippage_bps} 基点</dd><dt>运行设备</dt><dd>${esc(d.snapshot.model_identity?.device||c.device)}</dd></dl>`;
  $("update-paper").textContent=s.last_date?"更新模拟盘":"初始化模拟盘";
  renderAccountTable();
  renderPredictOptions();
  renderPrediction();
}
const symbolCell=(row)=>`<span class="symbol-code">${esc(row.symbol)}</span><small>${esc(store.detail.config.symbols[row.symbol]||pool[row.symbol]||"")}</small>`;
const side=qty=>Number(qty)>0?"买入":"卖出";
const tradeColumns=[["交易日",r=>esc(r.date)],["ETF",symbolCell],["方向",r=>side(r.shares)],["份额",r=>Math.abs(r.shares)],["成交价",r=>Number(r.price).toFixed(3)],["佣金（元）",r=>money(r.fee)],["剩余现金（元）",r=>money(r.cash_after)]];
function renderAccountTable(){
  if(!store.detail)return;
  const d=store.detail,s=d.state;
  if(store.accountView==="holdings")$("account-table").innerHTML=table([["ETF",symbolCell],["持有份额",r=>esc(r.shares)],["账本日收盘价",r=>r.close?.toFixed(3)??"—"],["持仓市值（元）",r=>money(r.value)]],d.holdings,"当前没有持仓",s.last_date?"账户可能持有现金，或正在等待首次模拟成交。":"初始化模拟盘后，持仓会显示在这里。");
  if(store.accountView==="pending"){
    const p=s.pending,rows=p?Object.entries(p.targets).map(([symbol,weight])=>({symbol,weight,asof:p.asof,date:p.execute_date})):[];
    $("account-table").innerHTML=table([["ETF",symbolCell],["目标权重",r=>pct(r.weight)],["信号截至",r=>esc(r.asof)],["待模拟开盘",r=>esc(r.date)]],rows,p?"目标配置为全部现金":"没有待执行信号",p?`待模拟开盘 ${p.execute_date}；更新后按持仓情况生成卖出。`:"每周信号生成后，待执行的目标配置会显示在这里。");
    if(p)$("account-table").innerHTML+=`<p class="hint">创建于 ${esc(dateTime(p.created_at))}。待执行的是目标配置，成交数量将在模拟开盘时计算。</p>`;
  }
  if(store.accountView==="orders")$("account-table").innerHTML=table([["交易日",r=>esc(r.date)],["ETF",symbolCell],["方向",r=>side(r.requested_shares)],["委托 / 成交份额",r=>`${Math.abs(r.requested_shares)} / ${Math.abs(r.filled_shares||0)}`],["状态",r=>esc(({filled:"已成交",partial:"部分成交",skipped:"未成交"})[r.status]||r.status)],["原因",r=>esc(({opening_price_limit:"开盘触及涨跌停",cash_or_prior_liquidity:"现金或前日流动性不足"})[r.reason]||"—")]],s.orders||[]);
  if(store.accountView==="trades")$("account-table").innerHTML=table(tradeColumns,s.trades||[]);
}
function renderPrediction(){
  const stocks=$("predict-source").value==="stocks";
  const item=store.detail.stock_predictions?.items?.find(i=>i.code===$("predict-symbol").value);
  const p=stocks?item?.prediction:store.detail.prediction;
  if(stocks)renderStockResults();
  if(!p){$("prediction-info").textContent=item?.error||"运行预测后查看结果";drawChart("prediction-chart",[],"price","选择标的并运行预测。");$("prediction-table").innerHTML="";return;}
  $("prediction-info").textContent=`${item?.status==="invalid"?"⚠ 模型输出未通过校验，以下仅供诊断。 ":""}${p.symbol} · ${p.stock_name||store.detail.config.symbols[p.symbol]||pool[p.symbol]||""} · 截至 ${p.asof} · 预测 ${p.forecast.length} 个交易日 · 期末模型涨跌幅 ${pct(p.predicted_return)}${p.prediction_only?" · 仅预测，不生成委托":""}`;
  const history=(p.history||[{date:p.asof,close:p.input_last_close}]).map(r=>({date:r.date,value:r.close}));
  drawChart("prediction-chart",[{name:"历史收盘",color:"#3264dc",points:history},{name:"模型预测",color:"#b07bdf",dashed:true,points:[history.at(-1),...p.forecast.map(r=>({date:r.date,value:r.close}))]}],"price");
  $("prediction-table").innerHTML=table([["预测日期",r=>esc(r.date)],["开盘",r=>r.open.toFixed(3)],["最高",r=>r.high.toFixed(3)],["最低",r=>r.low.toFixed(3)],["收盘",r=>r.close.toFixed(3)]],[...p.forecast].reverse());
}
function renderBacktest(){
  const d=store.backtest||store.detail,m=d.backtest_summary?.metrics,b=d.backtest_summary?.baseline;
  metrics("backtest-metrics",[["策略收益",pct(m?.total_return),"已计入佣金与滑点",m?.total_return>0?"positive":m?.total_return<0?"negative":""],
    ["基准收益",pct(b?.total_return),"同一标的池等权买入持有"],["策略最大回撤",pct(m?.max_drawdown),"含初始资金到首日的变动"],
    ["策略成交笔数",m?.fills??"—",m?`佣金合计 ${money(m.total_commission)} 元`:"运行回测后查看"]]);
  $("backtest-period").textContent=m?`${d.backtest_summary.start} 至 ${d.backtest_summary.end} · ${m.sessions} 个交易日`:"当前模拟盘尚无回测结果";
  drawChart("backtest-chart",[{name:"Kronos 策略",color:"#3264dc",points:(d.backtest_equity||[]).map(r=>({date:r.date,value:+r.equity}))},{name:"等权基准",color:"#aab7cb",points:(d.baseline_equity||[]).map(r=>({date:r.date,value:+r.equity}))}],"money","设置回测长度并运行历史回测。");
  $("backtest-trades").innerHTML=table(tradeColumns,d.backtest_trades||[]);
  $("download-backtest").hidden=!m;
  $("download-backtest").href=store.backtestRun?`/download?account=${store.account}&run=${store.backtestRun}&file=backtest/report.html`:`/download?account=${store.account}&file=report.html`;
}
function fillSettings(){
  const c=store.detail.config;
  const multiplier={signal_threshold:100,commission_rate:100,max_volume_participation:100};
  for(const [key,value]of Object.entries(c)){if($(key))$(key).value=typeof value==="number"?Number((value*(multiplier[key]||1)).toFixed(8)):value;}
  $("new-name").value="";
  $("symbol-options").innerHTML=Object.entries(pool).map(([s,n])=>`<label class="symbol-choice"><input type="checkbox" name="symbols" value="${s}" ${s in c.symbols?"checked":""}><span>${esc(n)}<small>${s}</small></span></label>`).join("");
  $("symbol-options").onchange=()=>{const count=document.querySelectorAll('[name="symbols"]:checked').length;$("max_holdings").max=count||1;if(+$("max_holdings").value>count&&count)$("max_holdings").value=count;};
}
function renderRuns(){
  const rows=[...store.runs].reverse();
  $("runs-table").innerHTML=table([["创建时间",r=>esc(dateTime(r.created_at))],["任务",r=>esc(actionNames[r.action])],["状态",r=>`<span class="badge ${r.status==="failed"||r.status==="interrupted"?"failed":r.status==="running"?"busy":r.status==="queued"?"queued":""}">${esc(statusNames[r.status])}</span>`],["操作",r=>`<button class="table-button" data-log="${r.id}">日志</button>${r.status==="completed"&&["predict","stock_predict","backtest"].includes(r.action)?`<button class="table-button" data-result="${r.id}" data-action="${r.action}">查看结果</button>`:""}`]],rows,"暂无新运行记录","已有账户和部署时的首次回测已保留。通过上方按钮开始新实验。");
  $("runs-table").querySelectorAll("[data-log]").forEach(button=>button.onclick=safe(async()=>showJob(await api(`/api/job?account=${store.account}&id=${button.dataset.log}`))));
  $("runs-table").querySelectorAll("[data-result]").forEach(button=>button.onclick=safe(async()=>{
    if(button.dataset.action==="backtest"){$("backtest-result").value=button.dataset.result;await loadBacktest(button.dataset.result);changeTab("backtest");}
    else{const detail=await api(`/api/account?account=${store.account}&run=${button.dataset.result}`);if(button.dataset.action==="stock_predict"){store.detail.stock_predictions=detail.stock_predictions;$("predict-source").value="stocks";}else{store.detail.prediction=detail.prediction;$("predict-source").value="etf";}renderPredictOptions();renderPrediction();changeTab("predict");}
  }));
}
async function loadBacktest(run){store.backtestRun=run;store.backtest=run?await api(`/api/account?account=${store.account}&run=${run}`):null;renderBacktest();}
async function loadAccount(resetForm=true){
  const account=store.account;
  const [d,runs]=await Promise.all([api(`/api/account?account=${account}`),api(`/api/runs?account=${account}`)]);
  if(account!==store.account)return;
  store.detail=d;store.runs=runs;renderAccount();renderRuns();if(resetForm)fillSettings();
  const options=store.runs.filter(r=>r.action==="backtest"&&r.status==="completed");
  const had=store.backtestRun;
  $("backtest-result").innerHTML=`<option value="">${account==="original"?"部署时的首次回测（保留）":"选择回测记录"}</option>`+options.map(r=>`<option value="${r.id}">${esc(dateTime(r.created_at))} · ${r.options.days} 个交易日</option>`).join("");
  const selected=options.some(r=>r.id===had)?had:(account==="original"?"":options[0]?.id||"");
  $("backtest-result").value=selected;await loadBacktest(selected);
}
async function bootstrap(resetForm=true){
  const data=await api("/api/bootstrap");store.token=data.token;store.accounts=data.accounts;
  if(!data.accounts.some(a=>a.id===store.account))store.account="original";
  $("account").innerHTML=data.accounts.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join("");$("account").value=store.account;
  data.active_jobs.forEach(watchJob);await loadAccount(resetForm);
}
function showJob(job){
  $("jobs-panel").hidden=false;$("job-status").textContent=statusNames[job.status];$("job-status").className="badge "+(["failed","interrupted"].includes(job.status)?"failed":["running","queued"].includes(job.status)?"busy":"");
  const account=store.accounts.find(a=>a.id===job.account)?.name||job.account;
  $("job-description").textContent=`${account} · ${actionNames[job.action]} · ${dateTime(job.created_at)}${job.result_summary?` · ${job.result_summary.valid_count} 只通过 / ${job.result_summary.invalid_count} 只隔离 / ${job.result_summary.failed_count} 只失败`:""}${job.error?" · "+job.error:""}`;
  $("job-progress").hidden=!["running","queued"].includes(job.status);$("job-log").textContent=job.log||"等待输出…";
}
function watchJob(job){
  if(store.watched.has(job.id))return;
  store.watched.set(job.id,true);
  const poll=async()=>{
    try{
      const current=await api(`/api/job?account=${job.account}&id=${job.id}`);
      if(store.account===job.account)showJob(current);
      if(["queued","running"].includes(current.status)){setTimeout(poll,1200);return;}
      store.watched.delete(job.id);
      if(store.account===job.account){if(current.status==="completed"&&current.action==="backtest")store.backtestRun=current.id;await loadAccount(false);notice(current.status==="completed"?`${actionNames[current.action]}已完成，结果已保存。`:current.error||"任务未完成，请查看日志",current.status!=="completed");}
    }catch(error){notice("暂时无法连接本地服务，正在重试…",true);setTimeout(poll,5000);}
  };poll();
}
async function run(action,options={}){
  const job=await api("/api/jobs",{account:store.account,action,options});showJob(job);watchJob(job);notice(`${actionNames[action]}已提交；可以查看日志，或继续浏览已有记录。`);await loadAccount(false);
}
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>changeTab(b.dataset.tab));
$("account").onchange=safe(async()=>{store.account=$("account").value;localStorage.setItem("kronos-account",store.account);store.backtestRun="";store.backtest=null;$("jobs-panel").hidden=true;notice("");await loadAccount();});
$("refresh-view").onclick=safe(()=>bootstrap(false));
$("fetch-data").onclick=safe(()=>run("fetch"));$("update-paper").onclick=safe(()=>run("paper"));
$("predict-form").onsubmit=safe(async e=>{e.preventDefault();if($("predict-source").value==="stocks")await run("stock_predict",{symbols:[$("predict-symbol").value]});else await run("predict",{symbol:$("predict-symbol").value,asof:$("predict-asof").value});});
$("predict-source").onchange=safe(async()=>{renderPredictOptions();if($("predict-source").value==="stocks")await refreshWatchlist();renderPrediction();});
$("predict-symbol").onchange=()=>renderPrediction();
$("refresh-watchlist").onclick=safe(refreshWatchlist);
$("predict-watchlist").onclick=safe(async()=>{if($("predict-source").value!=="stocks")throw new Error("请先选择个股研究自选股模式");await run("stock_predict");});
$("backtest-form").onsubmit=safe(async e=>{e.preventDefault();await run("backtest",{days:Number($("backtest-days").value),end:$("backtest-end").value});});
$("backtest-result").onchange=safe(()=>loadBacktest($("backtest-result").value));
$("account-tabs").querySelectorAll("button").forEach(b=>b.onclick=()=>{store.accountView=b.dataset.accountView;$("account-tabs").querySelectorAll("button").forEach(x=>x.classList.toggle("active",x===b));renderAccountTable();});
$("settings-form").onsubmit=safe(async e=>{
  e.preventDefault();const config=structuredClone(store.detail.config),percents=new Set(["signal_threshold","commission_rate","max_volume_participation"]);
  for(const key of Object.keys(config)){if($(key))config[key]=key==="device"?$(key).value:Number($(key).value)/(percents.has(key)?100:1);}
  config.symbols=Object.fromEntries([...document.querySelectorAll('[name="symbols"]:checked')].map(input=>[input.value,pool[input.value]]));
  const result=await api("/api/accounts",{name:$("new-name").value,config});store.account=result.id;store.backtestRun="";localStorage.setItem("kronos-account",result.id);await bootstrap();changeTab("overview");notice("新模拟盘已保存。原账户记录保留；现在可以运行回测，或初始化新账户。");
});
bootstrap().catch(error=>notice(error.message,true));
let resizeTimer;
window.addEventListener("resize",()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(!store.detail)return;if(store.tab==="backtest")renderBacktest();if(store.tab==="predict")renderPrediction();if(store.tab==="overview")drawChart("paper-chart",[{name:"模拟盘资产",color:"#3264dc",points:(store.detail.state.equity||[]).map(r=>({date:r.date,value:r.equity}))}]);},150);});

function renderPredictOptions(){
  const stocks=$("predict-source").value==="stocks",previous=$("predict-symbol").value;
  const entries=stocks?(store.watchlist?.items||[]):Object.entries(store.detail.config.symbols).map(([code,name])=>({code,name}));
  const options=[...entries];
  if(stocks)for(const item of store.detail.stock_predictions?.items||[]){if(!options.some(v=>v.code===item.code))options.push({code:item.code,name:item.name+" · 已保存结果"});}
  $("predict-symbol").innerHTML=options.map(i=>`<option value="${esc(i.code)}">${esc(i.code)} · ${esc(i.name)}</option>`).join("");
  if(options.some(i=>i.code===previous))$("predict-symbol").value=previous;
  $("predict-asof").disabled=stocks;
  $("predict-date-help").textContent=stocks?"使用最近完整收盘日（18:00 后确认），个股模式不回填历史预测。":"留空使用最近已确认的日线";
  $("refresh-watchlist").hidden=!stocks;$("predict-watchlist").hidden=!stocks;
  $("stock-prediction-results").hidden=!stocks;
  $("watchlist-status").textContent=stocks?`当前自选 ${entries.length} 只；${store.watchlist?.retrieved_at?"读取于 "+dateTime(store.watchlist.retrieved_at):"点击刷新自选股读取名单"}。按当前模拟盘 ${store.detail.config.lookback} 日输入、${store.detail.config.horizon} 日预测参数运行；不改变 ETF 标的池。`:"ETF 预测沿用原有日线缓存与参数。";
}
async function refreshWatchlist(){
  try{store.watchlist=await api("/api/watchlist");renderPredictOptions();renderPrediction();if(store.watchlist.skipped.length)notice(`以下代码暂不支持：${store.watchlist.skipped.join("、")}`,true);}
  catch(error){$("watchlist-status").textContent="读取自选失败，请先打开个股研究平台。已保存的预测仍可查看。";throw error;}
}
function renderStockResults(){
  const batch=store.detail.stock_predictions;
  $("stock-prediction-results").innerHTML=table([["自选股",i=>`${esc(i.code)} · ${esc(i.name)}`],["数据截至",i=>esc(i.prediction?.asof||batch?.asof)],["状态",i=>i.status==="completed"?"OHLC 检查通过":i.status==="invalid"?"⚠ 输出异常 · 已隔离":"失败（保留）"],["结果",i=>i.status==="completed"?pct(i.prediction.predicted_return):esc(i.error?.includes("Invalid OHLC ordering")?"预测高低价与开收盘价不一致":i.error)],["操作",i=>i.prediction?`<button class="table-button" data-stock-result="${esc(i.code)}">${i.status==="completed"?"查看预测":"查看原始输出"}</button>`:"—"]],[...(batch?.items||[])].reverse(),"尚无自选股预测","读取自选列表后，可预测单只或全部自选股。结果仅供研究，不参与原账户交易。");
  $("stock-prediction-results").querySelectorAll("[data-stock-result]").forEach(b=>b.onclick=()=>{$("predict-symbol").value=b.dataset.stockResult;renderPrediction();});
}
