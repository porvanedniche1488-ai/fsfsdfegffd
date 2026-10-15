# -*- coding: utf-8 -*-
"""
Модуль аналізу ринку Binance Futures.

Без жодного "очікування підтвердження" — сигна генеруються миттєво на основі:
  1) Open Interest (зростання OI + рух ціни = підтверджений тренд)
  2) Обсяг (спайк відносно середнього)
  3) RSI (перепроданість/перекупленість)
  4) EMA 9/21 (напрямок короткого тренду)
  5) Баланс лонгів/шортів (Long/Short Ratio, counter-fading натовпу)
  6) Фандінг (ф'ючерсний фандинг rate)

Рівень сигналу:
  SHORT signal -> монета йде вниз
  LONG signal  -> монета йде вгору
Довжина сигналу (короткий/довгий):
  "Короткий"  = сильний імпульс, TP1 близько (scalp)
  "Довгий"    = трендовий сигнал, TP далі (swing)
"""
from __future__ import annotations

import time
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

import httpx

BINANCE_FAPI = "https://fapi.binance.com"
# публічні дані по довгих вікнах (не потребують ключа)


@dataclass
class Signal:
    symbol: str
    side: str            # "LONG" or "SHORT"
    duration: str        # "SHORT" (короткий) or "LONG" (довгий)
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    confidence: int      # 0..100 — шанс спрацювання
    reasons: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def label(self) -> str:
        ru_side = "ЛОНГ" if self.side == "LONG" else "ШОРТ"
        ru_dur = "ДОВГИЙ" if self.duration == "LONG" else "КОРОТКИЙ"
        return f"{ru_dur} {ru_side}"


def _ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class Analyzer:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15)

    async def _get(self, path: str, params: dict) -> any:
        r = await self.client.get(BINANCE_FAPI + path, params=params)
        r.raise_for_status()
        return r.json()

    async def top_symbols(self, n: int) -> list[str]:
        """Топ-n USDT-ф'ючерсів за добовим обсягом."""
        data = await self._get("/fapi/v1/ticker/24hr", {})
        rows = [d for d in data if d["symbol"].endswith("USDT")]
        rows.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
        return [d["symbol"] for d in rows[:n]]

    async def candles(self, symbol: str, interval: str = "15m", limit: int = 100) -> list[dict]:
        raw = await self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        out = []
        for k in raw:
            out.append({
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        return out

    async def open_interest_hist(self, symbol: str, period: str = "1h", limit: int = 6) -> list[float]:
        raw = await self._get("/futures/data/openInterestHist",
                              {"symbol": symbol, "period": period, "limit": limit})
        return [float(x["sumOpenInterestValue"]) for x in raw]

    async def global_long_short_ratio(self, symbol: str, period: str = "1h", limit: int = 6) -> Optional[float]:
        try:
            raw = await self._get("/futures/data/globalLongShortAccountRatio",
                                  {"symbol": symbol, "period": period, "limit": limit})
            if not raw:
                return None
            return float(raw[-1]["longShortRatio"])
        except Exception:
            return None

    async def funding_rate(self, symbol: str) -> Optional[float]:
        try:
            raw = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            return float(raw["lastFundingRate"])
        except Exception:
            return None

    async def analyze_symbol(self, symbol: str) -> Optional[Signal]:
        """Збираємо дані та рахуємо сигнал."""
        try:
            kl = await self.candles(symbol, "15m", 100)
        except Exception:
            return None
        if len(kl) < 30:
            return None

        closes = [c["close"] for c in kl]
        vols = [c["volume"] for c in kl]
        price = closes[-1]

        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        rsi = _rsi(closes, 14)
        if ema9 is None or ema21 is None or rsi is None:
            return None

        # зміна ціни за останні 4 свічки (1 година)
        chg_1h = (price - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0.0
        # спайк обсягу
        vol_avg = statistics.mean(vols[-21:-1]) if len(vols) > 21 else statistics.mean(vols)
        vol_ratio = vols[-1] / vol_avg if vol_avg > 0 else 1.0

        # Open interest change (за останні години)
        oi_change_pct = 0.0
        try:
            oi = await self.open_interest_hist(symbol, "1h", 6)
            if len(oi) >= 2 and oi[0] > 0:
                oi_change_pct = (oi[-1] - oi[0]) / oi[0] * 100
        except Exception:
            pass

        ls_ratio = await self.global_long_short_ratio(symbol, "1h", 6)
        funding = await self.funding_rate(symbol)

        score = 0.0  # додатньо -> лонг, від'ємно -> шорт
        reasons: list[str] = []

        # 1. Напрямок тренду EMA
        trend_up = ema9 > ema21
        if trend_up:
            score += 2
            reasons.append("EMA9>EMA21 (тренд вгору)")
        else:
            score -= 2
            reasons.append("EMA9<EMA21 (тренд вниз)")

        # 2. Рух ціни + OI разом = справжній рух
        if chg_1h > 0.5 and oi_change_pct > 1.0:
            score += 2
            reasons.append(f"OI+{oi_change_pct:.1f}% при зростанні ціни — реальний попит")
        elif chg_1h < -0.5 and oi_change_pct > 1.0:
            score -= 2
            reasons.append(f"OI+{oi_change_pct:.1f}% при падінні ціни — реальний тиск продажів")

        # 2b. Імпульс ціни (щоб шорт-сетупи не зникали через відсутність OI-спайка)
        if chg_1h >= 2.0:
            score += 1
            reasons.append(f"Імпульс вгору {chg_1h:+.1f}% за годину")
        elif chg_1h <= -2.0:
            score -= 1
            reasons.append(f"Імпульс вниз {chg_1h:+.1f}% за годину")

        # 3. Обсяг
        if vol_ratio > 1.8:
            reasons.append(f"Спайк обсягу x{vol_ratio:.1f}")
            score += 1 if chg_1h > 0 else -1

        # 4. RSI — шукаємо розвороти
        if rsi < 30:
            score += 2
            reasons.append(f"RSI={rsi:.0f} перепродано — шанс відскоку вгору")
        elif rsi > 70:
            score -= 2
            reasons.append(f"RSI={rsi:.0f} перекуплено — шанс корекції вниз")

        # 5. Counter-fading натовпу
        if ls_ratio is not None:
            if ls_ratio > 2.0:
                score -= 1
                reasons.append(f"Натовп перевантажений лонгами ({ls_ratio:.2f}) — ризик зливу вниз")
            elif ls_ratio < 0.6:
                score += 1
                reasons.append(f"Натовп у шортах ({ls_ratio:.2f}) — потенціал сквізу вгору")

        # 6. Funding
        if funding is not None:
            if funding > 0.0005:
                score -= 1
                reasons.append(f"Фандинг високий (+{funding*100:.3f}%) — перегрів лонгів")
            elif funding < -0.0002:
                score += 1
                reasons.append(f"Негативний фандинг ({funding*100:.3f}%) — шорти платять, бичачий фон")

        # Вирішення
        threshold = 3.0
        if abs(score) < threshold:
            return None

        side = "LONG" if score > 0 else "SHORT"

        # Довжина сигналу: дуже сильний імпульс = короткий (scalp),
        # помірна сила + тренд = довгий (swing)
        strength = abs(score)
        atr = self._atr(kl, 14) or (price * 0.01)

        if strength >= 6:
            duration = "SHORT"   # гарячий scalp-сигнал
            sl_mult, tp1_mult, tp2_mult = 0.8, 1.2, 2.5
        else:
            duration = "LONG"    # трендовий swing
            sl_mult, tp1_mult, tp2_mult = 1.5, 2.0, 4.0

        if side == "LONG":
            entry = price
            stop = entry - atr * sl_mult
            tp1 = entry + atr * tp1_mult
            tp2 = entry + atr * tp2_mult
        else:
            entry = price
            stop = entry + atr * sl_mult
            tp1 = entry - atr * tp1_mult
            tp2 = entry - atr * tp2_mult

        # Конфіденс: базово від сили сигналу + бонуси
        conf = min(95, max(55, 55 + int(strength * 5)))
        if vol_ratio > 2:
            conf = min(97, conf + 3)

        return Signal(
            symbol=symbol.replace("USDT", ""),
            side=side,
            duration=duration,
            entry=entry,
            stop_loss=stop,
            tp1=tp1,
            tp2=tp2,
            confidence=conf,
            reasons=reasons,
        )

    @staticmethod
    def _atr(kl: list[dict], period: int = 14) -> Optional[float]:
        if len(kl) < period + 1:
            return None
        trs = []
        for i in range(1, len(kl)):
            h, l, pc = kl[i]["high"], kl[i]["low"], kl[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs[-period:]) / period

    async def scan_market(self, top_n: int = 120, min_confidence: int = 60) -> list[Signal]:
        """Паралельний скан топ-ні монет. Повертає ВСІ сигнали не слабші за поріг
        (не зупиняється на першому ж — кожен скан дає повний список різних монет)."""
        import asyncio as _aio
        symbols = await self.top_symbols(top_n)
        # аналізуємо паралельно, але обмежуємо кількість одночасних запитів
        sem = _aio.Semaphore(8)

        async def guarded(sym: str):
            async with sem:
                try:
                    return await self.analyze_symbol(sym)
                except Exception:
                    return None

        results = await _aio.gather(*(guarded(s) for s in symbols))
        signals = [sig for sig in results if sig is not None and sig.confidence >= min_confidence]
        signals.sort(key=lambda x: x.confidence, reverse=True)
        return signals


def format_signal(sig: Signal) -> str:
    """Гарне повідомлення для Telegram (MarkdownV2-safe простий текст)."""
    arrow = "🟢" if sig.side == "LONG" else "🔴"
    lines = [
        f"{arrow} {sig.label()} — {sig.symbol}/USDT",
        "",
        f"💰 Вхід: {sig.entry:.6g}",
        f"🛑 Стоп-лос: {sig.stop_loss:.6g}",
        f"🎯 Тейк 1: {sig.tp1:.6g}",
        f"🎯🎯 Тейк 2: {sig.tp2:.6g}",
        f"📊 Шанс спрацювання: ~{sig.confidence}%",
        "",
        "Чому:",
    ]
    for r in sig.reasons[:5]:
        lines.append(f"• {r}")
    lines.append("")
    lines.append("⚠️ Не фінансова порада. Ризик на себе.")
    return "\n".join(lines)
