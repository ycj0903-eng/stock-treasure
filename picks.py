#!/usr/bin/env python3
"""
股市尋寶｜每日策略選股結果
讀取 data.json，用和網頁完全相同的條件算出各策略組合的入選股票，輸出 picks.json。

用法：python picks.py site/data.json picks.json
"""
from __future__ import annotations

import json
import sys


def sma_last(a: list[float], n: int, i: int) -> float | None:
    if i < n - 1:
        return None
    return sum(a[i - n + 1:i + 1]) / n


def prep(s: dict) -> dict:
    k = s["k"]
    c = [r[4] for r in k]
    v = [r[5] for r in k]
    i = len(c) - 1
    lo = max(0, i - 19)
    val20 = sum(c[j] * v[j] for j in range(lo, i + 1)) * 1000 / (i - lo + 1) / 1e8
    prev = c[i - 1] if i > 0 else c[i]
    return {"s": s, "c": c, "v": v, "i": i, "close": c[i], "prev": prev, "val20": val20}


# ── 條件（與網頁 index.html 的定義一致）──
def ma_bull(S, P):
    c, i = S["c"], S["i"]
    m = {n: sma_last(c, n, i) for n in (5, 10, 20, 60)}
    return m[60] is not None and m[5] > m[10] > m[20] > m[60]


def new_high(S, P):
    c, i, n = S["c"], S["i"], P["a"]
    return i >= n - 1 and S["close"] >= max(c[i - n + 1:i + 1])


def vol_surge(S, P):
    v, i, n = S["v"], S["i"], P["a"]
    if i < n:
        return False
    avg = sum(v[i - j] for j in range(1, n + 1)) / n
    return v[i] >= avg * P["b"]


def val_min(S, P):
    return S["val20"] >= P["a"]


def it_buy(S, P):
    t = S["s"].get("t") or []
    n = 0
    for x in reversed(t):
        if x > 0:
            n += 1
        else:
            break
    return n >= P["a"]


def inst_sum(S, P):
    tot = 0
    for key in ("f", "t", "dl"):
        a = S["s"].get(key)
        if a:
            tot += sum(a[-5:])
    return bool(S["s"].get("f")) and tot >= P["a"]


def above_ma(S, P):
    m = sma_last(S["c"], P["a"], S["i"])
    return m is not None and S["close"] > m


PRESETS = {
    "強勢多頭": [(ma_bull, {}), (new_high, {"a": 60}), (vol_surge, {"a": 5, "b": 1.5}), (val_min, {"a": 1})],
    "法人認養": [(it_buy, {"a": 3}), (inst_sum, {"a": 500}), (above_ma, {"a": 20}), (val_min, {"a": 0.5})],
}

DESC = {
    "強勢多頭": "均線多頭排列（5>10>20>60）、收盤創 60 日新高、成交量 ≥ 前 5 日均量 1.5 倍、20 日均成交值 ≥ 1 億",
    "法人認養": "投信連續買超 3 日、三大法人近 5 日合計買超 ≥ 500 張、站上 20 日均線、20 日均成交值 ≥ 0.5 億",
}


def row(S) -> dict:
    s, c, v, i = S["s"], S["c"], S["v"], S["i"]
    avg5 = sum(v[i - 5:i]) / 5 if i >= 5 else None
    r20 = (c[i] / c[i - 20] - 1) * 100 if i >= 20 else None

    def s5(key):
        a = s.get(key)
        return int(sum(a[-5:])) if a else None

    return {
        "id": s["id"], "name": s["n"], "industry": s.get("ind", ""), "market": s.get("m", ""),
        "close": c[i], "chg_pct": round((c[i] / S["prev"] - 1) * 100, 2),
        "volume": v[i], "vol_ratio": round(v[i] / avg5, 2) if avg5 else None,
        "ret20_pct": round(r20, 2) if r20 is not None else None,
        "value20_e8": round(S["val20"], 2),
        "foreign5": s5("f"), "trust5": s5("t"),
        "fut": bool(s.get("fut")), "warrant": s.get("wt"),
        "index": s.get("ix", []),
    }


def main(src: str, dst: str) -> None:
    d = json.load(open(src, encoding="utf-8"))
    stocks = [prep(s) for s in d["stocks"] if s.get("k") and len(s["k"]) >= 30]
    out = {"asof": d["asof"], "generated": d.get("generated"), "universe": len(stocks), "strategies": {}}
    for name, conds in PRESETS.items():
        hits = []
        for S in stocks:
            try:
                if all(f(S, P) for f, P in conds):
                    hits.append(row(S))
            except (ZeroDivisionError, IndexError, TypeError):
                continue
        hits.sort(key=lambda r: -(r["vol_ratio"] or 0))
        out["strategies"][name] = {"rule": DESC[name], "count": len(hits), "stocks": hits}
        print(f"{name}：{len(hits)} 檔")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "site/data.json",
         sys.argv[2] if len(sys.argv) > 2 else "picks.json")
