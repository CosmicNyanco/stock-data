"""毎日の株価取得。

保有銘柄(holdings.csv)と監視銘柄(watchlist.csv = SBIのスクリーニングCSV)について、
始値・終値・前日比・買い時の指標を取得し、prices.csv と status.json に出力する。
ニュースの要約や判断は、このスクリプトでは行わない(Claudeの定期実行が担当)。
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

JST = ZoneInfo("Asia/Tokyo")
STALE_DAYS = 30  # 監視銘柄CSVがこの日数更新されなければ警告
SLEEP_SEC = 1.0  # 連続アクセスを避ける待ち時間
HOLDINGS_FILE = "extra_codes.csv"  # 公開リポジトリ用: コードのみ(株数・取得単価は置かない)
WATCHLIST_FILE = "watchlist.csv"

# 買い時の判定(3条件のうち2つ以上で「買い検討」)
DEV25_LIMIT = -5.0  # 25日移動平均線からの乖離(%)がこれ以下
HIGH52_LIMIT = -10.0  # 52週高値からの下落率(%)がこれ以下
RSI_LIMIT = 35.0  # RSI(14日)がこれ以下


def read_text(path):
    """UTF-8(BOM付き/なし)とShift_JIS(cp932)の両方に対応して読む。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを判別できません")


def to_float(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_holdings():
    if not os.path.exists(HOLDINGS_FILE):
        return {}
    result = {}
    for r in csv.DictReader(io.StringIO(read_text(HOLDINGS_FILE))):
        code = (r.get("code") or "").strip()
        if code:
            result[code] = {
                "shares": to_float(r.get("shares")),
                "cost": to_float(r.get("cost")),
                "account": (r.get("account") or "").strip(),
            }
    return result


def load_watchlist():
    """SBIのスクリーニング結果CSVを読む。"""
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    result = {}
    for r in csv.DictReader(io.StringIO(read_text(WATCHLIST_FILE))):
        code = (r.get("コード") or "").strip()
        if not code:
            continue
        per = to_float(r.get("PER(株価収益率)(倍)"))
        pbr = to_float(r.get("PBR(株価純資産倍率)(倍)"))
        result[code] = {
            "name": (r.get("銘柄名") or "").strip(),
            "market": (r.get("市場") or "").strip(),
            "per": per,
            "pbr": pbr,
            "pbr_x_per": round(per * pbr, 1) if per and pbr else None,
            "yield_pct": to_float(r.get("配当利回り(%)")),
            "roe_pct": to_float(r.get("ROE(自己資本利益率)(%)")),
            "equity_ratio_pct": to_float(r.get("自己資本比率(%)")),
            "sales_chg_pct": to_float(r.get("売上高変化率(%)")),
            "profit_chg_pct": to_float(r.get("経常利益変化率(%)")),
        }
    return result


def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return float(100 - 100 / (1 + rs))


def analyze(df):
    """株価の履歴から、始値・終値と買い時の指標を計算する。"""
    df = df.dropna(subset=["Close"])
    if len(df) < 2:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["Close"])
    prev_close = float(prev["Close"])
    out = {
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
        "open": round(float(last["Open"]), 2),
        "close": round(close, 2),
        "prev_close": round(prev_close, 2),
        "change_pct": round((close / prev_close - 1) * 100, 2),
        "dev25_pct": None,
        "high52_drop_pct": None,
        "rsi14": None,
    }
    if len(df) >= 25:
        ma25 = float(df["Close"].tail(25).mean())
        out["dev25_pct"] = round((close / ma25 - 1) * 100, 2)
    if len(df) >= 30:
        high52 = float(df["High"].tail(250).max())
        out["high52_drop_pct"] = round((close / high52 - 1) * 100, 2)
        out["rsi14"] = round(calc_rsi(df["Close"]), 1)

    signals = 0
    if out["dev25_pct"] is not None and out["dev25_pct"] <= DEV25_LIMIT:
        signals += 1
    if out["high52_drop_pct"] is not None and out["high52_drop_pct"] <= HIGH52_LIMIT:
        signals += 1
    if out["rsi14"] is not None and out["rsi14"] <= RSI_LIMIT:
        signals += 1
    out["signals"] = signals
    out["buy_flag"] = 1 if signals >= 2 else 0
    return out


def fetch_history(code):
    last_error = None
    for _ in range(3):
        try:
            df = yf.Ticker(f"{code}.T").history(period="1y", auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
            last_error = "データが空です"
        except Exception as e:  # 取得失敗は記録して続行する
            last_error = str(e)[:100]
        time.sleep(3)
    raise RuntimeError(last_error or "取得失敗")


COLUMNS = [
    "code", "role", "name", "market", "last_date", "open", "close", "prev_close",
    "change_pct", "dev25_pct", "high52_drop_pct", "rsi14", "signals", "buy_flag",
    "shares", "cost", "account", "pnl_pct", "pnl_yen",
    "per", "pbr", "pbr_x_per", "yield_pct", "roe_pct", "equity_ratio_pct",
    "sales_chg_pct", "profit_chg_pct", "fetch_error",
]


def build_status(rows, holdings, watchlist, now):
    warnings = []
    failed = [r["code"] for r in rows if r.get("fetch_error")]
    last_dates = [r["last_date"] for r in rows if r.get("last_date")]
    last_trade_date = max(last_dates) if last_dates else None

    updated_raw = os.environ.get("WATCHLIST_UPDATED", "").strip()
    age_days = None
    if updated_raw:
        try:
            age_days = (now - datetime.fromisoformat(updated_raw)).days
        except ValueError:
            pass
    stale = False
    if not watchlist:
        warnings.append("監視銘柄リスト(watchlist.csv)がありません。SBIのスクリーニング結果をアップロードしてください。")
    elif age_days is None:
        warnings.append("監視銘柄リストの更新日を取得できませんでした。")
    elif age_days >= STALE_DAYS:
        stale = True
        warnings.append(f"監視銘柄リストが{age_days}日間更新されていません(目安: {STALE_DAYS}日)。SBIでスクリーニングし直してください。")
    if failed:
        warnings.append(f"株価を取得できなかった銘柄: {', '.join(failed)}")
    if last_trade_date and (now.date() - datetime.fromisoformat(last_trade_date).date()).days >= 4:
        warnings.append(f"最新の株価日付が{last_trade_date}です。休場が続いているか、データが古い可能性があります。")

    return {
        "generated_at_jst": now.strftime("%Y-%m-%d %H:%M"),
        "last_trade_date": last_trade_date,
        "holdings_count": len(holdings),
        "watchlist_count": len(watchlist),
        "fetch_error_count": len(failed),
        "watchlist_updated": updated_raw or None,
        "watchlist_age_days": age_days,
        "watchlist_stale": stale,
        "stale_days_threshold": STALE_DAYS,
        "warnings": warnings,
    }


def main():
    now = datetime.now(JST)
    holdings = load_holdings()
    watchlist = load_watchlist()
    codes = list(dict.fromkeys(list(holdings) + list(watchlist)))
    if not codes:
        print("銘柄がありません")
        return 1

    rows = []
    for code in codes:
        in_h, in_w = code in holdings, code in watchlist
        row = {
            "code": code,
            "role": "監視",  # 公開リポジトリでは保有の区別をしない
        }
        row.update(watchlist.get(code, {}))
        h = holdings.get(code)
        if h:
            row.update(h)
        try:
            result = analyze(fetch_history(code))
            if result is None:
                raise RuntimeError("履歴が短すぎます")
            row.update(result)
            if h and h["cost"] and h["shares"]:
                row["pnl_pct"] = round((row["close"] / h["cost"] - 1) * 100, 2)
                row["pnl_yen"] = round((row["close"] - h["cost"]) * h["shares"])
            print(f"OK   {code}")
        except Exception as e:
            row["fetch_error"] = str(e)[:100]
            print(f"FAIL {code}: {e}")
        rows.append(row)
        time.sleep(SLEEP_SEC)

    with open("prices.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    status = build_status(rows, holdings, watchlist, now)
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    print(json.dumps(status, ensure_ascii=False, indent=2))
    ok = sum(1 for r in rows if not r.get("fetch_error"))
    return 0 if ok > 0 else 1  # 全滅ならワークフローを失敗させる(GitHubからメールが届く)


if __name__ == "__main__":
    sys.exit(main())
