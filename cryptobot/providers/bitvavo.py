"""Marktdaten von Bitvavo — öffentliche Endpunkte, kein Konto, kein Schlüssel.

Alles hier ist nur lesend. Der Handelsschlüssel taucht in diesem Modul nicht auf
und wird auch nicht gebraucht.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.bitvavo.com/v2"
DAY_MS = 86_400_000


class ProviderError(Exception):
    """Die Datenquelle hat nicht geliefert."""


@dataclass(frozen=True)
class Ticker:
    market: str
    bid: float
    ask: float
    volume_quote: float
    fetched_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else float("inf")


@dataclass(frozen=True)
class OrderBook:
    market: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    fetched_at: datetime

    def depth_within(self, pct: float, side: str = "asks") -> float:
        """Kumuliertes Volumen in Euro bis `pct` vom Mittelkurs."""
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

    def slippage_for(self, notional_eur: float) -> float:
        """Erwartete Slippage beim Kauf, durch Durchlaufen der Ask-Seite.

        Reicht die sichtbare Tiefe nicht, wird unendlich zurückgegeben — die Position
        gilt dann als nicht ausführbar.
        """
        if not self.asks or not self.bids or notional_eur <= 0:
            return float("inf")
        mid = (self.asks[0][0] + self.bids[0][0]) / 2.0
        rest, kosten, menge_gesamt = notional_eur, 0.0, 0.0
        for preis, menge in self.asks:
            wert = preis * menge
            nimm = min(rest, wert)
            menge_gesamt += nimm / preis
            kosten += nimm
            rest -= nimm
            if rest <= 1e-9:
                break
        if rest > 1e-9 or menge_gesamt <= 0:
            return float("inf")
        vwap = kosten / menge_gesamt
        return vwap / mid - 1.0


def _get(url: str, versuche: int = 3, timeout: float = 20.0):
    letzter: Exception | None = None
    for n in range(versuche):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            letzter = e
            if n < versuche - 1:
                time.sleep(2**n)
    raise ProviderError(f"{url} nicht erreichbar: {letzter}")


class BitvavoData:
    """Lesender Zugriff mit einfachem Dateicache."""

    def __init__(self, cache_dir: str | Path | None = None, candle_ttl_s: int = 900) -> None:
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        self.candle_ttl = candle_ttl_s

    # -- Universum ----------------------------------------------------------
    def markets_by_volume(self, quote: str = "EUR", stablecoins: set[str] | None = None) -> list[tuple[str, float]]:
        stablecoins = stablecoins or set()
        rows = _get(f"{BASE}/ticker/24h")
        out: list[tuple[str, float]] = []
        for e in rows:
            markt = e.get("market", "")
            if not markt.endswith(f"-{quote}"):
                continue
            basis = markt.split("-")[0]
            if basis in stablecoins:
                continue
            try:
                out.append((markt, float(e.get("volumeQuote") or 0.0)))
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    # -- Kerzen -------------------------------------------------------------
    def daily_candles(self, market: str, days: int = 500) -> list[tuple[int, float]]:
        datei = self.cache / f"{market}_1d.json" if self.cache else None
        if datei and datei.exists() and time.time() - datei.stat().st_mtime < self.candle_ttl:
            roh = json.loads(datei.read_text())
            return [(int(t), float(c)) for t, c in roh]

        ende = int(time.time() * 1000)
        grenze = ende - days * DAY_MS
        gesammelt: dict[int, float] = {}
        for _ in range(4):
            rows = _get(f"{BASE}/{market}/candles?interval=1d&limit=1440&end={ende}")
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                ts = int(row[0])
                if ts >= grenze:
                    gesammelt[ts] = float(row[4])
            aeltester = min(int(r[0]) for r in rows)
            if len(rows) < 1440 or aeltester <= grenze:
                break
            ende = aeltester - 1
        reihe = sorted(gesammelt.items())
        if datei:
            datei.write_text(json.dumps(reihe))
        return reihe

    # -- Momentaufnahmen ----------------------------------------------------
    def ticker(self, market: str) -> Ticker:
        b = _get(f"{BASE}/ticker/book?market={market}")
        t24 = _get(f"{BASE}/ticker/24h?market={market}")
        if not isinstance(b, dict) or "bid" not in b or "ask" not in b:
            raise ProviderError(f"{market}: unvollständige Bid/Ask-Antwort")
        vol = 0.0
        if isinstance(t24, dict):
            try:
                vol = float(t24.get("volumeQuote") or 0.0)
            except (TypeError, ValueError):
                vol = 0.0
        return Ticker(
            market=market,
            bid=float(b["bid"]),
            ask=float(b["ask"]),
            volume_quote=vol,
            fetched_at=datetime.now(timezone.utc),
        )

    def orderbook(self, market: str, depth: int = 200) -> OrderBook:
        d = _get(f"{BASE}/{market}/book?depth={depth}")
        if not isinstance(d, dict) or "bids" not in d or "asks" not in d:
            raise ProviderError(f"{market}: unvollständiges Orderbuch")
        return OrderBook(
            market=market,
            bids=[(float(p), float(m)) for p, m in d["bids"]],
            asks=[(float(p), float(m)) for p, m in d["asks"]],
            fetched_at=datetime.now(timezone.utc),
        )
