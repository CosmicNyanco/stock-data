"""適時開示(TDnet)の取得。

保有銘柄・監視銘柄に関する開示だけを、disclosures.csv に蓄積する(タイトルとPDFへのリンクのみ)。
個人利用の範囲で、1日1回、少ない回数のアクセスにとどめる(待ち時間あり)。
要約や判断は、このスクリプトでは行わない。
"""

import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from fetch_prices import JST, load_holdings

BASE = "https://www.release.tdnet.info/inbs/"
LIST_URL = BASE + "I_list_{page:03d}_{ymd}.html"
DAYS_BACK = 2  # 今日を含めて何営業日分(土日を除く)を見るか
TOP_N = 10  # 監視銘柄から、買い検討の上位を何銘柄まで調べるか
KEEP_DAYS = 45  # 蓄積する日数
SLEEP_SEC = 2.0  # アクセス間の待ち時間
MAX_PAGES = 12  # 1日あたりの最大ページ数(1ページ100件)
PAGE_SIZE = 100
USER_AGENT = "Mozilla/5.0 (compatible; personal-stock-screener/1.0; once a day)"

MAX_PDFS = 15  # 1回の実行で本文を取り出すPDFの最大数(重要度「高」のみ)
MAX_PDF_PAGES = 4  # 各PDFの先頭から何ページ分の文字を取り出すか
MAX_TEXT_CHARS = 8000  # 1つの開示から保存する最大文字数
TEXT_DIR = "disclosure_texts"

OUT_CSV = "disclosures.csv"
OUT_STATUS = "disclosure_status.json"
COLUMNS = ["date", "time", "code", "name", "title", "category", "importance", "url", "text_file"]

# 表題から分類する(上から順に最初に当てはまったもの)
RULES = [
    ("要注意", ["事故", "不正", "不適切", "第三者委員会", "調査委員会", "特別調査", "行政処分",
              "訴訟", "上場廃止", "監理", "整理銘柄", "債務超過", "継続企業"]),
    ("業績修正", ["業績予想の修正", "業績予想及び配当予想の修正", "実績値との差異", "実績値の差異",
              "業績予想の差異", "通期業績予想", "業績予想に関するお知らせ", "差異に関するお知らせ"]),
    ("配当", ["配当予想の修正", "配当方針", "剰余金の配当", "期末配当", "中間配当", "配当金", "株主還元"]),
    ("自社株買い状況", ["取得状況"]),  # 毎月の定例報告。重要度は低
    ("自社株買い", ["自己株式の取得", "自己株式取得", "自己株式の消却"]),
    ("資本・M&A", ["公開買付", "TOB", "合併", "株式交換", "株式移転", "会社分割", "事業譲渡",
               "株式分割", "株式併合", "第三者割当", "新株予約権", "公募", "売出", "資本業務提携",
               "子会社化", "株式取得", "MBO", "非上場化", "自己株式の処分"]),
    ("決算資料", ["説明資料", "補足資料", "決算説明", "プレゼンテーション"]),
    ("決算", ["決算短信", "決算発表"]),
    ("人事", ["役員", "代表取締役", "人事", "異動"]),
]
HIGH = {"要注意", "業績修正", "配当", "自社株買い", "資本・M&A", "決算"}
MID = {"決算資料"}


def classify(title):
    for category, words in RULES:
        if any(w in title for w in words):
            importance = "高" if category in HIGH else ("中" if category in MID else "低")
            return category, importance
    return "その他", "低"


def clean(text):
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def raw_cell(row, cls):
    m = re.search(r'<td[^>]*class="[^"]*\b%s\b[^"]*"[^>]*>(.*?)</td>' % cls, row, re.S | re.I)
    return m.group(1) if m else ""


def norm_code(code5):
    """TDnetの5桁コード(例 18850, 231A0)を4桁(1885, 231A)に直す。"""
    s = clean(code5).upper()
    if len(s) == 5 and s.endswith("0"):
        s = s[:4]
    return s


def parse_page(text, ymd):
    """1ページ分の開示を読み取る。(全行数, 行のリスト) を返す。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        if "kjCode" not in tr:
            continue
        title_cell = raw_cell(tr, "kjTitle")
        m = re.search(r'<a[^>]*href="([^"]+)"', title_cell, re.I)
        href = m.group(1) if m else ""
        title = clean(title_cell)
        category, importance = classify(title)
        rows.append({
            "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "time": clean(raw_cell(tr, "kjTime")),
            "code": norm_code(raw_cell(tr, "kjCode")),
            "name": clean(raw_cell(tr, "kjName")),
            "title": title,
            "category": category,
            "importance": importance,
            "url": (BASE + href) if href and not href.startswith("http") else href,
        })
    return rows


def fetch(url):
    """(HTTPステータス, 本文) を返す。404は (404, "") 。それ以外の失敗は例外。"""
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, ""
            last = e
        except Exception as e:
            last = e
        time.sleep(5)
    raise RuntimeError(str(last)[:150])


def to_float(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def target_codes():
    """調べる銘柄: 保有銘柄 + 買い検討の上位TOP_N(prices.csvから。条件はレポートと同じ)。"""
    holdings = set(load_holdings())
    candidates = []
    if os.path.exists("prices.csv"):
        with open("prices.csv", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                pbr_per = to_float(r.get("pbr_x_per"))
                yld = to_float(r.get("yield_pct"))
                if (r.get("role") in ("監視", "保有+監視") and not r.get("fetch_error")
                        and r.get("signals") == "3" and pbr_per is not None and pbr_per <= 15
                        and yld is not None):
                    candidates.append((yld, r["code"]))
    candidates.sort(reverse=True)
    return holdings, [code for _, code in candidates[:TOP_N]]


def recent_weekdays(today, count):
    """今日から遡って、土日を除く count 日分の日付を返す。"""
    days, d = [], today
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


def fetch_binary(url):
    last = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            last = e
        time.sleep(5)
    raise RuntimeError(str(last)[:150])


def pdf_to_text(data):
    """PDFの先頭 MAX_PDF_PAGES ページの文字を取り出す。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    return text[:MAX_TEXT_CHARS] if text else "(文字を取り出せませんでした。画像のPDFの可能性があります)"


def extract_texts(rows, status):
    """重要度「高」で、まだ本文がない開示のPDFを読み、disclosure_texts/ に保存する。"""
    os.makedirs(TEXT_DIR, exist_ok=True)
    done, failed = 0, 0
    for r in rows:  # 新しい順
        if done + failed >= MAX_PDFS:
            break
        if r["importance"] != "高" or r.get("text_file") or not r["url"].lower().endswith(".pdf"):
            continue
        name = os.path.basename(r["url"])[:-4] + ".txt"
        try:
            text = pdf_to_text(fetch_binary(r["url"]))
            header = f'{r["date"]} {r["time"]} {r["code"]} {r["name"]}\n{r["title"]}\n{r["url"]}\n\n'
            with open(os.path.join(TEXT_DIR, name), "w", encoding="utf-8") as f:
                f.write(header + text)
            r["text_file"] = f"{TEXT_DIR}/{name}"
            done += 1
        except Exception as e:
            failed += 1
            status["errors"].append(f'PDF {r["code"]} {r["title"][:20]}: {str(e)[:100]}')
            print(f'PDF ERROR {r["code"]}: {e}')
        time.sleep(SLEEP_SEC)
    status["pdf_texts_saved"] = done
    status["pdf_texts_failed"] = failed


def read_existing():
    if not os.path.exists(OUT_CSV):
        return []
    with open(OUT_CSV, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main():
    now = datetime.now(JST)
    holdings, candidates = target_codes()
    codes = holdings | set(candidates)
    if not codes:
        print("銘柄がありません")
        return 1

    status = {"generated_at_jst": now.strftime("%Y-%m-%d %H:%M"), "days": {}, "errors": [], "warnings": [],
              "extra_codes": sorted(holdings), "candidates": candidates}
    if not os.path.exists("prices.csv"):
        status["warnings"].append("prices.csv がないため、保有銘柄だけを調べました")
    found = []
    requests_ok = 0
    requests_all = 0

    for day_date in recent_weekdays(now.date(), DAYS_BACK):
        ymd = day_date.strftime("%Y%m%d")
        day = {"pages": 0, "rows": 0, "matched": 0}
        for page in range(1, MAX_PAGES + 1):
            requests_all += 1
            try:
                code, text = fetch(LIST_URL.format(page=page, ymd=ymd))
            except Exception as e:
                status["errors"].append(f"{ymd} page{page}: {e}")
                print(f"ERROR {ymd} page{page}: {e}")
                break
            requests_ok += 1
            time.sleep(SLEEP_SEC)
            if code == 404:
                break
            rows = parse_page(text, ymd)
            day["pages"] += 1
            day["rows"] += len(rows)
            if not rows:
                if len(text) > 3000 and "見つかりません" not in text and "該当" not in text:
                    msg = f"{ymd} page{page}: 表を読み取れませんでした(ページ形式が変わった可能性)"
                    status["warnings"].append(msg)
                    print(msg)
                    print(text[:1500])  # 原因調査用
                break
            hits = [r for r in rows if r["code"] in codes]
            day["matched"] += len(hits)
            found.extend(hits)
            if len(rows) < PAGE_SIZE:
                break
        status["days"][ymd] = day
        print(ymd, day)

    # 既存の蓄積と合わせる(URLで重複を除く)。古いものは捨てる。
    merged = {r["url"] or f'{r["date"]}{r["time"]}{r["code"]}{r["title"]}': r for r in read_existing()}
    new_count = 0
    for r in found:
        key = r["url"] or f'{r["date"]}{r["time"]}{r["code"]}{r["title"]}'
        if key not in merged:
            new_count += 1
        else:
            r["text_file"] = merged[key].get("text_file", "")  # 取り出し済みの本文は残す
        merged[key] = r
    limit = (now.date() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = [r for r in merged.values() if r["date"] >= limit]
    kept.sort(key=lambda r: (r["date"], r["time"]), reverse=True)
    extract_texts(kept, status)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    total_rows = sum(d["rows"] for d in status["days"].values())
    if total_rows == 0 and requests_ok > 0:
        status["warnings"].append(f"直近{DAYS_BACK}営業日の開示が0件でした(休場が続いたか、取得に失敗した可能性)")
    status.update({"new_rows": new_count, "total_rows": len(kept), "codes_watched": len(codes)})
    with open(OUT_STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if requests_ok > 0 else 1  # すべての取得が失敗したときだけ失敗扱い


if __name__ == "__main__":
    sys.exit(main())
