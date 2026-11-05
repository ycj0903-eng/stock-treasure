#!/usr/bin/env python3
"""
股市尋寶｜把策略選股結果推播到 Telegram
讀 picks.json，整理成訊息後用 Telegram Bot 傳給你。

環境變數：
  TELEGRAM_BOT_TOKEN  BotFather 給的 token
  TELEGRAM_CHAT_ID    你的 chat id（一串數字）
用法：
  python notify_telegram.py picks.json 強勢多頭
  python notify_telegram.py picks.json 強勢多頭 法人認養   # 一次推多個策略
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

SITE = "https://ycj0903-eng.github.io/stock-treasure/"
ACTIONS = "https://github.com/ycj0903-eng/stock-treasure/actions"
TW = dt.timezone(dt.timedelta(hours=8))
INDEX_NAMES = {"tw50": "0050", "mid100": "中型100", "ftse": "富時"}


def last_weekday(today: dt.date) -> dt.date:
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def fmt_signed(x, unit=""):
    if x is None:
        return None
    return f"{'+' if x > 0 else ''}{x:,}{unit}"


def build(picks: dict, name: str, limit: int = 20) -> str:
    st = picks["strategies"].get(name)
    if st is None:
        return f"找不到策略「{name}」"
    lines = []
    asof = dt.date.fromisoformat(picks["asof"])
    expect = last_weekday(dt.datetime.now(TW).date())
    if asof < expect:  # 遇到國定假日也可能出現，屬正常
        lines.append(f"⚠️ 資料停在 {asof:%m/%d}，若昨天有開盤，代表昨晚的更新可能失敗：{ACTIONS}")
    lines.append(f"📈 {name}｜資料日 {asof:%Y-%m-%d}｜共 {st['count']} 檔")
    if st["count"] == 0:
        lines.append("今天沒有股票符合條件。")
    for k, s in enumerate(st["stocks"][:limit], 1):
        parts = [f"{k}. {s['id']} {s['name']}（{s['industry']}）{s['close']:g} {s['chg_pct']:+.2f}%"]
        if s.get("vol_ratio") is not None:
            parts.append(f"量比 {s['vol_ratio']:.1f} 倍")
        if s.get("ret20_pct") is not None:
            parts.append(f"20日 {s['ret20_pct']:+.1f}%")
        chip = []
        if s.get("foreign5") is not None:
            chip.append(f"外資5日 {fmt_signed(s['foreign5'], ' 張')}")
        if s.get("trust5") is not None:
            chip.append(f"投信5日 {fmt_signed(s['trust5'], ' 張')}")
        if chip:
            parts.append("、".join(chip))
        tags = []
        if s.get("fut"):
            tags.append("有股期")
        if s.get("warrant"):
            tags.append("有權證")
        tags += [INDEX_NAMES.get(i, i) for i in s.get("index", [])]
        if tags:
            parts.append("、".join(tags))
        lines.append("｜".join(parts))
    if st["count"] > limit:
        lines.append(f"…另有 {st['count'] - limit} 檔，請到網站查看")
    lines.append(f"條件：{st['rule']}")
    lines.append(f"K 線圖：{SITE}")
    return "\n".join(lines)


def send(token: str, chat_id: str, text: str) -> None:
    for i in range(0, len(text), 3900):  # Telegram 單則上限 4096 字
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[i:i + 3900],
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
        if not res.get("ok"):
            raise SystemExit(f"Telegram 傳送失敗：{res}")


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "picks.json"
    names = sys.argv[2:] or ["強勢多頭"]
    picks = json.load(open(src, encoding="utf-8"))
    text = "\n\n".join(build(picks, n) for n in names)
    print(text)
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        raise SystemExit("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，請到 GitHub 的 Secrets 設定。")
    send(token, chat, text)
    print("已傳送到 Telegram")


if __name__ == "__main__":
    main()
