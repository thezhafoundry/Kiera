import numpy as np
import faiss
import pytest

from worker import _cached_read_index, _install_faiss_index_cache


@pytest.fixture
def small_index_file(tmp_path):
    """A real, tiny FAISS IVF-Flat index on disk, shaped like the
    production added_*.index files (IVF + Flat quantizer)."""
    dim = 8
    vectors = np.random.RandomState(0).rand(50, dim).astype("float32")
    quantizer = faiss.IndexFlatL2(dim)
    index = faiss.IndexIVFFlat(quantizer, dim, 4)
    index.train(vectors)
    index.add(vectors)
    path = tmp_path / "test.index"
    faiss.write_index(index, str(path))
    return str(path), vectors


def test_cached_read_index_returns_same_object_on_repeat_calls(small_index_file):
    path, _ = small_index_file
    _install_faiss_index_cache()

    first = faiss.read_index(path)
    second = faiss.read_index(path)

    assert first is second


def test_cached_read_index_reconstruct_n_matches_real_reconstruction(small_index_file):
    path, vectors = small_index_file
    _install_faiss_index_cache()

    index = faiss.read_index(path)
    reconstructed = index.reconstruct_n(0, index.ntotal)

    assert reconstructed.shape == vectors.shape
    # reconstruct_n on an IVF-Flat index returns vectors in the order they
    # were added, matching the original training set exactly.
    np.testing.assert_allclose(reconstructed, vectors, rtol=1e-5)


def test_cached_reconstruct_n_ignores_disk_after_first_call(small_index_file, monkeypatch):
    path, _ = small_index_file
    _install_faiss_index_cache()

    faiss.read_index(path)  # primes the cache
    call_count = {"n": 0}
    real_open = open

    def counting_open(*args, **kwargs):
        if str(args[0]) == path:
            call_count["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    faiss.read_index(path)  # should be served from cache, no disk access

    assert call_count["n"] == 0
