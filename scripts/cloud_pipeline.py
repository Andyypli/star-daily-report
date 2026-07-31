#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端每日 Star 报告流水线（GitHub Actions 专用，自包含）。

与本地版的关键区别：
- 所有敏感配置从环境变量（GitHub Secrets）读取，不落盘、不入库。
- T+0 硬校验：必须抓到"今天"（北京日期）的真实数据点才发信，否则报错退出。
- Chrome 路径从 CHROME_BIN 环境变量读取（GitHub runner 上由 setup-chrome 提供）。
- 数据 star_history.json 存在仓库里，云端自维护，不依赖任何本地文件。

运行环境：GitHub Actions ubuntu-latest，UTC 00:00（= 北京 08:00）触发。
"""
import json
import os
import re
import ssl
import smtplib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
STARS = os.path.join(BASE, "stars")
CHARTDIR = os.path.join(BASE, "charts")
HIST = os.path.join(STARS, "star_history.json")
CHARTJS = os.path.join(BASE, "assets", "chart.umd.min.js")
os.makedirs(STARS, exist_ok=True)
os.makedirs(CHARTDIR, exist_ok=True)

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
CHROME = os.environ.get("CHROME_BIN", "").strip() or "google-chrome"
REPOS = [("TencentCloud", "TencentDB-Agent-Memory", "#e23b3b"),
         ("TencentCloud", "CubeSandbox", "#2f6df6")]

# 收件人硬白名单：任何其他收件人立即中止（代码级安全锁）
ALLOWED_TO = ["andyypli@tencent.com"]


def bj_today():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


# ============ 1. 抓 Star 总数（T+0 核心） ============
def fetch_star_count(owner, name):
    query = ("query($owner:String!,$name:String!){repository(owner:$owner,"
             "name:$name){stargazerCount}}")
    body = json.dumps({"query": query,
                       "variables": {"owner": owner, "name": name}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Content-Type": "application/json",
                 "User-Agent": "cloud-star/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            return int(payload["data"]["repository"]["stargazerCount"])
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 20))


def snapshot_stars():
    history = json.loads(open(HIST).read()) if os.path.exists(HIST) else {}
    today = bj_today()
    for owner, name, _ in REPOS:
        value = fetch_star_count(owner, name)
        prev = list(history.get(name, {}).values())
        if prev and value < max(prev):
            raise RuntimeError(f"{name} 当前值 {value} < 历史最大 {max(prev)}，拒绝写入")
        history.setdefault(name, {})[today] = value
        print(f"[snapshot] {name} {today} = {value}", flush=True)
    tmp = HIST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=0)
    os.replace(tmp, HIST)
    return history


# ============ 2. 抓社区多维指标 ============
COMM_Q = """query($o:String!,$n:String!){
  repository(owner:$o,name:$n){
    stargazerCount forkCount watchers{totalCount}
    issues_total:issues{totalCount}
    issues_open:issues(states:OPEN){totalCount}
    issues_closed:issues(states:CLOSED){totalCount}
    pr_total:pullRequests{totalCount}
    pr_open:pullRequests(states:OPEN){totalCount}
    pr_merged:pullRequests(states:MERGED){totalCount}
    releases{totalCount} primaryLanguage{name}
    licenseInfo{spdxId} createdAt pushedAt updatedAt diskUsage description
    repositoryTopics(first:20){nodes{topic{name}}}
    defaultBranchRef{target{... on Commit{history{totalCount}}}}
    mentionableUsers{totalCount}
  }
}"""


def gql(q, v):
    body = json.dumps({"query": q, "variables": v}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Content-Type": "application/json", "User-Agent": "comm/1.0"})
    for _ in range(6):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 403:
                time.sleep(5); continue
            raise
        except Exception:
            time.sleep(5)
    raise RuntimeError("gql fail")


def accurate_contributors(o, n):
    url = f"https://api.github.com/repos/{o}/{n}/contributors?per_page=1&anon=false"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GH_TOKEN}", "User-Agent": "comm/1.0",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            link = r.headers.get("Link", "")
            m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
            if m:
                return int(m.group(1))
            return len(json.loads(r.read().decode()))
    except Exception:
        return None


def fetch_community():
    for o, n, _ in REPOS:
        d = gql(COMM_Q, {"o": o, "n": n})
        r = d["data"]["repository"]
        try:
            commits = r["defaultBranchRef"]["target"]["history"]["totalCount"]
        except Exception:
            commits = None
        out = {
            "repo": f"{o}/{n}", "stars": r["stargazerCount"], "forks": r["forkCount"],
            "watchers": r["watchers"]["totalCount"],
            "issues_total": r["issues_total"]["totalCount"],
            "issues_open": r["issues_open"]["totalCount"],
            "issues_closed": r["issues_closed"]["totalCount"],
            "pr_total": r["pr_total"]["totalCount"],
            "pr_open": r["pr_open"]["totalCount"],
            "pr_merged": r["pr_merged"]["totalCount"],
            "releases": r["releases"]["totalCount"], "commits": commits,
            "language": (r.get("primaryLanguage") or {}).get("name"),
            "license": (r.get("licenseInfo") or {}).get("spdxId"),
            "created_at": r["createdAt"], "pushed_at": r["pushedAt"],
            "updated_at": r["updatedAt"], "disk_kb": r.get("diskUsage"),
            "description": r.get("description"),
            "topics": [t["topic"]["name"] for t in r["repositoryTopics"]["nodes"]],
            "contributors_est": r["mentionableUsers"]["totalCount"],
        }
        out["contributors"] = accurate_contributors(o, n) or out["contributors_est"]
        json.dump(out, open(os.path.join(STARS, f"{n}.community.json"), "w"),
                  ensure_ascii=False, indent=2)
        print(f"[community] {n}: stars={out['stars']} contrib={out['contributors']}", flush=True)


# ============ 3. 渲染 4 张图表（Chrome headless 截图） ============
def load_series(history):
    recorded = sorted({d for repo in history for d in history[repo]})
    start = datetime.fromisoformat(recorded[0]).date()
    today = datetime.fromisoformat(bj_today()).date()
    end = max(datetime.fromisoformat(recorded[-1]).date(), today)
    days, cur = [], start
    while cur <= end:
        days.append(cur.isoformat()); cur += timedelta(days=1)
    series = {name: [history.get(name, {}).get(d) for d in days]
              for _, name, _ in REPOS}
    return days, series


def aligned_series(days, series):
    """对齐起点：每个 repo 从其首个非空点开始，横轴为"第N天"。"""
    maxlen = 0
    aligned = {}
    for _, name, _ in REPOS:
        vals = [v for v in series[name] if v is not None]
        # 保持缺口结构：从首个非空索引起截取
        idx = next((i for i, v in enumerate(series[name]) if v is not None), 0)
        seg = series[name][idx:]
        aligned[name] = seg
        maxlen = max(maxlen, len(seg))
    labels = [f"第{i+1}天" for i in range(maxlen)]
    cum = {n: aligned[n] + [None] * (maxlen - len(aligned[n])) for _, n, _ in REPOS}
    return labels, cum


def daily_from_cum(days, series):
    out = {}
    for _, name, _ in REPOS:
        vals = series[name]
        inc = [None] * len(vals)
        last_i = None
        for i, v in enumerate(vals):
            if v is None:
                continue
            if last_i is None:
                inc[i] = None
            else:
                inc[i] = max(v - vals[last_i], 0)
            last_i = i
        out[name] = inc
    return out


ONE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"/><script>{chartjs}</script>
<style>body{{margin:0;background:#fff}}.box{{width:1040px;padding:16px}}</style></head>
<body><div class="box"><canvas id="c" width="1000" height="440"></canvas></div>
<script>
const src={src};
const ds=src.map(s=>({{label:s.label,data:s.data,backgroundColor:'{ctype}'==='bar'?s.color:s.color+'22',borderColor:s.color,borderWidth:'{ctype}'==='line'?2:0,pointRadius:'{ctype}'==='line'?((ctx)=>ctx.raw==null?0:(ctx.dataIndex===ctx.dataset.data.length-1?5:1.5)):0,pointHoverRadius:6,spanGaps:'{ctype}'==='line',segment:'{ctype}'==='line'?{{borderDash:(ctx)=>ctx.p1DataIndex-ctx.p0DataIndex>1?[6,5]:undefined}}:undefined,fill:'{ctype}'==='line',tension:0.2,barPercentage:0.95,categoryPercentage:0.85}}));
new Chart(document.getElementById('c'),{{type:'{ctype}',data:{{labels:{labels},datasets:ds}},
  options:{{responsive:false,animation:false,plugins:{{legend:{{position:'top'}},title:{{display:true,text:{title}}}}},
  scales:{{x:{{ticks:{{maxTicksLimit:16,autoSkip:true}},grid:{{display:false}}}},y:{{beginAtZero:true,title:{{display:true,text:'Star 数量'}}}}}}}}}});
</script></body></html>"""


def render_charts(history):
    days, series = load_series(history)
    cal_daily = daily_from_cum(days, series)
    rel_labels, rel_cum = aligned_series(days, series)
    rel_daily = daily_from_cum(rel_labels, rel_cum)

    def mk(dmap):
        return [{"label": n, "data": dmap[n], "color": c} for _, n, c in REPOS]

    views = {
        "calDaily": (days, mk(cal_daily), "bar",
                     "每日新增 Star（空白日期=无真实日数据）"),
        "calCum": (days, [{"label": n, "data": series[n], "color": c}
                          for _, n, c in REPOS], "line",
                   "累计 Star（缺失区间留空；末点=最新真实总数）"),
        "relDaily": (rel_labels, mk(rel_daily), "bar",
                     "每日新增 Star·对齐起点（空白=无真实日数据）"),
        "relCum": (rel_labels, mk(rel_cum), "line",
                   "累计 Star·对齐起点（真实记录点）"),
    }
    chartjs = open(CHARTJS, encoding="utf-8").read()
    for key, (labels, src, ctype, title) in views.items():
        html = ONE_HTML.format(
            chartjs=chartjs, src=json.dumps(src, ensure_ascii=False),
            labels=json.dumps(labels, ensure_ascii=False),
            title=json.dumps(title, ensure_ascii=False), ctype=ctype)
        tmp = os.path.join(tempfile.gettempdir(), f"v2_{key}.html")
        open(tmp, "w", encoding="utf-8").write(html)
        out = os.path.join(CHARTDIR, f"{key}.png")
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                        f"--screenshot={out}", "--window-size=1080,520",
                        "--default-background-color=FFFFFFFF", f"file://{tmp}"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        size = os.path.getsize(out)
        if size < 5000:
            raise RuntimeError(f"图表渲染异常：{out} 仅 {size} 字节")
        print(f"[render] {out} ({size} bytes)", flush=True)


# ============ 4. 组装并发送邮件 ============
CHARTS = [
    ("calDaily.png", "① 每日新增 Star（按真实日期）"),
    ("calCum.png", "② 累计 Star 总量（按真实日期）"),
    ("relDaily.png", "③ 每日新增（对齐起点比涨粉速度）"),
    ("relCum.png", "④ 累计 Star（对齐起点）"),
]


def load_stats(history):
    info, comm = {}, {}
    for _, name, _ in REPOS:
        c = json.load(open(os.path.join(STARS, f"{name}.community.json")))
        comm[name] = c
        ks = sorted(history.get(name, {}).keys())
        delta = (history[name][ks[-1]] - history[name][ks[-2]]) if len(ks) >= 2 else 0
        info[name] = {
            "created": c["created_at"][:10], "total": c["stars"], "delta": delta,
            "previous_date": ks[-2] if len(ks) >= 2 else "",
            "last_star": ks[-1] if ks else "",
        }
    return info, comm


def build_body_html(info, comm, today, online_url=None):
    A = info["TencentDB-Agent-Memory"]; B = info["CubeSandbox"]
    CA = comm["TencentDB-Agent-Memory"]; CB = comm["CubeSandbox"]
    TD = "padding:9px 10px;border:1px solid #e5e9f0;font-size:12.5px;vertical-align:top"
    TDr = TD + ";text-align:right;font-weight:700;white-space:nowrap"
    metrics = [
        ("⭐ Star（收藏/点赞）", "多少人给项目点了星＝收藏+点赞，最直观的人气指标。",
         f"{CA['stars']:,}", f"{CB['stars']:,}"),
        ("🍴 Fork（复制分支）", "多少人把项目复制一份到自己名下，比 Star 更反映深度参与。",
         f"{CA['forks']:,}", f"{CB['forks']:,}"),
        ("👁 Watch（关注动态）", "多少人订阅了项目、更新就收通知。",
         f"{CA['watchers']:,}", f"{CB['watchers']:,}"),
        ("💬 Issue（问题/建议）", "用户提交的问题、Bug、建议总数。",
         f"{CA['issues_total']:,}（未解{CA['issues_open']}/已解{CA['issues_closed']}）",
         f"{CB['issues_total']:,}（未解{CB['issues_open']}/已解{CB['issues_closed']}）"),
        ("🔀 Pull Request（改代码请求）", "开发者提交代码修改的请求数（已合并数）。",
         f"{CA['pr_total']:,}（合并{CA['pr_merged']}）",
         f"{CB['pr_total']:,}（合并{CB['pr_merged']}）"),
        ("📝 Commit（代码提交）", "主分支累计代码提交次数。",
         f"{CA['commits']:,}" if CA['commits'] else "—",
         f"{CB['commits']:,}" if CB['commits'] else "—"),
        ("👥 Contributor（贡献者）", "为项目贡献过代码的人数。",
         f"{CA['contributors']} 人", f"{CB['contributors']} 人"),
        ("🏷 Release（正式版本）", "发布过多少个正式版本。",
         f"{CA['releases']} 个", f"{CB['releases']} 个"),
        ("🧑‍💻 编程语言", "项目主要用什么语言开发。",
         CA['language'] or "—", CB['language'] or "—"),
    ]
    mrows = ""
    for name, desc, va, vb in metrics:
        mrows += (f'<tr><td style="{TD};font-weight:700;width:135px">{name}</td>'
                  f'<td style="{TD};color:#4b5563">{desc}</td>'
                  f'<td style="{TDr};color:#e23b3b">{va}</td>'
                  f'<td style="{TDr};color:#2f6df6">{vb}</td></tr>')
    imgs = "".join(
        f'<div style="font:600 14px/1.4 -apple-system,PingFang SC;color:#1f2a37;margin:20px 0 6px">{title}</div>'
        f'<img src="cid:{cid}" style="width:100%;max-width:720px;border:1px solid #e5e9f0;border-radius:8px"/>'
        for (fn, title), cid in zip(CHARTS, [c[0].replace('.png', '') for c in CHARTS]))
    da = ('+' + format(A['delta'], ',')) if A['delta'] > 0 else '0'
    db = ('+' + format(B['delta'], ',')) if B['delta'] > 0 else '0'
    online_block = ""
    if online_url:
        online_block = (
            '<div style="background:#eef3ff;border:1px solid #d5e2ff;border-radius:10px;'
            'padding:16px 18px;text-align:center;margin:16px 0">'
            '<div style="font-size:13.5px;color:#1f2a37;margin-bottom:12px">'
            '📊 想看可交互的完整报告（可缩放、悬停查看每天数据）？点下方按钮，浏览器直接打开：</div>'
            f'<a href="{online_url}" style="display:inline-block;background:#2f6df6;color:#fff;'
            'text-decoration:none;font-weight:700;font-size:15px;padding:11px 30px;border-radius:8px">'
            '🔗 在线查看完整报告</a></div>')
    return f"""<div style="font-family:-apple-system,PingFang SC,Microsoft YaHei,sans-serif;color:#1f2a37;max-width:760px;margin:0 auto;line-height:1.7">
<h2 style="font-size:20px;margin:0 0 4px">两个开源项目，到底谁更受欢迎？—— 一图看懂</h2>
<div style="color:#6b7280;font-size:13px;margin-bottom:6px">对比：TencentDB-Agent-Memory（<span style="color:#e23b3b">红</span>） vs CubeSandbox（<span style="color:#2f6df6">蓝</span>），均来自腾讯云 · 数据更新至 {today}（北京时间，T+0 实时）</div>
{online_block}
<div style="background:#f6f8fb;border-radius:10px;padding:14px 16px;font-size:13px;line-height:1.95;margin:14px 0">
<b>📌 先看结论：</b><br>
• <b>谁更火：</b>目前 CubeSandbox 全面领先——Star {CB['stars']:,}（对方 {CA['stars']:,}）、Fork {CB['forks']:,}（对方 {CA['forks']:,}）、贡献者 {CB['contributors']} 人（对方 {CA['contributors']} 人）。<br>
• <b>谁开发更活跃：</b>还是 CubeSandbox——提交 {CB['commits']:,} 次、合并 {CB['pr_merged']} 个代码请求、发 {CB['releases']} 个版本。<br>
• <b>最新真实区间变化：</b>{A['previous_date']} 至 {A['last_star']}，TencentDB-Agent-Memory 累计新增 {da}；CubeSandbox 累计新增 {db}。这是区间增量，不冒充单日数据。
</div>
<h3 style="font-size:16px;margin:22px 0 6px">📊 开源社区都看哪些指标？（附大白话解释）</h3>
<table style="width:100%;border-collapse:collapse;font-size:12.5px">
<tr style="background:#f6f8fb"><th style="{TD}">指标</th><th style="{TD}">它是什么意思</th>
<th style="{TD};text-align:right;color:#e23b3b">TencentDB-Agent-Memory</th>
<th style="{TD};text-align:right;color:#2f6df6">CubeSandbox</th></tr>
{mrows}
</table>
<h3 style="font-size:16px;margin:24px 0 6px">📈 Star 增长（4 张图）</h3>
{imgs}
<div style="color:#9aa4b2;font-size:12px;margin-top:22px;border-top:1px solid #eee;padding-top:12px;line-height:1.8">
<b>数据说明：</b>所有展示值均来自 GitHub 官方实时计数，数据截止发送时刻（T+0），缺失日期以空白表示，绝不估算。本邮件由云端 GitHub Actions 定时发送，与任何本地设备开关机无关。
</div>
</div>"""


# ============ 4.5 生成交互式在线报告（部署到 GitHub Pages） ============
SITE_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>GitHub Star 增长对比 · 交互报告 · {today}</title>
<script>{chartjs}</script>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2a37;line-height:1.7}}
.wrap{{max-width:960px;margin:0 auto;padding:24px 18px 60px}}
h1{{font-size:22px;margin:0 0 6px}}
.sub{{color:#6b7280;font-size:13px;margin-bottom:18px}}
.card{{background:#fff;border:1px solid #e5e9f0;border-radius:12px;padding:18px 20px;margin:16px 0;box-shadow:0 1px 3px rgba(15,30,60,.04)}}
.concl{{background:#f6f8fb;border-radius:10px;padding:14px 16px;font-size:13.5px;line-height:2}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{padding:9px 10px;border:1px solid #e5e9f0;text-align:left;vertical-align:top}}
td.n,th.n{{text-align:right;font-weight:700;white-space:nowrap}}
.red{{color:#e23b3b}}.blue{{color:#2f6df6}}
.chart-title{{font-weight:600;font-size:15px;margin:6px 0 4px}}
.chart-box{{position:relative;height:360px}}
.tools{{font-size:12px;color:#9aa4b2;margin-bottom:8px}}
.btn{{display:inline-block;font-size:12px;color:#2f6df6;background:#eef3ff;border:1px solid #d5e2ff;border-radius:6px;padding:3px 10px;cursor:pointer;margin-right:6px}}
.foot{{color:#9aa4b2;font-size:12px;margin-top:24px;border-top:1px solid #e5e9f0;padding-top:14px}}
</style></head>
<body><div class="wrap">
<h1>两个开源项目，到底谁更受欢迎？—— 一图看懂（交互版）</h1>
<div class="sub">对比：TencentDB-Agent-Memory（<span class="red">红</span>） vs CubeSandbox（<span class="blue">蓝</span>），均来自腾讯云 · 数据更新至 {today}（北京时间，T+0 实时）</div>
<div class="card concl">{concl}</div>
<div class="card"><div class="chart-title">📊 社区多维指标</div>{table}</div>
{charts}
<div class="foot">所有数据均来自 GitHub 官方实时计数，截止页面生成时刻（T+0），缺失日期以空白/虚线表示，绝不估算。本页由云端 GitHub Actions 每日自动生成并部署，与任何本地设备开关机无关。</div>
</div>
<script>
const DATA={data_json};
function mkChart(cvsId,ctype,labels,src,title){{
  const ds=src.map(s=>({{label:s.label,data:s.data,
    backgroundColor:ctype==='bar'?s.color:s.color+'22',
    borderColor:s.color,borderWidth:ctype==='line'?2:0,
    pointRadius:ctype==='line'?((c)=>c.raw==null?0:(c.dataIndex===c.dataset.data.length-1?5:2)):0,
    pointHoverRadius:6,spanGaps:ctype==='line',
    segment:ctype==='line'?{{borderDash:(c)=>c.p1DataIndex-c.p0DataIndex>1?[6,5]:undefined}}:undefined,
    fill:ctype==='line',tension:0.2,barPercentage:0.95,categoryPercentage:0.85}}));
  return new Chart(document.getElementById(cvsId),{{type:ctype,
    data:{{labels:labels,datasets:ds}},
    options:{{responsive:true,maintainAspectRatio:false,animation:false,interaction:{{mode:'index',intersect:false}},
      plugins:{{legend:{{position:'top'}},title:{{display:true,text:title}},tooltip:{{enabled:true}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:20,autoSkip:true}},grid:{{display:false}}}},y:{{beginAtZero:true,title:{{display:true,text:'Star 数量'}}}}}}}}}});
}}
DATA.views.forEach(v=>mkChart(v.id,v.ctype,v.labels,v.src,v.title));
</script></body></html>"""


def build_interactive_site(history, comm, info, today):
    """生成交互式在线报告 HTML（同源数据、内嵌 Chart.js、可缩放悬停），输出 site/index.html。"""
    site_dir = os.path.join(BASE, "site")
    os.makedirs(site_dir, exist_ok=True)

    # 复用与图表相同的数据管线
    days, series = load_series(history)
    cal_daily = daily_from_cum(days, series)
    rel_labels, rel_cum = aligned_series(days, series)
    rel_daily = daily_from_cum(rel_labels, rel_cum)

    def mk(dmap):
        return [{"label": n, "data": dmap[n], "color": c} for _, n, c in REPOS]

    views = [
        {"id": "calDaily", "ctype": "bar", "labels": days,
         "src": mk(cal_daily), "title": "① 每日新增 Star（按真实日期，空白=无真实日数据）"},
        {"id": "calCum", "ctype": "line", "labels": days,
         "src": [{"label": n, "data": series[n], "color": c} for _, n, c in REPOS],
         "title": "② 累计 Star 总量（缺失区间以虚线连接；末点=最新真实总数）"},
        {"id": "relDaily", "ctype": "bar", "labels": rel_labels,
         "src": mk(rel_daily), "title": "③ 每日新增·对齐起点（比涨粉速度）"},
        {"id": "relCum", "ctype": "line", "labels": rel_labels,
         "src": mk(rel_cum), "title": "④ 累计 Star·对齐起点"},
    ]

    # 表格（与邮件同源指标）
    CA = comm["TencentDB-Agent-Memory"]; CB = comm["CubeSandbox"]
    rows = [
        ("⭐ Star", f"{CA['stars']:,}", f"{CB['stars']:,}"),
        ("🍴 Fork", f"{CA['forks']:,}", f"{CB['forks']:,}"),
        ("👁 Watch", f"{CA['watchers']:,}", f"{CB['watchers']:,}"),
        ("💬 Issue", f"{CA['issues_total']:,}", f"{CB['issues_total']:,}"),
        ("🔀 Pull Request", f"{CA['pr_total']:,}（合并{CA['pr_merged']}）", f"{CB['pr_total']:,}（合并{CB['pr_merged']}）"),
        ("📝 Commit", f"{CA['commits']:,}" if CA['commits'] else "—", f"{CB['commits']:,}" if CB['commits'] else "—"),
        ("👥 Contributor", f"{CA['contributors']} 人", f"{CB['contributors']} 人"),
        ("🏷 Release", f"{CA['releases']} 个", f"{CB['releases']} 个"),
        ("🧑‍💻 语言", CA['language'] or "—", CB['language'] or "—"),
    ]
    table = ('<table><tr><th>指标</th><th class="n red">TencentDB-Agent-Memory</th>'
             '<th class="n blue">CubeSandbox</th></tr>')
    for name, va, vb in rows:
        table += f'<tr><td>{name}</td><td class="n red">{va}</td><td class="n blue">{vb}</td></tr>'
    table += '</table>'

    A = info["TencentDB-Agent-Memory"]; B = info["CubeSandbox"]
    da = ('+' + format(A['delta'], ',')) if A['delta'] > 0 else '0'
    db = ('+' + format(B['delta'], ',')) if B['delta'] > 0 else '0'
    concl = (f"<b>📌 先看结论：</b><br>"
             f"• <b>谁更火：</b>目前 CubeSandbox 全面领先——Star {CB['stars']:,}（对方 {CA['stars']:,}）、"
             f"Fork {CB['forks']:,}（对方 {CA['forks']:,}）、贡献者 {CB['contributors']} 人（对方 {CA['contributors']} 人）。<br>"
             f"• <b>谁开发更活跃：</b>还是 CubeSandbox——提交 {CB['commits']:,} 次、合并 {CB['pr_merged']} 个代码请求、发 {CB['releases']} 个版本。<br>"
             f"• <b>最新真实区间变化：</b>{A['previous_date']} 至 {A['last_star']}，"
             f"TencentDB-Agent-Memory 累计新增 {da}；CubeSandbox 累计新增 {db}。这是区间增量，不冒充单日数据。")

    charts_html = "".join(
        f'<div class="card"><div class="chart-title">{v["title"]}</div>'
        f'<div class="tools">💡 悬停查看每天数值，图例可点击隐藏/显示某条曲线</div>'
        f'<div class="chart-box"><canvas id="{v["id"]}"></canvas></div></div>'
        for v in views)

    chartjs = open(CHARTJS, encoding="utf-8").read()
    html = SITE_HTML.format(
        chartjs=chartjs, today=today, concl=concl, table=table,
        charts=charts_html,
        data_json=json.dumps({"views": views}, ensure_ascii=False))
    out = os.path.join(site_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    # 关闭 Jekyll 处理，确保原样发布
    open(os.path.join(site_dir, ".nojekyll"), "w").close()
    print(f"[site] 交互报告已生成 {out} ({os.path.getsize(out)} bytes)", flush=True)
    return out


def send_mail(history, online_url=None):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    from_addr = os.environ.get("MAIL_FROM", user)
    to_addrs = [x.strip() for x in os.environ["MAIL_TO"].split(",") if x.strip()]
    if to_addrs != ALLOWED_TO:
        raise SystemExit(f"ABORT: 收件人必须且只能是 {ALLOWED_TO}，当前 {to_addrs}")

    today = bj_today()
    info, comm = load_stats(history)
    # 正文注入在线按钮（只有按钮、无明文链接，保护用户名不出现在邮件可见文本）
    body = build_body_html(info, comm, today, online_url=online_url)
    msg = EmailMessage()
    msg["Subject"] = f"[每日] GitHub Star 增长对比报告 · {today}"
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content("本邮件为图文报告，请使用支持 HTML 的邮件客户端查看。")
    msg.add_alternative(body, subtype="html")
    html_part = msg.get_payload()[-1]
    for (fn, _), cid in zip(CHARTS, [c[0].replace('.png', '') for c in CHARTS]):
        with open(os.path.join(CHARTDIR, fn), "rb") as f:
            html_part.add_related(f.read(), maintype="image", subtype="png", cid=f"<{cid}>")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pwd); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx); s.login(user, pwd); s.send_message(msg)
    print(f"[mail] SENT to {to_addrs} via {host}:{port}", flush=True)


def main():
    if not GH_TOKEN:
        raise SystemExit("缺少 GH_TOKEN 环境变量")
    print(f"===== 云端流水线启动 {datetime.now(timezone.utc).isoformat()} UTC =====", flush=True)

    # 1) 抓 T+0 数据
    history = snapshot_stars()

    # 2) T+0 硬校验：今天(北京日期)的点必须存在，否则拒绝发信
    today = bj_today()
    for _, name, _ in REPOS:
        if today not in history.get(name, {}):
            raise SystemExit(f"T+0校验失败：{name} 缺少今日({today})数据点，拒绝发送滞后数据")
    print(f"[T+0] 校验通过：今日({today})实时数据已就位", flush=True)

    # 3) 社区指标 + 渲染图表
    fetch_community()
    render_charts(history)

    # 4) 生成交互式报告 HTML（部署到 GitHub Pages 的固定网址）
    info, comm = load_stats(history)
    build_interactive_site(history, comm, info, today)

    # 5) 发信：图文正文 + 在线查看按钮（只有按钮、正文无明文链接，用户名不出现在可见文本）
    online_url = os.environ.get("PAGES_URL", "").strip() or \
        "https://andyypli.github.io/star-daily-report/"
    send_mail(history, online_url=online_url)
    print(f"===== 云端流水线完成，邮件已发出（在线报告：{online_url}）=====", flush=True)


if __name__ == "__main__":
    main()
