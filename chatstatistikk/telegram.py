"""Small Bot API client. Never log request URLs (they contain the token)."""
import json
import time
import urllib.error
import urllib.request
import uuid


class APIError(Exception):
    def __init__(self, code, description, retry_after=0):
        super().__init__(description)
        self.code, self.retry_after = code, retry_after


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.next_call = 0

    def call(self, method, **data):
        return self._send(method, json.dumps(data).encode(), 'application/json')

    def document(self, chat, content):
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="scoreboard.csv"\r\n'
                'Content-Type: text/csv; charset=utf-8\r\n\r\n').encode()
        body += content + f'\r\n--{boundary}--\r\n'.encode()
        return self._send('sendDocument', body, 'multipart/form-data; boundary=' + boundary)

    def _send(self, method, body, content_type):
        time.sleep(max(0, self.next_call - time.monotonic()))
        request = urllib.request.Request(self.base + method, data=body,
                                         headers={'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise APIError(error.code, 'Telegram HTTP error') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, 'Telegram network error; outcome may be unknown') from None
        self.next_call = time.monotonic() + 0.05
        if not result.get('ok'):
            delay = result.get('parameters', {}).get('retry_after', 0)
            self.next_call = time.monotonic() + delay
            raise APIError(result.get('error_code', 0), result.get('description', 'API error'), delay)
        return result['result']
