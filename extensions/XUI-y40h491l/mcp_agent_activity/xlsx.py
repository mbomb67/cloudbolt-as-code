"""
A small multi-sheet .xlsx writer. The appliance ships neither openpyxl nor
xlsxwriter, so the OOXML parts are written by hand with inline strings, as the
Technology Reference extension (extensions/XUI-vwrd15py) does. Each sheet gets
a bold, frozen header row with filters and column widths sized to its text.
"""
import io
import zipfile

HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
MAX_WIDTH = 60
# Excel's limit for a cell is 32,767 characters.
MAX_CELL = 32000
STYLES = (
    HEAD + '<styleSheet xmlns="' + MAIN + '">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def esc(value):
    """XML-escape text, drop the characters XML 1.0 forbids, and cap the length."""
    text = "" if value is None else str(value)
    text = "".join(c for c in text if c in ("\t", "\n", "\r") or ord(c) >= 0x20)[:MAX_CELL]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def column(index):
    """0 -> A, 26 -> AA."""
    name, n = "", index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


class Workbook(object):
    def __init__(self):
        self.sheets = []

    def add_sheet(self, name, headers, rows):
        """Excel sheet names: at most 31 characters, none of []:*?/\\ and unique in the workbook."""
        clean = name or "Sheet"
        for ch in "[]:*?/\\":
            clean = clean.replace(ch, " ")
        clean = clean.strip()[:31] or "Sheet"
        used = {sheet[0].lower() for sheet in self.sheets}
        candidate, i = clean, 1
        while candidate.lower() in used:
            suffix = " ({})".format(i)
            candidate = clean[: 31 - len(suffix)] + suffix
            i += 1
        self.sheets.append((candidate, list(headers), [list(row) for row in rows]))

    def _sheet_xml(self, headers, rows):
        widths = [len(str(h)) for h in headers]
        for row in rows:
            for i, value in enumerate(row):
                longest = max((len(part) for part in str("" if value is None else value).split("\n")), default=0)
                if i < len(widths):
                    widths[i] = max(widths[i], longest)
                else:
                    widths.append(longest)
        out = [HEAD, '<worksheet xmlns="', MAIN, '">']
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
        if widths:
            out.append("<cols>")
            for i, width in enumerate(widths, start=1):
                out.append('<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i, min(MAX_WIDTH, width + 2)))
            out.append("</cols>")
        out.append("<sheetData>")
        for r, row in enumerate([headers] + rows, start=1):
            style = ' s="1"' if r == 1 else ""
            out.append('<row r="{}">'.format(r))
            for c, value in enumerate(row):
                out.append('<c r="{}{}" t="inlineStr"{}><is><t xml:space="preserve">{}</t></is></c>'.format(
                    column(c), r, style, esc(value)))
            out.append("</row>")
        out.append("</sheetData>")
        if headers:
            out.append('<autoFilter ref="A1:{}{}"/>'.format(column(len(headers) - 1), len(rows) + 1))
        out.append("</worksheet>")
        return "".join(out)

    def to_bytes(self):
        ids = range(1, len(self.sheets) + 1)
        content_types = (
            HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="{}"/>'.format(i, SHEET_TYPE)
                      for i in ids)
            + "</Types>"
        )
        root_rels = (
            HEAD + '<Relationships xmlns="' + RELS + '">'
            '<Relationship Id="rId1" Type="' + DOC_RELS + '/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        )
        workbook = (
            HEAD + '<workbook xmlns="' + MAIN + '" xmlns:r="' + DOC_RELS + '"><sheets>'
            + "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(esc(name), i, i)
                      for i, (name, _h, _r) in zip(ids, self.sheets))
            + "</sheets></workbook>"
        )
        workbook_rels = (
            HEAD + '<Relationships xmlns="' + RELS + '">'
            + "".join('<Relationship Id="rId{0}" Type="{1}/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(
                i, DOC_RELS) for i in ids)
            + '<Relationship Id="rId{}" Type="{}/styles" Target="styles.xml"/>'.format(len(self.sheets) + 1, DOC_RELS)
            + "</Relationships>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", root_rels)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
            archive.writestr("xl/styles.xml", STYLES)
            for i, (_name, headers, rows) in zip(ids, self.sheets):
                archive.writestr("xl/worksheets/sheet{}.xml".format(i), self._sheet_xml(headers, rows))
        return buffer.getvalue()
