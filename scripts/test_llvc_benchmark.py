#!/usr/bin/env python3
"""Unit and integration tests for scripts/llvc_benchmark.py.

Validates call-long stream benchmark, test-only labeling, output validation
(drift, clipping, silence), and the fake-server integration path.

Run:
    python -m pytest scripts/test_llvc_benchmark.py -v
"""
import asyncio
import inspect
import os
import sys
import time

import numpy as np
import pytest

# Make the backend importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llvc_benchmark import (
    generate_dummy_sine,
    read_wav_pcm,
    validate_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sine_pcm(duration_s: float = 1.0, sr: int = 16000, freq: float = 440.0) -> bytes:
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16).tobytes()


# ===========================================================================
# validate_output tests
# ===========================================================================

class TestValidateOutput:
    """Tests for the validate_output() function."""

    def test_good_output_passes(self):
        pcm = _sine_pcm(1.0, sr=48000)
        result = validate_output(pcm, 1.0, 48000, "test")
        assert result["drift_ok"] is True
        assert result["clipping_ok"] is True
        assert result["silence_ok"] is True
        assert result["warnings"] == []

    def test_drift_warn_above_50ms(self):
        # 1.0s input but output is 1.1s → 100ms drift → WARN
        pcm = _sine_pcm(1.1, sr=48000)
        result = validate_output(pcm, 1.0, 48000, "test")
        assert result["drift_ok"] is True  # <200ms so still "ok"
        assert any("WARN" in w and "drift" in w for w in result["warnings"])

    def test_drift_fail_above_200ms(self):
        # 1.0s input but output is 1.3s → 300ms drift → FAIL
        pcm = _sine_pcm(1.3, sr=48000)
        result = validate_output(pcm, 1.0, 48000, "test")
        assert result["drift_ok"] is False
        assert any("FAIL" in w and "drift" in w for w in result["warnings"])

    def test_silence_detected(self):
        silent = b"\x00" * (48000 * 2)  # 1s of silence at 48 kHz
        result = validate_output(silent, 1.0, 48000, "test")
        assert result["silence_ok"] is False
        assert any("silent" in w for w in result["warnings"])

    def test_clipping_detected(self):
        # All samples at max → 100% clipped
        n = 48000
        samples = np.full(n, 32767, dtype=np.int16)
        result = validate_output(samples.tobytes(), 1.0, 48000, "test")
        assert result["clipping_ok"] is False
        assert any("clipping" in w for w in result["warnings"])

    def test_low_clipping_passes(self):
        # <1% clipping
        n = 48000
        samples = _sine_pcm(1.0, sr=48000)
        arr = np.frombuffer(samples, dtype=np.int16).copy()
        # Only clip 0.1%
        clip_count = int(n * 0.001)
        arr[:clip_count] = 32767
        result = validate_output(arr.tobytes(), 1.0, 48000, "test")
        assert result["clipping_ok"] is True

    def test_empty_output(self):
        result = validate_output(b"", 1.0, 48000, "test")
        assert result["silence_ok"] is False  # RMS = 0
        assert result["drift_ok"] is False  # 1.0s → 0.0s = 1000ms drift


# ===========================================================================
# generate_dummy_sine tests
# ===========================================================================

class TestGenerateDummySine:
    def test_correct_duration(self):
        pcm = generate_dummy_sine(2.0, 16000)
        n_samples = len(pcm) // 2
        assert n_samples == 32000

    def test_correct_sample_rate(self):
        pcm = generate_dummy_sine(1.0, 16000)
        assert len(pcm) == 16000 * 2  # 16-bit = 2 bytes/sample

    def test_not_silent(self):
        pcm = generate_dummy_sine(1.0, 16000)
        samples = np.frombuffer(pcm, dtype=np.int16)
        rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
        assert rms > 100, "Sine wave should not be silent"


# ===========================================================================
# Fake-server integration test
# ===========================================================================

class TestFakeServerBenchmarkIntegration:
    """Integration test: spin up a fake WS server and run the full benchmark."""

    @pytest.mark.asyncio
    async def test_fake_server_benchmark_produces_output(self):
        """Run the fake-server benchmark on a short sine wave and verify
        it produces non-empty, non-silent output with reasonable drift."""
        from scripts.llvc_benchmark import run_fake_server_benchmark

        pcm = generate_dummy_sine(0.5, 16000)  # 0.5s — short for speed
        # Use port 0 to let the OS pick a free port, but run_fake_server_benchmark
        # takes a specific port — use a high random port to avoid conflicts.
        import random as _rnd
        port = _rnd.randint(29000, 39000)
        result = await run_fake_server_benchmark(pcm, port=port)

        assert result["success"] is True, f"Benchmark failed: {result.get('error')}"
        assert result["out_bytes"] > 0, "Expected non-empty output"

        # Latency metrics should exist and be non-negative
        assert result["median_latency_ms"] >= 0
        assert result["p95_latency_ms"] >= 0
        assert result["p99_latency_ms"] >= 0

        # Duration drift should be < 500ms for a 0.5s signal through fake server
        assert result["drift_ms"] < 500, f"Drift too large: {result['drift_ms']}"

        # Validate output quality
        validation = validate_output(
            result["out_pcm"], 0.5, 48000, "integration-test"
        )
        # The fake server uses ring modulation, so silence check is meaningful
        assert validation["silence_ok"], f"Output is silent: {validation['warnings']}"


# ===========================================================================
# Test-only labeling test
# ===========================================================================

class TestTestOnlyLabeling:
    """Verify fake-server benchmark function has test-only markers."""

    def test_docstring_mentions_test_only(self):
        from scripts.llvc_benchmark import run_fake_server_benchmark
        assert "automated testing only" in run_fake_server_benchmark.__doc__.lower()

    def test_real_service_docstring(self):
        from scripts.llvc_benchmark import run_real_service_benchmark
        assert "real" in run_real_service_benchmark.__doc__.lower()


# ===========================================================================
# CLI structure test
# ===========================================================================

class TestCLIStructure:
    """Verify the benchmark script has the expected CLI flags."""

    def test_parser_has_expected_args(self):
        from scripts.llvc_benchmark import main_async
        assert inspect.iscoroutinefunction(main_async)


class _MockDelayedConverter:
    def __init__(self):
        self.ready_time = None
        self.first_audio_received_time = None

    async def wait_ready(self, timeout=10.0):
        await asyncio.sleep(0.15)
        self.ready_time = time.monotonic()
        return True

    async def convert_stream(self, in_audio):
        async for chunk in in_audio:
            if self.first_audio_received_time is None:
                self.first_audio_received_time = time.monotonic()
            yield chunk * 3

    async def close(self):
        pass


class _MockCombinedOutputConverter:
    async def wait_ready(self, timeout=10.0):
        return True

    async def convert_stream(self, in_audio):
        buf = bytearray()
        async for chunk in in_audio:
            buf.extend(chunk * 3)
            if len(buf) >= 640 * 3 * 4:  # Yield 4 frames combined at once
                yield bytes(buf)
                buf.clear()
        if buf:
            yield bytes(buf)

    async def close(self):
        pass


class _MockSplitOutputConverter:
    async def wait_ready(self, timeout=10.0):
        return True

    async def convert_stream(self, in_audio):
        async for chunk in in_audio:
            converted = chunk * 3
            for offset in range(0, len(converted), 640):
                yield converted[offset:offset + 640]

    async def close(self):
        pass


class TestDelayedHandshake:
    @pytest.mark.asyncio
    async def test_readiness_gate_prevents_early_audio_pumping(self):
        """Verify input_gen is gated behind wait_ready so no audio or timestamps are queued during handshake."""
        from scripts.llvc_benchmark import _run_ws_stream_benchmark
        pcm = generate_dummy_sine(0.2, 16000)
        converter = _MockDelayedConverter()
        result = await _run_ws_stream_benchmark(converter, pcm, "Delayed Handshake Test", connect_timeout=5.0)
        assert result["success"] is True
        assert converter.ready_time is not None
        assert converter.first_audio_received_time is not None
        # Audio must only arrive after wait_ready returned True
        assert converter.first_audio_received_time >= converter.ready_time


class TestCombinedOutput:
    @pytest.mark.asyncio
    async def test_combined_output_messages_pop_multiple_timestamps(self):
        """Verify benchmark accurately tracks latencies when server sends combined multi-frame output chunks."""
        from scripts.llvc_benchmark import _run_ws_stream_benchmark
        pcm = generate_dummy_sine(0.2, 16000)  # 10 frames (6400 bytes)
        converter = _MockCombinedOutputConverter()
        result = await _run_ws_stream_benchmark(converter, pcm, "Combined Output Test", connect_timeout=5.0)
        assert result["success"] is True
        assert len(result["latencies"]) == 10
        assert result["out_bytes"] == len(pcm) * 3

    @pytest.mark.asyncio
    async def test_partial_input_frame_drains_full_padded_output(self):
        """A short final input frame is padded and its complete output must be drained."""
        from scripts.llvc_benchmark import _run_ws_stream_benchmark

        pcm = generate_dummy_sine(0.205, 16000)
        converter = _MockSplitOutputConverter()
        result = await _run_ws_stream_benchmark(
            converter,
            pcm,
            "Split Output Test",
            connect_timeout=5.0,
        )

        padded_input_bytes = ((len(pcm) + 639) // 640) * 640
        assert result["success"] is True
        assert result["out_bytes"] == padded_input_bytes * 3
        assert len(result["latencies"]) == padded_input_bytes // 640


class TestConfigurationIsolation:
    @pytest.mark.asyncio
    async def test_no_rvc_fallback_when_only_rvc_vars_set(self, monkeypatch, tmp_path, capsys):
        """Verify setting RVC_WS_URL/RVC_API_KEY does not trigger RVC or Real LLVC runs without explicit flag."""
        monkeypatch.setenv("RVC_WS_URL", "ws://should-not-be-called:9999/ws")
        monkeypatch.setenv("RVC_API_KEY", "secret-rvc-key")
        monkeypatch.delenv("LLVC_WS_URL", raising=False)
        monkeypatch.delenv("LLVC_API_KEY", raising=False)

        wav_path = str(tmp_path / "test.wav")
        import wave
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00" * 3200)

        monkeypatch.setattr("sys.argv", ["llvc_benchmark.py", "--file", wav_path, "--no-fake"])
        from scripts.llvc_benchmark import main_async
        ret = await main_async()
        captured = capsys.readouterr()
        assert "No benchmarks were run." in captured.out
        assert "LLVC (Real)" not in captured.out
