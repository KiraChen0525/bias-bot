"""離線測試：以模擬的 Yahoo / 證交所回應驗證計算、訊息與去重。
執行：python -m unittest discover -s tests -v
需要 FIXTURE_DIR 內有 <代號>.csv（欄位 date,close），預設讀取環境變數 FIXTURE_DIR。
"""
import contextlib
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
FIXTURES = Path(os.environ.get("FIXTURE_DIR", Path(__file__).parent / "fixtures"))
TZ = dt.timezone(dt.timedelta(hours=8))

tmp = tempfile.mkdtemp()
os.environ["BOT_DATA_DIR"] = tmp
os.environ["DRY_RUN"] = "1"
import bias_bot as bot  # noqa: E402


class Resp:
    def __init__(self, obj):
        self.obj = obj

    def json(self):
        return self.obj


def fixture_rows(code):
    p = FIXTURES / f"{code}.csv"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return [(r["date"], float(r["close"])) for r in csv.DictReader(f)]


def make_http(mis_date, mis_prices):
    def fake(url, params=None, session=None):
        if "mis.twse.com.tw/stock/index" in url:
            return Resp({})
        if "getStockInfo" in url:
            arr = []
            for ex in params["ex_ch"].split("|"):
                code = ex.split("_")[1].split(".")[0]
                if code in mis_prices:
                    px = mis_prices[code]
                    arr.append({"c": code, "z": f"{px:.4f}", "o": f"{px:.4f}", "y": "2585.0000", "d": mis_date, "t": "10:00:00"})
            return Resp({"msgArray": arr})
        code = url.split("/chart/")[1].split(".")[0]
        rows = fixture_rows(code)
        if rows is None:
            raise RuntimeError("HTTP 404")
        ts = [int(dt.datetime.strptime(d, "%Y-%m-%d").replace(hour=9, tzinfo=TZ).timestamp()) for d, _ in rows]
        cl = [c for _, c in rows]
        return Resp({"chart": {"result": [{"timestamp": ts, "meta": {}, "indicators": {"quote": [
            {"open": [c * 0.99 for c in cl], "high": cl, "low": cl, "close": cl, "volume": [1000] * len(cl)}]}}]}})
    return fake


def run(mode, now, mis_date, mis_prices, **env):
    buf = io.StringIO()
    with mock.patch.dict(os.environ, {"BOT_NOW": now, **env}), \
            mock.patch.object(bot, "http_get", make_http(mis_date, mis_prices)), \
            contextlib.redirect_stdout(buf):
        bot.main(["x", mode])
    return buf.getvalue()


class Calc(unittest.TestCase):
    def test_trigger_roundtrip(self):
        closes = [c for _, c in fixture_rows("2330")]
        for n in (20, 60):
            for k in (-2, 2):
                p = bot.solve_trigger(closes[-(n - 1):], k)
                z = bot.zscore(closes[-(n - 1):] + [p], n)["z"]
                self.assertAlmostEqual(z, k, places=6)

    def test_matches_earlier_numbers(self):
        closes = [c for _, c in fixture_rows("2330")]
        self.assertAlmostEqual(bot.solve_trigger(closes[-19:], -2), 2331.8, delta=0.1)
        self.assertAlmostEqual(bot.solve_trigger(closes[-59:], -2), 2259.1, delta=0.1)
        self.assertAlmostEqual(bot.solve_trigger(closes[-19:], 2), 2613.5, delta=0.1)

    def test_ticks(self):
        self.assertEqual(bot.floor_tick(2331.79), 2330)
        self.assertEqual(bot.ceil_tick(2613.5), 2615)
        self.assertEqual(bot.floor_tick(833.62), 833)
        self.assertEqual(bot.floor_tick(87.46), 87.4)
        self.assertEqual(bot.ceil_tick(48.01), 48.05)
        self.assertEqual(bot.floor_tick(1000), 1000)

    def test_z_matches_earlier(self):
        closes = [c for _, c in fixture_rows("2330")]
        self.assertAlmostEqual(bot.zscore(closes, 20)["z"], 1.89, places=2)
        self.assertAlmostEqual(bot.zscore(closes, 60)["z"], 2.40, places=2)
        self.assertIsNone(bot.zscore(closes, 240))  # 資料不足不硬算


class Flow(unittest.TestCase):
    def test_1_holiday_skips(self):
        out = run("close", "2026-10-10T14:40:00+08:00", "20261009", {"2330": 2585})
        self.assertIn("休市", out)
        self.assertFalse(bot.ZONES.exists())

    def test_2_close_builds_zones_and_message(self):
        out = run("close", "2026-10-07T14:40:00+08:00", "20261007", {"2330": 2585})
        z = json.loads(bot.ZONES.read_text(encoding="utf-8"))
        self.assertEqual(z["asof"], "2026-10-07")
        self.assertAlmostEqual(z["zones"]["2330"]["20"]["buy"], 2331.8, delta=0.1)
        self.assertNotIn("240", z["zones"]["2330"])  # 只有約 109 天，沒有長期
        self.assertTrue((bot.PRICES / "2330.csv").exists())
        for s in ("收盤整理", "台積電", "中期超漲", "≤2,330", "取得失敗", "信驊"):
            self.assertIn(s, out)

    def test_3_open_message(self):
        out = run("open", "2026-10-08T09:15:00+08:00", "20261008", {"2330": 2300, "2308": 1990})
        self.assertIn("開盤報價", out)
        self.assertIn("🟢 <b>2330 台積電</b>", out)  # 開盤價已在購買區間內，排前面並標示
        self.assertNotIn("🟢 <b>2308", out)

    def test_4_intraday_alert_and_dedup(self):
        out = run("intraday", "2026-10-08T10:00:00+08:00", "20261008", {"2330": 2300, "2308": 1990})
        self.assertIn("購買警示", out)
        self.assertIn("2330 台積電", out)
        self.assertNotIn("2308 台達電", out)
        again = run("intraday", "2026-10-08T10:10:00+08:00", "20261008", {"2330": 2300, "2308": 1990})
        self.assertIn("沒有新的警示", again)
        # 隔天重置
        nxt = run("intraday", "2026-10-09T10:00:00+08:00", "20261009", {"2330": 2300})
        self.assertIn("購買警示", nxt)

    def test_5_over_alert(self):
        os.remove(bot.STATE)
        out = run("intraday", "2026-10-08T11:00:00+08:00", "20261008", {"2383": 6400})
        self.assertIn("超漲警示", out)
        self.assertIn("2383 台光電", out)

    def test_6_outside_hours(self):
        out = run("intraday", "2026-10-08T15:30:00+08:00", "20261008", {"2330": 2300})
        self.assertIn("非盤中", out)

    def test_7_telegram_error_hides_token(self):
        import requests
        with mock.patch.dict(os.environ, {"DRY_RUN": "", "TELEGRAM_BOT_TOKEN": "SECRET123", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(bot.requests, "post",
                                  side_effect=requests.ConnectionError("https://api.telegram.org/botSECRET123/sendMessage")):
            with self.assertRaises(RuntimeError) as cm:
                bot.send_telegram("hi")
        self.assertNotIn("SECRET123", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
