# -*- coding: utf-8 -*-
"""
[한/미 주식 데이터 수집 및 AI 분석 JSON 생성 파이프라인]
- 실행 결과: 스크립트 위치에 data.json 생성 (index.html에서 fetch('./data.json')으로 사용)

# 필요 패키지 설치:
# pip install yfinance requests beautifulsoup4 pandas lxml
"""

import json
import math
import os
import re
import traceback
from datetime import datetime

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
}

# ============================================================
# 관심종목 설정: batch/watchlist.json 에서 로드 (코드 수정 불필요)
# category: SAFE(안전형) / MOMENTUM(공격형) / DEFENSIVE·VALUE(보수/가치형)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "batch", "watchlist.json")

# watchlist.json 이 없거나 비어 있을 때 사용하는 기본값
DEFAULT_KR_WATCHLIST = [
    {"symbol": "005930", "name": "삼성전자",   "category": "SAFE"},
    {"symbol": "000660", "name": "SK하이닉스", "category": "SAFE"},
]
DEFAULT_US_WATCHLIST = [
    {"symbol": "NVDA", "name": "NVIDIA", "category": "MOMENTUM"},
    {"symbol": "MSFT", "name": "Microsoft", "category": "DEFENSIVE"},
]


def safe(fn, default=None):
    """예외를 삼키고 기본값을 반환하는 공통 래퍼"""
    try:
        return fn()
    except Exception:
        traceback.print_exc()
        return default


def load_watchlist():
    """batch/watchlist.json -> (KR_WATCHLIST, US_WATCHLIST)
    파일이 없거나 파싱 실패 시 내장 기본 목록으로 폴백."""
    def _read():
        with open(WATCHLIST_PATH, encoding="utf-8") as f:
            wl = json.load(f)
        kr = [x for x in wl.get("kr", []) if x.get("symbol")]
        us = [x for x in wl.get("us", []) if x.get("symbol")]
        if not kr and not us:
            raise ValueError("watchlist.json is empty")
        return kr or DEFAULT_KR_WATCHLIST, us or DEFAULT_US_WATCHLIST

    kr, us = safe(_read, default=(None, None))
    if kr is None or us is None:
        print(f"[warn] {WATCHLIST_PATH} 로드 실패 — 기본 관심종목 사용")
        return DEFAULT_KR_WATCHLIST, DEFAULT_US_WATCHLIST
    print(f"[ok] watchlist 로드 완료 (KR {len(kr)} / US {len(us)})")
    return kr, us


KR_WATCHLIST, US_WATCHLIST = load_watchlist()


# ============================================================
# 1) 매크로 / 미국 시장 지수 (yfinance)
# ============================================================
# yfinance 요청 타임아웃(초)
YF_TIMEOUT = 15
# 데이터 비어있을 시 순차 폴백 기간
FALLBACK_PERIODS = ["6mo", "3mo", "1mo"]


def _yf_session():
    """타임아웃이 설정된 requests 세션 (yfinance 내부 HTTP 요청에 적용)"""
    session = requests.Session()
    orig_request = session.request

    def _request(*args, **kwargs):
        kwargs.setdefault("timeout", YF_TIMEOUT)
        return orig_request(*args, **kwargs)

    session.request = _request
    return session


_YF_SESSION = None


def fetch_index_history(ticker, period="6mo"):
    """yfinance 지수/종목 종가 시리즈 수집 (timeout + 빈 데이터 시 폴백)"""
    global _YF_SESSION
    if yf is None:
        return None

    def _run():
        global _YF_SESSION
        if _YF_SESSION is None:
            _YF_SESSION = _yf_session()

        tk = yf.Ticker(ticker, session=_YF_SESSION)

        # 1순위: 요청된 period, 실패 시 폴백 기간 순차 시도
        periods = [period] if period in FALLBACK_PERIODS else [period] + FALLBACK_PERIODS
        last_err = None
        for p in periods:
            try:
                hist = tk.history(period=p, interval="1d", timeout=YF_TIMEOUT)
            except TypeError:
                # 구버전 yfinance는 timeout 파라미터 미지원
                hist = safe(lambda: tk.history(period=p, interval="1d"))
            except Exception as e:
                last_err = e
                continue
            if hist is not None and not hist.empty and "Close" in hist.columns:
                close = hist["Close"].dropna()
                if not close.empty:
                    return close
        if last_err:
            raise last_err
        raise ValueError(f"empty history: {ticker}")

    return safe(_run)


def ma(series, window):
    if series is None or len(series) < window:
        return None
    return round(float(series.tail(window).mean()), 2)


def pct(a, b):
    if a is None or b in (None, 0) or (isinstance(b, float) and math.isnan(b)):
        return None
    return (a - b) / b * 100


def fetch_macro():
    """VIX, USD/KRW 환율 및 미국 지수 상태 수집"""
    result = {"vix": None, "exchange_rate": None,
              "us": {}, "kr_index": {}}

    vix_close = fetch_index_history("^VIX", "5d")
    if vix_close is not None:
        result["vix"] = round(float(vix_close.iloc[-1]), 1)

    fx_hist = fetch_index_history("KRW=X", "5d")
    if fx_hist is not None:
        result["exchange_rate"] = round(float(fx_hist.iloc[-1]), 1)

    for name, ticker in [("sp500", "^GSPC"), ("nasdaq", "^IXIC"), ("kospi", "^KS11")]:
        close = fetch_index_history(ticker)
        if close is not None:
            last = float(close.iloc[-1])
            result["us" if name != "kospi" else "kr_index"][name] = {
                "close": round(last, 2),
                "ma50": ma(close, 50),
                "ma60": ma(close, 60),
                "ma200": ma(close, 200),
                "chg_1m": round(pct(last, float(close.iloc[-21])) or 0, 2),
            }
    return result


# ============================================================
# 2) 한국 주식 (네이버 금융 비공식 엔드포인트 크롤링)
# ============================================================
def kr_daily_history(code, count=120):
    """네이버 fchart 일봉 CSV -> 종가 시리즈"""
    url = ("https://fchart.stock.naver.com/sise.nhn"
           f"?symbol={code}&timeframe=day&count={count}&requestType=0")
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()

    lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
    rows = []
    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) >= 6 and re.match(r"^\d{8}$", cols[0]):
            rows.append({"date": cols[0],
                         "open": float(cols[1]), "high": float(cols[2]),
                         "low": float(cols[3]), "close": float(cols[4]),
                         "volume": float(cols[5])})
    if not rows:
        raise ValueError(f"no candles for {code}")
    df = pd.DataFrame(rows)
    return df["close"], int(df.iloc[-1]["volume"])


def kr_per_pbr(code):
    """네이버 종목 메인 페이지 HTML에서 PER / PBR 파싱 (예외 강화)"""
    def _fetch():
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.text

    html = safe(_fetch)
    if not html:
        print(f"   [warn] PER/PBR 페이지 요청 실패: {code}")
        return None, None

    def _grab(key):
        try:
            m = re.search(r'id="%s"[^>]*>\s*([\d,.]+)' % key, html)
            if not m:
                raise ValueError(f"regex no match: {key}")
            val = float(m.group(1).replace(",", ""))
            if math.isnan(val) or math.isinf(val) or val < 0:
                raise ValueError(f"invalid value: {key}={val}")
            return round(val, 2)
        except Exception:
            print(f"   [warn] PER/PBR 정규식 파싱 실패 ({key}) code={code}")
            return None  # 파싱 실패 시 안전하게 기본값 반환

    return _grab("_per"), _grab("_pbr")


def kr_investor_netbuy(code):
    """네이버 외국인/기관 매매동향 테이블에서 최근일 순매수 금액(억원) 파싱"""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()

    tables = pd.read_html(resp.text)
    for tb in tables:
        if tb is None or tb.empty:
            continue
        cols = [str(c) for c in tb.columns]
        if any("순매수" in c for c in cols):
            date_col_idx = 0
            row = tb.dropna(how="all").iloc[-1]
            values = {}
            for c in tb.columns:
                cs = str(c)
                if "외국인" in cs and "순매수" in cs:
                    values["foreigner"] = _to_eok(row[c])
                elif "기관" in cs and "순매수" in cs:
                    values["inst"] = _to_eok(row[c])
            if values:
                values["date"] = str(row.iloc[date_col_idx])
                return values
    return {}


def _to_eok(v):
    """테이블 값 -> 억원 단위 숫자"""
    try:
        if isinstance(v, str):
            v = re.sub(r"[^\d.-]", "", v)
        return round(float(v) / 100_000_000, 1)  # 원 -> 억원
    except Exception:
        return None


def build_kr_stock(item, us_ctx):
    code = item["symbol"]
    name = item["name"]
    category = item.get("category", "SAFE")

    close, volume = kr_daily_history(code)
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else last
    chg_rate = (last - prev) / prev * 100

    ma20 = ma(close, 20)
    ma60 = ma(close, 60)
    ma120 = ma(close, 120)

    per, pbr = kr_per_pbr(code)
    inv = kr_investor_netbuy(code)
    frg = inv.get("foreigner")
    ins = inv.get("inst")

    # ---- reason_1 : 재무/가치 ----
    val_parts = []
    if per is not None:
        val_parts.append(f"PER {per:.1f}배")
    if pbr is not None:
        val_parts.append(f"PBR {pbr:.1f}배")
    val_txt = ", ".join(val_parts) if val_parts else "밸류에이션 정보 확인 실패"
    reason_1 = f"[재무/가치] 현재 {val_txt} 기준 평가. "
    if pbr is not None and pbr < 1.5:
        reason_1 += "PBR이 낮아 저평가 구간으로 판단되어 안전마진 확보."
    elif per is not None and per < 15:
        reason_1 += "업종 대비 합리적 수준의 수익률 밸류에이션 유지."
    else:
        reason_1 += "실적 모멘텀 대비 밸류에이션 부담은 중립적 수준."

    # ---- reason_2 : 기술/수급 ----
    tech_parts = []
    if ma60 and last >= ma60:
        tech_parts.append("60일 이평선 상방 지지")
    elif ma60:
        tech_parts.append("60일 이평선 이탈 상태로 되돌림 관심")
    if ma120 and last >= ma120:
        tech_parts.append("장기 추세(120일선) 우위 유지")
    sup_txt = " / ".join(tech_parts) if tech_parts else "추세 판단 데이터 부족"

    supply_parts = []
    if frg is not None:
        supply_parts.append(f"외국인 {'순매수' if frg > 0 else '순매도'} {abs(frg)}억원")
    if ins is not None:
        supply_parts.append(f"기관 {'순매수' if ins > 0 else '순매도'} {abs(ins)}억원")
    sup2_txt = ", ".join(supply_parts) if supply_parts else "수급 데이터 확인 실패"

    both_buy = (frg or 0) > 0 and (ins or 0) > 0
    reason_2 = (f"[기술/수급] {sup_txt}. 최근({inv.get('date', '')}) {sup2_txt}"
                + (" → 기관·외국인 동반 유입으로 수급 강세." if both_buy else "."))

    # ---- reason_3 : 한/미 산업 연계 ----
    sp = us_ctx.get("us", {}).get("sp500", {})
    fx = us_ctx.get("exchange_rate")
    link = []
    if sp.get("chg_1m") is not None:
        link.append(f"S&P500 최근 1개월 {sp['chg_1m']:+.1f}% 흐름과 연동")
    if fx:
        link.append(f"환율 {fx}원 수준은 {'수출기업 실적에 우호적' if fx > 1300 else '원화 강세로 수출 마진 압박 가능'}")
    link.append("글로벌 반도체/IT 공급망 회복 수혜 후보군")
    reason_3 = "[한/미 연계] " + ". ".join(link) + "."

    # ---- scenario ----
    target = last * (1.12 if category == "MOMENTUM" else 1.08)
    stop = last * (0.94 if category == "MOMENTUM" else 0.93)
    period = "1~2개월" if category == "SAFE" else "2~3개월"

    return {
        "symbol": code,
        "name": name,
        "market": "KRX",
        "category": category,
        "price": f"{int(round(last)):,}원",
        "change_rate": f"{'+' if chg_rate >= 0 else ''}{chg_rate:.1f}%",
        "analysis_report": {
            "reason_1": reason_1,
            "reason_2": reason_2,
            "reason_3": reason_3,
            "scenario": {
                "target": f"{int(round(target)):,}원",
                "stop": f"{int(round(stop)):,}원",
                "period": period,
            },
        },
    }


# ============================================================
# 3) 미국 주식 (yfinance)
# ============================================================
def build_us_stock(item, us_ctx):
    symbol = item["symbol"]
    name = item["name"]
    category = item.get("category", "SAFE")

    tk = yf.Ticker(symbol)
    info = safe(lambda: tk.info, {}) or {}
    hist = fetch_index_history(symbol, "1y")

    last = None
    chg_rate = 0.0
    ma50 = ma200 = None
    if hist is not None:
        last = float(hist.iloc[-1])
        prev = float(hist.iloc[-2]) if len(hist) > 1 else last
        chg_rate = (last - prev) / prev * 100
        ma50 = ma(hist, 50)
        ma200 = ma(hist, 200)

    pe = safe(lambda: info.get("trailingPE"), None)
    fwd_pe = safe(lambda: info.get("forwardPE"), None)
    pbr = safe(lambda: info.get("priceToBook"), None)
    rev_growth = safe(lambda: info.get("revenueGrowth"), None)

    price_str = f"${last:,.2f}" if last else "-"
    change_str = f"{'+' if chg_rate >= 0 else ''}{chg_rate:.1f}%"

    # ---- reason_1 ----
    parts = []
    if pe:
        parts.append(f"PER {pe:.1f}배")
    if fwd_pe:
        parts.append(f"Forward PER {fwd_pe:.1f}배")
    if pbr:
        parts.append(f"PBR {pbr:.1f}배")
    if rev_growth is not None:
        parts.append(f"매출 성장률 YoY {rev_growth * 100:+.1f}%")
    reason_1 = (f"[재무/가치] {', '.join(parts) if parts else '핵심 재무 지표 확인 실패'}. "
                + ("고성장 대비 고평가 논란에도 실적 모멘텀이 밸류에이션을 지지."
                   if rev_growth and rev_growth > 0.2 else
                   "안정적인 이익 구조와 현금흐름이 가치 방어력을 제공."))

    # ---- reason_2 ----
    tech = []
    if ma50 and last and last > ma50:
        tech.append("50일 이동평균선 위 상승 추세")
    if ma200 and last and last > ma200:
        tech.append("200일선 장기 강세 유지")
    inst_own = safe(lambda: info.get("heldPercentInstitutions"), None)
    if inst_own:
        tech.append(f"기관 보유비중 {inst_own * 100:.1f}%로 수급 안정적")
    reason_2 = "[기술/수급] " + (" / ".join(tech) if tech else "기술적 지표 확인 실패") + "."

    # ---- reason_3 ----
    sp = us_ctx.get("us", {}).get("sp500", {})
    vix = us_ctx.get("vix")
    link = []
    if sp.get("chg_1m") is not None:
        link.append(f"미국 대표지수(S&P500) 1개월 {sp['chg_1m']:+.1f}%와 동행하는 베타 성격")
    if vix is not None:
        link.append(f"VIX {vix} 수준의 변동성 환경에서 {'위험자산 선호 유리' if vix < 20 else '변동성 확대 리스크 존재'}")
    link.append("글로벌 빅테크 CAPEX 및 AI 인프라 투자 사이클 직결 종목")
    reason_3 = "[한/미 연계] " + ". ".join(link) + "."

    base = last if last else 100
    target = base * (1.15 if category == "MOMENTUM" else 1.08)
    stop = base * (0.92 if category == "MOMENTUM" else 0.94)
    period = "1~2개월" if category == "SAFE" else "2~3개월"

    return {
        "symbol": symbol,
        "name": name,
        "market": "US",
        "category": category,
        "price": price_str,
        "change_rate": change_str,
        "analysis_report": {
            "reason_1": reason_1,
            "reason_2": reason_2,
            "reason_3": reason_3,
            "scenario": {
                "target": f"${target:,.2f}",
                "stop": f"${stop:,.2f}",
                "period": period,
            },
        },
    }


# ============================================================
# 4) 시장 국면(Regime) 점수 산출
# ============================================================
def build_market_regime(macro):
    score = 50

    vix = macro.get("vix")
    if vix is not None:
        if vix < 15:
            score += 15
        elif vix < 20:
            score += 10
        elif vix < 25:
            score -= 5
        else:
            score -= 20

    sp = macro.get("us", {}).get("sp500", {})
    if sp:
        if sp.get("ma50") and sp.get("ma200"):
            score += 15 if sp["ma50"] > sp["ma200"] else -10
        if sp.get("ma50") and sp.get("close"):
            score += 5 if sp["close"] > sp["ma50"] else -5

    kospi = macro.get("kr_index", {}).get("kospi", {})
    if kospi:
        if kospi.get("ma60") and kospi.get("close"):
            score += 10 if kospi["close"] > kospi["ma60"] else -10
        if kospi.get("chg_1m"):
            score += min(max(kospi["chg_1m"], -10), 10) / 2

    score = int(max(0, min(100, score)))
    status = "ATTACK" if score >= 65 else ("DEFENSE" if score < 40 else "NEUTRAL")

    kr_txt = "데이터 부족으로 판단 보류"
    if kospi.get("close") and kospi.get("ma60"):
        pos = "지지" if kospi["close"] > kospi["ma60"] else "이탈"
        kr_txt = (f"KOSPI({kospi['close']:,.0f}) 60일선 {pos}, "
                  f"최근 1개월 {kospi.get('chg_1m', 0):+.1f}% 흐름")

    us_txt = "데이터 부족으로 판단 보류"
    if sp.get("close"):
        trend = "단기 상승 추세" if sp.get("ma50") and sp["close"] > sp["ma50"] else "단기 조정 국면"
        us_txt = f"S&P500({sp['close']:,.0f}) {trend}"

    return {
        "score": score,
        "status": status,
        "kr_summary": kr_txt,
        "us_summary": us_txt,
        "macro": {
            "vix": vix if vix is not None else 0,
            "exchange_rate": macro.get("exchange_rate") or 0,
        },
    }


# ============================================================
# 5) 메인 실행
# ============================================================
def main():
    print("[1/4] 매크로 & 지수 데이터 수집 중...")
    macro = fetch_macro() or {}

    print("[2/4] 한국 주식 데이터 수집 중 (네이버 금융)...")
    kr_stocks = []
    for item in KR_WATCHLIST:
        stock = safe(lambda i=item: build_kr_stock(i, macro))
        if stock:
            kr_stocks.append(stock)
            print(f"   OK  {item['name']}")
        else:
            print(f"   FAIL {item['name']}")

    print("[3/4] 미국 주식 데이터 수집 중 (yfinance)...")
    us_stocks = []
    for item in US_WATCHLIST:
        stock = safe(lambda i=item: build_us_stock(i, macro))
        if stock:
            us_stocks.append(stock)
            print(f"   OK  {item['name']}")
        else:
            print(f"   FAIL {item['name']}")

    print("[4/4] 시장 국면 분석 및 JSON 저장 중...")
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_regime": build_market_regime(macro),
        "swing_stocks": kr_stocks + us_stocks,
    }

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{out_path} 생성 완료!")
    print(f"   updated_at : {output['updated_at']}")
    print(f"   regime     : score={output['market_regime']['score']} ({output['market_regime']['status']})")
    print(f"   stocks     : {len(output['swing_stocks'])}개")


if __name__ == "__main__":
    main()
