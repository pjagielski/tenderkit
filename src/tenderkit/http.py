"""Small bounded JSON transport; core does not require requests or an API key."""
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = 'tenderkit (+https://github.com/pjagielski/pharma-procurement-pipeline)'
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class HttpFailure(RuntimeError):
    def __init__(self, response):
        super().__init__(f'HTTP_{response.status_code}')
        self.response = response


class Response:
    def __init__(self, status, body, headers=None, url=None):
        self.status_code, self.body = status, body
        self.headers = headers or {}
        self.url = url

    @property
    def content(self):
        return self.body

    @property
    def text(self):
        return self.body.decode('utf-8', errors='replace')

    def iter_content(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index:index + size]

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HttpFailure(self)

    def json(self):
        return json.loads(self.body)


class Session:
    def __init__(self, opener=None):
        self.opener = opener

    def request(self, method, url, *, params=None, json=None, headers=None, timeout=60, stream=False):
        if params:
            url += '?' + urlencode(params)
        request_headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json', **(headers or {})}
        body = None
        if json is not None:
            # Avoid shadowing the public requests-compatible argument.
            from json import dumps
            body = dumps(json).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        request = Request(url, body, request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout) if self.opener else urlopen(request, timeout=timeout)
        except HTTPError as error:
            response = error
        if stream:
            return StreamingResponse(response)
        with response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError('Source response exceeds size limit')
            return Response(response.code, content, response.headers, response.geturl())

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


class StreamingResponse:
    def __init__(self, response):
        self.response = response
        self.status_code, self.headers = response.code, response.headers
        self.url = response.geturl()

    def iter_content(self, size):
        try:
            while block := self.response.read(size):
                yield block
        finally:
            self.close()

    def close(self):
        self.response.close()
