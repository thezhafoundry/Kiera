"""Tests for the production RVC WebSocket benchmark."""

import asyncio

import pytest

from scripts.rvc_stream_benchmark import (
    percentile,
    run_stream_benchmark,
)


class _FakeRVCConverter:
    def __init__(self) -> None:
        self.is_healthy = False
        self.on_stats = None
        self.profile = "baseline"
        self.model_version = "rvc-test"
        self.block_ms = 320
        self.context_ms = 400
        self.sola_ms = 80
        self.use_trt = True
        self.drop_count = 0
        self.stale_input_drop_count = 0
        self.input_overflow_drop_count = 0
        self.output_drop_count = 0
        self.connection_failure_count = 0
        self.closed = False

    async def convert_stream(self, in_audio):
        self.is_healthy = True
        sequence = 0
        async for frame in in_audio:
            sequence += 1
            if self.on_stats is not None:
                self.on_stats({
                    "sequence_id": sequence,
                    "infer_ms": 12.0,
                    "converter_wait_ms": 345.0,
                    "network_rtt_ms": 13.0,
                })
            yield frame * 3
        self.is_healthy = False

    async def close(self):
        self.closed = True
        self.is_healthy = False


def test_percentile_uses_linear_interpolation() -> None:
    assert percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert percentile([10.0, 20.0], 95) == pytest.approx(19.5)
    assert percentile([], 95) == 0.0


@pytest.mark.asyncio
async def test_stream_benchmark_uses_one_active_session_and_collects_stats() -> None:
    converter = _FakeRVCConverter()
    pcm = b"\x01\x00" * 3200  # 200 ms at 16 kHz

    result = await run_stream_benchmark(
        converter,
        pcm,
        pace_seconds=0.0,
        readiness_timeout=1.0,
        drain_timeout=1.0,
    )

    assert result["success"] is True
    assert result["input_duration_ms"] == 200.0
    assert result["output_duration_ms"] == 200.0
    assert result["stats_count"] == 10
    assert result["infer_ms"]["p95"] == 12.0
    assert result["converter_wait_ms"]["p95"] == 345.0
    assert result["network_rtt_ms"]["p95"] == 13.0
    assert result["profile"] == "baseline"
    assert result["model_version"] == "rvc-test"
    assert result["use_trt"] is True
    assert result["drops"]["total"] == 0
    assert converter.closed is True


@pytest.mark.asyncio
async def test_stream_benchmark_fails_when_active_session_never_becomes_ready() -> None:
    class NeverReady(_FakeRVCConverter):
        async def convert_stream(self, in_audio):
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

    converter = NeverReady()
    result = await run_stream_benchmark(
        converter,
        b"\x00\x00" * 320,
        pace_seconds=0.0,
        readiness_timeout=0.05,
        drain_timeout=0.05,
    )

    assert result["success"] is False
    assert "active RVC session" in result["error"]
    assert converter.closed is True
