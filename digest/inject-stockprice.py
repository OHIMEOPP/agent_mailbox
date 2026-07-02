#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
後處理：把台股「最近交易日收盤價」回填到 stock-digest-out.md 第③部每一行的股名/代號後面。

資料源（皆 TWSE/TPEx 官方、免登入、零捏造）：
  - 上市：https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL   (Code, ClosingPrice)
  - 上櫃：https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes (SecuritiesCompanyCode, Close)

行為：
  - 處理第③部（「受益股清單」）＋第④部（「第二層受惠鏈」）＋第⑤部（「百元內銅板小股」）
    ~ 結尾 ⚠ 行之間的編號行 (^\d+\.)
  - ⑤ 部額外檢查：實際昨收 >100 元的，在價格後加「（>100，僅參考）」（LLM 挑錯低價股的提醒）
  - 找該行第一個 (代號)，在 ) 後插入 " <收盤>元"；查不到標 " N/A"
  - idempotent：若 ) 後已是 數字+元 / N/A 就跳過，不重複插
  - 抓不到資料（網路掛）：原檔不動、回傳 0（讓 digest 照常投遞，只是沒價）

用法： python3 inject-stockprice.py [stock-digest-out.md]
"""
import sys
import re
import json
import urllib.request

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
UA = "Mozilla/5.0 (digest stockprice injector)"


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _fmt_price(raw):
    """ '2340.00' -> '2340'； '78.50' -> '78.5'； 非數字 -> None """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "--", "---", "N/A", "0", "0.00"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 0:
        return None
    return f"{v:.2f}".rstrip("0").rstrip(".")


def build_price_table():
    """ code(str) -> '收盤元字串'。任一源失敗就盡量用另一源；兩源全失敗回 None。 """
    table = {}
    ok = False
    try:
        for row in _fetch_json(TWSE_URL):
            code = (row.get("Code") or "").strip()
            p = _fmt_price(row.get("ClosingPrice"))
            if code and p:
                table[code] = p
        ok = True
    except Exception as e:  # noqa
        print(f"[inject-stockprice] WARN TWSE fetch failed: {e}", file=sys.stderr)
    try:
        for row in _fetch_json(TPEX_URL):
            code = (row.get("SecuritiesCompanyCode") or row.get("Code") or "").strip()
            p = _fmt_price(row.get("Close"))
            if code and p and code not in table:
                table[code] = p
        ok = True
    except Exception as e:  # noqa
        print(f"[inject-stockprice] WARN TPEx fetch failed: {e}", file=sys.stderr)
    return table if ok else None


# 第③部編號行：抓第一個 (代號) ；括號全/半形皆可
CODE_RE = re.compile(r"([（(])(\d{4,6})([）)])")
# 已回填偵測：) 後面已接 空白+數字/元 或 N/A
ALREADY_RE = re.compile(r"^[ 　]*(?:[\d.]+\s*元|N/A)")


def process(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    table = build_price_table()
    if table is None:
        print("[inject-stockprice] 兩源皆失敗，原檔不動。", file=sys.stderr)
        return 0

    in_section = False
    in_copper = False   # ⑤ 百元內銅板小股：填價後若實價 >100 元加註記
    n_filled = 0
    out = []
    for line in lines:
        if "百元內銅板小股" in line or "銅板小股" in line:
            # ⑤ 專區：一樣回填股價，另外對 >100 元者加「（>100，僅參考）」
            in_section = True
            in_copper = True
        elif "受益股清單" in line or "第二層受惠鏈" in line:
            # ③ 受益股清單 與 ④ 第二層受惠鏈（衛星/小型股）都要回填股價
            in_section = True
            in_copper = False
        elif line.lstrip().startswith("⚠"):
            in_section = False
            in_copper = False

        if in_section and re.match(r"^\s*\d+\.", line):
            m = CODE_RE.search(line)
            if m and not ALREADY_RE.match(line[m.end():]):
                code = m.group(2)
                price = table.get(code)
                tag = f" {price}元" if price else " N/A"
                if in_copper and price:
                    try:
                        if float(price) > 100:
                            tag += "（>100，僅參考）"
                    except ValueError:
                        pass
                line = line[:m.end()] + tag + line[m.end():]
                if price:
                    n_filled += 1
        out.append(line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    print(f"[inject-stockprice] 回填 {n_filled} 檔股價（昨收）。")
    return n_filled


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "stock-digest-out.md"
    process(target)
