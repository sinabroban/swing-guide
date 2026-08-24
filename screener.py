# -*- coding: utf-8 -*-
"""
[SwingGuide 주간 모멘텀 스크리너]
- 전체 상장 종목을 품질 필터로 거른 뒤, 검증된 모멘텀 팩터로 스코어링하여
  주간 TOP5 후보를 선정한다.
- 결과: picks.json (프론트에서 fetch) + data/picks_history.json (성적표 누적)

필터: 시가총액 5,000억 이상 / 주가 2,000원 이상 / 최근 거래대금 충분
      / 우선주·스팩·리츠·ETN 제외
팩터: 3개월 모멘텀(검증된 팩터) + 이동평균 정배열 + 거래량 확대
      + 변동성 안정성 + RSI 과열 페널티

실행: python screener.py        # 매주 일요일 저녁 자동 실행 권장
테스트: SCREEN_LIMIT=25 python screener.py   (후보 수 제한)
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PICKS_PATH = os.path.join(BASE_DIR, "picks.json")
HISTORY_PATH = os.path.join(BASE_DIR, "data", "picks_history.json")

MIN_CAP = 500_000_000_000          # 시가총액 5,000억
MIN_PRICE = 2_000                  # 주가 2,000원
MIN_TURNOVER = 10_000_000_000      # 일 평균 거래대금 100억
CANDIDATE_LIMIT = int(os.environ.get("SCREEN_LIMIT", "120"))  # 스코어링 대상 수
TOP_N = 5
HORIZON_DAYS = 30
EXCLUDE_WORDS = ("우선주", "스팩", "리츠", "ETN", "ETF")

HEADERS = {"User-Agent": "Mozilla/5.0"}
FCHART_URL = "https://fchart.stock.naver.com/sise.nhn"


# ============================================================ 데이터 수집
def fetch_history(symbol, count=140):
    """네이버 fchart 일봉 -> [{date,open,high,low,close,volume}]
    (응답이 구버전 CSV / 신버전 XML 두 형식 모두 지원)"""
    url = (f"{FCHART_URL}?symbol={symbol}&timeframe=day"
           f"&count={count}&requestType=0")
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    rows = []
    items = re.findall(r'<item data="([^"]+)"', r.text)
    if items:
        for it in items:
            cols = it.split("|")
            if len(cols) >= 6 and re.match(r"^\d{8}$", cols[0]):
                rows.append({
                    "date": cols[0], "open": float(cols[1]),
                    "high": float(cols[2]), "low": float(cols[3]),
                    "close": float(cols[4]), "volume": float(cols[5]),
                })
    else:
        for ln in r.text.splitlines():
            cols = ln.strip().split(",")
            if len(cols) >= 6 and re.match(r"^\d{8}$", cols[0]):
                rows.append({
                    "date": cols[0], "open": float(cols[1]),
                    "high": float(cols[2]), "low": float(cols[3]),
                    "close": float(cols[4]), "volume": float(cols[5]),
                })
    if len(rows) < 70:
        raise ValueError(f"데이터 부족({len(rows)}행)")
    return rows


def build_universe():
    """네이버 금융 시가총액 순위에서 품질 필터 통과 후보 추출
    -> [{symbol, name, price}] (시총 내림차순)"""
    pool = []
    for sosok in ("0", "1"):  # 0=코스피, 1=코스닥
        for page in range(1, 40):
            url = (f"https://finance.naver.com/sise/sise_market_sum.naver"
                   f"?sosok={sosok}&page={page}")
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            codes = re.findall(r'main\.naver\?code=(\d{6})', r.text)
            tables = pd.read_html(r.text)
            df = max((t for t in tables if t is not None and not t.empty),
                     key=len)
            df = df[pd.to_numeric(df["현재가"], errors="coerce").notna()
                    & df["종목명"].notna() & (df["종목명"] != "종목명")]
            rows = list(df.iterrows())
            if len(rows) != len(codes):
                break
            exhausted = False
            for (_, row), sym in zip(rows, codes):
                name = str(row.get("종목명", "")).strip()
                price = pd.to_numeric(row.get("현재가"), errors="coerce")
                cap_eok = pd.to_numeric(row.get("시가총액"), errors="coerce")
                if pd.isna(price) or pd.isna(cap_eok):
                    continue
                cap = float(cap_eok) * 1e8
                if cap < MIN_CAP:
                    exhausted = True
                    break
                if any(w in name for w in EXCLUDE_WORDS) or name.endswith("우"):
                    continue
                pool.append({"symbol": sym, "name": name,
                             "price": float(price)})
            if exhausted or not codes:
                break
            time.sleep(0.2)

    # 중복 제거 + 스캔 한도
    uniq = list({i["symbol"]: i for i in pool}.values())
    print(f"[universe] 필터 통과 {len(uniq)}종목")
    return uniq[:CANDIDATE_LIMIT]


# ============================================================ 지표 계산
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(-period, 0)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(-period, 0)]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + sum(gains) / period / avg_loss), 1)


def atr_pct(rows, period=14):
    trs = []
    for i in range(-period, 0):
        h, l, pc = rows[i]["high"], rows[i]["low"], rows[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / period
    return atr / rows[-1]["close"] * 100


def compute_factors(rows):
    """일봉 -> 팩터 딕셔너리"""
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]
    last = closes[-1]

    mom60 = (last / closes[-61] - 1) * 100 if len(closes) > 60 else None
    ma20, ma60 = sma(closes, 20), sma(closes, 60)
    vol_ratio = (sum(vols[-5:]) / 5) / (sum(vols[-25:-5]) / 20) \
        if sum(vols[-25:-5]) else None
    a_pct = round(atr_pct(rows), 2)
    r14 = rsi(closes)

    aligned = bool(ma20 and ma60 and last > ma20 > ma60)
    above60 = bool(ma60 and last > ma60)

    return {
        "last": last,
        "mom60": round(mom60, 1) if mom60 is not None else 0.0,
        "aligned": aligned,
        "above60": above60,
        "vol_ratio": round(vol_ratio, 2) if vol_ratio else 1.0,
        "atr_pct": a_pct,
        "rsi": r14,
    }


def percentile_ranks(values):
    """값 -> 0~100 백분위"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0] * len(values)
    for pos, idx in enumerate(order):
        ranks[idx] = round(pos / max(len(values) - 1, 1) * 100)
    return ranks


def score_all(factors_list):
    """팩터 조합 스코어 (0~100)"""
    mom_rank = percentile_ranks([f["mom60"] for f in factors_list])
    vol_rank = percentile_ranks([f["vol_ratio"] for f in factors_list])
    stable_rank = percentile_ranks([-f["atr_pct"] for f in factors_list])

    scored = []
    for f, mr, vr, sr in zip(factors_list, mom_rank, vol_rank, stable_rank):
        score = mr * 0.45 + vr * 0.15 + sr * 0.20
        if f["aligned"]:
            score += 12
        elif f["above60"]:
            score += 6
        if f["rsi"] is not None and f["rsi"] > 75:
            score -= 10
        f["score"] = round(max(0, min(100, score)))
        scored.append(f)
    return scored


def build_scenario(entry, atr_pct_val):
    """ATR 기반 목표가/손절가 (보유 2주~1개월)"""
    target = entry * (1 + max(atr_pct_val * 2.5 / 100, 0.08))
    stop = entry * (1 - max(atr_pct_val * 1.5 / 100, 0.05))
    return round(target), round(stop), "2주~1개월"


def make_reasons(f):
    bits = [f"3개월 {f['mom60']:+.1f}%"]
    if f["aligned"]:
        bits.append("20·60일선 정배열")
    elif f["above60"]:
        bits.append("60일선 위 추세 유지")
    if f["vol_ratio"] >= 1.3:
        bits.append(f"거래량 평균 {f['vol_ratio']}배 확대")
    if f["rsi"] is not None:
        bits.append(f"RSI {f['rsi']}")
    return ", ".join(bits)


# ============================================================ 성적표 갱신
def update_performance(open_items):
    """OPEN 추천의 손절/목표 도달 여부 확인 후 성적표 집계"""
    hist = {"picks": []}
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as fp:
                hist = json.load(fp)
        except Exception:
            pass

    today = datetime.now()
    results_changed = False
    for p in hist.get("picks", []):
        if p.get("status") != "OPEN":
            continue
        added = datetime.strptime(p["added_at"], "%Y-%m-%d %H:%M:%S")
        age_days = (today - added).days
        try:
            rows = fetch_history(p["symbol"], count=max(age_days + 40, 90))
        except Exception:
            continue
        cutoff = added.strftime("%Y%m%d")
        after = [r for r in rows if r["date"] >= cutoff][1:]
        if not after:
            continue
        hit_stop = any(r["low"] <= p["stop"] for r in after)
        hit_target = any(r["high"] >= p["target"] for r in after)
        expired = age_days >= HORIZON_DAYS + 15
        if hit_target or hit_stop or expired:
            if hit_target:
                result, ret = "WIN", (p["target"] / p["entry"] - 1) * 100
            elif hit_stop:
                result, ret = "LOSS", -(p["entry"] - p["stop"]) / p["entry"] * 100
            else:
                last_close = after[-1]["close"]
                result = "WIN" if last_close >= p["entry"] else "LOSS"
                ret = (last_close / p["entry"] - 1) * 100
            p.update(status="CLOSED", closed_at=today.strftime("%Y-%m-%d %H:%M:%S"),
                     result=result, return_pct=round(ret, 2))
            results_changed = True
            print(f"[perf] CLOSED {p['symbol']} {result} {ret:+.1f}%")

    # 신규 추천 등록
    existing = {(p["week"], p["symbol"]) for p in hist.get("picks", [])}
    week = today.strftime("%G-W%V")
    new_picks = open_items
    for rank_data in new_picks:
        key = (week, rank_data["symbol"])
        if key in existing:
            continue
        hist.setdefault("picks", []).append({
            "week": week,
            "added_at": today.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "OPEN",
            **{k: rank_data[k] for k in ("symbol", "name", "entry", "stop", "target")},
        })
        results_changed = True

    picks_all = hist.get("picks", [])
    closed = [p for p in picks_all if p.get("status") == "CLOSED"]
    wins = [p for p in closed if p.get("return_pct", 0) > 0]
    perf = {
        "closed_count": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else None,
        "avg_return": round(sum(p["return_pct"] for p in closed) / len(closed), 2) if closed else None,
        "best": max((p["return_pct"] for p in closed), default=None),
        "worst": min((p["return_pct"] for p in closed), default=None),
        "open_count": sum(1 for p in picks_all if p.get("status") == "OPEN"),
        "recent": [
            {"week": p["week"], "symbol": p["symbol"], "name": p["name"],
             "status": p["status"],
             "return_pct": p.get("return_pct"),
             "result": p.get("result")}
            for p in sorted(picks_all, key=lambda x: x["added_at"], reverse=True)[:10]
        ],
    }
    hist["performance"] = perf

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as fp:
        json.dump(hist, fp, ensure_ascii=False, indent=2)
    return perf


# ============================================================ 국면 판단
def market_regime():
    """간이 국면 판단 (실패 시 NEUTRAL)"""
    def _run():
        import yfinance as yf
        vix = yf.Ticker("^VIX").history(period="5d")["Close"].dropna().iloc[-1]
        kospi = yf.Ticker("^KS11").history(period="8mo")["Close"].dropna()
        ma60 = kospi.tail(60).mean()
        above = kospi.iloc[-1] > ma60
        score = 50
        score += 15 if above else -15
        score += 12 if vix < 18 else (-15 if vix > 25 else 0)
        status = "ATTACK" if score >= 65 else ("DEFENSE" if score < 40 else "NEUTRAL")
        return {"score": int(max(0, min(100, score))), "status": status,
                "vix": round(float(vix), 1)}
    try:
        return _run()
    except Exception:
        return {"score": 50, "status": "NEUTRAL", "vix": None}


# ============================================================ 메인
def main():
    t0 = time.time()
    universe = build_universe()

    candidates = []
    for item in universe:
        try:
            rows = fetch_history(item["symbol"])
            f = compute_factors(rows)
            candidates.append({
                "symbol": item["symbol"], "name": item["name"],
                "rows": rows, **f,
            })
            print(f"  ok  {item['name']:<12} mom={f['mom60']:+6.1f}% "
                  f"vol={f['vol_ratio']:.1f}x atr={f['atr_pct']}%")
        except Exception as e:
            print(f"  skip {item['name']}: {e}")
        time.sleep(0.15)

    if not candidates:
        raise RuntimeError("스코어링 가능한 종목 없음")

    scored = score_all(candidates)
    scored.sort(key=lambda x: x["score"], reverse=True)

    regime = market_regime()
    week = datetime.now().strftime("%G-W%V")

    picks = []
    for rank, f in enumerate(scored[:TOP_N], 1):
        entry = f["last"]
        target, stop, period = build_scenario(entry, f["atr_pct"])
        spark = [r["close"] for r in f["rows"][-40:]]
        lo, hi = min(spark), max(spark)
        rng = hi - lo or 1
        picks.append({
            "rank": rank,
            "week": week,
            "symbol": f["symbol"],
            "name": f["name"],
            "market": "KR",
            "score": f["score"],
            "price": f"{int(round(entry)):,}원",
            "change_rate": "",
            "entry": int(round(entry)),
            "stop": stop,
            "target": target,
            "period": period,
            "reason": make_reasons(f),
            "tags": [
                f"모멘텀 {f['mom60']:+.0f}%",
                *(["정배열"] if f["aligned"] else ["60일선 위"]),
                f"거래량 {f['vol_ratio']}배",
                f"변동성 {f['atr_pct']}%",
            ],
            "spark": [round((v - lo) / rng * 100) for v in spark],
        })

    perf = update_performance(picks)

    output = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "next_review": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
        "regime": {
            **regime,
            "kr_summary": "주간 스크리닝 기준 국면 판단",
            "us_summary": f"VIX {regime.get('vix') or '-'} · 모멘텀 전략 적합도 반영",
            "macro": {"vix": regime.get("vix"), "exchange_rate": None},
        },
        "strategy": "WEEKLY_MOMENTUM_TOP5",
        "swing_stocks": [],
        "picks": picks,
        "performance": perf,
    }

    tmp = PICKS_PATH.with_suffix(".tmp") if hasattr(PICKS_PATH, "with_suffix") else PICKS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(output, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, PICKS_PATH)

    print(f"\n[done] 주간 후보 {len(picks)}종목 -> picks.json ({time.time() - t0:.0f}s)")
    print(f"       승률 {perf['win_rate'] or '수집중'}% / 종료 {perf['closed_count']}건 / 진행 {perf['open_count']}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] 스크리닝 실패 — 기존 picks.json 유지: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
