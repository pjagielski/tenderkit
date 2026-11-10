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
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HttpFailure(self)

    def json(self):
        return json.loads(self.body)


class Session:
    def request(self, method, url, *, params=None, json=None, headers=None, timeout=60):
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
            response = urlopen(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError('Source response exceeds size limit')
            return Response(response.code, content)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)
