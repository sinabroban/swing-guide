# -*- coding: utf-8 -*-
"""
SwingGuide 배치 파이프라인
- 네이버 금융(KRX) / Yahoo Finance(US) 데이터 수집
- 기술적 지표(MA/RSI/거래량) 및 시장 국면 스코어 계산
- LLM(선택)으로 3대 추천 근거 문장 생성, 실패 시 룰 기반 템플릿 폴백
- data/report.json 생성 (실패 시 이전 성공 데이터 보존 + stale 플래그)

사용법:
  python generate_report.py                 # 전체 실행
  set OPENAI_API_KEY=sk-...                 # LLM 근거 생성 활성화(선택)
  set OPENAI_BASE_URL=https://api.openai.com/v1
  set OPENAI_MODEL=gpt-4o-mini
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

BATCH_DIR = Path(__file__).resolve().parent
ROOT_DIR = BATCH_DIR.parent
REPORT_PATH = ROOT_DIR / "data" / "report.json"
WATCHLIST_PATH = BATCH_DIR / "watchlist.json"

NAVER_CHART_URL = "https://fchart.stock.naver.com/sise.nhn"
TIMEOUT = 15
RETRY = 2

CATEGORY_SCENARIO = {
    # category: (목표 상승률, 손절 폭, 보유기간)
    "SAFE": (0.10, 0.05, "2~3개월"),
    "MOMENTUM": (0.15, 0.07, "1~1.5개월"),
    "VALUE": (0.08, 0.05, "2~3개월"),
}

LLM_SYSTEM_PROMPT = (
    "당신은 한국/미국 주식 스윙 투자 리포트 작성가입니다. "
    "제공된 객관적 지표만을 근거로, 일방적 매수 추천이 아니라 "
    "투자자 스스로 판단하도록 돕는 참고용 브리핑을 작성합니다. "
    "반드시 아래 JSON 형식으로만 답변하세요:\n"
    '{"reason_1_fundamental": "...", "reason_2_technical": "...", '
    '"reason_3_macro_link": "...", "kr_summary": "...", "us_summary": "..."} '
    "각 필드는 한국어 한두 문장, 단정적 미래 예측 금지."
)


# ---------------------------------------------------------------- 수집 계층

def fetch_json(url, params=None):
    last_err = None
    for _ in range(RETRY + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(1.5)
    raise ConnectionError(f"fetch failed: {url} ({last_err})")


def fetch_krx_daily(symbol, count=130):
    """네이버 금융 차트 API로 KRX 일봉 수집 -> [{date, open, high, low, close, volume}]"""
    raw = fetch_json(NAVER_CHART_URL, {
        "symbol": symbol, "timeframe": "day",
        "count": count, "requestType": "0",
    })
    rows = raw.get("result", {}).get("output", [])
    if len(rows) < 70:
        raise ValueError(f"KRX {symbol}: 데이터 부족({len(rows)}행)")
    out = []
    for row in rows:
        try:
            out.append({
                "date": str(row[0]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, IndexError):
            continue
    if not out:
        raise ValueError(f"KRX {symbol}: 파싱 실패")
    return out


def fetch_us_daily(symbol, period="6mo"):
    """Yahoo Finance 일봉 수집 (yfinance)"""
    import yfinance as yf
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty or len(df) < 70:
        raise ValueError(f"US {symbol}: 데이터 부족")
    out = []
    for idx, row in df.iterrows():
        vol = row.get("Volume") or 0
        out.append({
            "date": idx.strftime("%Y-%m-%d"),
            "close": float(row["Close"]),
            "volume": float(vol),
        })
    return out


def fetch_macro():
    """VIX + 원/달러 환율 (Yahoo Finance)"""
    import yfinance as yf
    vix, fx = None, None
    try:
        h = yf.Ticker("^VIX").history(period="5d", interval="1d")
        if h is not None and not h.empty:
            vix = round(float(h["Close"].iloc[-1]), 1)
    except Exception:
        pass
    try:
        h = yf.Ticker("USDKRW=X").history(period="5d", interval="1d")
        if h is not None and not h.empty:
            fx = round(float(h["Close"].iloc[-1]), 1)
    except Exception:
        pass
    return {"vix": vix, "exchange_rate": fx}


def fetch_index_daily(ticker):
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="6mo", interval="1d")
    if df is None or df.empty:
        return []
    return [float(c) for c in df["Close"]]


# ---------------------------------------------------------------- 분석 계층

def sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def analyze_series(daily):
    """일봉 리스트 -> 가격/등락률/지표 딕셔너리"""
    closes = [d["close"] for d in daily]
    price = closes[-1]
    prev = closes[-2] if len(closes) > 1 else price
    return {
        "price": round(price, 2),
        "change_rate": round((price / prev - 1) * 100, 2),
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
        "ma120": sma(closes, 120),
        "rsi14": rsi(closes),
        "vol_ratio": _vol_ratio(daily),
        "above_ma20": price >= (sma(closes, 20) or price),
        "above_ma60": price >= (sma(closes, 60) or price),
    }


def _vol_ratio(daily):
    vols = [d["volume"] for d in daily if d["volume"]]
    if len(vols) < 21:
        return None
    avg20 = sum(vols[-21:-1]) / 20
    return round(vols[-1] / avg20, 2) if avg20 else None


def market_regime_score(kr_closes, us_closes, macro):
    """0~100 국면 점수: 지수의 이동평균 위치 + VIX"""
    score = 50
    for closes in (kr_closes, us_closes):
        if not closes:
            continue
        px = closes[-1]
        ma60 = sma(closes, 60)
        ma20 = sma(closes, 20)
        if ma60 and px > ma60:
            score += 12
        elif ma60 and px < ma60:
            score -= 12
        if ma20 and px > ma20:
            score += 6
        else:
            score -= 6
    vix = macro.get("vix")
    if vix is not None:
        if vix < 18:
            score += 12
        elif vix < 24:
            score += 4
        elif vix < 30:
            score -= 8
        else:
            score -= 20
    return max(0, min(100, int(score)))


def regime_status(score):
    if score >= 65:
        return "ATTACK"
    if score >= 45:
        return "CAUTION"
    return "DEFENSE"


# ---------------------------------------------------------------- 근거 생성 계층

def build_stock_context(stock, m):
    return (
        f"종목: {stock['name']}({stock['symbol']}, {stock['market']}), "
        f"성향분류: {stock['category']}, 현재가: {m['price']}, 등락률: {m['change_rate']}%, "
        f"MA20: {round(m['ma20'], 2) if m['ma20'] else 'N/A'}, "
        f"MA60: {round(m['ma60'], 2) if m['ma60'] else 'N/A'}, "
        f"RSI14: {m['rsi14']}, 거래량비율(당일/20일평균): {m['vol_ratio']}"
    )


def llm_generate_reasons(stocks_with_metrics, regime_score, status, macro):
    """LLM으로 3대 근거 + 국면 요약 일괄 생성. 실패 시 None 반환."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    lines = [build_stock_context(s, m) for s, m in stocks_with_metrics]
    user_prompt = (
        f"시장국면점수: {regime_score}/100 ({status}), "
        f"VIX: {macro.get('vix')}, 원달러환율: {macro.get('exchange_rate')}\n"
        + "\n".join(lines)
        + "\n\n각 종목별 reason_1~3과 kr_summary/us_summary를 작성하세요. "
          "keys는 종목 심볼을 추가로 붙여 다음 구조로: "
          '{"stocks": {"<symbol>": {"reason_1_fundamental": ..., }}, "kr_summary": ..., "us_summary": ...}'
    )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=90)
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        print("[warn] LLM 생성 실패 — 룰 기반 템플릿으로 대체합니다.")
        traceback.print_exc()
        return None


def template_reasons(stock, m, macro):
    """룰 기반 폴백: 계산된 지표에서 근거 문장 자동 조립"""
    name = stock["name"]
    tech_bits = []
    if m["above_ma20"]:
        tech_bits.append("20일 이동평균선 위 안정권")
    if m["above_ma60"]:
        tech_bits.append("60일 이동평균선 지지")
    if m["rsi14"] is not None:
        tech_bits.append(f"RSI {m['rsi14']}({('과열 유의' if m['rsi14'] > 70 else '중립·과매도 아님' if m['rsi14'] >= 35 else '단기 과매도')})")
    if m["vol_ratio"]:
        tech_bits.append(f"평균 대비 거래량 {m['vol_ratio']}배")

    fund_by_cat = {
        "SAFE": "실적 개선 흐름이 유지되고 밸류에이션 부담이 크지 않은 주도주 구간으로 판단됩니다.",
        "MOMENTUM": "성장 모멘텀이 강하지만 변동성이 커 포지션 관리가 중요한 구간입니다.",
        "VALUE": "PER/PBR 기준 저평가 구간으로 하방 방어력이 상대적으로 높은 가치주입니다.",
    }

    fx = macro.get("exchange_rate")
    macro_txt = []
    if stock["market"] == "US":
        macro_txt.append("미국 고금리·달러 강세 국면에서 실적 통화 효과 확인 필요")
        macro_txt.append("관련 국내 밸류체인주와 동반 변동성 확대 가능성")
    else:
        macro_txt.append("원/달러 환율 {}원대{} 수출 마진에 영향".format(
            int(fx) if fx else "1,300",
            "(참고치 없음)" if not fx else ""))
        macro_txt.append("미국 동종 산업 지수 흐름과 공급망 연동성 높음")

    return {
        "reason_1_fundamental": fund_by_cat[stock["category"]],
        "reason_2_technical": ", ".join(tech_bits) if tech_bits else "추세 지표 판단 데이터 부족",
        "reason_3_macro_link": ". ".join(macro_txt) + ".",
    }


def build_scenario(price, category):
    up, down, hold = CATEGORY_SCENARIO[category]
    target = price * (1 + up)
    stop = price * (1 - down)
    digits = 2 if price < 1000 else 0
    return {
        "target_price": round(target, digits),
        "stop_loss": round(stop, digits),
        "holding_period": hold,
    }


# ---------------------------------------------------------------- 파이프라인

def load_watchlist():
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        wl = json.load(f)
    stocks = [
        dict(item, market="KRX") for item in wl.get("kr", [])
    ] + [
        dict(item, market="US") for item in wl.get("us", [])
    ]
    if not stocks:
        raise ValueError("watchlist.json 이 비어 있습니다.")
    return stocks


def collect_all(watchlist):
    results, errors = [], []
    for s in watchlist:
        try:
            if s["market"] == "KRX":
                daily = fetch_krx_daily(s["symbol"])
            else:
                daily = fetch_us_daily(s["symbol"])
            results.append((s, analyze_series(daily)))
            print(f"[ok]   {s['market']:3s} {s['name']}")
        except Exception as e:
            errors.append(f"{s['symbol']}({s['name']}): {e}")
            print(f"[fail] {s['market']:3s} {s['name']} -> {e}")
    if not results:
        raise RuntimeError("모든 종목 수집 실패: " + "; ".join(errors))
    return results, errors


def main():
    watchlist = load_watchlist()
    stocks_metrics, collect_errors = collect_all(watchlist)

    macro = fetch_macro()
    try:
        kr_idx = fetch_index_daily("^KS11")
    except Exception:
        kr_idx = []
    try:
        us_idx = fetch_index_daily("^GSPC")
    except Exception:
        us_idx = []

    score = market_regime_score(kr_idx, us_idx, macro)
    status = regime_status(score)

    llm_out = llm_generate_reasons(stocks_metrics, score, status, macro)

    swing_stocks = []
    for s, m in stocks_metrics:
        reasons = None
        if llm_out and isinstance(llm_out.get("stocks"), dict):
            reasons = llm_out["stocks"].get(s["symbol"])
        if not reasons or not all(reasons.get(k) for k in
                                  ("reason_1_fundamental", "reason_2_technical", "reason_3_macro_link")):
            reasons = template_reasons(s, m, macro)

        swing_stocks.append({
            "symbol": s["symbol"],
            "name": s["name"],
            "market": s["market"],
            "category": s["category"],
            "price": m["price"],
            "change_rate": m["change_rate"],
            "analysis_report": {
                **reasons,
                "scenario": build_scenario(m["price"], s["category"]),
            },
        })

    kr_summary = (llm_out or {}).get("kr_summary") or \
        f"KOSPI 국면 점수 {score}점 — {'지지 구간' if status == 'ATTACK' else '관망 필요' if status == 'CAUTION' else '수비 자세 권고'}"
    us_summary = (llm_out or {}).get("us_summary") or \
        f"S&P500 대비 VIX {macro.get('vix') or 'N/A'} — {'위험선호 흐름' if status == 'ATTACK' else '변동성 주의'}"

    report = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stale": bool(collect_errors),
        "collect_errors": collect_errors,
        "market_regime": {
            "score": score,
            "status": status,
            "kr_summary": kr_summary,
            "us_summary": us_summary,
            "macro": macro,
        },
        "swing_stocks": swing_stocks,
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPORT_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REPORT_PATH)  # 원자적 교체: 쓰다 실패해도 이전 파일 보존

    print(f"\n[done] {len(swing_stocks)}종목 리포트 생성 -> {REPORT_PATH}")
    if collect_errors:
        print(f"[warn] 일부 실패(stale=true): {collect_errors}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 전체 실패 시: 기존 report.json을 건드리지 않음 (폴백 유지)
        print(f"[error] 배치 전체 실패 — 기존 report.json 유지: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
