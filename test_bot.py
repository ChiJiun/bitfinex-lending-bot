"""bot.py 純函式的單元測試:定價、拆單、天期上限。執行:py -m pytest -q"""
import bot

CFG = {
    "min_offer_size": 150, "max_offer_size": 5000, "num_ladder_steps": 4,
    "min_daily_rate_pct": 0.01, "start_percentile": 90, "floor_percentile": 40,
    "decay_minutes": 240, "max_period_days": 7, "reprice_tolerance_pct": 2,
}


def test_period_never_exceeds_one_week():
    assert bot.clamp_period(120) == 7
    assert bot.clamp_period(30) == 7
    assert bot.clamp_period(7) == 7
    assert bot.clamp_period(1) == 2
    assert bot.clamp_period("abc") == 2
    cfg = dict(CFG, max_period_days=120)  # 設定檔亂設也壓回 7
    assert bot.pick_period(0.05, market=120, spike_rate_pct=0.03, cfg=cfg) == 7
    assert bot.pick_period(0.05, market=2, spike_rate_pct=0.03, cfg=cfg) == 7


def test_pick_period_spike_lock_only_when_rate_hits_threshold():
    assert bot.pick_period(0.02, market=2, spike_rate_pct=0.03, cfg=CFG) == 2
    assert bot.pick_period(0.03, market=2, spike_rate_pct=0.03, cfg=CFG) == 7
    assert bot.pick_period(0.02, market=7, spike_rate_pct=0.03, cfg=CFG) == 7
    assert bot.pick_period(0.09, market=2, spike_rate_pct=0.0, cfg=CFG) == 2  # 無門檻就不鎖
    assert bot.pick_period(0.09, market=2, spike_rate_pct=0.03, cfg=dict(CFG, max_period_days=5)) == 5


def test_percentile_and_grouping():
    trades = [[1, 0, 100, 0.0002, 2], [2, 0, 100, 0.0001, 2], [3, 0, 100, 0.0003, 7],
              [4, 0, 100, 0.0004, 120], [5, 0, 100, None, 2]]
    bands = bot.group_rates(trades, (2, 7))
    assert bands[2] == [0.01, 0.02] and bands[7] == [0.03]
    assert 120 not in bands
    assert bot.percentile([], 50) == 0.0
    r = [0.01, 0.02, 0.03, 0.04, 0.05]
    assert bot.percentile(r, 0) == 0.01 and bot.percentile(r, 100) == 0.05 and bot.percentile(r, 40) == 0.03


def test_decay_from_start_to_floor():
    assert bot.decayed_percentile(90, 40, 240, 0) == 90
    assert bot.decayed_percentile(90, 40, 240, 120) == 65
    assert bot.decayed_percentile(90, 40, 240, 1000) == 40
    assert bot.decayed_percentile(90, 40, 0, 1000) == 90  # 不遞減


def test_target_rate_respects_floor_and_rungs():
    rates = sorted(0.01 + i * 0.001 for i in range(100))  # 0.010 .. 0.109
    assert abs(bot.target_rate(rates, CFG, 0) - 0.10) < 1e-9        # 第 90 分位
    assert abs(bot.target_rate(rates, CFG, 240) - 0.05) < 1e-9      # 衰減到第 40 分位
    assert bot.target_rate([0.001], CFG, 0) == 0.01                 # 地板
    cfg = dict(CFG, rung_spread_percentile=5)
    assert bot.target_rate(rates, cfg, 0, rung=1) > bot.target_rate(rates, cfg, 0, rung=0)


def test_split_amount_never_exceeds_available_and_no_dust():
    assert bot.split_amount(100, CFG) == []
    assert bot.split_amount(160, CFG) == [160.0]
    assert bot.split_amount(437.6, CFG) == [218.8, 218.8]
    parts = bot.split_amount(623.0, CFG)
    assert len(parts) == 4 and all(p >= 150 for p in parts) and sum(parts) <= 623.0
    parts = bot.split_amount(437.5999999, CFG)                      # 浮點尾差
    assert sum(parts) <= 437.5999999
    parts = bot.split_amount(25000, CFG)
    assert len(parts) == 5 and max(parts) <= 5000
    parts = bot.split_amount(120000, CFG)
    assert len(parts) == 24


def test_plan_picks_better_market_and_falls_back():
    bands = {2: [0.01, 0.012, 0.014, 0.016, 0.018], 7: [0.02, 0.022, 0.024, 0.026, 0.028]}
    plan = bot.plan_new_offers(623, bands, spike_rate_pct=0.05, cfg=CFG)
    assert len(plan) == 4 and all(market == 7 and period == 7 for _, _, period, market in plan)
    assert all(rate >= 0.028 - 1e-9 for _, rate, _, _ in plan)
    plan = bot.plan_new_offers(623, {2: bands[2], 7: []}, spike_rate_pct=0.05, cfg=CFG)
    assert all(market == 2 and period == 2 for _, _, period, market in plan)
    plan = bot.plan_new_offers(623, {2: bands[2], 7: []}, spike_rate_pct=0.015, cfg=CFG)
    assert all(period == 7 for _, _, period, _ in plan)              # 尖峰鎖倉(仍不超過一週)
    assert bot.plan_new_offers(623, {2: [], 7: []}, 0, CFG) == []
    assert bot.plan_new_offers(100, bands, 0, CFG) == []
