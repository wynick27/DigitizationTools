import difflib
import html
import re
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QTextEdit


BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "div", "dl", "dt",
    "dd", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tr", "ul",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


@dataclass
class MarkupError:
    message: str
    line: int = 1
    column: int = 1

    def display(self):
        return f"第 {self.line} 行，第 {self.column} 列：{self.message}"


@dataclass
class MarkupProjection:
    mode: str
    source: str
    visible_text: str
    visible_to_source: list[int]
    rendered_html: str
    errors: list[MarkupError] = field(default_factory=list)

    def map_position(self, position):
        position = max(0, min(int(position), len(self.visible_to_source) - 1))
        return self.visible_to_source[position]

    def map_range_start(self, position):
        if not self.visible_text:
            return 0
        if position >= len(self.visible_text):
            return self.visible_to_source[-2] + 1
        return self.map_position(position)

    def map_range_end(self, position):
        if position <= 0 or not self.visible_text:
            return self.map_range_start(position)
        position = min(position, len(self.visible_text))
        return self.visible_to_source[position - 1] + 1


class MarkupPreviewEdit(QTextEdit):
    focus_in_signal = pyqtSignal()
    zoom_signal = pyqtSignal(int)

    def __init__(self, side, parent=None):
        super().__init__(parent)
        self.side = side
        self.setAcceptRichText(False)

    def focusInEvent(self, event):
        self.focus_in_signal.emit()
        super().focusInEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_signal.emit(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def adjust_font_size(self, direction):
        font = self.document().defaultFont()
        size = font.pointSizeF()
        if size <= 0:
            size = self.font().pointSizeF()
        new_size = max(6.0, min(72.0, size + (1 if direction > 0 else -1)))
        if new_size == size:
            return
        font.setPointSizeF(new_size)
        self.document().setDefaultFont(font)


class _ProjectionBuilder:
    def __init__(self, source):
        self.source = source
        self.chars = []
        self.mapping = []

    def append(self, value, source_position):
        for char in value:
            self.chars.append(char)
            self.mapping.append(source_position)

    def append_slice(self, start, end):
        for position in range(start, end):
            self.chars.append(self.source[position])
            self.mapping.append(position)

    def newline(self, source_position):
        if self.chars and self.chars[-1] != "\n":
            self.append("\n", source_position)

    def finish(self):
        return "".join(self.chars), self.mapping + [len(self.source)]


def _line_col(text, position):
    line = text.count("\n", 0, position) + 1
    last_newline = text.rfind("\n", 0, position)
    column = position + 1 if last_newline < 0 else position - last_newline
    return line, column


def _iter_html_tokens(source):
    position = 0
    entity_re = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")
    while position < len(source):
        if source.startswith("<!--", position):
            end = source.find("-->", position + 4)
            if end < 0:
                return
            end += 3
            yield position, end, source[position:end]
            position = end
            continue
        if source[position] == "<":
            if not (
                source.startswith("<!", position)
                or re.match(r"</?[A-Za-z]", source[position:])
            ):
                position += 1
                continue
            quote = None
            end = position + 1
            while end < len(source):
                char = source[end]
                if quote:
                    if char == quote:
                        quote = None
                elif char in ('"', "'"):
                    quote = char
                elif char == ">":
                    end += 1
                    yield position, end, source[position:end]
                    position = end
                    break
                end += 1
            else:
                return
            continue
        if source[position] == "&":
            entity = entity_re.match(source, position)
            if entity:
                yield position, entity.end(), entity.group(0)
                position = entity.end()
                continue
        position += 1


def _project_html(source):
    builder = _ProjectionBuilder(source)
    errors = []
    stack = []
    hidden_content_tags = {"script", "style"}
    cursor = 0

    for token_start, token_end, token in _iter_html_tokens(source):
        raw_text = source[cursor:token_start]
        content_hidden = any(tag in hidden_content_tags for tag in stack)
        stray = re.search(r"[<>]", raw_text) if not content_hidden else None
        if stray:
            position = cursor + stray.start()
            line, column = _line_col(source, position)
            errors.append(MarkupError("HTML 中存在未闭合的尖括号或标签", line, column))
        if not content_hidden:
            builder.append_slice(cursor, token_start)
        if token.startswith("&"):
            if not any(tag in hidden_content_tags for tag in stack):
                decoded = html.unescape(token)
                builder.append(decoded, token_start)
        elif token.startswith("<!--") or token.startswith("<!"):
            pass
        else:
            tag_match = re.match(r"<\s*(/?)\s*([A-Za-z][\w:-]*)", token)
            if not tag_match:
                line, column = _line_col(source, token_start)
                errors.append(MarkupError("无法识别的 HTML 标签", line, column))
            else:
                closing = bool(tag_match.group(1))
                tag = tag_match.group(2).lower()
                self_closing = token.rstrip().endswith("/>") or tag in VOID_TAGS
                if tag in BLOCK_TAGS:
                    builder.newline(token_start)
                if closing:
                    if tag not in stack:
                        line, column = _line_col(source, token_start)
                        errors.append(MarkupError(f"多余的结束标签 </{tag}>", line, column))
                    else:
                        while stack and stack[-1] != tag:
                            missing = stack.pop()
                            line, column = _line_col(source, token_start)
                            errors.append(
                                MarkupError(f"标签 <{missing}> 在 </{tag}> 前未闭合", line, column)
                            )
                        if stack:
                            stack.pop()
                elif not self_closing:
                    stack.append(tag)
        cursor = token_end

    raw_text = source[cursor:]
    content_hidden = any(tag in hidden_content_tags for tag in stack)
    stray = re.search(r"[<>]", raw_text) if not content_hidden else None
    if stray:
        position = cursor + stray.start()
        line, column = _line_col(source, position)
        errors.append(MarkupError("HTML 中存在未闭合的尖括号或标签", line, column))
    if not any(tag in hidden_content_tags for tag in stack):
        builder.append_slice(cursor, len(source))
    while stack:
        tag = stack.pop()
        errors.append(MarkupError(f"标签 <{tag}> 未闭合", *_line_col(source, len(source))))
    visible, mapping = builder.finish()
    return MarkupProjection("html", source, visible, mapping, source, errors)


def _append_markdown_inline(builder, source, start, end):
    segment = source[start:end]
    hidden_markers = set()
    paired_patterns = (
        re.compile(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1"),
        re.compile(r"(?<!\w)(\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)"),
        re.compile(r"(`+)(.+?)\1"),
    )
    for pattern in paired_patterns:
        for match in pattern.finditer(segment):
            marker = match.group(1)
            marker_start = start + match.start()
            marker_end = start + match.end() - len(marker)
            hidden_markers.update(range(marker_start, marker_start + len(marker)))
            hidden_markers.update(range(marker_end, marker_end + len(marker)))

    position = start
    while position < end:
        char = source[position]
        if position in hidden_markers:
            position += 1
            continue
        if char == "\\" and position + 1 < end:
            builder.append(source[position + 1], position + 1)
            position += 2
            continue
        if source.startswith("<!--", position):
            close = source.find("-->", position + 4, end)
            if close >= 0:
                position = close + 3
                continue
        if char == "<":
            close = source.find(">", position + 1, end)
            if close >= 0 and re.match(r"</?[A-Za-z][^>]*>$", source[position:close + 1]):
                position = close + 1
                continue
        image = char == "!" and position + 1 < end and source[position + 1] == "["
        if char == "[" or image:
            label_start = position + 2 if image else position + 1
            label_end = source.find("]", label_start, end)
            if label_end >= 0 and label_end + 1 < end and source[label_end + 1] == "(":
                url_end = source.find(")", label_end + 2, end)
                if url_end >= 0:
                    _append_markdown_inline(builder, source, label_start, label_end)
                    position = url_end + 1
                    continue
        if char == "&":
            entity = re.match(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);", source[position:end])
            if entity:
                builder.append(html.unescape(entity.group(0)), position)
                position += len(entity.group(0))
                continue
        builder.append(char, position)
        position += 1


def _split_table_row(line):
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = re.split(r"(?<!\\)\|", stripped)
    normalized = [cell.replace("\\|", "|").strip() for cell in cells]
    return ["" if cell == "~~" else cell for cell in normalized]


def _split_table_row_raw(line):
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return re.split(r"(?<!\\)\|", stripped)


def _is_table_separator(line):
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _row_span_marker(cell):
    compact = cell.strip()
    return bool(re.fullmatch(r"_[\s_^=]*_", compact))


def _render_inline(value):
    escaped = html.escape(value, quote=False)
    protected = []

    def protect_image(match):
        token = f"\ue000{len(protected)}\ue001"
        alt = match.group(1).replace('"', "&quot;")
        source = match.group(2).replace('"', "&quot;")
        protected.append(
            f'<img src="{source}" alt="{alt}">'
        )
        return token

    def protect_link(match):
        token = f"\ue000{len(protected)}\ue001"
        label = match.group(1)
        target = match.group(2).replace('"', "&quot;")
        protected.append(f'<a href="{target}">{label}</a>')
        return token

    # Destinations commonly contain underscores. Protect complete media/link
    # spans so emphasis parsing cannot corrupt their URLs or attributes.
    escaped = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", protect_image, escaped)
    escaped = re.sub(r"\[([^]]+)\]\(([^)]+)\)", protect_link, escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", lambda m: f"<em>{m.group(1) or m.group(2)}</em>", escaped)
    escaped = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", escaped)
    for index, replacement in enumerate(protected):
        escaped = escaped.replace(f"\ue000{index}\ue001", replacement)
    return escaped


def _render_table(lines, start_line, errors):
    rows = [_split_table_row(line) for line in lines]
    column_count = len(rows[0])
    if len(rows) < 2 or not _is_table_separator(lines[1]):
        return None
    body_rows = [rows[0]] + rows[2:]
    for offset, row in enumerate(rows):
        if len(row) != column_count:
            errors.append(
                MarkupError(
                    f"Markdown 表格列数不一致：期望 {column_count} 列，实际 {len(row)} 列",
                    start_line + offset,
                    1,
                )
            )

    grid = []
    for row_index, row in enumerate(body_rows):
        padded = row[:column_count] + [""] * max(0, column_count - len(row))
        grid.append([
            {"text": cell, "colspan": 1, "rowspan": 1, "hidden": False, "valign": "middle"}
            for cell in padded
        ])
        original = lines[0] if row_index == 0 else lines[row_index + 1]
        raw_cells = _split_table_row_raw(original)
        for column in range(1, min(len(raw_cells), column_count)):
            # No characters between adjacent pipes means the cell to the left
            # spans this column. Whitespace (`| |`) remains a normal empty cell.
            if raw_cells[column] != "":
                continue
            anchor = column - 1
            while anchor >= 0 and grid[-1][anchor]["hidden"]:
                anchor -= 1
            if anchor >= 0:
                grid[-1][anchor]["colspan"] += 1
                grid[-1][column]["hidden"] = True

    for row in range(1, len(grid)):
        for column, cell in enumerate(grid[row]):
            if not _row_span_marker(cell["text"]):
                continue
            align_top = "^" in cell["text"]
            align_bottom = "=" in cell["text"]
            if align_top and align_bottom:
                errors.append(MarkupError("行合并标记不能同时包含 ^ 和 =", start_line + row + 1, 1))
            anchor_row = row - 1
            while anchor_row >= 0 and (not grid[anchor_row][column]["text"].strip() or grid[anchor_row][column]["hidden"]):
                anchor_row -= 1
            if anchor_row < 0:
                errors.append(MarkupError("行合并标记上方没有可合并单元格", start_line + row + 1, 1))
                continue
            anchor = grid[anchor_row][column]
            anchor["rowspan"] = row - anchor_row + 1
            anchor["valign"] = "top" if align_top else "bottom" if align_bottom else "middle"
            for hidden_row in range(anchor_row + 1, row + 1):
                grid[hidden_row][column]["hidden"] = True

    output = ["<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\">"]
    for row_index, row in enumerate(grid):
        output.append("<tr>")
        tag = "th" if row_index == 0 else "td"
        for cell in row:
            if cell["hidden"]:
                continue
            attrs = []
            if cell["colspan"] > 1:
                attrs.append(f'colspan="{cell["colspan"]}"')
            if cell["rowspan"] > 1:
                attrs.append(f'rowspan="{cell["rowspan"]}"')
            attrs.append(f'valign="{cell["valign"]}"')
            output.append(f"<{tag} {' '.join(attrs)}>{_render_inline(cell['text'])}</{tag}>")
        output.append("</tr>")
    output.append("</table>")
    return "".join(output)


def _project_markdown(source):
    builder = _ProjectionBuilder(source)
    errors = []
    lines = source.splitlines(keepends=True)
    html_parts = []
    source_position = 0
    line_index = 0
    in_fence = False
    fence_marker = None

    while line_index < len(lines):
        line = lines[line_index]
        content = line.rstrip("\r\n")
        newline_length = len(line) - len(content)
        fence = re.match(r"^\s*(`{3,}|~{3,})", content)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
                html_parts.append("<pre><code>")
            elif marker == fence_marker:
                in_fence = False
                fence_marker = None
                html_parts.append("</code></pre>")
            builder.newline(source_position + len(content))
            source_position += len(line)
            line_index += 1
            continue

        if in_fence:
            builder.append_slice(source_position, source_position + len(content))
            if newline_length:
                builder.append("\n", source_position + len(content))
            html_parts.append(html.escape(content) + "\n")
            source_position += len(line)
            line_index += 1
            continue

        if line_index + 1 < len(lines) and "|" in content and _is_table_separator(lines[line_index + 1].rstrip("\r\n")):
            table_lines = [content, lines[line_index + 1].rstrip("\r\n")]
            end_index = line_index + 2
            while end_index < len(lines) and "|" in lines[end_index]:
                table_lines.append(lines[end_index].rstrip("\r\n"))
                end_index += 1
            rendered = _render_table(table_lines, line_index + 1, errors)
            if rendered:
                html_parts.append(rendered)
            for table_line_index in range(line_index, end_index):
                raw = lines[table_line_index]
                raw_content = raw.rstrip("\r\n")
                if table_line_index != line_index + 1:
                    row_offset = source_position
                    for cell in _split_table_row(raw_content):
                        found = source.find(cell, row_offset, row_offset + len(raw_content) + 1)
                        if found >= 0 and not _row_span_marker(cell):
                            _append_markdown_inline(builder, source, found, found + len(cell))
                            builder.append("\t", found + len(cell))
                            row_offset = found + len(cell)
                    builder.newline(source_position + len(raw_content))
                source_position += len(raw)
            line_index = end_index
            continue

        prefix = re.match(r"^(\s{0,3})(#{1,6}\s+|>\s*|[-+*]\s+|\d+[.)]\s+)", content)
        visible_start = source_position + (prefix.end() if prefix else 0)
        _append_markdown_inline(builder, source, visible_start, source_position + len(content))
        if newline_length:
            builder.append("\n", source_position + len(content))

        rendered_line = content[prefix.end() if prefix else 0:]
        prefix_token = prefix.group(2) if prefix else ""
        if prefix and "#" in prefix_token:
            level = len(prefix.group(2).strip())
            html_parts.append(f"<h{level}>{_render_inline(rendered_line)}</h{level}>")
        elif prefix_token.startswith(">"):
            html_parts.append(f"<blockquote>{_render_inline(rendered_line)}</blockquote>")
        elif re.match(r"[-+*]\s", prefix_token):
            html_parts.append(f"<ul><li>{_render_inline(rendered_line)}</li></ul>")
        elif re.match(r"\d+[.)]\s", prefix_token):
            html_parts.append(f"<ol><li>{_render_inline(rendered_line)}</li></ol>")
        elif not rendered_line.strip():
            html_parts.append("<br>")
        else:
            html_parts.append(f"<p>{_render_inline(rendered_line)}</p>")
        source_position += len(line)
        line_index += 1

    if in_fence:
        errors.append(MarkupError("Markdown 代码围栏未闭合", len(lines), 1))
        html_parts.append("</code></pre>")
    visible, mapping = builder.finish()
    return MarkupProjection("markdown", source, visible, mapping, "".join(html_parts), errors)


def build_markup_projection(source, mode="plain"):
    mode = (mode or "plain").lower()
    if mode == "html":
        return _project_html(source)
    if mode == "markdown":
        return _project_markdown(source)
    return MarkupProjection(
        "plain", source, source, list(range(len(source) + 1)),
        f"<pre>{html.escape(source)}</pre>", [],
    )


def map_projection_opcodes(opcodes, projection_left, projection_right):
    return [
        (
            tag,
            projection_left.map_range_start(i1),
            projection_left.map_range_end(i2),
            projection_right.map_range_start(j1),
            projection_right.map_range_end(j2),
        )
        for tag, i1, i2, j1, j2 in opcodes
    ]


def projection_for_rendered_text(projection, rendered_plain_text):
    """Compose Qt's rendered plain-text positions back to source positions."""
    matcher = difflib.SequenceMatcher(
        None, projection.visible_text, rendered_plain_text, autojunk=False
    )
    opcodes = matcher.get_opcodes()
    mapping = []
    last_visible = 0
    for rendered_position in range(len(rendered_plain_text)):
        visible_position = None
        for tag, i1, i2, j1, j2 in opcodes:
            if j1 <= rendered_position < j2:
                if tag == "equal":
                    visible_position = i1 + (rendered_position - j1)
                elif i2 > i1 and j2 > j1:
                    visible_position = i1 + min(
                        (rendered_position - j1) * (i2 - i1) // (j2 - j1),
                        i2 - i1 - 1,
                    )
                else:
                    visible_position = i1
                break
        if visible_position is None:
            visible_position = last_visible
        last_visible = visible_position
        mapping.append(projection.map_position(visible_position))
    mapping.append(len(projection.source))
    return MarkupProjection(
        projection.mode,
        projection.source,
        rendered_plain_text,
        mapping,
        projection.rendered_html,
        list(projection.errors),
    )


def apply_visible_text_edit(source, projection, new_visible_text):
    """Apply visible-text edits while preserving hidden markup between characters."""
    old_visible = projection.visible_text
    mapping = projection.visible_to_source
    matcher = difflib.SequenceMatcher(None, old_visible, new_visible_text, autojunk=False)
    output = []
    source_cursor = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        start_source = mapping[i1]
        if start_source > source_cursor:
            output.append(source[source_cursor:start_source])
        if tag == "equal":
            end_source = mapping[i2]
            output.append(source[start_source:end_source])
            source_cursor = end_source
        elif tag == "insert":
            output.append(new_visible_text[j1:j2])
            source_cursor = start_source
        else:
            output.append(new_visible_text[j1:j2])
            for visible_index in range(i1, i2):
                gap_start = min(mapping[visible_index] + 1, len(source))
                gap_end = mapping[visible_index + 1]
                if gap_end > gap_start:
                    output.append(source[gap_start:gap_end])
            source_cursor = mapping[i2]
    if source_cursor < len(source):
        output.append(source[source_cursor:])
    return "".join(output)
