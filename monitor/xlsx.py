"""Минимальный писатель .xlsx на стандартной библиотеке: листы, ширины, шапка, фильтр, ссылки, форматы."""

import re
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# индексы стилей (cellXfs)
TEXT, HEAD, MONEY, PCT, DATE, LINK, WRAP, INT = 0, 1, 2, 3, 4, 5, 6, 7


class Link:
    def __init__(self, url, text=None):
        self.url = url or ""
        self.text = text or url or ""


class Sheet:
    def __init__(self, name, columns):
        """columns: [(заголовок, ширина, стиль по умолчанию)]"""
        self.name = re.sub(r"[\[\]:*?/\\]", " ", name)[:31]
        self.columns = columns
        self.rows = []
        self.cond = []  # (колонка, правило)
        self.charts = []
        self.filter = True
        self.header = True

    def add_chart(self, title, cats, series, anchor, y_title=""):
        """Линейный график.
        cats: (лист, колонка, первая строка, последняя строка) — подписи по оси X (1-based строки).
        series: [(название, лист, колонка, первая строка, последняя строка, цвет 'RRGGBB')].
        anchor: (колонка, строка, ширина в колонках, высота в строках), 0-based."""
        self.charts.append({"title": title, "cats": cats, "series": series, "anchor": anchor, "y_title": y_title})

    def add(self, *values):
        self.rows.append(values)

    def color_sign(self, col_index):
        """Красный — больше нуля (мы дороже), зелёный — меньше."""
        self.cond.append(col_index)


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _t(text):
    return escape(_BAD.sub("", str(text)))


def _cell(ref, value, style):
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>' if style else ""
    if isinstance(value, tuple):
        value, style = value
        return _cell(ref, value, style)
    if isinstance(value, Link):
        if not value.url:
            return _cell(ref, value.text, style)
        url = value.url.replace('"', "%22")[:255]
        label = str(value.text)[:250]
        formula = 'HYPERLINK("' + url + '","' + label.replace('"', '""') + '")'
        return f'<c r="{ref}" s="{LINK}" t="str"><f>{_t(formula)}</f><v>{_t(label)}</v></c>'
    if isinstance(value, bool):
        return _cell(ref, "да" if value else "нет", style)
    if isinstance(value, datetime):
        serial = (value - datetime(1899, 12, 30)).total_seconds() / 86400
        return f'<c r="{ref}" s="{DATE}"><v>{serial:.6f}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value!r}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{_t(value)}</t></is></c>'


def _sheet_xml(sh):
    ncol = len(sh.columns)
    last = _col(ncol - 1)
    nrows = len(sh.rows) + 1
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, (_, w, _) in enumerate(sh.columns)
    )
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last}{nrows}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        + ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>' if sh.header else "")
        + '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{cols}</cols><sheetData>"
    ]
    first = 1
    if sh.header:
        head = "".join(_cell(f"{_col(i)}1", h, HEAD) for i, (h, _, _) in enumerate(sh.columns))
        out.append(f'<row r="1" ht="30" customHeight="1">{head}</row>')
        first = 2
    for r, row in enumerate(sh.rows, start=first):
        cells = "".join(_cell(f"{_col(i)}{r}", v, sh.columns[i][2] if i < ncol else TEXT) for i, v in enumerate(row))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    if sh.rows and sh.filter:
        out.append(f'<autoFilter ref="A1:{last}{nrows}"/>')
    prio = 1
    for c in sh.cond:
        rng = f"{_col(c)}2:{_col(c)}{max(nrows, 2)}"
        out.append(
            f'<conditionalFormatting sqref="{rng}">'
            f'<cfRule type="cellIs" dxfId="0" priority="{prio}" operator="greaterThan"><formula>0.005</formula></cfRule>'
            f'<cfRule type="cellIs" dxfId="1" priority="{prio + 1}" operator="lessThan"><formula>-0.005</formula></cfRule>'
            f"</conditionalFormatting>"
        )
        prio += 2
    out.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')
    if sh.charts:
        out.append('<drawing r:id="rId1"/>')
    out.append("</worksheet>")
    return "".join(out)


_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _ref(sheet, col, r1, r2):
    name = sheet.replace("'", "''")
    return f"'{_t(name)}'!${_col(col)}${r1}:${_col(col)}${r2}"


def _rich(text, size=1000, bold=False):
    b = ' b="1"' if bold else ' b="0"'
    return (
        f'<c:tx><c:rich><a:bodyPr/><a:p><a:pPr><a:defRPr sz="{size}"{b}/></a:pPr>'
        f'<a:r><a:rPr lang="ru-RU" sz="{size}"{b}><a:solidFill><a:srgbClr val="1D2320"/></a:solidFill></a:rPr>'
        f"<a:t>{_t(text)}</a:t></a:r></a:p></c:rich></c:tx>"
    )


_AXIS_TEXT = (
    '<c:txPr><a:bodyPr/><a:p><a:pPr><a:defRPr sz="800"><a:solidFill><a:srgbClr val="66706B"/></a:solidFill>'
    '</a:defRPr></a:pPr><a:endParaRPr lang="ru-RU"/></a:p></c:txPr>'
)


def _chart_xml(ch):
    sers = []
    for i, (name, sheet, col, r1, r2, color) in enumerate(ch["series"]):
        dash = '<a:prstDash val="dash"/>' if i else ""
        sers.append(
            f'<c:ser><c:idx val="{i}"/><c:order val="{i}"/><c:tx><c:v>{_t(name)}</c:v></c:tx>'
            f'<c:spPr><a:ln w="25400" cap="rnd"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f"{dash}<a:round/></a:ln></c:spPr>"
            f'<c:marker><c:symbol val="circle"/><c:size val="6"/><c:spPr><a:solidFill><a:srgbClr val="{color}"/>'
            f'</a:solidFill><a:ln w="9525"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln></c:spPr></c:marker>'
            f"<c:cat><c:strRef><c:f>{_ref(*ch['cats'])}</c:f></c:strRef></c:cat>"
            f'<c:val><c:numRef><c:f>{_ref(sheet, col, r1, r2)}</c:f></c:numRef></c:val><c:smooth val="0"/></c:ser>'
        )
    y_title = f'<c:title>{_rich(ch["y_title"], 800)}<c:overlay val="0"/></c:title>' if ch["y_title"] else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<c:chartSpace xmlns:c="{_C}" xmlns:a="{_A}" xmlns:r="{_R}"><c:roundedCorners val="0"/>'
        f'<c:chart><c:title>{_rich(ch["title"], 1000, True)}<c:overlay val="0"/></c:title>'
        f'<c:autoTitleDeleted val="0"/><c:plotArea><c:layout/>'
        f'<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>{"".join(sers)}'
        f'<c:marker val="1"/><c:axId val="5001"/><c:axId val="5002"/></c:lineChart>'
        f'<c:catAx><c:axId val="5001"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/>'
        f'<c:axPos val="b"/><c:numFmt formatCode="General" sourceLinked="1"/><c:majorTickMark val="none"/>'
        f'<c:minorTickMark val="none"/><c:tickLblPos val="low"/>'
        f'<c:spPr><a:ln w="6350"><a:solidFill><a:srgbClr val="C9CEC8"/></a:solidFill></a:ln></c:spPr>{_AXIS_TEXT}'
        f'<c:crossAx val="5002"/><c:crosses val="autoZero"/><c:auto val="1"/><c:lblAlgn val="ctr"/>'
        f'<c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>'
        f'<c:valAx><c:axId val="5002"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/>'
        f'<c:axPos val="l"/><c:majorGridlines><c:spPr><a:ln w="6350"><a:solidFill><a:srgbClr val="E7EAE5"/>'
        f"</a:solidFill></a:ln></c:spPr></c:majorGridlines>{y_title}"
        f'<c:numFmt formatCode="#,##0.0" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
        f'<c:tickLblPos val="nextTo"/><c:spPr><a:ln><a:noFill/></a:ln></c:spPr>{_AXIS_TEXT}'
        f'<c:crossAx val="5001"/><c:crosses val="autoZero"/><c:crossBetween val="between"/></c:valAx>'
        f"<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr></c:plotArea>"
        f'<c:legend><c:legendPos val="b"/><c:overlay val="0"/>{_AXIS_TEXT}</c:legend>'
        f'<c:plotVisOnly val="1"/><c:dispBlanksAs val="span"/></c:chart>'
        f'<c:spPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:ln w="6350"><a:solidFill>'
        f'<a:srgbClr val="E1E4DF"/></a:solidFill></a:ln></c:spPr></c:chartSpace>'
    )


def _drawing_xml(charts, first_chart_no):
    parts = []
    for i, ch in enumerate(charts):
        col, row, w, h = ch["anchor"]
        parts.append(
            f'<xdr:twoCellAnchor editAs="oneCell">'
            f"<xdr:from><xdr:col>{col}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
            f"<xdr:to><xdr:col>{col + w}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row + h}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
            f'<xdr:graphicFrame macro=""><xdr:nvGraphicFramePr><xdr:cNvPr id="{i + 2}" name="График {i + 1}"/>'
            f'<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></xdr:xfrm>'
            f'<a:graphic><a:graphicData uri="{_C}"><c:chart xmlns:c="{_C}" xmlns:r="{_R}" r:id="rId{i + 1}"/>'
            f"</a:graphicData></a:graphic></xdr:graphicFrame><xdr:clientData/></xdr:twoCellAnchor>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        f'xmlns:a="{_A}">{"".join(parts)}</xdr:wsDr>'
    )


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="4">
<numFmt numFmtId="164" formatCode="#,##0.00"/>
<numFmt numFmtId="165" formatCode="+0.0%;\\-0.0%;0.0%"/>
<numFmt numFmtId="166" formatCode="dd.mm.yyyy hh:mm"/>
<numFmt numFmtId="167" formatCode="#,##0"/>
</numFmts>
<fonts count="3">
<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><u/><sz val="11"/><color rgb="FF1F5FBF"/><name val="Calibri"/><family val="2"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE9EDF2"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="2">
<dxf><font><color rgb="FFB42318"/></font></dxf>
<dxf><font><color rgb="FF067647"/></font></dxf>
</dxfs>
</styleSheet>"""


def write(path, sheets):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(sheets))
        )
        drawing_no = chart_no = 0
        for i, sh in enumerate(sheets):
            if not sh.charts:
                continue
            drawing_no += 1
            overrides += (
                f'<Override PartName="/xl/drawings/drawing{drawing_no}.xml" '
                f'ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>'
            )
            z.writestr(
                f"xl/worksheets/_rels/sheet{i + 1}.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{_R}/drawing" Target="../drawings/drawing{drawing_no}.xml"/>'
                "</Relationships>",
            )
            z.writestr(f"xl/drawings/drawing{drawing_no}.xml", _drawing_xml(sh.charts, chart_no + 1))
            chart_rels = []
            for j, ch in enumerate(sh.charts):
                chart_no += 1
                overrides += (
                    f'<Override PartName="/xl/charts/chart{chart_no}.xml" '
                    f'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>'
                )
                z.writestr(f"xl/charts/chart{chart_no}.xml", _chart_xml(ch))
                chart_rels.append(
                    f'<Relationship Id="rId{j + 1}" Type="{_R}/chart" Target="../charts/chart{chart_no}.xml"/>'
                )
            z.writestr(
                f"xl/drawings/_rels/drawing{drawing_no}.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(chart_rels)
                + "</Relationships>",
            )
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        sheet_tags = "".join(
            f'<sheet name="{_t(s.name)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, s in enumerate(sheets)
        )
        defined = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
            f"&apos;{_t(s.name)}&apos;!$A$1:${_col(len(s.columns) - 1)}${len(s.rows) + 1}</definedName>"
            for i, s in enumerate(sheets)
            if s.rows and s.filter
        )
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheet_tags}</sheets>"
            + (f"<definedNames>{defined}</definedNames>" if defined else "")
            + "</workbook>",
        )
        rels = "".join(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(sheets))
        )
        rels += (
            f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/'
            f'2006/relationships/styles" Target="styles.xml"/>'
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
            "</Relationships>",
        )
        z.writestr("xl/styles.xml", _STYLES)
        for i, s in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(s))
