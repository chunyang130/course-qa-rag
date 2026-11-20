#!/usr/bin/env python3
"""由結構化資料產生的知識庫文件。

業務只維護 sources/pricing.csv（像 Excel 一樣一列一門課），不用改 Markdown。
sources/pricing.md 是文件範本，{{課程定價}} 會被換成由 CSV 產生的條列。
rag.py 載入知識庫時直接呼叫這裡，所以 CSV 就是唯一的資料來源，不會有「改了表格卻忘了更新文件」的問題。

只用 Python 標準函式庫。
"""
import csv, os

COLUMNS = ["課程", "價格", "計價方式", "備註"]
UNITS = {"每期", "全年"}
PRICE_RANGE = (5_000, 100_000)       # 本中心課程的合理價格範圍；超出多半是打錯（少一個 0 或多一個 0）
PLACEHOLDER = "{{課程定價}}"


def read_pricing(path):
    """讀取並檢查價目表。回傳 (課程列表, 錯誤清單)；有錯誤時課程列表可能不完整。"""
    rows, errors = [], []
    if not os.path.exists(path):
        return rows, [f"找不到 {os.path.basename(path)}"]
    with open(path, encoding="utf-8-sig", newline="") as f:   # utf-8-sig：Excel 另存的 CSV 開頭會多一個 BOM
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            return rows, [f"欄位必須是 {'、'.join(COLUMNS)}，目前是 {'、'.join(reader.fieldnames or [])}"]
        seen = set()
        for line, r in enumerate(reader, start=2):             # 第 1 列是標題，資料從第 2 列開始
            name = (r["課程"] or "").strip()
            raw_price = (r["價格"] or "").strip().replace(",", "")
            unit = (r["計價方式"] or "").strip()
            note = (r["備註"] or "").strip()
            if not name:
                errors.append(f"第 {line} 列：課程名稱是空的")
                continue
            if name in seen:
                errors.append(f"第 {line} 列：課程「{name}」重複")
            seen.add(name)
            if not raw_price.isdigit():
                errors.append(f"第 {line} 列：「{name}」的價格「{r['價格']}」不是整數")
                continue
            price = int(raw_price)
            if not PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
                errors.append(f"第 {line} 列：「{name}」的價格 {price:,} 超出合理範圍 "
                              f"{PRICE_RANGE[0]:,}–{PRICE_RANGE[1]:,}，請確認是否打錯")
            if unit not in UNITS:
                errors.append(f"第 {line} 列：「{name}」的計價方式「{unit}」只能是 {'或'.join(sorted(UNITS))}")
            rows.append({"課程": name, "價格": price, "計價方式": unit, "備註": note})
    if not rows and not errors:
        errors.append("價目表沒有任何課程")
    return rows, errors


def format_line(r):
    """一門課 → 一行條列，格式和人工撰寫的文件一致。"""
    price = f"NT${r['價格']:,}"
    if r["計價方式"] == "每期":
        text = f"每期 {price}" + (f"（{r['備註']}）" if r["備註"] else "")
    else:
        text = f"{price}（{r['計價方式']}" + (f"，{r['備註']}" if r["備註"] else "") + "）"
    return f"- {r['課程']}：{text}"


def generated_docs(src_dir):
    """回傳 ({檔名（不含副檔名）: Markdown 內容}, 錯誤清單)。"""
    docs, errors = {}, []
    template = os.path.join(src_dir, "pricing.md")
    if not os.path.exists(template):
        return docs, errors
    rows, errs = read_pricing(os.path.join(src_dir, "pricing.csv"))
    errors += errs
    text = open(template, encoding="utf-8").read()
    if PLACEHOLDER not in text:
        errors.append(f"sources/pricing.md 裡找不到 {PLACEHOLDER}")
    docs["pricing"] = text.replace(PLACEHOLDER, "\n".join(format_line(r) for r in rows))
    return docs, errors
