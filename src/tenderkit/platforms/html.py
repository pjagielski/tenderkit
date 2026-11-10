"""Small HTML tree for platform tables; no scripts, styles or browser execution."""
from html.parser import HTMLParser


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def find_all(self, tag=None, *, attr=None, value=None, direct=False):
        found = []
        for child in self.children:
            if not isinstance(child, Node):
                continue
            matches = (tag is None or child.tag == tag) and (attr is None or (
                value(child.attrs.get(attr, '')) if callable(value) else
                value in child.attrs.get(attr, '').split() if attr == 'class' else child.attrs.get(attr) == value))
            if matches:
                found.append(child)
            if not direct:
                found.extend(child.find_all(tag, attr=attr, value=value))
        return found

    def find(self, tag=None, **kwargs):
        return next(iter(self.find_all(tag, **kwargs)), None)

    def text(self, separator='', strip=True):
        def strings(node):
            for child in node.children:
                if isinstance(child, str):
                    yield child.strip() if strip else child
                elif child.tag not in ('script', 'style'):
                    yield from strings(child)
        return separator.join(s for s in strings(self) if s)


class Tree(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        # HTML tables often omit the closing tags of adjacent rows/cells.
        if tag in ('tr', 'td', 'th') and self.stack[-1].tag == tag:
            self.stack.pop()
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)
