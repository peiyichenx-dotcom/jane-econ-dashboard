#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jane 經濟局勢儀表板 — 每日資料抓取與訊號運算
在 GitHub Actions 上執行，產出 data.json 供 index.html 渲染。
每個指標獨立抓取，失敗不影響其他指標（沿用前次資料並標示 stale）。
用法: python fetch_data.py [--mock]
"""
import json, re, sys, csv, io, os, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

MOCK = "--mock" in sys.argv
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# 2026 FOMC 會議日程（第二日）
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------- 資料來源 ----------------

def fred(series_id, keep=400):
    """FRED → [(date, value)] 升冪。優先用官方 API（需 FRED_API_KEY 環境變數，
    GitHub Actions 的 IP 會被 FRED 網頁端封鎖，API 端點不受影響）。"""
    key = os.environ.get("FRED_API_KEY", "").strip()
    start = (datetime.now() - timedelta(days=1300)).strftime("%Y-%m-%d")
    out = []
    if key:
        url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
               f"&api_key={key}&file_type=json&observation_start={start}&sort_order=asc")
        j = json.loads(http_get(url))
        for o in j.get("observations", []):
            if o.get("value") not in (".", "", None):
                try:
                    out.append((o["date"], float(o["value"])))
                except ValueError:
                    pass
    else:
        txt = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
        for row in csv.reader(io.StringIO(txt)):
            if len(row) >= 2 and re.match(r"\d{4}-\d{2}-\d{2}", row[0].strip()) and row[1].strip() not in (".", ""):
                try:
                    out.append((row[0].strip(), float(row[1].strip())))
                except ValueError:
                    pass
    return out[-keep:]


def yahoo(symbol, rng="1y"):
    err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
               f"?range={rng}&interval=1d")
        try:
            j = json.loads(http_get(url, timeout=20))
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            out = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                out.append((datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), float(c)))
            if out:
                return out
        except Exception as e:
            err = e
    raise err or RuntimeError("yahoo empty")


def stooq(symbol):
    d2 = datetime.now().strftime("%Y%m%d")
    d1 = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    txt = http_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d&d1={d1}&d2={d2}")
    out = []
    for row in csv.reader(io.StringIO(txt)):
        if len(row) >= 5 and re.match(r"\d{4}-\d{2}-\d{2}", row[0]):
            try:
                out.append((row[0], float(row[4])))
            except ValueError:
                pass
    return out


def market(yahoo_sym, stooq_sym):
    try:
        s = yahoo(yahoo_sym)
        if len(s) > 20:
            return s
    except Exception as e:
        print(f"  yahoo {yahoo_sym} failed: {e}")
    try:
        return stooq(stooq_sym)
    except Exception as e:
        print(f"  stooq {stooq_sym} failed: {e}")
        return []


def cnn_fear_greed():
    txt = http_get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                   headers={"Referer": "https://edition.cnn.com/markets/fear-and-greed",
                            "Accept": "application/json"})
    j = json.loads(txt)
    hist = [(datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
             round(float(p["y"]), 0)) for p in j["fear_and_greed_historical"]["data"]]
    return hist[-260:]


def coingecko_btc():
    j = json.loads(http_get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365&interval=daily"))
    seen, out = set(), []
    for t, v in j["prices"]:
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if d not in seen:
            seen.add(d)
            out.append((d, float(v)))
    return out


def coingecko_stables():
    j = json.loads(http_get(
        "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=tether,usd-coin"))
    caps = {c["id"]: c["market_cap"] for c in j}
    usdt, usdc = caps.get("tether", 0), caps.get("usd-coin", 0)
    share = usdc / (usdt + usdc) * 100 if (usdt and usdc) else None
    return share, usdt, usdc


def frankfurter_dxy():
    """Yahoo 被擋時的 DXY 備援：用 ECB 匯率按 ICE 公式計算（誤差約 ±0.3）"""
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    j = json.loads(http_get(
        f"https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=EUR,JPY,GBP,CAD,SEK,CHF"))
    out = []
    for d, r in sorted(j["rates"].items()):
        try:
            v = (50.14348112 * r["EUR"] ** 0.576 * r["JPY"] ** 0.136 * r["GBP"] ** 0.119
                 * r["CAD"] ** 0.091 * r["SEK"] ** 0.042 * r["CHF"] ** 0.036)
            out.append((d, round(v, 3)))
        except KeyError:
            pass
    return out


def dxy_series():
    try:
        s = yahoo("DX-Y.NYB")
        if len(s) > 20:
            return s
    except Exception as e:
        print(f"  yahoo dxy failed: {e}", flush=True)
    return frankfurter_dxy()


def coingecko_chart(coin_id):
    j = json.loads(http_get(
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=365&interval=daily"))
    seen, out = set(), []
    for t, v in j["prices"]:
        d = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if d not in seen:
            seen.add(d)
            out.append((d, float(v)))
    return out


def gold_series():
    try:
        s = yahoo("GC=F")
        if len(s) > 20:
            return s
    except Exception as e:
        print(f"  yahoo gold failed: {e}", flush=True)
    return coingecko_chart("pax-gold")  # PAXG ≈ 現貨金價


def spx_series():
    try:
        s = yahoo("^GSPC")
        if len(s) > 20:
            return s
    except Exception as e:
        print(f"  yahoo spx failed: {e}", flush=True)
    return fred("SP500")


def er_api_twd():
    j = json.loads(http_get("https://open.er-api.com/v6/latest/USD"))
    return float(j["rates"]["TWD"])


def multpl_cape():
    txt = http_get("https://www.multpl.com/shiller-pe")
    m = re.search(r"Current Shiller PE Ratio[^0-9]*([0-9]+\.[0-9]+)", txt.replace("\n", " "))
    return float(m.group(1)) if m else None


# ---------------- 工具 ----------------

def last(s): return s[-1][1] if s else None
def last_date(s): return s[-1][0] if s else None


def val_days_ago(s, days):
    if not s:
        return None
    cutoff = (datetime.strptime(s[-1][0], "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    prev = [v for d, v in s if d <= cutoff]
    return prev[-1] if prev else s[0][1]


def yoy(s):
    if len(s) < 13:
        return None
    return (s[-1][1] / s[-13][1] - 1) * 100


def spark(s, n=120):
    return [round(v, 4) for _, v in s[-n:]]


def sma(s, n):
    if len(s) < n:
        return None
    return sum(v for _, v in s[-n:]) / n


def fmt_change(cur, prev, decimals=2, unit=""):
    if cur is None or prev is None:
        return ""
    d = cur - prev
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "＝")
    return f"{arrow}{abs(d):.{decimals}f}{unit}"


def mock_series(base, vol, n=260, drift=0.0):
    import random
    random.seed(abs(hash(str(base))) % 9999)
    out, v = [], base
    d = datetime.now() - timedelta(days=n)
    for i in range(n):
        d += timedelta(days=1)
        if d.weekday() < 5:
            v = max(0.0001, v + random.uniform(-vol, vol) + drift)
            out.append((d.strftime("%Y-%m-%d"), round(v, 4)))
    return out


# ---------------- 主流程 ----------------

def main():
    prev_data = {}
    if os.path.exists(OUT):
        try:
            prev_data = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass
    prev_ind = {i["id"]: i for i in prev_data.get("indicators", [])}

    S = {}
    errors = []
    _jobs = []

    def grab(key, fn, *a):
        _jobs.append((key, fn, a))

    def run_jobs():
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=8)
        futs = {key: ex.submit(fn, *a) for key, fn, a in _jobs}
        for key, fut in futs.items():
            try:
                S[key] = fut.result(timeout=75)
                n = len(S[key]) if isinstance(S[key], list) else S[key]
                print(f"OK  {key}: {n}", flush=True)
            except Exception as e:
                errors.append(f"{key}: {type(e).__name__} {e}")
                S[key] = []
                print(f"ERR {key}: {type(e).__name__} {e}", flush=True)
        ex.shutdown(wait=False, cancel_futures=True)

    if MOCK:
        S["dxy"] = mock_series(101, 0.4); S["dgs2"] = mock_series(4.1, 0.04)
        S["walcl"] = [(d, v * 1000) for d, v in mock_series(6700, 8, 120)]
        S["rrp"] = mock_series(5, 2)
        S["m2"] = [(f"{2024 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}-01", 21500 + i * 60) for i in range(24)]
        S["t10y2y"] = mock_series(0.28, 0.03); S["dgs10"] = mock_series(4.45, 0.05)
        S["dfii10"] = mock_series(2.2, 0.03); S["hy"] = mock_series(2.8, 0.06)
        S["vix"] = mock_series(17, 1.2); S["icsa"] = [(d, v * 1000) for d, v in mock_series(232, 8, 90)]
        S["unrate"] = [(f"{2024 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}-01", round(4.1 + i * 0.01, 1)) for i in range(24)]
        S["cpi"] = [(f"{2024 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}-01", 310 * (1.025 ** (i / 12))) for i in range(30)]
        S["retail"] = [(f"{2024 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}-01", 700 + i * 2) for i in range(24)]
        S["umcsent"] = [(f"{2024 + (i + 6) // 12}-{(i + 6) % 12 + 1:02d}-01", 60 + i * 0.5) for i in range(24)]
        S["ffr"] = [(d, 4.0) for d, _ in mock_series(4.0, 0)]
        S["fng"] = [(d, min(99, max(1, v))) for d, v in mock_series(35, 3)]
        S["spx"] = mock_series(6300, 45, drift=2.5); S["gold"] = mock_series(4100, 30)
        S["copper"] = mock_series(6.1, 0.06); S["twd"] = mock_series(29.5, 0.1)
        S["btc"] = mock_series(105000, 2500, drift=80)
        S["stables"] = (44.0, 170e9, 133e9); S["cape"] = 38.5
    else:
        grab("dxy", dxy_series)
        grab("dgs2", fred, "DGS2")
        grab("walcl", fred, "WALCL")
        grab("rrp", fred, "RRPONTSYD")
        grab("m2", fred, "M2SL")
        grab("t10y2y", fred, "T10Y2Y", 800)
        grab("dgs10", fred, "DGS10")
        grab("dfii10", fred, "DFII10")
        grab("hy", fred, "BAMLH0A0HYM2")
        grab("vix", fred, "VIXCLS")
        grab("icsa", fred, "ICSA")
        grab("unrate", fred, "UNRATE")
        grab("cpi", fred, "CPIAUCSL")
        grab("retail", fred, "RSAFS")
        grab("umcsent", fred, "UMCSENT")
        grab("ffr", fred, "DFEDTARU")
        grab("fng", cnn_fear_greed)
        grab("spx", spx_series)
        grab("gold", gold_series)
        grab("copper", market, "HG=F", "hg.f")
        grab("pcopp", fred, "PCOPPUSDM", 40)
        grab("twd", market, "TWD=X", "usdtwd")
        grab("twd_now", er_api_twd)
        grab("btc", coingecko_btc)
        grab("stables", coingecko_stables)
        grab("cape", multpl_cape)
        run_jobs()

    # 銅金比
    gold_map = dict(S.get("gold") or [])
    cg = [(d, v / gold_map[d] * 1000) for d, v in (S.get("copper") or []) if d in gold_map and gold_map[d]]
    if not cg and S.get("pcopp") and S.get("gold"):
        # 備援：IMF 月頻銅價（$/噸→$/磅）÷ 當月金價
        for d, v in S["pcopp"]:
            gv = next((g for gd, g in S["gold"] if gd >= d), None)
            if gv:
                cg.append((d, v / 2204.62 / gv * 1000))

    # 累積型序列（來源只給當下值，跨執行累積歷史）
    def accumulate(ind_id, value):
        hist = []
        if ind_id in prev_ind:
            hist = prev_ind[ind_id].get("acc") or []
        today = datetime.now(TPE).strftime("%Y-%m-%d")
        if value is not None:
            hist = [h for h in hist if h[0] != today] + [[today, round(value, 2)]]
        return hist[-260:]

    stables = S.get("stables") or (None, None, None)
    if isinstance(stables, list):
        stables = (None, None, None)
    usdc_share = stables[0]
    usdc_hist = accumulate("usdc_share", usdc_share)
    cape_val = S.get("cape") if not isinstance(S.get("cape"), list) else None
    cape_hist = accumulate("cape", cape_val)

    # ---------------- 指標與訊號 ----------------
    # signal: good(綠/利風險資產或健康) info(藍/中性) warn(黃) bad(紅)；opp: Jane 買點旗標
    IND = []

    def add(id, group, label, value, unit, decimals, series=None, signal="info",
            signal_text="", interp="", ref="", source="", extra="", opp=False, acc=None,
            band=None, refs=None):
        if value is None and id in prev_ind:
            p = dict(prev_ind[id]); p["stale"] = True
            IND.append(p); return
        IND.append({
            "id": id, "group": group, "label": label,
            "value": (round(value, decimals) if isinstance(value, (int, float)) else value),
            "unit": unit, "decimals": decimals,
            "date": last_date(series) if series else datetime.now(TPE).strftime("%Y-%m-%d"),
            "change": fmt_change(last(series), val_days_ago(series, 8), decimals) if series else "",
            "signal": signal, "signal_text": signal_text, "interp": interp,
            "ref": ref, "source": source, "extra": extra,
            "spark": spark(series) if series else ([v for _, v in (acc or [])]),
            "opp": opp, "stale": False, "acc": acc,
            "band": band, "refs": refs,
        })

    def yoy_series(s, keep_months=37):
        if len(s) < 14:
            return None
        out = [(s[i][0], round((s[i][1] / s[i - 12][1] - 1) * 100, 2)) for i in range(12, len(s))]
        return out[-keep_months:]

    # === 一、每日流動性五件套 ===
    dxy = last(S["dxy"]); sig = st = None
    if dxy is not None:
        sig = "bad" if dxy > 105 else ("good" if dxy < 100 else "info")
        st = ("美元強勢：風險資產偏弱模式" if dxy > 105 else
              ("美元弱勢：風險資產偏強模式" if dxy < 100 else "中性區間（100–105），看趨勢方向"))
    add("dxy", "liq", "美元指數 DXY", dxy, "", 2, S["dxy"], sig or "info", st or "",
        "Jane 的核心觀念：美元的流向反映全球資本的流向。門檻：>105 美元強勢＝風險資產偏弱；<100 美元弱勢＝風險資產偏強。DXY 升＝資金流向美元與美債；降＝流向股票、原物料、加密。",
        "手冊篇118/225・文盲篇117", "Yahoo/ECB計算", refs=[100, 105])

    v2 = last(S["dgs2"]); v2p = val_days_ago(S["dgs2"], 30); sig = st = None
    if v2 is not None and v2p is not None:
        falling = v2 < v2p - 0.02; rising = v2 > v2p + 0.02
        sig = "good" if falling else ("bad" if rising else "info")
        st = ("下行：Fed 放水預期，風險資產有利" if falling else
              ("上行：資金收緊訊號" if rising else "近月持平"))
    add("us2y", "liq", "美國2年期公債殖利率", v2, "%", 2, S["dgs2"], sig or "info", st or "",
        "Jane 視 2Y 為 Fed 政策影響流動性的領先指標：升＝資金收緊、風險資產跌；降＝資金釋放、風險資產漲。",
        "手冊篇225", "FRED DGS2")

    wal = last(S["walcl"]); walp = val_days_ago(S["walcl"], 90)
    wal_up = (wal is not None and walp is not None and wal > walp)
    add("fed_bs", "liq", "Fed 資產負債表", wal / 1e6 if wal else None, "兆$", 2,
        [(d, v / 1e6) for d, v in S["walcl"]] if S["walcl"] else None,
        "good" if wal_up else "warn",
        "近3月擴張中＝流動性增加" if wal_up else "近3月縮減＝流動性收縮",
        "資產負債表擴大＝市場資金增加＝流動性擴張；縮表反之。每週四 H.4.1 報告更新。",
        "手冊篇225", "FRED WALCL")

    rrp = last(S["rrp"]); rrpp = val_days_ago(S["rrp"], 30)
    rrp_down = (rrp is not None and rrpp is not None and rrp <= rrpp)
    rrp_ok = rrp is not None and (rrp < 50 or rrp_down)
    add("rrp", "liq", "逆回購 RRP", rrp, "十億$", 1, S["rrp"],
        "good" if rrp_ok else "warn",
        "資金未被鎖在 Fed，流動性在市場中" if rrp_ok else "RRP 上升＝資金被鎖回 Fed",
        "RRP 餘額下降＝原先被鎖在 Fed 的資金回流市場＝流動性增加訊號；餘額大代表資金被鎖住。",
        "手冊篇118/225", "FRED RRPONTSYD")

    m2y = yoy(S["m2"])
    add("m2", "liq", "M2 貨幣供給（年增）", m2y, "%", 1, yoy_series(S["m2"]),
        "good" if (m2y is not None and m2y > 3) else ("warn" if (m2y is not None and m2y < 0) else "info"),
        ("M2 擴張：資金流向資產市場" if (m2y is not None and m2y > 3) else
         ("M2 收縮：市場承壓" if (m2y is not None and m2y < 0) else "溫和增長")),
        "M2 增加＝市場資金變多、流向資產市場。與 Fed 資產負債表、RRP 合成 Jane 的流動性總公式：Fed↑＋RRP↓＋M2↑＝進攻訊號，反之防禦。",
        "手冊篇225", "FRED M2SL", refs=[0],
        extra=f"最新 M2 ≈ ${last(S['m2'])/1000:.1f}T（{last_date(S['m2'])}）" if last(S["m2"]) else "")

    liq_score = sum([1 if wal_up else 0, 1 if rrp_ok else 0, 1 if (m2y or 0) > 0 else 0])
    liq_on = liq_score >= 2

    # === 二、利率與債市 ===
    ffr = last(S["ffr"]); ffrp = val_days_ago(S["ffr"], 120)
    cutting = ffr is not None and ffrp is not None and ffr < ffrp
    hiking = ffr is not None and ffrp is not None and ffr > ffrp
    next_fomc = next((d for d in FOMC_2026 if d >= datetime.now(TPE).strftime("%Y-%m-%d")), "TBA")
    add("ffr", "rates", "Fed 基準利率（上緣）", ffr, "%", 2, S["ffr"],
        "good" if cutting else ("bad" if hiking else "info"),
        "降息循環中＝三大買進訊號之首" if cutting else ("升息中＝資金退潮" if hiking else "利率按兵不動"),
        "Jane 把 Fed 開始連續降息列為精準買進訊號之首，並強調利率方向主導所有資產價格的走向。",
        "篇12/74/207", "FRED DFEDTARU", extra=f"下次 FOMC：{next_fomc}")

    yc = last(S["t10y2y"]); yc_series = S["t10y2y"]; sig = st = None
    was_inverted_recent = any(v < 0 for _, v in yc_series[-450:]) if yc_series else False
    if yc is not None:
        if yc < 0:
            sig, st = "bad", "倒掛中：12–18個月內衰退機率高"
        elif was_inverted_recent:
            sig, st = "warn", "已解除倒掛——Jane：真正危機常在恢復之後"
        else:
            sig, st = "good", "曲線正常，無倒掛警訊"
    add("curve", "rates", "殖利率曲線 10Y−2Y", yc * 100 if yc is not None else None, "bps", 0,
        [(d, v * 100) for d, v in yc_series] if yc_series else None, sig or "info", st or "",
        "Jane 稱之為用單一數字判斷經濟危機最可靠的方法：倒掛後12–18個月幾乎必有衰退（2000/2008/2020）。特別注意：真正的危機往往發生在倒掛恢復之後、市場放鬆警戒時。",
        "手冊篇25/137", "FRED T10Y2Y", refs=[0])

    v10 = last(S["dgs10"]); v10p = val_days_ago(S["dgs10"], 30)
    ten_spike = bool(v10 and v10p and v10 > v10p + 0.15)
    add("us10y", "rates", "美國10年期殖利率", v10, "%", 2, S["dgs10"],
        "bad" if ten_spike else "info",
        "快速上升：估值與房貸承壓" if ten_spike else "波動正常範圍",
        "10年期快速上升＝升息環境訊號、美元升值、高估值資產與房地產承壓；跨國利差擴大＝資金流入美元資產。",
        "手冊篇118/126", "FRED DGS10")

    rr = last(S["dfii10"])
    add("real_rate", "rates", "實質利率（10Y TIPS）", rr, "%", 2, S["dfii10"],
        "good" if (rr is not None and rr < 1) else ("warn" if (rr is not None and rr > 2.3) else "info"),
        ("實質利率低：利多黃金/BTC" if (rr is not None and rr < 1) else
         ("實質利率偏高：壓抑金價與估值" if (rr is not None and rr > 2.3) else "中性水位")),
        "實質利率上升→美元升值；下降→美元貶值。轉負時利多黃金、比特幣等替代資產。",
        "手冊篇118/153", "FRED DFII10")

    # === 三、危機預警燈板 ===
    crisis_flags = []
    if yc is not None and yc < 0:
        crisis_flags.append("殖利率曲線倒掛")
    hyv = last(S["hy"]); hyp = val_days_ago(S["hy"], 30)
    hy_bps = hyv * 100 if hyv is not None else None
    hy_widen = hyv is not None and hyp is not None and (hyv - hyp) * 100 > 80
    sig = st = None
    if hy_bps is not None:
        if hy_bps > 500 or hy_widen:
            sig, st = "bad", "利差急擴：系統性風險警訊"; crisis_flags.append("信用利差急擴")
        elif hy_bps > 400:
            sig, st = "warn", "利差偏高，留意企業違約風險"
        else:
            sig, st = "good", "信用市場平靜"
    add("hy_spread", "crisis", "高收益信用利差 HY OAS", hy_bps, "bps", 0,
        [(d, v * 100) for d, v in S["hy"]] if S["hy"] else None, sig or "info", st or "",
        "信用利差快速擴大＝市場對企業違約的擔憂急升，是系統性風險的早期訊號。Jane 每週危機清單成員。門檻：<350 平靜、>400 戒備、>500 或單月急擴80bp＝警報。",
        "手冊篇137/163", "FRED BAMLH0A0HYM2", refs=[400])

    vix = last(S["vix"])
    vix_spike = vix is not None and vix > 30
    if vix_spike:
        crisis_flags.append("VIX>30 恐慌")
    add("vix", "crisis", "VIX 恐慌指數", vix, "", 1, S["vix"],
        "bad" if vix_spike else ("warn" if (vix or 0) > 25 else ("info" if (vix or 0) > 15 else "warn")),
        ("恐慌區：警戒＋留意佈局機會" if vix_spike else
         ("波動升溫（25–30），提高警覺" if (vix or 0) > 25 else
          ("平靜區間" if (vix or 0) > 15 else "過度自滿（<15），反而留意突變"))),
        "VIX 從低位（15以下）快速飆到30以上＝恐慌急劇惡化的警戒訊號；急升時資金逃向美元避險。長期<15 反而要警覺——最危險的時候是所有人都覺得安全的時候。",
        "手冊篇16/118/137", "FRED VIXCLS",
        band={"min": 10, "max": 45, "zones": [[10, 15, "warn"], [15, 25, "info"], [25, 30, "warn"], [30, 45, "bad"]]})

    ic = last(S["icsa"]); ic_k = ic / 1000 if ic else None
    if ic_k is not None and ic_k > 300:
        crisis_flags.append("初領失業金>30萬")
    add("claims", "crisis", "初領失業救濟金", ic_k, "千件", 0,
        [(d, v / 1000) for d, v in S["icsa"]] if S["icsa"] else None,
        "bad" if (ic_k or 0) > 300 else ("warn" if (ic_k or 0) > 260 else "good"),
        ("突破30萬件：衰退前兆" if (ic_k or 0) > 300 else
         ("升溫中，密切觀察" if (ic_k or 0) > 260 else "就業市場穩定")),
        "單週暴增至30萬件以上＝消費放緩與衰退前兆（2008、2020危機前皆飆升）。Jane 列為最早察覺實體經濟崩跌的方法首位。週資料毛躁，看四週趨勢。",
        "最快最簡單篇243", "FRED ICSA", refs=[300])

    cgv = last(cg); cgp = val_days_ago(cg, 60)
    cg_fall = cgv is not None and cgp is not None and cgv < cgp * 0.9
    if cg_fall:
        crisis_flags.append("銅金比急跌")
    add("copper_gold", "crisis", "銅金比 ×1000", cgv, "", 3, cg,
        "bad" if cg_fall else "info",
        "快速下跌：全球景氣信心惡化" if cg_fall else "未見急跌，景氣信心尚穩",
        "銅代表工業需求、黃金代表避險需求：銅金比快速下跌＝市場對全球景氣的信心急劇惡化、衰退風險上升。換算：銅價($/lb)÷金價($/oz)×1000，數字本身無意義，看方向與速度。",
        "手冊篇145", "Yahoo/FRED",
        extra=(f"銅 ${last(S['copper']):.2f}/lb・金 ${last(S['gold']):,.0f}/oz"
               if last(S.get("copper") or []) and last(S.get("gold") or []) else ""))

    # === 四、景氣與通膨 ===
    un = last(S["unrate"])
    un12low = min((v for _, v in S["unrate"][-13:]), default=None) if S["unrate"] else None
    un_rise = (un - un12low) if (un is not None and un12low is not None) else None
    if un_rise is not None and un_rise >= 0.8:
        crisis_flags.append("失業率快速上升")
    add("unrate", "econ", "失業率", un, "%", 1, S["unrate"],
        "bad" if (un_rise or 0) >= 0.8 else ("warn" if (un_rise or 0) >= 0.4 else "good"),
        ((f"較12月低點+{un_rise:.1f}pp：接近衰退門檻" if (un_rise or 0) >= 0.8 else
          (f"較12月低點+{un_rise:.1f}pp，開始鬆動" if (un_rise or 0) >= 0.4 else "就業穩定，Fed 無急迫降息壓力"))
         if un_rise is not None else ""),
        "失業率自低點上升近1個百分點的情況，歷史上幾乎只出現在衰退期間＝衰退訊號＋Fed 降息的依據。Jane 認為就業數據才是 Fed 決策的真正核心。",
        "思考脈絡篇188・手冊篇74", "FRED UNRATE")

    cpi_y = yoy(S["cpi"]); cpi_series = yoy_series(S["cpi"]); sig = st = None
    if cpi_y is not None:
        if cpi_y > 4: sig, st = "bad", "通膨警戒區（>4%）：限制降息空間"
        elif cpi_y > 3: sig, st = "warn", "高於目標，留意停滯性通膨組合"
        elif cpi_y < 1: sig, st = "warn", "過低：需求疲弱訊號"
        else: sig, st = "good", "接近 Fed 2% 目標區"
    add("cpi", "econ", "CPI 年增率", cpi_y, "%", 1, cpi_series, sig or "info", st or "",
        "門檻：4–5%以上＝通膨手冊啟動（買原物料/能源/黃金）；2%＝Fed 目標；3–4%以上且 GDP 停滯＝停滯性通膨（黃金史上最佳環境）。方向比水位重要：在降途與回升途是兩種劇本。",
        "手冊篇35/153", "FRED CPIAUCSL", refs=[2, 4],
        extra=f"資料月份：{last_date(S['cpi'])}" if S["cpi"] else "")

    rs = S["retail"]
    neg3 = (len(rs) >= 4 and all(rs[-i][1] < rs[-i - 1][1] for i in (1, 2, 3)))
    rs_yoy = yoy(rs)
    if neg3:
        crisis_flags.append("零售連3月負成長")
    add("retail", "econ", "零售銷售（年增）", rs_yoy, "%", 1, yoy_series(rs),
        "bad" if neg3 else ("warn" if (rs_yoy or 0) < 0 else "good"),
        ("連3個月負成長：消費萎縮正式開始" if neg3 else
         ("年增轉負，留意" if (rs_yoy or 0) < 0 else "消費維持擴張（占GDP逾60%）")),
        "連續3個月以上負成長＝消費萎縮正式開始。消費占美國 GDP 逾60%，股市對消費變化極為敏感。注意：名目值，高通膨時期會虛胖，要配 CPI 一起讀。",
        "最快最簡單篇243", "FRED RSAFS", refs=[0], extra=f"資料月份：{last_date(rs)}" if rs else "")

    um = last(S["umcsent"]); ump = val_days_ago(S["umcsent"], 120)
    um_drop = um is not None and ump is not None and um < ump * 0.88
    add("csent", "econ", "消費者信心（密大）", um, "", 1, S["umcsent"],
        "bad" if (um or 99) < 60 else ("warn" if um_drop else "good"),
        ("信心低迷：衰退風險區" if (um or 99) < 60 else
         ("急跌中：Jane 的衰退將至訊號" if um_drop else "信心尚穩")),
        "消費信心急跌＝衰退將至的訊號（Jane 引 Conference Board <70 門檻；此處用密大指數，約 <60 為對應低迷區）。",
        "手冊篇25・篇243", "FRED UMCSENT")

    # === 五、市場情緒與時機開關 ===
    fng = last(S["fng"])
    fng_int = int(fng) if fng is not None else None
    opp_fng = fng_int is not None and fng_int < 20
    sig = st = None
    if fng_int is not None:
        if fng_int < 20: sig, st = "good", "極度恐慌 <20：Jane 的精準買進訊號！"
        elif fng_int > 80: sig, st = "bad", "極度貪婪 >80：快速賣出、轉成現金"
        elif fng_int < 45: sig, st = "info", "恐慌偏向，逢低留意"
        elif fng_int > 55: sig, st = "warn", "貪婪偏向，控制追高"
        else: sig, st = "info", "情緒中性"
    add("fng", "mood", "CNN 恐懼貪婪指數", fng_int, "", 0, S["fng"], sig or "info", st or "",
        "Jane 的量化開關：<20 極度恐慌＝買進訊號；>80 極度貪婪＝賣出轉現金。把它當群眾心理的鏡子，練習辨識群眾何時失去理性。中間值（30–70）無資訊量，別過度解讀。",
        "最快最簡單篇12/48/62/205", "CNN", opp=opp_fng,
        band={"min": 0, "max": 100, "zones": [[0, 20, "good"], [20, 45, "info"], [45, 55, "info"], [55, 80, "warn"], [80, 100, "bad"]]})

    spx = S["spx"]; spx_last = last(spx)
    ath = max((v for _, v in spx), default=None) if spx else None
    dd = (spx_last / ath - 1) * 100 if (spx_last and ath) else None
    opp_dd = dd is not None and dd <= -20
    sig = st = None
    if dd is not None:
        if dd <= -40: sig, st = "good", "−40%：Jane：剩餘現金全部投入"
        elif dd <= -30: sig, st = "good", "−30%：第二次買進點"
        elif dd <= -20: sig, st = "good", "−20%：第一次買進點到了"
        elif dd <= -10: sig, st = "warn", "修正中（−10%），準備彈藥"
        else: sig, st = "info", "接近高點區，遵守賣出紀律"
    if opp_dd:
        crisis_flags.append("S&P自高點跌逾20%")
    add("spx_dd", "mood", "S&P500 距近一年高點", dd, "%", 1, spx, sig or "info", st or "",
        "分批公式：跌20%→第一買；30%→第二買；40%以上→全投入。賣出端：較前高+30%以上→先賣一半。機械式規則的意義：到時候你一定不敢，所以現在就寫死。",
        "最快最簡單篇11/12/48/116", "Yahoo/FRED",
        band={"min": -45, "max": 5, "zones": [[-45, -40, "good"], [-40, -30, "good"], [-30, -20, "good"], [-20, -10, "warn"], [-10, 5, "info"]]},
        extra=f"S&P500 {spx_last:,.0f}・一年高點 {ath:,.0f}" if spx_last and ath else "", opp=opp_dd)

    ma200 = sma(spx, 200)
    above = spx_last is not None and ma200 is not None and spx_last >= ma200
    gap = (spx_last / ma200 - 1) * 100 if (spx_last and ma200) else None
    gap_series = None
    if spx and len(spx) > 220:
        gap_series = [(spx[i][0], round((spx[i][1] / (sum(v for _, v in spx[i - 199:i + 1]) / 200) - 1) * 100, 2))
                      for i in range(len(spx) - 120, len(spx)) if i >= 199]
    add("ma200", "mood", "S&P500 vs 200日均線", gap, "%", 1, gap_series,
        ("info" if above else "good") if gap is not None else "info",
        ((f"高於200日線 {gap:+.1f}%：趨勢完好" if above else f"跌破200日線（{gap:+.1f}%）：金融富豪分批買進時機")
         if gap is not None else "資料累積中"),
        "Jane 觀察：跌破200日線是行家分批買進的時機——前提是產業持續成長、市場龍頭、現金充足，那才是真正的折價。震盪市會反覆假穿，與跌幅階梯一起亮才算數。",
        "最快最簡單篇205", "計算", refs=[0], opp=(not above) if gap is not None else False)

    cape_cur = cape_val or (cape_hist[-1][1] if cape_hist else None)
    add("cape", "mood", "Shiller CAPE", cape_cur, "", 1, None,
        "bad" if (cape_cur or 0) > 35 else ("warn" if (cape_cur or 0) > 30 else "good"),
        ("極度過熱（>35）" if (cape_cur or 0) > 35 else
         ("高估值警戒區（>30）：後續10年報酬率通常很低" if (cape_cur or 0) > 30 else "估值合理區")),
        "門檻：>30＝高估值警戒區、>35＝極度過熱。歷史上超過30後，後續10年股市報酬率通常偏低，甚至出現大型修正。它決定倉位上限與現金底線，不決定買賣時點——高可以更高很多年。",
        "手冊篇141", "multpl.com", acc=cape_hist,
        band={"min": 15, "max": 45, "zones": [[15, 30, "good"], [30, 35, "warn"], [35, 45, "bad"]]})

    # === 六、美元霸權計分板 ===
    btc = S["btc"]; btc_last = last(btc); btc_ma200 = sma(btc, 200)
    btc_up = btc_last is not None and btc_ma200 is not None and btc_last > btc_ma200
    add("btc", "hegemony", "比特幣", btc_last, "$", 0, btc,
        "good" if btc_up else "warn",
        "位於200日線上：流動性溫度計偏暖" if btc_up else "位於200日線下：風險偏好降溫",
        "雙重角色：流動性溫度計＋Jane 眼中美國的資產放大器（美國是最大持有國，ETF 是資金流入幫浦）。她主張投資組合應分散一部分到 BTC/ETH，守住個人金融主權。",
        "思考脈絡篇215/240・手冊篇225", "CoinGecko")

    gold = S["gold"]; gold_last = last(gold); gold_p = val_days_ago(gold, 90)
    gold_up = gold_last is not None and gold_p is not None and gold_last > gold_p
    add("gold", "hegemony", "黃金", gold_last, "$/oz", 0, gold,
        "info", "季線趨勢向上：避險與去美元需求強" if gold_up else "近季回檔整理",
        "Jane 的配置底線：資產至少10–20%放黃金/白銀，作為危機的最後保險——2008、2020資金最先湧入的就是黃金；也是美元霸權動搖時的分流去處、停滯性通膨環境的歷史最佳資產。",
        "篇207/215・手冊篇153", "Yahoo/Stooq")

    add("usdc_share", "hegemony", "穩定幣市占 USDC", usdc_share, "%", 1, None,
        "info",
        ("USDC 份額上升＝美元霸權加固訊號" if (usdc_share is not None and len(usdc_hist) > 5 and usdc_share > usdc_hist[0][1])
         else "觀察 USDC vs USDT 消長"),
        "美中貨幣戰即時計分板：USDC（美元/美債1:1準備）市占提升＝美國陣營占上風＝美債多一層結構性買盤——用美國穩定幣等於間接買美債。",
        "CBDC篇165・思考脈絡篇237", "CoinGecko",
        extra=(f"USDT ${stables[1]/1e9:.0f}B・USDC ${stables[2]/1e9:.0f}B" if stables[1] else ""), acc=usdc_hist)

    twd = S["twd"]; twd_last = last(twd); twd_acc = None
    if not twd:
        cur = S.get("twd_now")
        if isinstance(cur, (int, float)):
            twd_acc = accumulate("usdtwd", cur)
            twd_last = cur
    add("usdtwd", "hegemony", "美元兌台幣", twd_last, "", 2, twd if twd else None,
        "info",
        ("台幣偏強：分批買美元的區間" if (twd_last or 99) < 30 else "台幣偏弱：美元資產已具匯率順風") if twd_last else "",
        "Jane 的操作節奏：台幣強（匯率低）時分批買美元、升高時換回台幣；並用 DXY 提前預測台幣方向，避免事後反應。",
        "文盲篇115/117", "Yahoo/er-api", acc=twd_acc)

    # ---------------- 總評 ----------------
    W = {"liq": 25, "rates": 20, "crisis": 20, "econ": 15, "mood": 10, "hegemony": 10}
    smap = {"good": 1.0, "info": 0.55, "warn": 0.3, "bad": 0.0}
    gsum, gw = {}, {}
    for i in IND:
        g = i["group"]
        gsum[g] = gsum.get(g, 0) + smap.get(i["signal"], 0.5)
        gw[g] = gw.get(g, 0) + 1
    score = sum(W[g] * (gsum[g] / gw[g]) for g in gsum if g in W)
    score = round(score / sum(W[g] for g in gsum if g in W) * 100)

    n_crisis = len(crisis_flags)
    if n_crisis >= 5:
        level, color = "危機警報", "red"
    elif n_crisis >= 3:
        level, color = "防禦警戒", "orange"
        score = min(score, 44)
    elif score >= 68:
        level, color = "擴張進攻", "green"
    elif score >= 45:
        level, color = "中性觀察", "blue"
    elif score >= 25:
        level, color = "防禦警戒", "orange"
    else:
        level, color = "危機警報", "red"

    opps = [i["label"] for i in IND if i.get("opp")]
    if opps and (n_crisis >= 3 or score < 45):
        level += "・遍地黃金"

    summary_bits = ["流動性總開關：開（進攻）" if liq_on else "流動性總開關：關（防禦）",
                    "Fed 降息循環中" if cutting else ("Fed 升息中" if hiking else "Fed 按兵不動")]
    if dxy is not None:
        summary_bits.append(f"DXY {dxy:.0f}（{'強勢' if dxy > 105 else '弱勢' if dxy < 100 else '中性'}）")
    summary_bits.append(f"危機警訊 {n_crisis}/8 項")
    summary = "、".join(summary_bits) + "。"
    if n_crisis >= 3:
        summary += " Jane：3個警訊→降風險；5個以上→危機非常接近。"
    if opps:
        summary += f" 買點旗標：{'、'.join(opps)}——檢查現金彈藥。"

    # ---------------- 資金流向 ----------------
    flows = []
    dxy_p = val_days_ago(S["dxy"], 30)
    if dxy is not None and dxy_p is not None:
        if dxy > dxy_p * 1.01:
            flows.append({"dir": "usd", "text": "資金流向美元・美債（DXY 近月走強）——新興市場與原物料承壓"})
        elif dxy < dxy_p * 0.99:
            flows.append({"dir": "risk", "text": "資金流出美元→股票、原物料、加密（DXY 近月走弱）"})
        else:
            flows.append({"dir": "flat", "text": "美元近月持平：資金觀望，等下一個利率訊號"})
    flows.append({"dir": "risk" if liq_on else "usd",
                  "text": "流動性水位：" + ("擴張中（Fed↑/RRP↓/M2↑偏多）＝股票與加密的上游活水" if liq_on else "收縮中＝資金退向美元、公債與黃金")})
    if gold_last and gold_p and spx_last:
        spx_p = val_days_ago(spx, 90)
        g_ret = gold_last / gold_p - 1
        s_ret = (spx_last / spx_p - 1) if spx_p else 0
        if g_ret > s_ret + 0.03:
            flows.append({"dir": "safe", "text": f"近一季黃金（{g_ret:+.1%}）跑贏股票（{s_ret:+.1%}）：避險與去美元資金在集結"})
        elif s_ret > g_ret + 0.03:
            flows.append({"dir": "risk", "text": f"近一季股票（{s_ret:+.1%}）跑贏黃金（{g_ret:+.1%}）：風險偏好主導"})
        else:
            flows.append({"dir": "flat", "text": "股票與黃金同步走：Risk-On 與避險資金雙軌並行"})
    if btc_last:
        btc_p = val_days_ago(btc, 30)
        if btc_p:
            b_ret = btc_last / btc_p - 1
            flows.append({"dir": "risk" if b_ret > 0.05 else ("safe" if b_ret < -0.05 else "flat"),
                          "text": f"加密資金流：BTC 近月 {b_ret:+.1%}——" + ("邊際流動性湧入風險端" if b_ret > 0.05 else ("邊際資金撤出風險端" if b_ret < -0.05 else "持平觀望"))})
    if hy_bps is not None:
        flows.append({"dir": "flat" if hy_bps < 400 else "safe",
                      "text": f"信用市場：HY 利差 {hy_bps:.0f}bps——" + ("資金仍願意借給企業，信用暢通" if hy_bps < 400 else "資金撤出企業債，信用收縮")})
    if dxy is not None and dxy_p is not None and dxy > dxy_p * 1.01 and liq_on:
        flows.append({"dir": "flat",
                      "text": "⚠ 美元轉強與流動性擴張訊號矛盾中——Jane 的仲裁規則：跟著利率（2Y）的方向走"})

    # ---------------- 資產影響矩陣 ----------------
    def stance(good_cond, bad_cond):
        return "順風" if good_cond else ("逆風" if bad_cond else "中性")

    assets = [
        {"key": "stocks", "name": "股票", "stance": stance(liq_on and not hiking and n_crisis < 3, (not liq_on and hiking) or n_crisis >= 3),
         "note": ("流動性開、利率不升＝順風；" if (liq_on and not hiking) else "流動性或利率不利；") + ("危機燈板乾淨" if n_crisis == 0 else f"燈板有{n_crisis}項警訊")},
        {"key": "bonds", "name": "長期債券", "stance": stance(cutting or (un_rise or 0) >= 0.4, hiking or (cpi_y or 0) > 4),
         "note": ("衰退風險升溫或降息循環＝長債獲利窗口（TLT/IEF）" if (cutting or (un_rise or 0) >= 0.4) else
                  ("通膨/升息壓制債券價格" if (hiking or (cpi_y or 0) > 4) else "利率持平，領息等轉折"))},
        {"key": "gold", "name": "黃金", "stance": stance((rr or 9) < 1 or (dxy or 0) < 100 or n_crisis >= 2, (rr or 0) > 2.3 and (dxy or 0) > 105),
         "note": "Jane 底線：無論環境如何，10–20% 常備配置——危機的最後保險"},
        {"key": "re", "name": "房地產", "stance": stance(cutting and (v10 or 9) < 3.5, (v10 or 0) > 4 or hiking),
         "note": "高利率＝高槓桿房產最危險；Jane：自住一間為限，投資改走 REITs/代幣化"},
        {"key": "crypto", "name": "加密資產", "stance": stance(liq_on and btc_up, (not liq_on) and (not btc_up)),
         "note": "流動性溫度計：放水時最受益、收水時最先被賣；小比例配置守金融主權"},
        {"key": "cash", "name": "現金", "stance": ("彈藥時刻" if (opps or n_crisis >= 2) else "常備"),
         "note": ("買點旗標出現：這就是保留30%現金在等的時刻" if opps else "Jane 鐵律：平時至少10–30%現金，過熱時提高到40–50%")},
    ]

    data = {
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "updated_tpe": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "mock": MOCK,
        "errors": errors,
        "composite": {"score": score, "level": level, "color": color, "summary": summary,
                      "crisis_flags": crisis_flags, "crisis_total": 8,
                      "watchlist": [{"name": w, "fired": w in crisis_flags} for w in
                                    ["殖利率曲線倒掛", "信用利差急擴", "VIX>30 恐慌", "初領失業金>30萬",
                                     "銅金比急跌", "失業率快速上升", "零售連3月負成長", "S&P自高點跌逾20%"]],
                      "opportunity_flags": opps, "liq_on": liq_on},
        "flows": flows,
        "assets": assets,
        "groups": [
            {"id": "liq", "title": "每日流動性五件套", "note": "Jane：頂尖投資人每天早晨先確認的組合 — 手冊篇225"},
            {"id": "rates", "title": "利率與債市", "note": "利率方向主導所有資產價格 — 篇207"},
            {"id": "crisis", "title": "危機預警燈板", "note": "3個警訊→降風險；5個以上→危機非常接近 — 篇137"},
            {"id": "econ", "title": "景氣與通膨", "note": "每天10分鐘：利率、匯率、失業率 — 篇39"},
            {"id": "mood", "title": "市場情緒與時機開關", "note": "在恐懼時買進，在貪婪時賣出"},
            {"id": "hegemony", "title": "美元霸權計分板", "note": "讀懂美元流向＝讀懂全球資本流向"},
        ],
        "indicators": IND,
    }
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"\nWrote data.json  score={score} level={level} crisis={n_crisis} errors={len(errors)}", flush=True)


if __name__ == "__main__":
    import socket
    socket.setdefaulttimeout(30)
    main()
    os._exit(0)  # 若有懸掛的抓取執行緒，強制正常退出