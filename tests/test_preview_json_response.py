import gzip
import json

import pytest
from pydantic import BaseModel

from image23mf.api.json_response import preview_json_response


class Result(BaseModel):
    title: str
    value: float
    missing: object = None


@pytest.mark.parametrize(
    "header,compressed",
    [
        ("gzip, deflate, br", True),
        ("GZIP; Q=0.5", True),
        ("*;q=1", True),
        ("gzip;q=0, *;q=1", False),
        ("gzip;q=0.001", True),
        ("gzip;q=oops", False),
        ("gzip;q=nan", False),
        ("gzip;q=2", False),
        ("gzip;q=-1", False),
        ("identity", False),
        ("", False),
    ],
)
def test_large_response_encoding_preserves_exact_model(header, compressed):
    result = Result(title="Prévisualisation — 検証 " * 1000, value=0.1234567890123456)
    response = preview_json_response(result, header)
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["content-type"] == "application/json"
    assert int(response.headers["content-length"]) == len(response.body)
    assert (response.headers.get("content-encoding") == "gzip") == compressed
    content = gzip.decompress(response.body) if compressed else response.body
    assert json.loads(content) == result.model_dump(mode="json")
    if compressed:
        assert len(response.body) < len(content) / 10


def test_small_results_do_not_pay_compression_overhead():
    response = preview_json_response(Result(title="Done", value=1), "gzip")
    assert "content-encoding" not in response.headers
