"""YouReel: client error reports never store query strings (tokens in URLs) and keep stack positions."""

from server.routers.diagnostics import strip_path_query, strip_text_queries


def test_path_loses_query_string_and_fragment():
    assert strip_path_query("/api/v1/files/x.png?token=secret#frag") == "/api/v1/files/x.png"
    assert strip_path_query("/app/projects/demo#tab") == "/app/projects/demo"
    assert strip_path_query("/app/projects") == "/app/projects"
    assert strip_path_query(None) is None


def test_urls_in_text_lose_queries_but_keep_line_and_column():
    stack = (
        "TypeError: x is undefined\n"
        "    at f (http://127.0.0.1:5173/src/a.tsx?t=1700000000:10:5)\n"
        "    at http://studio.example/assets/index-abc.js:1:2345\n"
        "fetch https://studio.example/api/v1/files/a.mp4?token=secret failed"
    )
    assert strip_text_queries(stack) == (
        "TypeError: x is undefined\n"
        "    at f (http://127.0.0.1:5173/src/a.tsx:10:5)\n"
        "    at http://studio.example/assets/index-abc.js:1:2345\n"
        "fetch https://studio.example/api/v1/files/a.mp4 failed"
    )
