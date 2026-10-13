#!/usr/bin/env python3
"""AI 伺服器供應鏈 乖離 ±σ 警示機器人（Telegram 推播）

模式：
  close     收盤後：更新每日開高低收量、重算「隔日購買/超漲區間價」、推播收盤整理
  open      開盤後：推播開盤價與距離購買區間
  intraday  盤中：現價跌入購買區間或衝入超漲區間就推播（同一檔同一天只推一次）
  test      送一則測試訊息

乖離定義：z_N = (收盤 - MA_N) / SD_N，SD_N 為同一 N 日收盤價的母體標準差。
區間價：下一個收盤價 P 使 z = ±門檻（視窗為最近 N-1 個已知收盤價加上 P）。
"""
import csv
import datetime as dt
import html
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BOT_DATA_DIR", ROOT / "data"))
PRICES = DATA / "prices"
ZONES = DATA / "zones.json"
STATE = DATA / "alert_state.json"
CONFIG = ROOT / "stocks.json"

TZ = dt.timezone(dt.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (bias-bot)"}
PERIOD_NAME = {20: "短期", 60: "中期", 240: "長期"}


# ---------- 基本工具 ----------
def now_tw():
    override = os.environ.get("BOT_NOW")
    return dt.datetime.fromisoformat(override) if override else dt.datetime.now(TZ)


def http_get(url, params=None, session=None):
    r = (session or requests).get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r


def fnum(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def fmt_price(x):
    if x is None:
        return "-"
    s = f"{x:,.2f}".rstrip("0").rstrip(".")
    return s


def tick_size(p):
    """台股升降單位（股票）。"""
    for limit, t in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1)):
        if p < limit:
            return t
    return 5


def floor_tick(p):  # 購買價「≤」：往下取到可成交的檔位
    t = tick_size(p)
    return round(math.floor(p / t + 1e-9) * t, 2)


def ceil_tick(p):  # 超漲價「≥」：往上取到可成交的檔位
    t = tick_size(p)
    return round(math.ceil(p / t - 1e-9) * t, 2)


def fmt_pct(x):
    return "-" if x is None else f"{x:+.1f}%"


def load_config():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg.setdefault("threshold", 2.0)
    cfg.setdefault("periods", [20, 60, 240])
    cfg.setdefault("near_pct", 5.0)
    return cfg


# ---------- 計算 ----------
def zscore(closes, n):
    if len(closes) < n:
        return None
    w = closes[-n:]
    ma = sum(w) / n
    sd = math.sqrt(sum((x - ma) ** 2 for x in w) / n)
    if sd == 0:
        return None
    return {"z": (closes[-1] - ma) / sd, "bias": (closes[-1] / ma - 1) * 100, "ma": ma, "sd": sd}


def solve_trigger(prev, k):
    """求下一個收盤價 P，使視窗 prev+[P] 的 z = k（k>0 超漲價、k<0 購買價）。"""
    n = len(prev) + 1
    s = sum(prev)
    q = sum(x * x for x in prev)
    a0, b0 = (n - 1) / n, -s / n  # P - mean = a0*P + b0
    va, vb, vc = 1 / n - 1 / n**2, -2 * s / n**2, q / n - s * s / n**2  # var = va*P^2 + vb*P + vc
    A = a0 * a0 - k * k * va
    B = 2 * a0 * b0 - k * k * vb
    C = b0 * b0 - k * k * vc
    if abs(A) < 1e-12:
        return None
    D = B * B - 4 * A * C
    if D < 0:
        return None
    roots = [(-B + sg * math.sqrt(D)) / (2 * A) for sg in (1, -1)]
    roots = [p for p in roots if (a0 * p + b0) * k > 0]
    return roots[0] if roots else None


def build_zones(closes, th, periods):
    out = {}
    for n in periods:
        if len(closes) < n:
            continue
        cur = zscore(closes, n)
        out[str(n)] = {
            "buy": solve_trigger(closes[-(n - 1):], -th),
            "over": solve_trigger(closes[-(n - 1):], th),
            "z": cur["z"] if cur else None,
            "bias": cur["bias"] if cur else None,
        }
    return out


# ---------- 歷史資料 ----------
def fetch_history(stock):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}.{stock['market']}"
    res = http_get(url, params={"range": "2y", "interval": "1d"}).json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    rows = {}
    for i, t in enumerate(ts):
        c = fnum(q["close"][i])
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")
        rows[d] = (d, fnum(q["open"][i]), fnum(q["high"][i]), fnum(q["low"][i]), c, fnum(q["volume"][i]))
    return [rows[d] for d in sorted(rows)]


def save_history(code, rows):
    PRICES.mkdir(parents=True, exist_ok=True)
    with open(PRICES / f"{code}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def load_history(code):
    p = PRICES / f"{code}.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [(r["date"], fnum(r["open"]), fnum(r["high"]), fnum(r["low"]), fnum(r["close"]), fnum(r["volume"]))
                for r in csv.DictReader(f)]


# ---------- 即時報價 ----------
def fetch_quotes_mis(stocks):
    sess = requests.Session()
    http_get("https://mis.twse.com.tw/stock/index.jsp", session=sess)
    ex = "|".join(f"{'tse' if s['market'] == 'TW' else 'otc'}_{s['code']}.tw" for s in stocks)
    data = http_get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                    params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                    session=sess).json()
    out = {}
    for m in data.get("msgArray", []):
        price = fnum(m.get("z"))
        if price is None:  # 尚無成交：不當作價格
            continue
        out[m.get("c")] = {"price": price, "open": fnum(m.get("o")), "prev": fnum(m.get("y")),
                           "date": m.get("d"), "time": m.get("t"), "src": "TWSE"}
    return out


def fetch_quote_yahoo(stock):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{stock['code']}.{stock['market']}"
    res = http_get(url, params={"range": "1d", "interval": "5m"}).json()["chart"]["result"][0]
    meta = res["meta"]
    opens = [x for x in res["indicators"]["quote"][0].get("open", []) if x is not None]
    return {"price": fnum(meta.get("regularMarketPrice")), "open": fnum(opens[0]) if opens else None,
            "prev": fnum(meta.get("chartPreviousClose")),
            "date": dt.datetime.fromtimestamp(meta["regularMarketTime"], TZ).strftime("%Y%m%d"),
            "time": "", "src": "Yahoo"}


def get_quotes(stocks):
    quotes = {}
    try:
        quotes = fetch_quotes_mis(stocks)
    except Exception as e:  # 證交所介面失敗就逐檔改用 Yahoo
        print(f"證交所即時介面失敗，改用 Yahoo：{type(e).__name__}")
    for s in stocks:
        if s["code"] in quotes:
            continue
        try:
            q = fetch_quote_yahoo(s)
            if q["price"] is not None:
                quotes[s["code"]] = q
        except Exception as e:
            print(f"{s['code']} 報價取得失敗：{type(e).__name__}")
    return quotes


# ---------- Telegram ----------
def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("DRY_RUN") or not token or not chat:
        print("---- (未發送：DRY_RUN 或未設定 Telegram) ----\n" + text + "\n----")
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
        except requests.RequestException:
            raise RuntimeError("Telegram 連線失敗") from None  # 不輸出含 token 的網址
        if not r.ok:
            raise RuntimeError(f"Telegram 回應 {r.status_code}：{r.text[:200]}")


# ---------- 模式 ----------
def label(s):
    return f"{s['code']} {html.escape(s['name'])}"


def mode_close(cfg, stocks):
    today = now_tw().strftime("%Y-%m-%d")
    quotes = {}
    try:
        quotes = fetch_quotes_mis(stocks)
    except Exception:
        pass
    traded_today = any(q.get("date") == today.replace("-", "") for q in quotes.values()) if quotes else None
    if traded_today is False and not os.environ.get("FORCE"):
        print("今日無成交資料（休市），不更新。")
        return

    th, periods = cfg["threshold"], cfg["periods"]
    zones, rows_by, fails = {}, {}, []
    for s in stocks:
        try:
            rows = fetch_history(s)
        except Exception as e:
            fails.append(f"{s['code']} {s['name']}（{type(e).__name__}）")
            continue
        # 收盤前執行時，最後一根可能是未完成的當日 K 棒，丟掉
        if rows and rows[-1][0] == today and now_tw().time() < dt.time(13, 40):
            rows = rows[:-1]
        if not rows:
            fails.append(f"{s['code']} {s['name']}（無資料）")
            continue
        save_history(s["code"], rows)
        rows_by[s["code"]] = rows
        zones[s["code"]] = build_zones([r[4] for r in rows], th, periods)

    if not rows_by:
        raise RuntimeError("所有標的歷史資料都取得失敗")
    asof = max(r[-1][0] for r in rows_by.values())
    ZONES.parent.mkdir(parents=True, exist_ok=True)
    ZONES.write_text(json.dumps({"asof": asof, "threshold": th, "zones": zones}, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    lines = [f"<b>📊 收盤整理 {asof}</b>（門檻 ±{th:g}σ）"]
    if asof != today:
        lines.append(f"⚠️ 資料只到 {asof}，今日 K 線可能尚未更新；稍後可手動執行 close 重算。")

    items = []
    for s in stocks:
        rows = rows_by.get(s["code"])
        if not rows:
            continue
        last = rows[-1]
        prev_close = rows[-2][4] if len(rows) > 1 else None
        chg = (last[4] / prev_close - 1) * 100 if prev_close else None
        z = zones[s["code"]]
        dists = [(z[str(n)]["buy"] / last[4] - 1) * 100 for n in periods
                 if str(n) in z and z[str(n)]["buy"]]
        items.append((max(dists) if dists else -999, s, last, chg, z))

    flags = []
    for _, s, last, chg, z in items:
        for n in periods:
            zz = (z.get(str(n)) or {}).get("z")
            if zz is not None and zz > th:
                flags.append(f"🔴 {label(s)} {PERIOD_NAME.get(n, n)}超漲 z={zz:+.2f}")
            elif zz is not None and zz < -th:
                flags.append(f"🟢 {label(s)} {PERIOD_NAME.get(n, n)}超跌 z={zz:+.2f}")
    if flags:
        lines += ["", "<b>今日觸發</b>"] + flags
    near = [(d, s, last, z) for d, s, last, chg, z in items if d >= -cfg["near_pct"]]
    if near:
        lines += ["", f"<b>隔日購買重點</b>（距購買價 {cfg['near_pct']:g}% 內）"]
        for d, s, last, z in near:
            lines.append(f"🟢 {label(s)} 收 {fmt_price(last[4])}，最近購買價距 {d:+.1f}%")

    lines += ["", "<b>各檔開/收與隔日購買區間</b>（由近到遠）"]
    for _, s, last, chg, z in sorted(items, key=lambda x: -x[0]):
        parts = []
        for n in periods:
            zn = z.get(str(n))
            if zn and zn["buy"]:
                parts.append(f"{PERIOD_NAME.get(n, n)}≤{fmt_price(floor_tick(zn['buy']))}({(zn['buy'] / last[4] - 1) * 100:+.1f}%)")
        zs = " ".join(f"z{n}{z[str(n)]['z']:+.1f}" for n in periods[:2] if str(n) in z and z[str(n)]["z"] is not None)
        lines.append(f"<b>{label(s)}</b> 開{fmt_price(last[1])} 收{fmt_price(last[4])}（{fmt_pct(chg)}）\n  {' '.join(parts) or '資料不足'}  {zs}")
    if fails:
        lines += ["", "取得失敗：" + "、".join(fails)]
    lines += ["", "技術面統計，非投資建議。"]
    send_telegram("\n".join(lines))


def load_zones():
    if not ZONES.exists():
        return None
    return json.loads(ZONES.read_text(encoding="utf-8"))


def mode_open(cfg, stocks):
    z = load_zones()
    quotes = get_quotes(stocks)
    today = now_tw().strftime("%Y%m%d")
    if not quotes or not any(q["date"] == today for q in quotes.values()):
        print("今日無即時成交資料（休市或尚未開盤）。")
        return
    if not z:
        send_telegram("⚠️ 尚未建立購買區間，請先在 GitHub Actions 手動執行 mode=close。")
        return
    lines = [f"<b>🔔 開盤報價 {now_tw():%Y-%m-%d}</b>（區間基準日 {z['asof']}）"]
    rows = []
    for s in stocks:
        q = quotes.get(s["code"])
        zn = z["zones"].get(s["code"], {})
        if not q or q["date"] != today:
            continue
        px = q["open"] or q["price"]
        gap = (px / q["prev"] - 1) * 100 if q.get("prev") else None
        parts, hit = [], False
        for n in cfg["periods"]:
            b = (zn.get(str(n)) or {}).get("buy")
            if b:
                hit = hit or px <= b
                parts.append(f"{PERIOD_NAME.get(n, n)}≤{fmt_price(floor_tick(b))}({(b / px - 1) * 100:+.1f}%)")
        rows.append((hit, f"{'🟢 ' if hit else ''}<b>{label(s)}</b> 開{fmt_price(px)}（{fmt_pct(gap)}） {' '.join(parts)}"))
    rows.sort(key=lambda r: not r[0])
    lines += [r[1] for r in rows]
    send_telegram("\n".join(lines))


def mode_intraday(cfg, stocks):
    t = now_tw()
    if not os.environ.get("FORCE") and (t.weekday() >= 5 or not dt.time(9, 0) <= t.time() <= dt.time(13, 35)):
        print("非盤中時段，略過。")
        return
    z = load_zones()
    if not z:
        print("尚未建立購買區間（先執行 close）。")
        return
    quotes = get_quotes(stocks)
    today = t.strftime("%Y%m%d")
    state = {"date": today, "sent": []}
    if STATE.exists():
        old = json.loads(STATE.read_text(encoding="utf-8"))
        if old.get("date") == today:
            state = old
    new = []
    for s in stocks:
        q = quotes.get(s["code"])
        if not q or q["date"] != today:
            continue
        for n in cfg["periods"]:
            zn = z["zones"].get(s["code"], {}).get(str(n))
            if not zn:
                continue
            for kind, thr, ok in (("buy", zn["buy"], lambda p, v: p <= v), ("over", zn["over"], lambda p, v: p >= v)):
                key = f"{s['code']}:{kind}:{n}"
                if thr is not None and ok(q["price"], thr) and key not in state["sent"]:
                    state["sent"].append(key)
                    icon, word, sym = ("🟢", "購買警示", "≤") if kind == "buy" else ("🔴", "超漲警示", "≥")
                    new.append(f"{icon} <b>{word}</b> {label(s)}\n  現價 {fmt_price(q['price'])}，{PERIOD_NAME.get(n, n)}區間 {sym}{fmt_price(floor_tick(thr) if kind == 'buy' else ceil_tick(thr))}"
                               f"（z{'<' if kind == 'buy' else '>'}{'-' if kind == 'buy' else '+'}{cfg['threshold']:g}，{q['src']} {q.get('time', '')}）")
    if new:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")  # 先記錄再發送，避免重複轟炸
        send_telegram(f"<b>⏰ 盤中警示 {t:%H:%M}</b>（區間基準日 {z['asof']}）\n" + "\n".join(new))
    else:
        print("沒有新的警示。")


def mode_test(cfg, stocks):
    send_telegram(f"✅ 乖離警示機器人測試訊息 {now_tw():%Y-%m-%d %H:%M}\n追蹤 {len(stocks)} 檔，門檻 ±{cfg['threshold']:g}σ。")


MODES = {"close": mode_close, "open": mode_open, "intraday": mode_intraday, "test": mode_test}


def main(argv):
    if len(argv) < 2 or argv[1] not in MODES:
        print("用法：python bias_bot.py [close|open|intraday|test]")
        return 2
    cfg = load_config()
    MODES[argv[1]](cfg, cfg["stocks"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
