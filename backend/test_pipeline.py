import asyncio
import numpy as np
import time
from unittest.mock import AsyncMock, patch
import httpx

from backend.noise.noise_suppressor import WebRTCNoiseSuppressor
from backend.converters.dummy import DummyVoiceConverter
from backend.converters.rvc import RVCVoiceConverter

async def test_noise_suppressor():
    print("\n--- Testing Noise Suppressor ---")
    
    # Initialize WebRTC Noise Suppressor
    suppressor = WebRTCNoiseSuppressor(ns_level=3)
    
    # 10ms of 16kHz mono audio is 160 samples (320 bytes)
    # Generate a dummy frame with silent and loud random noise
    np.random.seed(42)
    noise_frame = np.random.randint(-10000, 10000, 160, dtype=np.int16).tobytes()
    
    # Process frame
    start_t = time.perf_counter()
    denoised_frame = suppressor.process_frame(noise_frame)
    duration_ms = (time.perf_counter() - start_t) * 1000.0
    
    print(f"WebRTC Denoising took {duration_ms:.3f}ms")
    print(f"Input size: {len(noise_frame)} bytes, Output size: {len(denoised_frame)} bytes")
    assert len(denoised_frame) == len(noise_frame), "Output size mismatch!"
    
    # Check if amplitude was suppressed
    in_amp = np.mean(np.abs(np.frombuffer(noise_frame, dtype=np.int16)))
    out_amp = np.mean(np.abs(np.frombuffer(denoised_frame, dtype=np.int16)))
    print(f"Input average amplitude: {in_amp:.1f}")
    print(f"Denoised average amplitude: {out_amp:.1f}")
    # When the native webrtc-noise-gain lib is present the suppressor must reduce
    # amplitude; when it's missing (e.g. Windows dev boxes) the suppressor degrades
    # to passthrough, so only assert suppression if the processor is actually active.
    if getattr(suppressor, "_active", False):
        assert out_amp < in_amp, "Noise was not suppressed!"
        print("Noise Suppressor Test: SUCCESS (native suppression active)")
    else:
        assert out_amp == in_amp, "Passthrough must not alter audio when suppressor is inactive!"
        print("Noise Suppressor Test: SUCCESS (passthrough — native lib unavailable)")

async def test_dummy_converter():
    print("\n--- Testing Dummy Voice Converter ---")
    
    converter = DummyVoiceConverter(carrier_frequency=120.0)
    
    # Generate 500ms of input audio (50 frames of 10ms = 8000 samples @ 16kHz)
    t = np.linspace(0, 0.5, 8000, endpoint=False)
    sine_wave = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16) # 440Hz tone
    input_bytes = sine_wave.tobytes()
    
    # Create an async generator that yields the input audio in 10ms (320 bytes) chunks
    async def chunk_generator():
        chunk_size = 320
        for i in range(0, len(input_bytes), chunk_size):
            yield input_bytes[i:i+chunk_size]
            await asyncio.sleep(0.001) # simulate brief streaming arrival

    start_t = time.perf_counter()
    converted_chunks = []
    
    async for converted_chunk in converter.convert_stream(chunk_generator()):
        converted_chunks.append(converted_chunk)
        
    duration_ms = (time.perf_counter() - start_t) * 1000.0
    output_bytes = b"".join(converted_chunks)
    
    print(f"Dummy conversion took {duration_ms:.2f}ms for 500ms of audio")
    print(f"Input size: {len(input_bytes)} bytes, Output size: {len(output_bytes)} bytes")
    # DummyVoiceConverter now upsamples 16kHz -> 48kHz (3x) to match the
    # streaming converter output contract (see backend/converters/dummy.py).
    assert len(output_bytes) == len(input_bytes) * 3, "Conversion output size mismatch!"

    # Ensure audio was modified. Undo the 3x sample repeat before comparing so
    # the two arrays are the same shape as the original 16kHz signal.
    in_arr = np.frombuffer(input_bytes, dtype=np.int16)
    out_arr = np.frombuffer(output_bytes, dtype=np.int16)[::3]
    diff = np.mean(np.abs(in_arr - out_arr))
    print(f"Average signal modification delta: {diff:.1f}")
    assert diff > 10.0, "Audio was not modified by the dummy converter!"
    print("Dummy Voice Converter Test: SUCCESS")

async def test_rvc_converter_mocked():
    print("\n--- Testing RVC Converter with Mocked GPU Endpoint ---")
    
    endpoint_url = "https://mock-rvc-gpu-endpoint.modal.run"
    converter = RVCVoiceConverter(
        endpoint_url=endpoint_url,
        api_key="mock_secret_key",
        pitch_shift=2,
        budget_ms=3000.0
    )
    
    # Generate 400ms of dummy input audio
    t = np.linspace(0, 0.4, 6400, endpoint=False)
    input_bytes = (np.sin(2 * np.pi * 440 * t) * 5000).astype(np.int16).tobytes()
    
    async def chunk_generator():
        yield input_bytes

    # Create dummy mock response PCM bytes (half-amplitude as modification)
    mocked_pcm = (np.frombuffer(input_bytes, dtype=np.int16) // 2).tobytes()

    # Mock the HTTP POST request to the remote convert endpoint
    mock_request = httpx.Request("POST", f"{endpoint_url}/convert")
    mock_response = httpx.Response(200, content=mocked_pcm, request=mock_request)
    
    with patch.object(converter.client, 'post', new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        start_t = time.perf_counter()
        converted_chunks = []
        async for chunk in converter.convert_stream(chunk_generator()):
            converted_chunks.append(chunk)
            
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        output_bytes = b"".join(converted_chunks)
        
        # Verify the mock endpoint was hit with headers and parameters.
        # RVCVoiceConverter POSTs to endpoint_url verbatim (the deployed URL already
        # includes the /convert path), so assert against endpoint_url directly.
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == endpoint_url
        assert kwargs["params"] == {"pitch_shift": 2}
        assert "Authorization" in kwargs["headers"]
        assert kwargs["headers"]["Authorization"] == "Bearer mock_secret_key"
        
        print(f"RVC conversion (mocked) completed in {duration_ms:.2f}ms")
        print(f"Input size: {len(input_bytes)} bytes, Output size: {len(output_bytes)} bytes")
        
        assert len(output_bytes) == len(mocked_pcm), "Mock output size mismatch!"
        assert output_bytes == mocked_pcm, "Output bytes do not match expected mocked response!"
        
    await converter.close()
    print("RVC Voice Converter Test: SUCCESS")

async def main():
    print("Running automated pipeline verification tests...")
    await test_noise_suppressor()
    await test_dummy_converter()
    await test_rvc_converter_mocked()
    print("\nAll automated verification tests completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
