"""Ausgabe auf der Konsole — ohne Zusatzbibliotheken, damit der Bot überall läuft."""

from __future__ import annotations

import json
from datetime import datetime

from .journal import Decision, Fill
from .planner import Plan
from .portfolio import Portfolio

RANG = {"KAUFEN": 0, "VERKAUFEN": 1, "REDUZIEREN": 2, "HALTEN": 3,
        "BEOBACHTEN": 4, "NICHT KAUFEN": 5}


def _bar(anteil: float, breite: int = 20) -> str:
    voll = max(0, min(breite, round(anteil * breite)))
    return "█" * voll + "·" * (breite - voll)


def kopf(plan: Plan, portfolio: Portfolio, modus: str, jetzt: datetime) -> str:
    s = portfolio.s
    zeilen = [
        f"Krypto-Bot · {jetzt:%d.%m.%Y %H:%M} UTC · Modus {modus.upper()}",
        "=" * 78,
        f"  Kapital        {plan.equity:>12,.2f} EUR      "
        f"Eingezahlt {portfolio.invested_capital():>10,.2f} EUR",
        f"  davon Cash     {s.cash:>12,.2f} EUR      "
        f"Investiert {plan.exposure:>9.0%}  {_bar(plan.exposure)}",
        f"  Positionen     {len(s.positions):>12}          "
        f"Regime     {plan.regime} (Faktor {plan.regime_factor:.2f})",
    ]
    if s.peak_equity > 0:
        dd = plan.equity / s.peak_equity - 1
        zeilen.append(f"  Drawdown       {dd:>12.1%}          "
                      f"Realisiert {s.realized_pnl:>10,.2f} EUR")
    for h in plan.hinweise:
        zeilen.append(f"  ! {h}")
    return "\n".join(zeilen)


def positionen(portfolio: Portfolio, preise: dict[str, float]) -> str:
    if not portfolio.s.positions:
        return "\nOffene Positionen: keine — vollständig in Cash."
    zeilen = ["", "Offene Positionen",
              "-" * 78,
              f"  {'Markt':<12}{'Menge':>14}{'Einstand':>12}{'Kurs':>12}{'Wert EUR':>12}{'G/V':>10}"]
    gesamt = 0.0
    for m, p in sorted(portfolio.s.positions.items()):
        kurs = preise.get(m, p.entry_price)
        wert = p.value(kurs)
        gesamt += wert
        zeilen.append(f"  {m:<12}{p.units:>14.6f}{p.entry_price:>12.4f}"
                      f"{kurs:>12.4f}{wert:>12,.2f}{p.pnl_pct(kurs):>10.1%}")
    zeilen.append(f"  {'Summe':<12}{'':>14}{'':>12}{'':>12}{gesamt:>12,.2f}")
    return "\n".join(zeilen)


def entscheidungen(plan: Plan, limit: int = 12) -> str:
    sortiert = sorted(
        plan.entscheidungen,
        key=lambda d: (RANG.get(d.entscheidung, 9), -(d.trend_strength or 0)),
    )
    zeilen = ["", "Bewertung", "-" * 78]
    gezeigt = 0
    for d in sortiert:
        if d.entscheidung == "NICHT KAUFEN" and gezeigt >= limit:
            continue
        gezeigt += 1
        t = f"{d.trend_strength:.2f}" if d.trend_strength is not None else "  – "
        zeilen.append(
            f"  {d.market:<12}{d.entscheidung:<13}Trend {t:>5}  "
            f"Daten {d.datenvertrauen:>3}  Ziel {d.ziel_gewicht:>6.1%}  Ist {d.ist_gewicht:>6.1%}"
        )
        if d.filter_aktiv:
            zeilen.append(f"               Filter: {', '.join(sorted(set(d.filter_aktiv)))}")
        for g in d.begruendung[:2]:
            zeilen.append(f"               {g}")
    rest = len(sortiert) - gezeigt
    if rest > 0:
        zeilen.append(f"  … {rest} weitere ohne Signal")
    return "\n".join(zeilen)


def orders(plan: Plan, fills: list[Fill] | None) -> str:
    if not plan.orders:
        return "\nOrders: keine — nichts zu tun."
    zeilen = ["", "Orders", "-" * 78]
    for o in plan.orders:
        zeilen.append(f"  {o.seite:<5}{o.market:<12}{o.notional:>12,.2f} EUR  "
                      f"Limit {o.limit:>12.4f}  {o.grund}")
    if fills is not None:
        gebuehren = sum(f.gebuehr for f in fills)
        zeilen.append(f"  → {len(fills)} ausgeführt, Gebühren {gebuehren:,.2f} EUR")
    return "\n".join(zeilen)


def voll(plan: Plan, portfolio: Portfolio, preise: dict[str, float],
         modus: str, jetzt: datetime, fills: list[Fill] | None = None) -> str:
    return "\n".join([
        kopf(plan, portfolio, modus, jetzt),
        positionen(portfolio, preise),
        entscheidungen(plan),
        orders(plan, fills),
        "",
    ])


def als_json(plan: Plan, portfolio: Portfolio, modus: str, jetzt: datetime) -> str:
    from dataclasses import asdict
    return json.dumps({
        "schema_version": 1,
        "zeit": jetzt.isoformat(),
        "modus": modus,
        "equity": round(plan.equity, 2),
        "cash": round(portfolio.s.cash, 2),
        "exposure": round(plan.exposure, 4),
        "regime": plan.regime,
        "regime_factor": plan.regime_factor,
        "hinweise": plan.hinweise,
        "positionen": {m: {"menge": p.units, "einstand": p.entry_price}
                       for m, p in portfolio.s.positions.items()},
        "entscheidungen": [asdict(d) for d in plan.entscheidungen],
        "orders": [{"markt": o.market, "seite": o.seite, "notional": round(o.notional, 2),
                    "limit": o.limit, "grund": o.grund} for o in plan.orders],
    }, ensure_ascii=False, indent=2)
