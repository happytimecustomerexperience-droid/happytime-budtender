from types import SimpleNamespace


def test_embedding_2_uses_api_key_and_separate_retrieval_contents(monkeypatch):
    from core import constants
    from core.services import gemini

    captured = {}

    class FakeModels:
        def embed_content(self, *, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.5] * constants.EMBED_DIM) for _ in contents]
            )

    monkeypatch.setattr(
        gemini,
        "make_client",
        lambda api_key=None, *, force_api_key=False: (
            SimpleNamespace(models=FakeModels()),
            "api-key",
        ),
    )
    monkeypatch.setattr(gemini, "_RESOLVED_EMBED_MODEL", None)
    monkeypatch.setattr(constants, "MODELS", {**constants.MODELS, "embedding": "gemini-embedding-2"})

    vectors = gemini.embed(["return policy", "July specials"], task_type="RETRIEVAL_DOCUMENT")

    assert len(vectors) == 2
    assert all(len(vector) == constants.EMBED_DIM for vector in vectors)
    assert captured["model"] == "gemini-embedding-2"
    assert len(captured["contents"]) == 2
    assert captured["contents"][0].parts[0].text == "title: none | text: return policy"
    assert captured["config"].output_dimensionality == constants.EMBED_DIM
    assert captured["config"].task_type is None


def test_embedding_2_requires_gemini_api_key(monkeypatch):
    from core.services import gemini

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    try:
        gemini.make_client(force_api_key=True)
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)
    else:  # pragma: no cover - the test environment must not have a hidden key
        raise AssertionError("Embedding 2 unexpectedly found a Gemini API key")
