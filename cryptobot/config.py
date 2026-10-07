"""Konfiguration laden, per Umgebungsvariablen überschreiben, validieren.

Ein Fehler in der Konfiguration bricht den Start ab. Stille Standardwerte gibt es
nicht — sonst handelt der Bot mit Annahmen, die niemand getroffen hat.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .strategy import StrategyConfig


class ConfigError(Exception):
    """Die Konfiguration ist unbrauchbar."""


ENV_PREFIX = "CRYPTOBOT_"


def _apply_env(data: dict[str, Any]) -> list[str]:
    """CRYPTOBOT_RISK__TARGET_VOL_PORTFOLIO=0.15 überschreibt risk.target_vol_portfolio."""
    angewendet: list[str] = []
    for name, wert in os.environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        pfad = name[len(ENV_PREFIX) :].lower().split("__")
        ziel = data
        for teil in pfad[:-1]:
            if not isinstance(ziel.get(teil), dict):
                ziel = None
                break
            ziel = ziel[teil]
        if ziel is None or pfad[-1] not in ziel:
            continue
        alt = ziel[pfad[-1]]
        try:
            if isinstance(alt, bool):
                neu: Any = wert.strip().lower() in {"1", "true", "yes", "ja"}
            elif isinstance(alt, int) and not isinstance(alt, bool):
                neu = int(wert)
            elif isinstance(alt, float):
                neu = float(wert)
            else:
                neu = wert
        except ValueError as e:
            raise ConfigError(f"{name}: {wert!r} passt nicht zum Typ von {'.'.join(pfad)}") from e
        ziel[pfad[-1]] = neu
        angewendet.append(".".join(pfad))
    return angewendet


@dataclass
class Config:
    raw: dict[str, Any]
    quelle: str
    env_overrides: list[str] = field(default_factory=list)

    # -- bequemer Zugriff ---------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, pfad: str, default: Any = None) -> Any:
        ziel: Any = self.raw
        for teil in pfad.split("."):
            if not isinstance(ziel, dict) or teil not in ziel:
                return default
            ziel = ziel[teil]
        return ziel

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # -- Ableitungen --------------------------------------------------------
    def strategy(self) -> StrategyConfig:
        """Übersetzt die Konfiguration in die geprüfte Strategie-Konfiguration."""
        s, reg, ri, sch = self["signal"], self["regime"], self["risk"], self["schedule"]
        return StrategyConfig(
            fast_window=int(s["fast_window"]),
            slow_window=int(s["slow_window"]),
            momentum_window=int(s["momentum_window"]),
            trend_threshold=float(s["trend_threshold"]),
            regime_market=str(reg["market"]),
            regime_slow=int(reg["slow_window"]),
            regime_momentum=int(reg["momentum_window"]),
            target_vol_position=float(ri["target_vol_position"]),
            target_vol_portfolio=float(ri["target_vol_portfolio"]),
            max_weight=float(ri["max_weight"]),
            min_weight=float(ri["min_weight"]),
            max_positions=int(ri["max_positions"]),
            vol_halflife=int(ri["vol_halflife_days"]),
            cov_window=int(ri["cov_window_days"]),
            rebalance_every=int(sch["rebalance_every_days"]),
            no_trade_band=float(sch["no_trade_band"]),
            periods_per_year=365.0,
        )

    def is_live(self) -> bool:
        return str(self.get("trading.mode", "analysis")).lower() == "live"

    # -- Profile ------------------------------------------------------------
    def with_overrides(self, name: str, overrides: dict[str, Any]) -> "Config":
        """Kopie dieser Konfiguration mit punktuellen Änderungen.

        Schlüssel in Punktschreibweise, z. B. {"risk.target_vol_portfolio": 0.12}.
        Die Pfade unter paths/ bekommen einen Unterordner je Profil, damit die
        Portfolios sich nicht gegenseitig überschreiben.
        """
        import copy

        roh = copy.deepcopy(self.raw)
        for pfad, wert in (overrides or {}).items():
            ziel = roh
            teile = pfad.split(".")
            for t in teile[:-1]:
                if not isinstance(ziel.get(t), dict):
                    raise ConfigError(f"Profil {name}: Abschnitt {t!r} gibt es nicht")
                ziel = ziel[t]
            if teile[-1] not in ziel:
                raise ConfigError(f"Profil {name}: Schlüssel {pfad!r} gibt es nicht")
            ziel[teile[-1]] = wert

        for schluessel, wert in list(roh.get("paths", {}).items()):
            teil = Path(str(wert))
            roh["paths"][schluessel] = str(teil.parent / name / teil.name)

        kopie = Config(raw=roh, quelle=f"{self.quelle}#{name}", env_overrides=self.env_overrides)
        validate(kopie)
        return kopie

    def profiles(self) -> list[tuple[str, "Config", bool]]:
        """(Name, Konfiguration, ist_champion) je Profil.

        Ohne Profil-Abschnitt läuft genau ein Portfolio mit dieser Konfiguration.
        """
        roh = self.raw.get("profiles")
        if not roh:
            return [("standard", self, True)]
        namen = [p.get("name") for p in roh]
        if len(set(namen)) != len(namen):
            raise ConfigError("Profilnamen müssen eindeutig sein")
        champions = [p for p in roh if p.get("champion")]
        if len(champions) > 1:
            raise ConfigError("Es darf höchstens einen Champion geben")
        raus = []
        for eintrag in roh:
            name = eintrag.get("name")
            if not name:
                raise ConfigError("Jedes Profil braucht einen Namen")
            raus.append((name, self.with_overrides(name, eintrag.get("overrides") or {}),
                         bool(eintrag.get("champion"))))
        return raus



PFLICHT = ("trading", "signal", "regime", "risk", "schedule", "universe", "costs", "paths")


def validate(cfg: Config) -> None:
    fehlt = [k for k in PFLICHT if k not in cfg.raw]
    if fehlt:
        raise ConfigError(f"Abschnitte fehlen: {', '.join(fehlt)}")

    modus = str(cfg.get("trading.mode", "")).lower()
    if modus not in {"analysis", "paper", "live"}:
        raise ConfigError(f"trading.mode muss analysis, paper oder live sein — ist {modus!r}")

    s = cfg["signal"]
    if not 0 < s["fast_window"] < s["slow_window"]:
        raise ConfigError("signal.fast_window muss größer als 0 und kleiner als slow_window sein")
    if not 0 < s["trend_threshold"] <= 1:
        raise ConfigError("signal.trend_threshold muss zwischen 0 und 1 liegen")

    r = cfg["risk"]
    if not 0 < r["target_vol_portfolio"] <= 2.0:
        raise ConfigError("risk.target_vol_portfolio muss zwischen 0 und 2 liegen")
    if not 0 < r["target_vol_position"] <= r["target_vol_portfolio"] * 5:
        raise ConfigError("risk.target_vol_position ist im Verhältnis zum Portfolioziel unplausibel")
    if not 0 < r["min_weight"] < r["max_weight"] <= 1.0:
        raise ConfigError("risk: min_weight < max_weight <= 1 verletzt")
    if r["max_positions"] < 1:
        raise ConfigError("risk.max_positions muss mindestens 1 sein")
    if r["max_weight"] * r["max_positions"] < 1.0 and r["max_positions"] < 4:
        raise ConfigError("risk: zu wenige Positionen, das Portfolio kann nicht gefüllt werden")
    if not -1.0 < r["catastrophe_stop"] < 0:
        raise ConfigError("risk.catastrophe_stop muss negativ und größer als -1 sein")

    if cfg["schedule"]["rebalance_every_days"] < 1:
        raise ConfigError("schedule.rebalance_every_days muss mindestens 1 sein")
    if not 0 <= cfg["schedule"]["no_trade_band"] < 1:
        raise ConfigError("schedule.no_trade_band muss zwischen 0 und 1 liegen")

    if cfg["costs"]["slippage_bps"] < 0:
        raise ConfigError("costs.slippage_bps darf nicht negativ sein")

    # Der Live-Modus verlangt drei weitere Bestätigungen; hier wird die erste geprüft.
    if modus == "live" and os.environ.get("CRYPTOBOT_ALLOW_LIVE") != "1":
        raise ConfigError(
            "trading.mode ist live, aber CRYPTOBOT_ALLOW_LIVE=1 ist nicht gesetzt. "
            "Der Live-Modus verlangt zusätzlich das CLI-Flag --live und eine Bestätigung."
        )


def load(pfad: str | Path = "config.yaml") -> Config:
    p = Path(pfad)
    if not p.exists():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {p}")
    try:
        daten = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} ist kein gültiges YAML: {e}") from e
    if not isinstance(daten, dict):
        raise ConfigError(f"{p} enthält kein Objekt auf oberster Ebene")
    overrides = _apply_env(daten)
    cfg = Config(raw=daten, quelle=str(p), env_overrides=overrides)
    validate(cfg)
    return cfg
