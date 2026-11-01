#!/usr/bin/env python3
"""
股市尋寶｜盤後資料腳本
========================================
從 FinMind（必要時用 Yahoo Finance 補股價）抓台股盤後資料，
輸出 data.json 與 data.js，給「股市尋寶」網頁使用。

安裝：
    pip install requests pandas lxml yfinance

用法：
    # 建議先到 finmindtrade.com 註冊取得 token（流量上限較高）
    export FINMIND_TOKEN=你的token          # Windows: set FINMIND_TOKEN=你的token
    python taojin_data.py

    python taojin_data.py --chip-top 300     # 籌碼／基本面只抓成交值前 300 名 + 有股期的股票
    python taojin_data.py --list my.txt      # 另外加入自選股（每行一個代號）
    python taojin_data.py --days 260 --out ./site

輸出：
    data.json  → 網頁上按「載入資料」選這個檔
    data.js    → 跟 taojin.html 放同一個資料夾，直接打開網頁就會自動讀取

資料取得策略：
    1. FinMind 贊助（backer / sponsor）會員：可「不帶 data_id 一次抓某日全部股票」，
       腳本會自動偵測並用這個模式，全市場股價、法人、融資券、本益比都抓得到。
    2. 免費會員：股價改用 yfinance 一次下載全市場；
       法人、融資券、月營收、財報只抓「成交值前 N 名 + 股票期貨標的 + 自選股」，
       並且全部有本地快取，第二天起只補新資料。
    3. 股票期貨標的清單來自期交所網站，抓不到時沿用上次快取。

建議執行時間：交易日晚上 10 點後（法人、融資券資料都已公布）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

API = "https://api.finmindtrade.com/api/v4/data"
TAIFEX_STOCK_LIST = "https://www.taifex.com.tw/cht/2/stockLists"
UA = {"User-Agent": "Mozilla/5.0 (taojin-screener)"}


# ──────────────────────────── FinMind client ────────────────────────────
class FinMind:
    def __init__(self, token: str | None, rate_per_hour: int):
        self.s = requests.Session()
        self.token = token
        self.min_interval = 3600.0 / max(rate_per_hour, 1)
        self.last = 0.0
        self.calls = 0

    def get(self, dataset: str, data_id: str | None = None,
            start: str | None = None, end: str | None = None) -> pd.DataFrame | None:
        """回傳 DataFrame；沒有權限或失敗時回傳 None。"""
        params = {"dataset": dataset}
        if data_id:
            params["data_id"] = data_id
        if start:
            params["start_date"] = start
        if end:
            params["end_date"] = end
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}

        for attempt in range(4):
            wait = self.min_interval - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.s.get(API, params=params, headers=headers, timeout=60)
                self.last = time.time()
                self.calls += 1
                j = r.json()
            except Exception as e:  # 網路錯誤：退避重試
                print(f"    ! {dataset} {data_id or ''} 連線失敗：{e}，重試中")
                time.sleep(5 * (attempt + 1))
                continue
            status = j.get("status", r.status_code)
            if status == 200:
                return pd.DataFrame(j.get("data", []))
            if status == 402:
                print("    ! 已達 FinMind 每小時流量上限，等待 10 分鐘後繼續…")
                time.sleep(600)
                continue
            # 400 等：多半是權限不足（例如免費會員不帶 data_id）
            return None
        return None


# ──────────────────────────── cache helpers ────────────────────────────
class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def load(self, *parts: str) -> pd.DataFrame | None:
        p = self.path(*parts)
        if p.exists():
            try:
                return pd.read_pickle(p)
            except Exception:
                return None
        return None

    def save(self, df: pd.DataFrame, *parts: str) -> None:
        df.to_pickle(self.path(*parts))

    def age_days(self, *parts: str) -> float:
        p = self.path(*parts)
        if not p.exists():
            return 1e9
        return (time.time() - p.stat().st_mtime) / 86400


def incremental(fm: FinMind, cache: Cache, dataset: str, sid: str,
                start: str, end: str) -> pd.DataFrame:
    """逐檔增量抓取：只補快取最後一天之後的資料。"""
    old = cache.load(dataset, f"{sid}.pkl")
    fetch_from = start
    if old is not None and not old.empty and "date" in old:
        last = pd.to_datetime(old["date"]).max()
        fetch_from = max(start, (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    if old is not None and fetch_from > end:
        return old
    new = fm.get(dataset, sid, fetch_from, end)
    if new is None:
        return old if old is not None else pd.DataFrame()
    df = pd.concat([old, new]) if old is not None else new
    if not df.empty:
        df = df.drop_duplicates().sort_values("date")
        df = df[df["date"] >= start]
    cache.save(df, dataset, f"{sid}.pkl")
    return df


def bulk_by_date(fm: FinMind, cache: Cache, dataset: str, dates: list[str]) -> pd.DataFrame:
    """贊助會員模式：一次抓某日全市場，按日期快取。"""
    frames = []
    for i, d in enumerate(dates):
        df = cache.load(dataset, "by_date", f"{d}.pkl")
        if df is None:
            df = fm.get(dataset, start=d, end=d)
            if df is None:
                df = pd.DataFrame()
            if not df.empty or d < dates[-1]:  # 最新一天若還沒資料就先不快取
                cache.save(df, dataset, "by_date", f"{d}.pkl")
            if (i + 1) % 20 == 0:
                print(f"    {dataset}：{i + 1}/{len(dates)} 天")
        frames.append(df)
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames) if frames else pd.DataFrame()


# ──────────────────────────── data sources ────────────────────────────
def load_universe(fm: FinMind, cache: Cache) -> pd.DataFrame:
    info = None
    if cache.age_days("TaiwanStockInfo.pkl") < 7:
        info = cache.load("TaiwanStockInfo.pkl")
    if info is None:
        info = fm.get("TaiwanStockInfo")
        if info is None or info.empty:
            info = cache.load("TaiwanStockInfo.pkl")
            if info is None:
                sys.exit("無法取得 TaiwanStockInfo（股票清單），請確認網路與 token。")
        else:
            cache.save(info, "TaiwanStockInfo.pkl")
    info = info[info["type"].isin(["twse", "tpex"])]
    info = info[info["stock_id"].str.fullmatch(r"[1-9]\d{3}")]  # 4 碼普通股，排除 00 開頭 ETF
    # 排除已下市股票：清單的 date 欄位太舊的代表已不再交易
    if "date" in info:
        d = pd.to_datetime(info["date"], errors="coerce")
        recent = info[d >= d.max() - pd.Timedelta(days=30)]
        if len(recent) > 1500:  # 保險：篩完數量合理才採用
            info = recent
    info = info.drop_duplicates("stock_id", keep="last")
    info = info[["stock_id", "stock_name", "industry_category", "type"]].reset_index(drop=True)
    return info


def load_futures_list(cache: Cache) -> set[str]:
    """期交所『股票期貨／股票選擇權交易標的』清單。"""
    try:
        html = requests.get(TAIFEX_STOCK_LIST, headers=UA, timeout=30).text
        tables = pd.read_html(io.StringIO(html))
        ids: set[str] = set()
        for t in tables:
            cols = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
            t.columns = cols
            code_col = next((c for c in cols if "證券代號" in c), None)
            fut_col = next((c for c in cols if "股票期貨" in c and "標的" in c), None)
            if not code_col or not fut_col:
                continue
            mask = t[fut_col].astype(str).str.contains("●|◎|是", na=False)
            ids |= set(t.loc[mask, code_col].astype(str).str.strip().str[:4])
        ids = {i for i in ids if i.isdigit()}
        if len(ids) > 50:
            cache.save(pd.DataFrame({"stock_id": sorted(ids)}), "futures_list.pkl")
            return ids
        raise ValueError("清單筆數過少")
    except Exception as e:
        print(f"  ! 期交所股期清單抓取失敗（{e}），改用上次快取")
        old = cache.load("futures_list.pkl")
        return set(old["stock_id"]) if old is not None else set()


MONEYDJ_HOLDINGS = "https://www.moneydj.com/ETF/X/Basic/Basic0007a.xdjhtm?etfid={}"
TWSE_QUOTES = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={}&type={}&response=json"
TPEX_QUOTES = [
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={slash}&type=AL&response=json",
    "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc}&se=AL",
]


def _norm(name: str) -> str:
    return str(name).replace("*", "").replace(" ", "").replace("　", "").replace("-", "").upper()


def read_list_file(path: Path) -> set[str] | None:
    """lists/*.txt：每行一個代號（# 開頭為註解）。檔案存在就以它為準。"""
    if not path.exists():
        return None
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line[:4].isdigit():
            ids.add(line[:4])
    return ids


def etf_holdings(etf: str, name2id: dict[str, str], min_count: int) -> set[str] | None:
    """從 MoneyDJ 的 ETF 全部持股頁抓成分股（0050 = 台灣50、0051 = 中型100）。"""
    try:
        r = requests.get(MONEYDJ_HOLDINGS.format(etf), headers=UA, timeout=30)
        raw = r.content
        html = None
        for enc in ("utf-8", "big5", "cp950"):  # 網站沒宣告編碼時，requests 會誤判造成中文亂碼
            try:
                html = raw.decode(enc)
                if "股票名稱" in html or "持股" in html:
                    break
            except UnicodeDecodeError:
                continue
        if html is None:
            html = raw.decode("utf-8", "ignore")
        ids = set()
        for t in pd.read_html(io.StringIO(html)):
            t.columns = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
            col = next((c for c in t.columns if "名稱" in c), None)
            if col is None:
                continue
            for n in t[col].astype(str):
                m = re.search(r"\b([1-9]\d{3})\b", n)  # 名稱裡若含代號直接用
                sid = m.group(1) if m else name2id.get(_norm(re.sub(r"\(.*?\)|（.*?）", "", n)))
                if sid:
                    ids.add(sid)
        if not ids:
            print(f"    （{etf} 頁面長度 {len(html)}，找到表格但名稱對不上，可能網站改版）")
        if len(ids) >= min_count:
            return ids
        print(f"  ! {etf} 持股只對應到 {len(ids)} 檔，視為失敗")
    except Exception as e:
        print(f"  ! {etf} 持股抓取失敗：{e}")
    return None


def _walk_rows(obj):
    """在任意 JSON 結構裡找出『列』（list of str）。"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, (str, int, float)) for x in obj):
            yield [str(x).strip() for x in obj]
        else:
            for x in obj:
                yield from _walk_rows(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_rows(v)


def warrant_underlyings(asof: str, stock_names: dict[str, str]) -> set[str] | None:
    """從上市、上櫃當日行情取出所有權證名稱，用名稱前綴對回標的股票。"""
    d = dt.date.fromisoformat(asof)
    names: set[str] = set()
    urls = [TWSE_QUOTES.format(d.strftime("%Y%m%d"), t) for t in ("0999", "0999P")]
    urls += [u.format(slash=d.strftime("%Y/%m/%d"), roc=f"{d.year - 1911}/{d:%m/%d}") for u in TPEX_QUOTES]
    got_twse = got_tpex = False
    for u in urls:
        try:
            j = requests.get(u, headers=UA, timeout=30).json()
        except Exception:
            continue
        n0 = len(names)
        for row in _walk_rows(j):
            if len(row) >= 2 and len(row[0]) == 6 and row[0][:2].isdigit():
                nm = row[1]
                if any(k in nm for k in ("購", "售", "牛", "熊")):
                    names.add(nm)
        if len(names) > n0:
            if "twse" in u:
                got_twse = True
            else:
                got_tpex = True
        time.sleep(3)
    if not names:
        return None
    print(f"   權證 {len(names)} 檔（上市{'✓' if got_twse else '×'} 上櫃{'✓' if got_tpex else '×'}）")
    # 最長前綴比對，避免「台塑」誤配到「台塑化」的權證
    pairs = set()
    for sid, n in stock_names.items():
        pairs.add((_norm(n), sid))
        pairs.add((_norm(n).replace("KY", ""), sid))  # 權證名稱常省略 -KY
    by_len = sorted(pairs, key=lambda x: -len(x[0]))
    ids = set()
    for wn in names:
        w = _norm(wn)
        for n, sid in by_len:
            if len(n) >= 2 and w.startswith(n):
                ids.add(sid)
                break
    return ids


def load_lists(universe: pd.DataFrame, asof: str, cache: Cache, list_dir: Path) -> dict[str, set[str] | None]:
    """成分股與權證清單。lists/ 資料夾裡的 txt 優先，其次自動抓取，最後沿用快取。"""
    name2id = {_norm(n): s for s, n in zip(universe["stock_id"], universe["stock_name"])}
    stock_names = dict(zip(universe["stock_id"], universe["stock_name"]))
    auto = {
        "tw50": lambda: etf_holdings("0050.TW", name2id, 45),
        "mid100": lambda: etf_holdings("0051.TW", name2id, 90),
        "ftse": lambda: None,  # 富時台灣沒有公開免費清單，請自行維護 lists/ftse.txt
        "warrant": lambda: warrant_underlyings(asof, stock_names),
    }
    out: dict[str, set[str] | None] = {}
    for key, fetch in auto.items():
        ids = read_list_file(list_dir / f"{key}.txt")
        src = "lists/" + key + ".txt"
        if ids is None:
            ids, src = fetch(), "自動抓取"
            if ids:
                cache.save(pd.DataFrame({"stock_id": sorted(ids)}), f"list_{key}.pkl")
            else:
                old = cache.load(f"list_{key}.pkl")
                ids, src = (set(old["stock_id"]), "上次快取") if old is not None else (None, "")
        out[key] = ids
        print(f"   {key}: {len(ids) if ids else '無資料'} 檔 {src}")
    return out


def trading_dates(fm: FinMind, start: str, end: str) -> list[str]:
    df = fm.get("TaiwanStockPrice", "2330", start, end)
    if df is None or df.empty:
        sys.exit("無法取得交易日曆（2330 股價），請確認網路與 token。")
    return sorted(df["date"].unique().tolist())


def price_yfinance(universe: pd.DataFrame, start: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        print("  ! 沒有安裝 yfinance（pip install yfinance），改用 FinMind 逐檔抓股價（較慢）")
        return pd.DataFrame()
    suffix = universe["type"].map({"twse": ".TW", "tpex": ".TWO"})
    tickers = (universe["stock_id"] + suffix).tolist()
    frames = []
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        print(f"    yfinance {i + 1}–{i + len(chunk)} / {len(tickers)}")
        raw = yf.download(chunk, start=start, auto_adjust=False, group_by="ticker",
                          threads=True, progress=False)
        for t in chunk:
            try:
                d = raw[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if d.empty:
                continue
            frames.append(pd.DataFrame({
                "date": d.index.strftime("%Y-%m-%d"),
                "stock_id": t.split(".")[0],
                "open": d["Open"].values, "max": d["High"].values,
                "min": d["Low"].values, "close": d["Close"].values,
                "Trading_Volume": d["Volume"].values,
            }))
    return pd.concat(frames) if frames else pd.DataFrame()


# ──────────────────────────── transforms ────────────────────────────
def inst_net(df: pd.DataFrame) -> pd.DataFrame:
    """法人買賣超（張）→ 每日 foreign / trust / dealer 欄位。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["net"] = (df["buy"] - df["sell"]) / 1000
    grp = {"Foreign_Investor": "f", "Foreign_Dealer_Self": "f",
           "Investment_Trust": "t",
           "Dealer_self": "d", "Dealer_Hedging": "d", "Dealer": "d"}
    df["g"] = df["name"].map(grp)
    df = df.dropna(subset=["g"])
    return df.pivot_table(index=["stock_id", "date"], columns="g", values="net",
                          aggfunc="sum", fill_value=0).reset_index()


def month_revenue(df: pd.DataFrame) -> list:
    """回傳最近 13 個月 [年月, 營收(億), 年增%, 月增%]。"""
    if df is None or df.empty:
        return []
    d = df[["revenue_year", "revenue_month", "revenue"]].drop_duplicates(
        ["revenue_year", "revenue_month"], keep="last")
    d = d.sort_values(["revenue_year", "revenue_month"])
    key = {(int(r.revenue_year), int(r.revenue_month)): float(r.revenue) for r in d.itertuples()}
    out = []
    for (y, m), rev in key.items():
        prev_y = key.get((y - 1, m))
        prev_m = key.get((y, m - 1) if m > 1 else (y - 1, 12))
        yoy = round((rev / prev_y - 1) * 100, 1) if prev_y else None
        mom = round((rev / prev_m - 1) * 100, 1) if prev_m else None
        out.append([f"{y}-{m:02d}", round(rev / 1e8, 2), yoy, mom])
    return out[-13:]


def financials(df: pd.DataFrame) -> tuple[list, list]:
    """近四季單季 EPS 與毛利率（%）。"""
    if df is None or df.empty:
        return [], []
    p = df.pivot_table(index="date", columns="type", values="value", aggfunc="last").sort_index()
    eps = p["EPS"].dropna().tail(4).round(2).tolist() if "EPS" in p else []
    gm = []
    if "GrossProfit" in p and "Revenue" in p:
        g = (p["GrossProfit"] / p["Revenue"] * 100).replace([float("inf")], None).dropna()
        gm = g.tail(4).round(1).tolist()
    return eps, gm


def tail_list(s: pd.Series, n: int = 20, nd: int = 0) -> list:
    return [round(float(x), nd) if nd else int(round(float(x))) for x in s.tail(n).tolist()]


# ──────────────────────────── main ────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="股市尋寶 盤後資料腳本")
    ap.add_argument("--token", default=os.environ.get("FINMIND_TOKEN"), help="FinMind token（或設環境變數 FINMIND_TOKEN）")
    ap.add_argument("--days", type=int, default=420, help="保留幾個交易日的 K 線（預設 420，可回測 360 天）")
    ap.add_argument("--lists", default="./lists", help="成分股清單資料夾（0050/中型100/富時/權證，txt 每行一個代號）")
    ap.add_argument("--chip-top", type=int, default=300, help="免費模式下，籌碼/基本面抓成交值前幾名（預設 300）")
    ap.add_argument("--list", help="自選股清單檔，每行一個代號，一定會抓籌碼/基本面")
    ap.add_argument("--rate", type=int, default=550, help="FinMind 每小時請求上限（預設 550，贊助會員可調高）")
    ap.add_argument("--fund-refresh", type=float, default=5, help="月營收/財報快取幾天更新一次（預設 5）")
    ap.add_argument("--out", default=".", help="輸出資料夾")
    ap.add_argument("--cache", default="./taojin_cache", help="快取資料夾")
    args = ap.parse_args()

    if not args.token:
        print("提醒：沒有 FinMind token，流量上限很低。建議到 finmindtrade.com 註冊後設定 FINMIND_TOKEN。")

    fm = FinMind(args.token, args.rate)
    cache = Cache(Path(args.cache))
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=int(args.days * 1.55) + 10)).strftime("%Y-%m-%d")

    print("① 股票清單與股票期貨標的")
    universe = load_universe(fm, cache)
    fut_ids = load_futures_list(cache)
    print(f"   普通股 {len(universe)} 檔，股票期貨標的 {len(fut_ids)} 檔")

    dates = trading_dates(fm, start, end)[-args.days:]
    asof = dates[-1]
    print(f"   交易日 {dates[0]} ~ {asof}（{len(dates)} 天）")
    lists = load_lists(universe, asof, cache, Path(args.lists))

    print("② 偵測 FinMind 全市場模式")
    probe = fm.get("TaiwanStockPrice", start=asof, end=asof)
    bulk = probe is not None and not probe.empty and probe["stock_id"].nunique() > 100
    print("   ✓ 可用全市場模式（贊助會員）" if bulk else "   × 免費模式：股價用 yfinance，籌碼只抓重點股票")

    print("③ 股價")
    if bulk:
        price = bulk_by_date(fm, cache, "TaiwanStockPrice", dates)
    else:
        price = price_yfinance(universe, dates[0])
        if price.empty:
            ids = universe["stock_id"].tolist()
            frames = []
            for i, sid in enumerate(ids):
                frames.append(incremental(fm, cache, "TaiwanStockPrice", sid, dates[0], end))
                if (i + 1) % 50 == 0:
                    print(f"    {i + 1}/{len(ids)}")
            price = pd.concat([f for f in frames if not f.empty])
    # 交易日曆以「實際有多數股票成交的日期」為準，避免 FinMind 日曆比股價慢一天
    cnt = price.groupby("date")["stock_id"].nunique()
    extra = [d for d, n in cnt.items() if n >= cnt.max() * 0.5 and d >= dates[0] and d not in set(dates)]
    if extra:
        dates = sorted(set(dates) | set(extra))[-args.days:]
        print(f"   ※ FinMind 交易日曆缺少 {', '.join(extra)}，已依實際股價補上")
        asof = dates[-1]
    price = price[price["date"].isin(dates)]
    price = price[price["close"] > 0]
    print(f"   股價資料日：{asof}")
    price["vol"] = (price["Trading_Volume"] / 1000).round().astype(int)  # 股 → 張
    price = price.sort_values(["stock_id", "date"])
    by_price = {sid: g for sid, g in price.groupby("stock_id")}

    # 決定籌碼/基本面的範圍
    if bulk:
        chip_ids = set(by_price)
    else:
        val = {sid: float((g["close"] * g["vol"]).tail(20).mean()) for sid, g in by_price.items()}
        top = sorted(val, key=val.get, reverse=True)[:args.chip_top]
        chip_ids = set(top) | (fut_ids & set(by_price))
        if args.list and Path(args.list).exists():
            chip_ids |= {l.strip() for l in Path(args.list).read_text(encoding="utf-8").splitlines() if l.strip()}
        chip_ids &= set(by_price)
    print(f"   有股價 {len(by_price)} 檔；籌碼/基本面範圍 {len(chip_ids)} 檔")

    print("④ 法人買賣超、融資融券")
    chip_start = dates[-30]
    if bulk:
        inst = inst_net(bulk_by_date(fm, cache, "TaiwanStockInstitutionalInvestorsBuySell", dates[-30:]))
        margin = bulk_by_date(fm, cache, "TaiwanStockMarginPurchaseShortSale", dates[-30:])
        per = bulk_by_date(fm, cache, "TaiwanStockPER", dates[-1:])
    else:
        inst_frames, margin_frames, per_frames = [], [], []
        for i, sid in enumerate(sorted(chip_ids)):
            inst_frames.append(incremental(fm, cache, "TaiwanStockInstitutionalInvestorsBuySell", sid, chip_start, end))
            margin_frames.append(incremental(fm, cache, "TaiwanStockMarginPurchaseShortSale", sid, chip_start, end))
            if (i + 1) % 25 == 0:
                print(f"    {i + 1}/{len(chip_ids)}（已用 {fm.calls} 次請求）")
        inst = inst_net(pd.concat([f for f in inst_frames if not f.empty]) if inst_frames else pd.DataFrame())
        margin = pd.concat([f for f in margin_frames if not f.empty]) if margin_frames else pd.DataFrame()
        per = pd.DataFrame()
    by_inst = {sid: g.sort_values("date") for sid, g in inst.groupby("stock_id")} if not inst.empty else {}
    by_margin = {sid: g.sort_values("date") for sid, g in margin.groupby("stock_id")} if not margin.empty else {}
    by_per = {r.stock_id: r for r in per.itertuples()} if not per.empty else {}

    print("⑤ 月營收、財報、本益比（有快取時跳過）")
    rev_start = (today - dt.timedelta(days=800)).strftime("%Y-%m-%d")
    fin_start = (today - dt.timedelta(days=560)).strftime("%Y-%m-%d")
    fund: dict[str, dict] = {}
    for i, sid in enumerate(sorted(chip_ids)):
        rec = {}
        for ds, st in (("TaiwanStockMonthRevenue", rev_start),
                       ("TaiwanStockFinancialStatements", fin_start),
                       ("TaiwanStockPER", dates[-5])):
            if ds == "TaiwanStockPER" and bulk:
                continue
            df = cache.load(ds, f"{sid}.pkl")
            if df is None or cache.age_days(ds, f"{sid}.pkl") > args.fund_refresh:
                new = fm.get(ds, sid, st, end)
                if new is not None:
                    df = new
                    cache.save(df, ds, f"{sid}.pkl")
            rec[ds] = df
        fund[sid] = rec
        if (i + 1) % 25 == 0:
            print(f"    {i + 1}/{len(chip_ids)}（已用 {fm.calls} 次請求）")

    print("⑥ 組合輸出")
    info = universe.set_index("stock_id")
    stocks = []
    for sid, g in by_price.items():
        if len(g) < 60 or sid not in info.index:
            continue
        row = info.loc[sid]
        k = [[int(d.replace("-", "")), round(float(o), 2), round(float(h), 2),
              round(float(l), 2), round(float(c), 2), int(v)]
             for d, o, h, l, c, v in zip(g["date"], g["open"], g["max"], g["min"], g["close"], g["vol"])]
        s = {"id": sid, "n": row["stock_name"], "m": "上市" if row["type"] == "twse" else "上櫃",
             "ind": row["industry_category"], "fut": sid in fut_ids, "k": k}
        ix = [key for key in ("tw50", "mid100", "ftse") if lists.get(key) and sid in lists[key]]
        if ix:
            s["ix"] = ix
        if lists.get("warrant"):
            s["wt"] = sid in lists["warrant"]

        gi = by_inst.get(sid)
        if gi is not None and not gi.empty:
            for col, key in (("f", "f"), ("t", "t"), ("d", "dl")):
                if col in gi:
                    s[key] = tail_list(gi[col])
        gm_ = by_margin.get(sid)
        if gm_ is not None and not gm_.empty:
            s["mg"] = tail_list(gm_["MarginPurchaseTodayBalance"])
            s["sh"] = tail_list(gm_["ShortSaleTodayBalance"])

        f = fund.get(sid, {})
        rev = month_revenue(f.get("TaiwanStockMonthRevenue"))
        if rev:
            s["rev"] = rev
        eps, gm = financials(f.get("TaiwanStockFinancialStatements"))
        if eps:
            s["eps"] = eps
            if sum(eps) > 0:
                s["pe"] = round(k[-1][4] / sum(eps), 1)
        if gm:
            s["gm"] = gm
        p = by_per.get(sid)
        if p is None:
            pdf = f.get("TaiwanStockPER")
            if pdf is not None and not pdf.empty:
                p = pdf.sort_values("date").iloc[-1]
        if p is not None:
            pb, dy = getattr(p, "PBR", None), getattr(p, "dividend_yield", None)
            if pb is not None and pd.notna(pb):
                s["pb"] = round(float(pb), 2)
            if dy is not None and pd.notna(dy):
                s["dy"] = round(float(dy), 2)
            if "pe" not in s:
                pe = getattr(p, "PER", None)
                if pe is not None and pd.notna(pe) and pe > 0:
                    s["pe"] = round(float(pe), 1)
        stocks.append(s)

    out = {"asof": asof, "source": "FinMind" + ("" if bulk else " + Yahoo Finance"),
           "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "lists": {k: bool(v) for k, v in lists.items()}, "stocks": stocks}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    (out_dir / "data.json").write_text(text, encoding="utf-8")
    (out_dir / "data.js").write_text("window.TAOJIN_DATA=" + text + ";", encoding="utf-8")
    mb = len(text.encode("utf-8")) / 1e6
    print(f"完成：{len(stocks)} 檔，資料日 {asof}，檔案 {mb:.1f} MB，FinMind 請求 {fm.calls} 次")
    print(f"輸出：{(out_dir / 'data.json').resolve()}")


if __name__ == "__main__":
    main()
