from app.usage import metering


def test_gpt_5_6_model_uses_o200k_base(monkeypatch) -> None:
    requested_encodings: list[str] = []
    expected_encoding = object()

    def fake_get_encoding(name: str):
        requested_encodings.append(name)
        return expected_encoding

    monkeypatch.setattr(metering.tiktoken, "get_encoding", fake_get_encoding)
    metering._encoding_for_model.cache_clear()

    assert metering._encoding_for_model("gpt-5.6-terra") is expected_encoding
    assert requested_encodings == ["o200k_base"]

    metering._encoding_for_model.cache_clear()
