"""Error-body extraction: what turns an opaque HTTP 400 into a diagnosable one."""
import json

from opsramp.client import error_detail


class FakeResponse:
    def __init__(self, body: bytes, payload=None, raises=False):
        self.content = body
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises or self._payload is None:
            raise ValueError("not json")
        return self._payload


def from_json(payload):
    body = json.dumps(payload).encode()
    return FakeResponse(body, payload)


def test_message_field():
    assert error_detail(from_json({"message": "Invalid page size"})) == "Invalid page size"


def test_alternative_field_names():
    assert error_detail(from_json({"description": "queryString is required"})) == (
        "queryString is required"
    )
    assert error_detail(from_json({"errorMessage": "bad request"})) == "bad request"
    assert error_detail(from_json({"error_description": "expired"})) == "expired"


def test_nested_message():
    assert error_detail(from_json({"error": {"message": "nested reason"}})) == (
        "nested reason"
    )


def test_list_of_errors_is_joined():
    detail = error_detail(from_json([{"message": "first"}, {"message": "second"}]))
    assert detail == "first; second"


def test_plain_text_body_is_used():
    assert error_detail(FakeResponse(b"Bad Request: pageSize too large")) == (
        "Bad Request: pageSize too large"
    )


def test_whitespace_is_collapsed():
    assert error_detail(FakeResponse(b"line one\n   line two\t")) == "line one line two"


def test_html_error_page_is_ignored():
    body = b"<html><head><title>400</title></head><body>Bad Request</body></html>"
    assert error_detail(FakeResponse(body)) == ""


def test_long_bodies_are_truncated():
    detail = error_detail(FakeResponse(b"x" * 5000))
    assert len(detail) <= 303
    assert detail.endswith("...")


def test_empty_body():
    assert error_detail(FakeResponse(b"")) == ""


def test_unparseable_json_falls_back_to_text():
    assert error_detail(FakeResponse(b"{broken", raises=True)) == "{broken"
