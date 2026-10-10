"""PDF 按页解析；正文与表格都保留坐标。第一版不执行 OCR。"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

import pymupdf

from .schemas import Evidence, ParsedDocument

KEYWORDS = (
    "主要会计数据", "主要财务指标", "营业收入", "归属于", "经营活动产生",
    "合并利润表", "合并现金流量表", "合并资产负债表", "非经常性损益",
    "会计政策", "会计估计", "追溯调整", "合并范围", "现金流量补充", "现金流量表补充资料",
    "年初到报告期末利润表", "年初至报告期末利润表", "本报告期利润表",
    "年初到报告期末现金流量表", "年初至报告期末现金流量表",
    "母公司利润表", "母公司现金流量表", "母公司资产负债表",
)


def is_consolidated_profit_statement(text: str) -> bool:
    """只匹配明确的合并利润表标题，兼容季度报告累计表名称。"""
    return bool(re.search(r"合并(?:年初[到至]报告期末|本报告期)?利润表", re.sub(r"\s+", "", text)))


def statement_scopes(text: str) -> set[str]:
    return {"consolidated" if match == "合并" else "parent" for match in re.findall(
        r"(合并|母公司)(?:年初[到至]报告期末|本报告期)?(?:利润表|现金流量表|资产负债表)",
        re.sub(r"\s+", "", text))}


def annual_year_column_year(headers: str, context: str, *, report_end: date,
                            report_type: str, cover_text: str) -> int | None:
    """仅在核实年报与明确年度表上下文后解释“2024年”金额列。"""
    normalized = re.sub(r"\s+", "", headers)
    cover = re.sub(r"\s+", "", cover_text)
    local = re.sub(r"\s+", "", context)
    if report_type != "annual" or (report_end.month, report_end.day) != (12, 31):
        return None
    cover_periods = re.findall(r"((?:19|20)\d{2})年?(年度|半年度|第一季度|一季度|第三季度|三季度)报告", cover)
    if not cover_periods or set(cover_periods) != {(str(report_end.year), "年度")}:
        return None
    # 年报也会披露各单季度，不能让封面年度覆盖列或本表的短期间。
    period_context = re.sub(r"(?:19|20)\d{2}年?1月?[-—~～至]12月", "", local)
    if re.search(r"季度|半年度|上半年|前三季|前两季|\d{1,2}月", normalized + period_context):
        return None
    match = re.fullmatch(r"((?:19|20)\d{2})年(?:金额)?(?:(?:[（(]?(?:调整|重述)[前后][）)]?))*", normalized)
    if not match:
        return None
    local_lines = [re.sub(r"\s+", "", line).split("|", 1)[0] for line in context.splitlines()]
    if not any(re.fullmatch(r"主要(?:会计数据|财务指标)|非经常性损益项目|"
                           r"(?:(?:[一二三四五六七八九十]+|\d+)[、.．）)]?)?"
                           r"(?:合并|母公司)(?:利润表|现金流量表)", line) for line in local_lines):
        return None
    return int(match.group(1))


def annual_quarter_column_year(headers: str, context: str, *, report_end: date,
                               report_type: str, cover_text: str) -> int | None:
    """年度报告中无年份的季度列须绑定明示同年分季度表标题。"""
    if report_type != "annual" or (report_end.month, report_end.day) != (12, 31):
        return None
    cover = re.sub(r"\s+", "", cover_text)
    cover_periods = re.findall(r"((?:19|20)\d{2})年?(年度|半年度|第一季度|一季度|第三季度|三季度)报告", cover)
    if not cover_periods or set(cover_periods) != {(str(report_end.year), "年度")}:
        return None
    selected = re.sub(r"\s+", "", headers)
    column = re.fullmatch(r"第?([一二三四1234])季度(?:[（(](\d{1,2})月?[-—~～至](\d{1,2})月份?[）)])?", selected)
    if not column:
        return None
    quarter_no = int(column.group(1)) if column.group(1).isdigit() else "一二三四".index(column.group(1)) + 1
    if column.group(2) and (int(column.group(2)), int(column.group(3))) != (quarter_no * 3 - 2, quarter_no * 3):
        return None
    title_years = set()
    for line in context.splitlines():
        title = re.sub(r"\s+", "", line)
        match = re.fullmatch(r"(?:(?:[一二三四五六七八九十]+|\d+)[、.．）)]?)?((?:19|20)\d{2})年分季度主要财务数据", title)
        if match:
            title_years.add(int(match.group(1)))
    return report_end.year if title_years == {report_end.year} else None


def is_consolidated_statement_title(text: str) -> bool:
    """短独立标题才是正式报表入口，附注中的报表名称不增加权重。"""
    first_line = re.sub(r"\s+", "", text.splitlines()[0]) if text.strip() else ""
    return bool(re.fullmatch(r"(?:(?:[一二三四五六七八九十]+|\d+)[、.．）)]?)?合并(?:年初[到至]报告期末|本报告期)?(?:利润表|现金流量表|资产负债表)", first_line))


def parse_financial_doc(path: Path) -> ParsedDocument:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("单个 PDF 不得超过 30MB。")
    digest = hashlib.sha256(raw).hexdigest()
    doc_id = digest[:16]
    blocks: list[Evidence] = []
    warnings: list[str] = []
    page_heights: dict[int, float] = {}
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError(f"{path.name} 是加密 PDF，请使用可读取的公开版本。")
        if len(doc) > 600:
            raise ValueError("第一版最多支持单份 600 页 PDF。")
        page_count = len(doc)
        for page_idx, page in enumerate(doc):
            page_no = page_idx + 1
            page_heights[page_no] = page.rect.height
            page_blocks = page.get_text("blocks", sort=True)
            useful = [b for b in page_blocks if b[6] == 0 and str(b[4]).strip()]
            if sum(len(b[4].strip()) for b in useful) < 30:
                warnings.append(f"PDF第{page_no}页可提取文本较少，可能是扫描页；本版未进行OCR。")
            for index, block in enumerate(useful):
                blocks.append(Evidence(
                    id=f"{doc_id}:p{page_no}:text{index}", document_id=doc_id,
                    document_name=path.name, pdf_page=page_no,
                    printed_page=page.get_label() or None,
                    text=block[4].strip(), bbox=list(block[:4]), kind="text",
                ))
            # 只在含关键指标的页面进行较耗时的表格检测。
            page_text = "\n".join(b[4] for b in useful)
            if any(word in page_text for word in KEYWORDS):
                try:
                    # 某些公告用填色矩形交替铺背景；普通 lines 策略会把填色
                    # 边界误认成列，造成行名/金额/列头错位。严格线框只读真正
                    # 的线段；完全未找到线框时再保留原策略的检测结果。
                    detected = page.find_tables(strategy="lines_strict").tables
                    if not detected:
                        detected = page.find_tables().tables
                    for index, table in enumerate(detected):
                        rows = table.extract()
                        text = "\n".join(" | ".join(str(c or "") for c in row) for row in rows)
                        if text.strip():
                            blocks.append(Evidence(
                                id=f"{doc_id}:p{page_no}:table{index}", document_id=doc_id,
                                document_name=path.name, pdf_page=page_no,
                                printed_page=page.get_label() or None,
                                text=text, bbox=list(table.bbox), kind="table",
                                cells=[[str(c or "") for c in row] for row in rows],
                            ))
                except Exception:
                    warnings.append(f"PDF第{page_no}页表格结构未完整解析，保留正文供核验。")
    if not blocks:
        raise ValueError(f"{path.name} 无可读取文本；第一版需要文字版 PDF。")
    # 同列数、相邻页、上一表到页底而本表从页首开始，才继承跨页列头。
    # 原始页码不变，继承的是另一个可定位的证据ID。
    tables = [b for b in blocks if b.kind == "table" and b.cells]
    for table in tables:
        previous_bottom = max((b.bbox[3] for b in tables if b.pdf_page == table.pdf_page
                               and b.id != table.id and b.bbox and b.bbox[3] <= table.bbox[1]), default=0)
        table.context_evidence_ids = [b.id for b in blocks if b.kind == "text" and b.pdf_page == table.pdf_page
                                     and b.bbox and b.bbox[1] <= table.bbox[1]
                                     and b.bbox[1] >= previous_bottom
                                     and table.bbox[1] - b.bbox[1] <= 300
                                     and ("单位" in b.text or statement_scopes(b.text)
                                          or "非经常性损益" in b.text or "分季度主要财务数据" in b.text)]
        first = " ".join(table.cells[0])
        has_header = any(word in first for word in ("项目", "本期", "本报告期", "上年", "年初至", "季度")) or bool(
            re.search(r"(?:19|20)\d{2}\s*年", first))
        if has_header or not table.bbox or table.bbox[1] > 200:
            continue
        previous = next((b for b in reversed(tables) if b.pdf_page == table.pdf_page - 1
                         and len(b.cells[0]) == len(table.cells[0]) and b.bbox
                         and b.bbox[3] > page_heights[b.pdf_page] - 110), None)
        if previous:
            table.header_evidence_id = previous.header_evidence_id or previous.id
            table.context_evidence_ids = list(dict.fromkeys(previous.context_evidence_ids + table.context_evidence_ids))
    return ParsedDocument(id=doc_id, name=path.name, sha256=digest,
                          page_count=page_count, blocks=blocks, warnings=warnings)


def retrieve_blocks(doc: ParsedDocument, *, repair: bool = False,
                    max_chars: int = 38000, diagnostics: dict | None = None) -> tuple[list[Evidence], bool]:
    """页级关键词检索，附带同页单位与列头；不会把整份年报发给模型。"""
    pages: dict[int, list[Evidence]] = {}
    for block in doc.blocks:
        pages.setdefault(block.pdf_page, []).append(block)
    scores = {}
    by_id = {b.id: b for b in doc.blocks}
    statement_pages = set()
    for number, items in pages.items():
        for block in items:
            if block.kind != "table" or not block.cells:
                continue
            if any(is_consolidated_statement_title(by_id[eid].text)
                   for eid in block.context_evidence_ids if eid in by_id):
                statement_pages.add(number)
                if block.header_evidence_id and block.header_evidence_id in by_id:
                    statement_pages.add(by_id[block.header_evidence_id].pdf_page)
    for number, items in pages.items():
        text = "\n".join(x.text for x in items)
        scores[number] = sum(min(text.count(word), 4) for word in KEYWORDS)
        if number in statement_pages:
            scores[number] += 80
        if repair:
            scores[number] += 8 * sum(word in text for word in (
                "合并利润表", "合并现金流量表", "非经常性损益", "会计政策", "会计估计"))
            if is_consolidated_profit_statement(text):
                scores[number] += 8
    ranked = sorted(pages, key=lambda p: (-scores[p], p))
    selected_pages = {1} if 1 in pages else set()
    for number in ranked[:14]:
        if scores[number] > 0:
            selected_pages.add(number)
    for number in list(selected_pages):
        if number + 1 in pages and scores.get(number + 1, 0) > 0:
            selected_pages.add(number + 1)
    selected: list[Evidence] = []
    chars = 0
    truncated = False
    # 把第一页元信息放最前；其余按检索相关度，避免前面的MD&A淹没报表。
    ordering = sorted(selected_pages, key=lambda p: (p != 1, -scores.get(p, 0), p))
    for number in ordering:
        page_tables = [b for b in pages[number] if b.kind == "table" and len(b.cells) > 1]
        for block in sorted(pages[number], key=lambda b: b.kind != "table"):
            if block.kind == "text" and len(block.bbox) == 4 and any(
                    table.bbox[0] - 3 <= block.bbox[0] and table.bbox[1] - 3 <= block.bbox[1]
                    and table.bbox[2] + 3 >= block.bbox[2] and table.bbox[3] + 3 >= block.bbox[3]
                    for table in page_tables):
                # 单元格已保留，模型不再重复读取同一表的逐行正文。
                continue
            if chars + len(block.text) > max_chars:
                truncated = True
                continue
            selected.append(block)
            chars += len(block.text)
    selected_ids = {b.id for b in selected}
    for block in list(selected):
        related = block.context_evidence_ids + ([block.header_evidence_id] if block.header_evidence_id else [])
        for related_id in related:
            if related_id in selected_ids:
                continue
            inherited = by_id[related_id]
            if chars + len(inherited.text) <= max_chars:
                selected.append(inherited)
                selected_ids.add(inherited.id)
                chars += len(inherited.text)
            else:
                truncated = True
    if len(selected) < len(doc.blocks):
        truncated = True
    if diagnostics is not None:
        retained = {b.id for b in selected}
        diagnostics.update(max_chars=max_chars, used_chars=chars, repair=repair,
            pages=[{"pdf_page": p, "score": scores[p], "statement_anchor": p in statement_pages,
                    "candidate": p in selected_pages,
                    "selected_blocks": sum(b.id in retained for b in pages[p]),
                    "decision": "selected" if any(b.id in retained for b in pages[p]) else
                                "budget_or_duplicate" if p in selected_pages else "below_page_rank"}
                   for p in sorted(pages)],
            selection_rule="封面+相关度前14页及相关下一页；正式合并报表锚点加80；字符预算下优先表格，排除重复单元格正文。")
    return selected, truncated
