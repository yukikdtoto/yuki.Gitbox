"""日足データ取得(無料CSV: ECB参照レート[既定] / Stooq)。

Stooq は 2026-11 時点で自動取得に JavaScript のブラウザ確認(proof-of-work)を
返すため無人運用では使えない(この応答は DataError で止まる)。既定は ECB の
参照レート(毎営業日 16:00 CET 公表、EUR建て)から各ペアを算出する:
  USD_JPY = JPY/USD, EUR_USD = USD, GBP_USD = USD/GBP, AUD_USD = USD/AUD
ECB は終値ではなく 14:15 CET の参照レートで、OHLC は無い(O=H=L=C とする)。

失敗時は必ず例外 (DataError) で止める。空・欠損・古すぎる・形式違いの
データで「ポジション0」と誤判定して全決済する事故を防ぐため、
黙って空の DataFrame を返すことはしない。
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from .instruments import PAIRS

STOOQ_URL = "https://stooq.com/q/d/l/"
STOOQ_SYMBOLS = {
    "USD_JPY": "usdjpy",
    "EUR_USD": "eurusd",
    "GBP_USD": "gbpusd",
    "AUD_USD": "audusd",
}
REQUIRED_COLS = ["Open", "High", "Low", "Close"]


class DataError(RuntimeError):
    pass


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def parse_stooq_csv(text: str, pair: str) -> pd.DataFrame:
    if not text or not text.strip():
        raise DataError(f"{pair}: empty response")
    head = text.strip().splitlines()[0]
    if not head.lower().startswith("date"):
        # "No data" / "Exceeded the daily hits limit" / HTML など
        raise DataError(f"{pair}: unexpected response: {head[:120]!r}")
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:  # noqa: BLE001
        raise DataError(f"{pair}: CSV parse error: {e}") from e
    missing = [c for c in ["Date", *REQUIRED_COLS] if c not in df.columns]
    if missing:
        raise DataError(f"{pair}: missing columns {missing}")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).set_index("Date").sort_index()
    df = df[REQUIRED_COLS].astype(float)
    return df


def validate_daily(df: pd.DataFrame, pair: str, *, today: date | None = None,
                   max_age_days: int = 5, min_rows: int = 150,
                   exclude_today: bool = True) -> pd.DataFrame:
    today = today or _today_utc()
    if df is None or df.empty:
        raise DataError(f"{pair}: no rows")
    if df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")]
    if not df.index.is_monotonic_increasing:
        raise DataError(f"{pair}: index not sorted")
    # 当日(未確定)の足は使わない(ECBの参照レートは公表時点で確定なので除外しない)
    df = df[df.index.date < today] if exclude_today else df[df.index.date <= today]
    if len(df) < min_rows:
        raise DataError(f"{pair}: too few rows ({len(df)} < {min_rows})")
    if df[REQUIRED_COLS].isna().any().any():
        raise DataError(f"{pair}: NaN in OHLC")
    if (df[REQUIRED_COLS] <= 0).any().any():
        raise DataError(f"{pair}: non-positive price")
    if ((df["High"] < df["Low"])).any():
        raise DataError(f"{pair}: High < Low")
    last = df.index[-1].date()
    if (today - last).days > max_age_days:
        raise DataError(f"{pair}: stale data, last bar {last} (today {today})")
    # 1日で30%超の変動はデータ異常とみなす
    jump = df["Close"].pct_change().abs().max()
    if pd.notna(jump) and jump > 0.30:
        raise DataError(f"{pair}: suspicious daily jump {jump:.1%}")
    return df


def fetch_daily(pair: str, start: date, end: date | None = None, *,
                session: requests.Session | None = None, timeout: float = 30.0,
                today: date | None = None, max_age_days: int = 5,
                min_rows: int = 150) -> pd.DataFrame:
    """Stooq から日足を取得。失敗時は DataError。"""
    if pair not in STOOQ_SYMBOLS:
        raise DataError(f"unknown pair {pair}")
    end = end or (today or _today_utc())
    params = {"s": STOOQ_SYMBOLS[pair], "i": "d",
              "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")}
    sess = session or requests.Session()
    try:
        r = sess.get(STOOQ_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise DataError(f"{pair}: request failed: {e}") from e
    if r.status_code != 200:
        raise DataError(f"{pair}: HTTP {r.status_code}")
    df = parse_stooq_csv(r.text, pair)
    return validate_daily(df, pair, today=today, max_age_days=max_age_days, min_rows=min_rows)


ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"


def parse_ecb_csv(text: str) -> pd.DataFrame:
    if not text or not text.lower().startswith("date"):
        raise DataError(f"ECB: unexpected response: {text[:120]!r}")
    df = pd.read_csv(io.StringIO(text), na_values=["N/A"])
    need = ["Date", "USD", "JPY", "GBP", "AUD"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise DataError(f"ECB: missing columns {miss}")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).set_index("Date").sort_index()[need[1:]].astype(float)
    return df


def ecb_to_pairs(ecb: pd.DataFrame) -> dict[str, pd.DataFrame]:
    closes = {
        "USD_JPY": ecb["JPY"] / ecb["USD"],
        "EUR_USD": ecb["USD"],
        "GBP_USD": ecb["USD"] / ecb["GBP"],
        "AUD_USD": ecb["USD"] / ecb["AUD"],
    }
    out = {}
    for p, c in closes.items():
        c = c.dropna()
        out[p] = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c})
    return out


def fetch_ecb_all(start: date, end: date | None = None, *, session: requests.Session | None = None,
                  timeout: float = 60.0, today: date | None = None, max_age_days: int = 5,
                  min_rows: int = 150) -> dict[str, pd.DataFrame]:
    sess = session or requests.Session()
    try:
        r = sess.get(ECB_URL, timeout=timeout)
    except requests.RequestException as e:
        raise DataError(f"ECB: request failed: {e}") from e
    if r.status_code != 200:
        raise DataError(f"ECB: HTTP {r.status_code}")
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        text = z.read(z.namelist()[0]).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise DataError(f"ECB: not a valid zip: {e}") from e
    pairs = ecb_to_pairs(parse_ecb_csv(text))
    out = {}
    for p, df in pairs.items():
        df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        out[p] = validate_daily(df, p, today=today, max_age_days=max_age_days,
                                min_rows=min_rows, exclude_today=False)
    return out


def fetch_all(start: date, end: date | None = None, source: str = "ecb", **kw) -> dict[str, pd.DataFrame]:
    """全ペア取得。1つでも失敗したら全体を例外で止める(部分データで取引しない)。"""
    if source == "ecb":
        return fetch_ecb_all(start, end, **kw)
    if source == "stooq":
        return {p: fetch_daily(p, start, end, **kw) for p in PAIRS}
    raise DataError(f"unknown source {source}")


def closes_frame(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """終値を日付で揃える(共通営業日のみ)。"""
    df = pd.concat({p: d["Close"] for p, d in data.items()}, axis=1, join="inner")
    if df.empty:
        raise DataError("no overlapping dates across pairs")
    return df


# ---- バックテスト用キャッシュ(再現性のため CSV を保存) ----
def save_cache(data: dict[str, pd.DataFrame], cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for p, df in data.items():
        df.to_csv(cache_dir / f"{p}.csv", index_label="Date")


def load_cache(cache_dir: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for p in PAIRS:
        f = cache_dir / f"{p}.csv"
        if not f.exists():
            raise DataError(f"cache missing: {f}")
        out[p] = parse_stooq_csv(f.read_text(), p)
    return out


def five_years_ago(today: date | None = None) -> date:
    today = today or _today_utc()
    return today - timedelta(days=int(365.25 * 5) + 200)  # +200日はウォームアップ用
