"""信号检测单测：开仓/加仓/减仓/拆单/去重（离线，不碰网络）。"""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config import MonitorConfig
from src.monitor.signals import detect_signals
from src.store.db import Store

ADDR = "0x" + "ab" * 20
NOW = time.time() * 1000


def act(side, usdc, price, cid="0xc1", outcome="YES", dt_sec=0, title="Test Market", slug="test-market"):
    return {
        "type": "TRADE", "side": side, "size": usdc / price, "price": price,
        "usdcSize": usdc, "asset": "0xtok" + cid[3:15], "conditionId": cid,
        "outcome": outcome, "timestamp": NOW - dt_sec * 1000,
        "transactionHash": "0xtx" + side.lower() + str(int(usdc)),
        "title": title, "slug": slug,
    }


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.cfg = MonitorConfig(
            min_signal_usdc=200.0,
            sweep_window_sec=3600, sweep_min_trades=3, sweep_min_total_usdc=1000.0,
            dedup_window_sec=3600,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_open_then_add(self):
        sigs = detect_signals(ADDR, "tester", [act("BUY", 500, 0.5, cid="0xA")], self.store, self.cfg)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].type, "OPEN")
        # 同市场第二笔 → ADD
        sigs2 = detect_signals(ADDR, "tester",
                               [act("BUY", 500, 0.55, cid="0xA")], self.store, self.cfg,
                               known_condition_ids={"0xA"})
        self.assertEqual(sigs2[0].type, "ADD")

    def test_reduce(self):
        sigs = detect_signals(ADDR, "tester", [act("SELL", 800, 0.7, cid="0xB")], self.store, self.cfg)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].type, "REDUCE")

    def test_below_threshold_no_signal(self):
        sigs = detect_signals(ADDR, "tester", [act("BUY", 100, 0.5)], self.store, self.cfg)
        self.assertEqual(len(sigs), 0)

    def test_sweep_detection(self):
        # 8 笔 $150 小额（低于单笔阈值 $200）；第 7 笔时累积 $1050 ≥ $1000 即触发
        acts = [act("BUY", 150, 0.4, cid="0xC", dt_sec=400 * i) for i in range(8)]
        sigs = detect_signals(ADDR, "tester", acts, self.store, self.cfg)
        sweeps = [s for s in sigs if s.type == "SWEEP"]
        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0].trade_count, 7)
        self.assertAlmostEqual(sweeps[0].usdc, 1050.0)

    def test_sweep_not_triggered_below_total(self):
        acts = [act("BUY", 120, 0.4, cid="0xD", dt_sec=400 * i) for i in range(8)]  # 960 < 1000
        sigs = detect_signals(ADDR, "tester", acts, self.store, self.cfg)
        self.assertEqual([s for s in sigs if s.type == "SWEEP"], [])

    def test_dedup_same_hour(self):
        a1 = [act("BUY", 500, 0.5, cid="0xE", dt_sec=0)]
        a2 = [act("BUY", 600, 0.5, cid="0xE", dt_sec=30)]  # 同小时同市场同方向
        s1 = detect_signals(ADDR, "t", a1, self.store, self.cfg)
        self.store.save_signal(s1[0])
        s2 = detect_signals(ADDR, "t", a2, self.store, self.cfg)
        # 第二笔被同 hour bucket 去重
        self.assertEqual([s for s in s2 if s.conditionId == "0xE"], [])

    def test_mm_like_high_frequency_small(self):
        """小额高频（低于阈值）不产生任何信号——由 MM 检测在名单层处理。"""
        acts = [act("BUY" if i % 2 else "SELL", 50, 0.5, cid="0xF", dt_sec=60 * i) for i in range(20)]
        sigs = detect_signals(ADDR, "t", acts, self.store, self.cfg)
        self.assertEqual(sigs, [])


if __name__ == "__main__":
    unittest.main()
