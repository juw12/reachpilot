"""Official LinkedIn API client: OAuth (Sign In with LinkedIn + Share on LinkedIn), images, posts, comments.

Uses only the self-serve products, so it stays within LinkedIn's terms.
Tokens last 60 days and self-serve apps get no refresh token: re-run `autopilot.py auth` when warned.
"""
from __future__ import annotations

import re
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API = "https://api.linkedin.com"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = "openid profile w_member_social"

LITTLE_TEXT_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def to_little_text(text: str) -> str:
    """Posts API 'commentary' uses little-text format: escape reserved chars, keep hashtags clickable."""
    pieces = re.split(r"(#[A-Za-z][A-Za-z0-9_]*)", text)
    output = []
    for piece in pieces:
        if re.fullmatch(r"#[A-Za-z][A-Za-z0-9_]*", piece):
            output.append("{hashtag|\\#|" + piece[1:] + "}")
        else:
            output.append(LITTLE_TEXT_RESERVED.sub(r"\\\1", piece))
    return "".join(output)


def run_oauth(client_id: str, client_secret: str) -> dict:
    state = secrets.token_urlsafe(16)
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>ReachPilot is connected to LinkedIn. You can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": SCOPES,
    })
    server = HTTPServer(("localhost", 8765), Handler)
    server.timeout = 300
    print("Opening LinkedIn sign-in in your browser...")
    webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in received and "error" not in received and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if received.get("state") != state or "code" not in received:
        raise RuntimeError(f"OAuth failed: {received.get('error_description') or received or 'timed out'}")

    token_response = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": received["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=30)
    token_response.raise_for_status()
    token = token_response.json()

    profile = requests.get(
        f"{API}/v2/userinfo",
        headers={"Authorization": f"Bearer {token['access_token']}"},
        timeout=30,
    ).json()
    return {
        "access_token": token["access_token"],
        "expires_at": int(time.time()) + int(token.get("expires_in", 0)),
        "person_urn": f"urn:li:person:{profile['sub']}",
        "name": profile.get("name", ""),
    }


class LinkedInClient:
    def __init__(self, token: dict, api_version: str):
        self.author = token["person_urn"]
        self.access_token = token["access_token"]
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "LinkedIn-Version": api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _check(self, response: requests.Response, what: str) -> requests.Response:
        if response.status_code >= 300:
            raise RuntimeError(f"{what} failed: HTTP {response.status_code} {response.text[:400]}")
        return response

    def upload_image(self, path) -> str:
        init = self._check(requests.post(
            f"{API}/rest/images?action=initializeUpload",
            headers=self.headers,
            json={"initializeUploadRequest": {"owner": self.author}},
            timeout=30,
        ), "image init").json()["value"]
        with open(path, "rb") as handle:
            self._check(requests.put(
                init["uploadUrl"],
                headers={"Authorization": f"Bearer {self.access_token}"},
                data=handle.read(),
                timeout=120,
            ), "image upload")
        image_urn = init["image"]
        for _ in range(15):
            status = requests.get(
                f"{API}/rest/images/{urllib.parse.quote(image_urn)}", headers=self.headers, timeout=30
            )
            if status.ok and status.json().get("status") == "AVAILABLE":
                break
            time.sleep(2)
        return image_urn

    def create_post(self, text: str, images: list[tuple[str, str]]) -> str:
        body = {
            "author": self.author,
            "commentary": to_little_text(text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if len(images) == 1:
            body["content"] = {"media": {"id": images[0][0], "altText": images[0][1]}}
        elif len(images) > 1:
            body["content"] = {"multiImage": {"images": [{"id": urn, "altText": alt} for urn, alt in images[:20]]}}
        response = self._check(requests.post(f"{API}/rest/posts", headers=self.headers, json=body, timeout=60), "post")
        return response.headers.get("x-restli-id", "")

    def comment(self, post_urn: str, text: str) -> None:
        self._check(requests.post(
            f"{API}/rest/socialActions/{urllib.parse.quote(post_urn)}/comments",
            headers=self.headers,
            json={"actor": self.author, "object": post_urn, "message": {"text": text}},
            timeout=30,
        ), "first comment")

    def get_commentary(self, post_urn: str) -> str:
        """The post text as LinkedIn stores it (little-text escaped)."""
        response = self._check(requests.get(
            f"{API}/rest/posts/{urllib.parse.quote(post_urn, safe='')}",
            headers=self.headers,
            timeout=30,
        ), "read post")
        return response.json().get("commentary", "")

    def set_commentary(self, post_urn: str, commentary: str) -> None:
        """Edit the post text in place (needs only w_member_social, unlike comments)."""
        self._check(requests.post(
            f"{API}/rest/posts/{urllib.parse.quote(post_urn, safe='')}",
            headers={**self.headers, "X-RestLi-Method": "PARTIAL_UPDATE"},
            json={"patch": {"$set": {"commentary": commentary}}},
            timeout=30,
        ), "edit post")

    def inline_comment(self, post_urn: str, text: str) -> None:
        """Fallback when commenting is not permitted: swap the 'links in the comment' line for the links."""
        self.set_commentary(post_urn, inline_links(self.get_commentary(post_urn), to_little_text(text)))

    @staticmethod
    def post_url(post_urn: str) -> str:
        return f"https://www.linkedin.com/feed/update/{post_urn}/"


COMMENT_POINTER = re.compile(r"\b(in|see) the (first )?comments?\b|\blinks? (is |are )?in the comments?\b", re.I)


def inline_links(commentary: str, links: str) -> str:
    """Replace short 'see the first comment' lines with the links; append them if no pointer line exists."""
    lines = commentary.split("\n")
    replaced = False
    for index, line in enumerate(lines):
        if COMMENT_POINTER.search(line) and len(line) < 90:
            lines[index] = links if not replaced else ""
            replaced = True
    if replaced:
        return "\n".join(lines)
    return commentary.rstrip() + "\n\n" + links
