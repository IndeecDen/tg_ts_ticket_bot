"""Bounded HTML messages; split text without cutting tags or entities."""
import html
from html.parser import HTMLParser


def utf16_size(text):
    return len(text.encode('utf-16-le')) // 2


def clip(text, budget):
    if utf16_size(text) <= budget:
        return text
    result = []
    size = 1
    for char in text:
        size += utf16_size(char)
        if size > budget:
            break
        result.append(char)
    return ''.join(result) + '…'


def escaped_clip(text, budget):
    result = ''
    for char in text:
        escaped = html.escape(char)
        if utf16_size(result + escaped) > budget - 1:
            return result + '…'
        result += escaped
    return result


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text):
    parser = _PlainText()
    parser.feed(text)
    return ''.join(parser.parts)


class _Splitter(HTMLParser):
    tags = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
            'span', 'tg-spoiler', 'a', 'code', 'pre', 'blockquote', 'tg-emoji'}
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.stack = []
        self.current = ''
        self.chunks = []
        self.has_text = False

    def closing(self):
        return ''.join(f'</{tag}>' for tag, _ in reversed(self.stack))

    def flush(self):
        if self.has_text:
            self.chunks.append(self.current + self.closing())
        self.current = ''.join(raw for _, raw in self.stack)
        self.has_text = False

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        if tag not in self.tags:
            self.handle_data(raw)
            return
        if utf16_size(self.current + raw + f'</{tag}>' + self.closing()) > self.limit:
            self.flush()
        self.current += raw
        self.stack.append((tag, raw))

    def handle_endtag(self, tag):
        if tag not in self.tags:
            self.handle_data(f'</{tag}>')
            return
        if self.stack and self.stack[-1][0] == tag:
            self.current += f'</{tag}>'
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_data(self.get_starttag_text())

    def handle_data(self, data):
        for char in data:
            escaped = html.escape(char, quote=False)
            if utf16_size(self.current + escaped + self.closing()) > self.limit:
                self.flush()
            self.current += escaped
            self.has_text = True


def split_text(text, parse_mode='HTML', limit=4000):
    if parse_mode == 'HTML':
        parser = _Splitter(limit)
        parser.feed(text)
        parser.flush()
        return parser.chunks or [text]
    chunks, current, size = [], [], 0
    for char in text:
        units = utf16_size(char)
        if size + units > limit:
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += units
    if current:
        chunks.append(''.join(current))
    return chunks or [text]
