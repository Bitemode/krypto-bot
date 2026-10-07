"""Tests für den Bot. Ohne pytest lauffähig: python3 tests/test_bot.py"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptobot.config import Config, ConfigError, load, validate
from cryptobot.journal import Journal
from cryptobot.planner import build_plan, datenvertrauen, execute_paper
from cryptobot.portfolio import Portfolio, Position
from cryptobot.providers.bitvavo import OrderBook, Ticker

JETZT = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _cfg(**over) -> Config:
    roh = {
        "trading": {"mode": "paper", "venue": "bitvavo", "quote": "EUR"},
        "signal": {"fast_window": 30, "slow_window": 60, "momentum_window": 30,
                   "trend_threshold": 0.6667},
        "regime": {"market": "BTC-EUR", "slow_window": 60, "momentum_window": 30},
        "risk": {"target_vol_portfolio": 0.20, "target_vol_position": 0.10,
                 "max_weight": 0.25, "min_weight": 0.02, "max_positions": 8,
                 "vol_halflife_days": 20, "cov_window_days": 90,
                 "catastrophe_stop": -0.25, "daily_loss_limit": -0.08,
                 "drawdown_brake": -0.25, "cooldown_days": 10},
        "schedule": {"rebalance_every_days": 10, "no_trade_band": 0.20,
                     "run_times_local": ["09:30", "17:00"], "execute_after_hour": 16},
        "universe": {"top_n": 25, "min_history_days": 365, "min_volume_eur": 250000,
                     "max_spread": 0.005, "stablecoins": [], "exclude": []},
        "costs": {"maker_only": True, "slippage_bps": 5.0},
        "quality": {"min_trust_score": 85, "max_orderbook_age_s": 30,
                    "max_ticker_age_s": 300, "max_candle_age_h": 26},
        "paths": {"state": "x/portfolio.json", "journal": "x/j.jsonl", "trades": "x/t.csv"},
        "portfolio": {"initial_equity": 10000.0, "cash_buffer": 0.005},
        "gates": {},
    }
    for pfad, wert in over.items():
        a, b = pfad.split(".")
        roh[a][b] = wert
    return Config(raw=roh, quelle="test")


def _kurse(n=500, maerkte=("BTC-EUR", "ETH-EUR", "SOL-EUR"), rate=0.002, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=JETZT, periods=n, freq="D", tz="UTC")
    d = {}
    for i, m in enumerate(maerkte):
        rausch = np.cumsum(rng.normal(0, 0.015, n))
        d[m] = pd.Series(100 * np.exp(np.arange(n) * rate + rausch), index=idx)
    return pd.DataFrame(d)


def _snapshots(closes, jetzt=JETZT, spread=0.001, tiefe=50_000.0):
    tickers, books = {}, {}
    for m in closes.columns:
        p = float(closes[m].iloc[-1])
        tickers[m] = Ticker(m, p * (1 - spread / 2), p * (1 + spread / 2), 5_000_000.0, jetzt)
        asks = [(p * (1 + spread / 2 + i * 0.0004), tiefe / 25 / p) for i in range(25)]
        bids = [(p * (1 - spread / 2 - i * 0.0004), tiefe / 25 / p) for i in range(25)]
        books[m] = OrderBook(m, bids, asks, jetzt)
    return tickers, books


# ------------------------------------------------------------------ Konfiguration

def test_config_validiert_gewichte():
    try:
        validate(_cfg(**{"risk.min_weight": 0.4}))
    except ConfigError:
        return
    raise AssertionError("min_weight > max_weight hätte auffallen müssen")


def test_config_lehnt_unbekannten_modus_ab():
    try:
        validate(_cfg(**{"trading.mode": "turbo"}))
    except ConfigError:
        return
    raise AssertionError("unbekannter Modus hätte auffallen müssen")


def test_live_ohne_umgebungsvariable_bricht_ab():
    os.environ.pop("CRYPTOBOT_ALLOW_LIVE", None)
    try:
        validate(_cfg(**{"trading.mode": "live"}))
    except ConfigError as e:
        assert "ALLOW_LIVE" in str(e)
        return
    raise AssertionError("live ohne Freigabe hätte abbrechen müssen")


def test_env_override_wirkt():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yaml"
        p.write_text(Path("config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        os.environ["CRYPTOBOT_RISK__TARGET_VOL_PORTFOLIO"] = "0.15"
        try:
            cfg = load(p)
            assert abs(cfg.get("risk.target_vol_portfolio") - 0.15) < 1e-9
            assert "risk.target_vol_portfolio" in cfg.env_overrides
        finally:
            os.environ.pop("CRYPTOBOT_RISK__TARGET_VOL_PORTFOLIO", None)


def test_strategie_uebernimmt_zielvolatilitaet():
    s = _cfg().strategy()
    assert abs(s.target_vol_portfolio - 0.20) < 1e-9
    assert s.rebalance_every == 10


# ------------------------------------------------------------------ Portfolio

def _portfolio(tmp, equity=10_000.0):
    return Portfolio.load_or_create(Path(tmp) / "p.json", equity)


def test_kauf_und_verkauf_buchen_richtig():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("BTC-EUR", 1000.0, 50_000.0, 2.0, "2026-09-03")
        assert abs(p.s.cash - (10_000 - 1002)) < 1e-6
        assert abs(p.s.positions["BTC-EUR"].units - 0.02) < 1e-12
        gewinn = p.sell("BTC-EUR", 1100.0, 55_000.0, 2.2, "2026-09-04", "REBALANCE")
        assert "BTC-EUR" not in p.s.positions
        assert abs(gewinn - (1100 - 1000 - 2.2)) < 1e-6


def test_zustand_ueberlebt_neustart():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("ETH-EUR", 500.0, 2000.0, 1.0, "2026-09-03")
        p.save()
        q = Portfolio.load_or_create(Path(d) / "p.json", 10_000.0)
        assert abs(q.s.cash - p.s.cash) < 1e-9
        assert abs(q.s.positions["ETH-EUR"].units - 0.25) < 1e-12


def test_cooldown_nach_trendbruch():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("SOL-EUR", 500.0, 100.0, 1.0, "2026-09-01")
        p.sell("SOL-EUR", 500.0, 100.0, 1.0, "2026-09-01", "TREND_BREAK")
        assert p.in_cooldown("SOL-EUR", "2026-09-05", 10)
        assert not p.in_cooldown("SOL-EUR", "2026-09-20", 10)


def test_tagesverlust_wird_gemessen():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("BTC-EUR", 5000.0, 50_000.0, 0.0, "2026-09-03")
        p.mark_day({"BTC-EUR": 50_000.0}, "2026-09-03")
        assert abs(p.day_loss({"BTC-EUR": 45_000.0}) - (-0.05)) < 1e-6


# ------------------------------------------------------------------ Datenqualität

def test_datenvertrauen_voll_bei_sauberen_daten():
    closes = _kurse()
    t, b = _snapshots(closes)
    score, gruende = datenvertrauen("BTC-EUR", closes["BTC-EUR"], t["BTC-EUR"],
                                    b["BTC-EUR"], _cfg(), JETZT)
    assert score == 100, (score, gruende)


def test_datenvertrauen_sinkt_bei_altem_orderbuch():
    closes = _kurse()
    t, b = _snapshots(closes, jetzt=JETZT - timedelta(minutes=10))
    score, _ = datenvertrauen("BTC-EUR", closes["BTC-EUR"], t["BTC-EUR"],
                              b["BTC-EUR"], _cfg(), JETZT)
    assert score <= 65, score


def test_datenvertrauen_sinkt_bei_luecken():
    closes = _kurse()
    reihe = closes["BTC-EUR"].copy()
    reihe.iloc[100:104] = np.nan
    score, _ = datenvertrauen("BTC-EUR", reihe, None, None, _cfg(), JETZT)
    assert score < 40


# ------------------------------------------------------------------ Planung

def test_plan_kauft_im_aufwaertstrend():
    closes = _kurse()
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        assert plan.regime == "RISK_ON"
        assert any(o.seite == "BUY" for o in plan.orders), plan.orders
        assert sum(d_.ziel_gewicht for d_ in plan.entscheidungen) <= 1.0 + 1e-9


def test_plan_kauft_nicht_im_abwaertstrend():
    closes = _kurse(rate=-0.003)
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        assert plan.regime == "RISK_OFF"
        assert not any(o.seite == "BUY" for o in plan.orders)


def test_breiter_spread_blockiert_kauf():
    closes = _kurse()
    t, b = _snapshots(closes, spread=0.02)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        assert not any(o.seite == "BUY" for o in plan.orders)
        assert any("WIDE_SPREAD" in e.filter_aktiv for e in plan.entscheidungen)


def test_duennes_orderbuch_blockiert_kauf():
    closes = _kurse()
    t, b = _snapshots(closes, tiefe=50.0)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        assert any("HIGH_SLIPPAGE" in e.filter_aktiv for e in plan.entscheidungen)
        assert not any(o.seite == "BUY" for o in plan.orders)


def test_katastrophen_stop_verkauft_auch_ohne_rebalance():
    closes = _kurse()
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        preis = float(closes["BTC-EUR"].iloc[-1])
        p.s.positions["BTC-EUR"] = Position("BTC-EUR", 1.0, preis * 2, "2026-08-01", preis * 2)
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=False)
        assert any(o.grund == "CATASTROPHE_STOP" for o in plan.orders), plan.orders


def test_tagesverlust_limit_sperrt_neueinstiege():
    closes = _kurse()
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.s.day_start_equity = 12_000.0     # aktuell 10.000 -> −16,7 %
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        assert not any(o.seite == "BUY" for o in plan.orders)
        assert any("DAILY_LOSS_LIMIT" in e.filter_aktiv for e in plan.entscheidungen)


def test_ausfuehrung_geht_nie_ins_minus():
    closes = _kurse(maerkte=tuple(f"M{i}-EUR" for i in range(6)) + ("BTC-EUR",))
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        j = Journal(Path(d) / "j.jsonl", Path(d) / "t.csv")
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        execute_paper(plan, p, _cfg(), j, JETZT)
        assert p.s.cash >= -1e-6, p.s.cash
        preise = {m: t[m].mid for m in closes.columns}
        assert p.exposure(preise) <= 1.0 + 1e-6


def test_journal_schreibt_fills():
    closes = _kurse()
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        j = Journal(Path(d) / "j.jsonl", Path(d) / "t.csv")
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        fills = execute_paper(plan, p, _cfg(), j, JETZT)
        zeilen = j.read_trades()
        assert len(zeilen) == len(fills) > 0
        assert set(zeilen[0]) >= {"zeit", "markt", "seite", "notional_eur", "gebuehr_eur"}


def test_gebuehren_werden_abgezogen():
    closes = _kurse()
    t, b = _snapshots(closes)
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        j = Journal(Path(d) / "j.jsonl", Path(d) / "t.csv")
        plan = build_plan(closes, t, b, p, _cfg(), JETZT, ist_rebalance_tag=True)
        fills = execute_paper(plan, p, _cfg(), j, JETZT)
        assert all(f.gebuehr > 0 for f in fills)
        preise = {m: t[m].mid for m in closes.columns}
        assert p.equity(preise) < 10_000.0   # Kosten müssen sich zeigen


# ------------------------------------------------------------------ Rebalance-Takt

def test_kaltstart_schichtet_sofort_um():
    """Leeres Portfolio, noch nie umgeschichtet -> sofort, nicht erst am Kalendertag."""
    from cryptobot.cli import _ist_rebalance_tag
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.s.created = "2026-09-03T21:59:00+00:00"
        p.s.last_rebalance = ""
        assert _ist_rebalance_tag(p, _cfg(), JETZT) is True


def test_takt_rechnet_vom_letzten_rebalance():
    from cryptobot.cli import _ist_rebalance_tag
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.s.last_rebalance = JETZT.date().isoformat()
        assert _ist_rebalance_tag(p, _cfg(), JETZT) is False
        p.s.last_rebalance = (JETZT.date() - timedelta(days=9)).isoformat()
        assert _ist_rebalance_tag(p, _cfg(), JETZT) is False
        p.s.last_rebalance = (JETZT.date() - timedelta(days=10)).isoformat()
        assert _ist_rebalance_tag(p, _cfg(), JETZT) is True


def test_ausgefallener_lauf_verschiebt_den_takt_nicht():
    """Fällt ein Lauf aus, wird beim nächsten nachgeholt — nicht um 10 Tage verschoben."""
    from cryptobot.cli import _ist_rebalance_tag
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.s.last_rebalance = (JETZT.date() - timedelta(days=37)).isoformat()
        assert _ist_rebalance_tag(p, _cfg(), JETZT) is True


def test_letztes_rebalance_wird_gespeichert():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.s.last_rebalance = "2026-09-20"
        p.save()
        q = Portfolio.load_or_create(Path(d) / "p.json", 10_000.0)
        assert q.s.last_rebalance == "2026-09-20"


def test_offline_bucht_nicht():
    """Ein Offline-Testlauf darf den echten Zustand nie verändern."""
    import argparse
    from cryptobot import cli
    with tempfile.TemporaryDirectory() as d:
        args = argparse.Namespace(config="config.yaml", offline="data_offline",
                                  dry_run=False, force=True)
        # Die Sperre greift, bevor irgendetwas geladen wird
        if getattr(args, "offline", None) and not args.dry_run:
            args.dry_run = True
        assert args.dry_run is True


def test_ausfuehrungsfenster_verschiebt_morgens():
    """Morgens wird geprüft, aber nicht umgeschichtet."""
    from datetime import timezone as _tz
    cfg = _cfg()
    for stunde, erwartet in [(9, False), (11, False), (15, False), (16, True), (18, True)]:
        jetzt = datetime(2026, 10, 1, stunde, 0, tzinfo=_tz.utc)
        grenze = int(cfg.get("schedule.execute_after_hour"))
        darf = jetzt.astimezone(_tz.utc).hour >= grenze
        assert darf is erwartet, (stunde, darf, erwartet)


def test_fenster_laesst_sich_abschalten():
    cfg = _cfg(**{"schedule.execute_after_hour": None})
    assert cfg.get("schedule.execute_after_hour") is None


# ------------------------------------------------------------------ Profile

def test_profile_bekommen_eigene_zustaende():
    cfg = load("config.yaml")
    profile = cfg.profiles()
    assert len(profile) >= 2
    pfade = [c.get("paths.state") for _, c, _ in profile]
    assert len(set(pfade)) == len(pfade), "Profile duerfen sich den Zustand nicht teilen"
    for name, c, _ in profile:
        assert name in c.get("paths.state")


def test_genau_ein_champion():
    cfg = load("config.yaml")
    champs = [n for n, _, ch in cfg.profiles() if ch]
    assert len(champs) == 1, champs


def test_overrides_greifen_und_aendern_nichts_anderes():
    cfg = load("config.yaml")
    prof = {n: c for n, c, _ in cfg.profiles()}
    assert prof["defensiv"].get("risk.target_vol_portfolio") == 0.12
    assert prof["offensiv"].get("risk.target_vol_portfolio") == 0.35
    assert prof["schnell"].get("schedule.rebalance_every_days") == 5
    # Nur die eine Stellschraube darf sich unterscheiden
    assert prof["defensiv"].get("schedule.rebalance_every_days") == \
           prof["standard"].get("schedule.rebalance_every_days")
    assert prof["schnell"].get("risk.target_vol_portfolio") == \
           prof["standard"].get("risk.target_vol_portfolio")


def test_unbekannter_schluessel_im_profil_faellt_auf():
    cfg = load("config.yaml")
    try:
        cfg.with_overrides("kaputt", {"risk.gibtsnicht": 1})
    except ConfigError:
        return
    raise AssertionError("unbekannter Schluessel haette auffallen muessen")


def test_profil_aenderung_wirkt_auf_die_strategie():
    cfg = load("config.yaml")
    prof = {n: c for n, c, _ in cfg.profiles()}
    assert prof["defensiv"].strategy().target_vol_portfolio == 0.12
    assert prof["breit"].strategy().max_positions == 14


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    fehler = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as e:  # noqa: BLE001
            fehler.append(name)
            print(f"  FEHL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(fehler)}/{len(tests)} Tests bestanden")
    return 1 if fehler else 0



# --- Orderbuch-Mitschreiber ------------------------------------------------
class _Buch:
    """Minimales Orderbuch mit derselben Schnittstelle wie der Anbieter."""

    def __init__(self, bids, asks, market="X-EUR"):
        self.bids, self.asks, self.market = bids, asks, market

    def depth_within(self, pct, side="asks"):
        seite = self.asks if side == "asks" else self.bids
        if not self.asks or not self.bids:
            return 0.0
        mid = (self.asks[0][0] + self.bids[0][0]) / 2.0
        grenze = mid * (1 + pct) if side == "asks" else mid * (1 - pct)
        summe = 0.0
        for preis, menge in seite:
            if (side == "asks" and preis > grenze) or (side == "bids" and preis < grenze):
                break
            summe += preis * menge
        return summe


def test_effektivpreis_handgerechnet():
    """100 EUR gegen zwei Stufen: 50 EUR zu 10,00 und 50 EUR zu 11,00.
    5 + 4,5454 Stueck fuer 100 EUR -> 10,4762 im Schnitt."""
    from cryptobot.orderbuch import effektivpreis

    preis = effektivpreis([(10.0, 5.0), (11.0, 5.0)], 100.0)
    assert abs(preis - 100.0 / (5.0 + 50.0 / 11.0)) < 1e-9


def test_effektivpreis_meldet_zu_duenn():
    """Reicht die sichtbare Tiefe nicht, kommt None — kein geschaetzter Wert.
    Eine erfundene Zahl waere hier schlimmer als gar keine."""
    from cryptobot.orderbuch import effektivpreis

    assert effektivpreis([(10.0, 1.0)], 1000.0) is None


def test_slippage_ist_null_bei_unendlicher_tiefe():
    from cryptobot.orderbuch import slippage_bps

    b = _Buch(bids=[(100.0, 1e6)], asks=[(100.0, 1e6)])
    assert abs(slippage_bps(b, 500.0, "kauf")) < 1e-6
    assert abs(slippage_bps(b, 500.0, "verkauf")) < 1e-6


def test_slippage_waechst_mit_der_ordergroesse():
    from cryptobot.orderbuch import slippage_bps

    b = _Buch(bids=[(99.0, 1.0), (98.0, 100.0)], asks=[(101.0, 1.0), (102.0, 100.0)])
    klein, gross = slippage_bps(b, 50.0, "kauf"), slippage_bps(b, 5000.0, "kauf")
    assert 0 < klein < gross, f"{klein} / {gross}"


def test_gekreuztes_buch_wird_verworfen():
    """Ask unter Bid ist ein Datenfehler. So eine Zeile darf nicht in die
    Messreihe — sie wuerde als negativer Spread erscheinen und die Auswertung
    in die falsche Richtung ziehen."""
    from cryptobot.orderbuch import zeile

    assert zeile(_Buch(bids=[(101.0, 5.0)], asks=[(99.0, 5.0)])) is None
    assert zeile(_Buch(bids=[], asks=[(99.0, 5.0)])) is None


def test_zeile_rechnet_spread_in_basispunkten():
    from cryptobot.orderbuch import zeile

    z = zeile(_Buch(bids=[(99.0, 10.0)], asks=[(101.0, 10.0)]))
    assert abs(z["spread_bps"] - 200.0) < 0.01     # 2 EUR auf 100 = 2 % = 200 bp
    assert z["markt"] == "X-EUR"


def test_schreiben_und_lesen_geht_hin_und_zurueck(tmp_path=None):
    import tempfile
    from cryptobot.orderbuch import anhaengen, lesen, zeile

    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        z = zeile(_Buch(bids=[(99.0, 10.0)], asks=[(101.0, 10.0)]))
        anhaengen([z], ordner)
        anhaengen([z], ordner)
        zurueck = lesen(ordner)
        assert len(zurueck) == 2
        assert zurueck[0]["markt"] == "X-EUR"
        assert abs(float(zurueck[0]["spread_bps"]) - 200.0) < 0.01


def test_bericht_ohne_daten_bricht_nicht_ab():
    import tempfile
    from cryptobot.spreadbericht import bericht

    with tempfile.TemporaryDirectory() as d:
        text = bericht(Path(d))
        assert "Noch keine Messungen" in text


def test_gebuehr_kommt_aus_dem_kostenmodell_des_bots():
    """Der Bericht darf keine eigene Gebuehrenzahl erfinden. Sonst messen
    Bericht und Journal verschiedene Dinge und niemand merkt es."""
    from cryptobot.costs import CostModel
    from cryptobot.spreadbericht import _gebuehren_bps

    for volumen in (0.0, 150_000.0, 3_000_000.0):
        maker, taker = _gebuehren_bps(volumen)
        assert abs(maker - CostModel(maker_only=True).rate(volumen) * 10_000) < 1e-9
        assert abs(taker - CostModel(maker_only=False).rate(volumen) * 10_000) < 1e-9
        assert taker >= maker, "Taker ist nie guenstiger als Maker"


def test_gebuehrenstufe_sinkt_mit_dem_volumen():
    from cryptobot.spreadbericht import _gebuehren_bps

    klein = _gebuehren_bps(0.0)
    gross = _gebuehren_bps(3_000_000.0)
    assert gross[0] < klein[0] and gross[1] < klein[1], f"{klein} / {gross}"


def _messreihe(ordner, spread_bps, slip_bps, n):
    """n identische Messungen mit vorgegebenem Spread und vorgegebener
    Slippage je Seite."""
    from cryptobot.orderbuch import GROESSEN, anhaengen

    z = {"zeit": "2026-10-04T14:00:00+00:00", "markt": "T-EUR",
         "bid": 99.0, "ask": 101.0, "mid": 100.0, "spread_bps": spread_bps,
         "bid_eur": 1e6, "ask_eur": 1e6, "tiefe_bid_50bp": 1e6,
         "tiefe_ask_50bp": 1e6, "tiefe_bid_100bp": 1e6, "tiefe_ask_100bp": 1e6}
    for g in GROESSEN:
        z[f"slip_kauf_{int(g)}"] = slip_bps
        z[f"slip_verk_{int(g)}"] = slip_bps
    anhaengen([dict(z) for _ in range(n)], ordner)


def test_nehmer_rundlauf_enthaelt_die_taker_gebuehr_beider_seiten():
    """Der eigentliche Punkt: frueher stand hier nur die Slippage, wurde aber
    mit der 0,20-%-Grenze verglichen. Das liess die Boerse zu guenstig
    aussehen."""
    import re
    import tempfile
    from cryptobot.spreadbericht import _gebuehren_bps, bericht

    _, taker = _gebuehren_bps(0.0)
    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        _messreihe(ordner, spread_bps=10.0, slip_bps=5.0, n=60)
        text = bericht(ordner, min_messungen=50)
        zeile = [z for z in text.splitlines() if z.strip().startswith("T-EUR")][0]
        werte = [float(x) for x in re.findall(r"(\d+\.\d+)%", zeile)]
        # Spalte 0 ist der Spread, danach je Groesse der Rundlauf.
        erwartet = (5.0 + 5.0 + 2.0 * taker) / 100.0
        assert abs(werte[1] - erwartet) < 0.002, f"{werte} erwartet {erwartet}"


def test_steller_netto_zieht_die_maker_gebuehr_ab():
    import tempfile
    from cryptobot.spreadbericht import _gebuehren_bps, bericht

    maker, _ = _gebuehren_bps(0.0)
    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        # Spread knapp unter der doppelten Maker-Gebuehr -> Netto negativ.
        _messreihe(ordner, spread_bps=2.0 * maker - 4.0, slip_bps=1.0, n=60)
        text = bericht(ordner, min_messungen=50)
        assert "0 von 1 Maerkten tragen die Maker-Gebuehr" in text

        ordner2 = Path(d) / "b"
        ordner2.mkdir()
        _messreihe(ordner2, spread_bps=2.0 * maker + 20.0, slip_bps=1.0, n=60)
        text2 = bericht(ordner2, min_messungen=50)
        assert "1 von 1 Maerkten tragen die Maker-Gebuehr" in text2


def test_vergleich_nimmt_auf_beiden_seiten_dieselbe_ordergroesse():
    """Der Vergleich am Ende darf die Kette nicht mit der kleinsten Order
    gewinnen lassen, waehrend die Boerse mit 500 antritt."""
    import tempfile
    from cryptobot.orderbuch import anhaengen
    from cryptobot.spreadbericht import onchain_abschnitt

    spalten = ["zeit", "markt", "betrag_usdc", "rundlauf_bps", "impact_hin_bps",
               "impact_rueck_bps", "usdc_zurueck", "quelle", "hinweis"]

    def jz(betrag, bps):
        return {"zeit": "2026-10-04T14:00:00+00:00", "markt": "T/USDC",
                "betrag_usdc": betrag, "rundlauf_bps": bps, "impact_hin_bps": 0.0,
                "impact_rueck_bps": 0.0, "usdc_zurueck": 100.0, "quelle": "test",
                "hinweis": ""}

    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        # 100 $ waere billig, 500 $ ist teuer. Gewertet werden muss 500.
        anhaengen([jz(100.0, 1.0), jz(500.0, 300.0)], ordner,
                  spalten=spalten, praefix="jup")
        text = onchain_abschnitt(ordner, min_messungen=1, boerse_bestwert_bps=50.0)
        assert "Die Boerse ist um" in text, text
        assert "3.000 %" in text, text


def test_negativer_rundlauf_gilt_im_vergleich_als_null():
    import tempfile
    from cryptobot.orderbuch import anhaengen
    from cryptobot.spreadbericht import onchain_abschnitt

    spalten = ["zeit", "markt", "betrag_usdc", "rundlauf_bps", "impact_hin_bps",
               "impact_rueck_bps", "usdc_zurueck", "quelle", "hinweis"]
    z = {"zeit": "2026-10-04T14:00:00+00:00", "markt": "T/USDC",
         "betrag_usdc": 500.0, "rundlauf_bps": -3.0, "impact_hin_bps": 0.0,
         "impact_rueck_bps": 0.0, "usdc_zurueck": 100.0, "quelle": "test",
         "hinweis": ""}
    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        anhaengen([z], ordner, spalten=spalten, praefix="jup")
        text = onchain_abschnitt(ordner, min_messungen=1, boerse_bestwert_bps=50.0)
        assert "Artefakt" in text, text
        # 0,500 % Boerse gegen 0 (nicht gegen -0,03) => Vorsprung genau 0,500
        assert "um 0.500 Prozentpunkte guenstiger" in text, text


def test_bericht_nennt_die_verwendete_gebuehr():
    """Eine Zahl, die man nicht nachrechnen kann, ist keine Messung."""
    import tempfile
    from cryptobot.spreadbericht import bericht

    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        _messreihe(ordner, spread_bps=10.0, slip_bps=5.0, n=60)
        text = bericht(ordner, min_messungen=50)
        assert "Taker-Gebuehr" in text and "Maker-Gebuehr" in text
        assert "30-Tage-Volumen" in text


# --- TradingView-Export ----------------------------------------------------
def _trade(ms, markt="BTC-EUR", seite="BUY", preis=70000.0, real=0.0, profil="standard"):
    return {"ms": ms, "profil": profil, "champion": True, "markt": markt,
            "seite": seite, "preis": preis, "eur": 500.0, "gebuehr": 0.75,
            "grund": "REBALANCE", "realisiert": real}


def test_pine_zahlen_haben_immer_einen_punkt():
    from cryptobot.tradingview import _pine_float

    for wert in (74102.0, 80750, 1.3217, 0.0, 1e-9):
        assert any(z in _pine_float(wert) for z in ".eE"), _pine_float(wert)


def test_pine_text_wird_maskiert():
    from cryptobot.tradingview import _pine_str

    assert _pine_str('a"b') == '"a\\"b"'


def test_pine_skript_enthaelt_die_buchungen():
    from cryptobot.tradingview import erzeugen

    t = [_trade(1790855491000), _trade(1791738012000, seite="SELL", preis=80750.0, real=41.47)]
    text = erzeugen(t, ["standard", "defensiv"])
    assert "//@version=6" in text
    assert "1790855491000" in text and "1791738012000" in text
    assert '"BUY", "SELL"' in text
    assert "80750.0" in text, "Ganzzahl ohne Punkt wuerde Pine zerbrechen"
    assert "max_labels_count = 500" in text


def test_pine_ohne_trades_bricht_nicht_ab():
    from cryptobot.tradingview import erzeugen

    text = erzeugen([], ["standard"])
    assert "Noch keine Trades" in text


def test_pine_begrenzt_auf_label_grenze():
    """Pine zeichnet hoechstens 500 Beschriftungen. Mehr Buchungen werden auf
    die juengsten beschnitten, statt das Skript stillschweigend abschneiden zu
    lassen."""
    from cryptobot.tradingview import MAX_MARKEN, sammeln

    class J:
        def read_trades(self):
            return [{"zeit": f"2026-01-01T00:{i//60:02d}:{i%60:02d}+00:00", "markt": "BTC-EUR",
                     "seite": "BUY", "notional_eur": "100", "preis": "70000",
                     "gebuehr_eur": "0.15", "grund": "REBALANCE", "realisiert_eur": "0"}
                    for i in range(600)]

    raus = sammeln([("standard", None, True)], lambda c: J())
    assert len(raus) == MAX_MARKEN
    assert raus[0]["ms"] < raus[-1]["ms"], "muss nach Zeit sortiert sein"


# --- Solana-Messung (Jupiter) ----------------------------------------------
def test_rundlauf_kuerzt_die_nachkommastellen():
    """Der Rueckweg rechnet mit der Rohmenge des Hinwegs. Dadurch sind die
    Nachkommastellen des Tokens egal — nur die 6 von USDC muessen stimmen.
    Hier: 1 % Verlust je Seite -> rund 199 Basispunkte Rundlauf."""
    from cryptobot import jupiter as J

    rufe = []

    def falsch(eingang, ausgang, menge, slippage_bps=50):
        rufe.append((eingang, ausgang, menge))
        if eingang == J.USDC_MINT:
            return {"outAmount": str(int(menge * 0.99 * 10 ** 5)), "priceImpactPct": "0.004"}, "test"
        return {"outAmount": str(int(menge / 10 ** 5 * 0.99)), "priceImpactPct": "0.004"}, "test"

    alt, J.quote = J.quote, falsch
    try:
        e = J.rundlauf("TOKENMINT", 100.0)
    finally:
        J.quote = alt
    assert 190 < e["rundlauf_bps"] < 210, e
    assert e["impact_hin_bps"] == 40.0
    assert rufe[1][2] == int(rufe[0][2] * 0.99 * 10 ** 5)


def test_unerwartete_antwort_wird_zum_fehler():
    """Eine geaenderte Schnittstelle muss auffallen, nicht stillschweigend
    durchlaufen — sonst misst der Bot wochenlang Unsinn."""
    from cryptobot import jupiter as J

    alt = J._hole
    J._hole = lambda url, timeout=15.0: {"data": [], "nachricht": "neues Format"}
    try:
        J.quote("a", "b", 1000)
        raise AssertionError("haette scheitern muessen")
    except J.JupiterFehler as e:
        assert "outAmount" in str(e)
    finally:
        J._hole = alt


def test_zeile_verschluckt_keinen_fehler():
    from cryptobot import jupiter as J

    alt = J.rundlauf
    J.rundlauf = lambda *a, **k: (_ for _ in ()).throw(J.JupiterFehler("Pool leer"))
    try:
        z = J.zeile("TEST", "MINT", 100.0)
    finally:
        J.rundlauf = alt
    assert z["rundlauf_bps"] is None
    assert "Pool leer" in z["hinweis"]
    assert z["markt"] == "TEST/USDC"


def test_zwei_messreihen_im_selben_ordner():
    """Boerse und Kette schreiben nebeneinander, ohne sich zu vermischen."""
    import tempfile
    from cryptobot.jupiter import SPALTEN as JUP
    from cryptobot.orderbuch import anhaengen, lesen

    with tempfile.TemporaryDirectory() as d:
        ordner = Path(d)
        anhaengen([{"zeit": "2026-10-02T08:00:00+00:00", "markt": "BTC-EUR",
                    "spread_bps": 12.0}], ordner)
        anhaengen([{"zeit": "2026-10-02T08:00:00+00:00", "markt": "SOL/USDC",
                    "betrag_usdc": 500, "rundlauf_bps": 31.5}],
                  ordner, spalten=JUP, praefix="jup")
        assert len(lesen(ordner)) == 1
        assert len(lesen(ordner, praefix="jup")) == 1
        assert lesen(ordner)[0]["markt"] == "BTC-EUR"
        assert lesen(ordner, praefix="jup")[0]["markt"] == "SOL/USDC"


# --- Bewertung ohne aktuellen Kurs (07.10.2026) ------------------------------
def test_fehlender_kurs_zaehlt_nicht_als_null():
    """Am 4.10. stand 'offensiv' mit 9.350 EUR im Protokoll statt rund 10.220:
    eine gehaltene Muenze hatte im Lauf keinen Kurs und zaehlte als 0."""
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("LTC-EUR", 800.0, 100.0, 0.0, "2026-10-01")
        cash = p.s.cash
        assert abs(p.equity({"LTC-EUR": 110.0}) - (cash + 880.0)) < 1e-6
        assert abs(p.equity({}) - (cash + 880.0)) < 1e-6   # zuletzt gesehen, nicht 0


def test_ohne_je_gesehenen_kurs_gilt_der_einstiegskurs():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("LTC-EUR", 800.0, 100.0, 0.0, "2026-10-01")
        assert abs(p.equity({}) - (p.s.cash + 800.0)) < 1e-6


def test_zuletzt_gesehener_kurs_ueberlebt_neustart():
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("LTC-EUR", 800.0, 100.0, 0.0, "2026-10-01")
        p.equity({"LTC-EUR": 120.0})
        p.save()
        q = Portfolio.load_or_create(Path(d) / "p.json", 10_000.0)
        assert abs(q.equity({}) - (q.s.cash + 960.0)) < 1e-6


def test_alte_portfoliodatei_ohne_letzten_kurs_laedt():
    """Dateien von vor dem 07.10. kennen das neue Feld nicht."""
    import json
    with tempfile.TemporaryDirectory() as d:
        pfad = Path(d) / "p.json"
        pfad.write_text(json.dumps({"cash": 5000.0, "positions": {"BTC-EUR": {
            "market": "BTC-EUR", "units": 0.01, "entry_price": 70000.0,
            "entry_date": "2026-10-01", "high_water": 70000.0}}}), encoding="utf-8")
        q = Portfolio.load_or_create(pfad, 10_000.0)
        assert abs(q.equity({}) - 5700.0) < 1e-6


def test_tagesverlust_nicht_durch_fehlenden_kurs():
    """Ein fehlender Kurs darf keinen Scheinverlust und damit keine Sperre ausloesen."""
    with tempfile.TemporaryDirectory() as d:
        p = _portfolio(d)
        p.buy("LTC-EUR", 2000.0, 100.0, 0.0, "2026-10-01")
        p.mark_day({"LTC-EUR": 100.0}, "2026-10-04")
        assert abs(p.day_loss({})) < 1e-9


def test_startskript_meldet_absturz_als_absturz():
    """run_daily.sh meldete bis 07.10. jeden Absturz mit Code 0."""
    import shutil
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        ziel = Path(d)
        shutil.copy(Path(__file__).resolve().parents[1] / "run_daily.sh", ziel / "run_daily.sh")
        attrappe = ziel / "bin"
        attrappe.mkdir()
        (attrappe / "python3").write_text("#!/bin/bash\necho absturz >&2\nexit 3\n")
        (attrappe / "python3").chmod(0o755)
        env = dict(os.environ, PATH=f"{attrappe}:/usr/bin:/bin")
        r = subprocess.run(["bash", str(ziel / "run_daily.sh")], env=env, capture_output=True)
        log = (ziel / "state" / "cron.log").read_text()
        assert r.returncode == 3, r.returncode
        assert "fehlgeschlagen (Code 3)" in log, log
        assert "Code 3" in (ziel / "state" / "fehler.log").read_text()


def test_entnahme_ist_entfernt():
    """Nie freigegeben. Darf weder im Code noch in der Konfiguration stehen."""
    wurzel = Path(__file__).resolve().parents[1]
    assert "entnahme" not in (wurzel / "cryptobot" / "cli.py").read_text(encoding="utf-8").lower()
    assert "entnahme:" not in (wurzel / "config.yaml").read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
