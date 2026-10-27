"""Fixed-host GitHub requests and local webhook delivery without redirects."""

import json
import urllib.error
import urllib.parse
import urllib.request


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("HTTP redirects are disabled")


def open_request(request):
    try:
        return urllib.request.build_opener(NoRedirects).open(request, timeout=20)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


def request_json(url, token, payload=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.github.com"
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("Refusing a non-GitHub API URL")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "User-Agent": "hermes-github-mentions/0.1.0", "X-GitHub-Api-Version": "2022-11-28"},
    )
    with open_request(req) as response:
        return json.load(response), response.headers
