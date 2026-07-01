import os
import ctypes
import numpy as np
from abc import ABC, abstractmethod

class NoiseSuppressor(ABC):
    """
    Abstract base class for real-time noise suppressors.
    """
    
    @abstractmethod
    def process_frame(self, frame_bytes: bytes) -> bytes:
        """
        Processes 10ms of 16-bit PCM mono audio.
        For 16kHz, this is 160 samples (320 bytes).
        """
        pass

class WebRTCNoiseSuppressor(NoiseSuppressor):
    """
    Noise suppressor using the webrtc-noise-gain wrapper.
    Operates natively on 16kHz 16-bit mono PCM.
    """
    
    def __init__(self, ns_level: int = 3, auto_gain_dbfs: int = 0):
        """
        ns_level: Noise suppression level [0, 4] (4 is max).
        auto_gain_dbfs: Automatic Gain Control level [0, 31] (0 is disable).
        """
        try:
            from webrtc_noise_gain import AudioProcessor
            self.processor = AudioProcessor(auto_gain_dbfs, ns_level)
            self._active = True
            print(f"[Noise Suppression] Initialized WebRTC Noise Suppressor (Level {ns_level})")
        except ImportError:
            print("[Noise Suppression WARNING] webrtc-noise-gain is not installed! Noise suppression is bypassed.")
            self._active = False

    def process_frame(self, frame_bytes: bytes) -> bytes:
        if not self._active or len(frame_bytes) != 320:
            return frame_bytes
        try:
            result = self.processor.Process10ms(frame_bytes)
            return result.audio
        except Exception as e:
            # Fallback to raw frame on any processing error
            return frame_bytes

class RNNoiseSuppressor(NoiseSuppressor):
    """
    Noise suppressor using the C-compiled RNNoise library loaded via ctypes.
    RNNoise natively expects 480 samples of float32 mono @ 48kHz (10ms).
    This suppressor handles conversion and resampling.
    """
    
    def __init__(self, lib_path: str = None):
        if lib_path is None:
            # Try loading from local libs directory first
            local_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            lib_path = os.path.join(local_dir, "libs", "librnnoise.dylib")
            if not os.path.exists(lib_path):
                lib_path = "librnnoise.dylib" # fallback to system path
                
        try:
            self.lib = ctypes.CDLL(lib_path)
            
            # Define C signatures
            self.lib.rnnoise_create.argtypes = [ctypes.c_void_p]
            self.lib.rnnoise_create.restype = ctypes.c_void_p
            
            self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
            self.lib.rnnoise_destroy.restype = None
            
            self.lib.rnnoise_process_frame.argtypes = [
                ctypes.c_void_p, 
                ctypes.POINTER(ctypes.c_float), 
                ctypes.POINTER(ctypes.c_float)
            ]
            self.lib.rnnoise_process_frame.restype = ctypes.c_float
            
            self.state = self.lib.rnnoise_create(None)
            self._active = True
            print(f"[Noise Suppression] Initialized RNNoise Suppressor using library: {lib_path}")
        except Exception as e:
            print(f"[Noise Suppression WARNING] Could not load RNNoise library: {e}. Noise suppression is bypassed.")
            self._active = False

    def process_frame(self, frame_bytes: bytes) -> bytes:
        if not self._active:
            return frame_bytes
            
        # WebRTC/LiveKit sends 10ms of 16kHz PCM (320 bytes = 160 samples)
        # RNNoise expects exactly 480 samples of float32 @ 48kHz (1920 bytes)
        # We must:
        # 1. Convert int16 PCM to float32
        # 2. Resample from 16kHz to 48kHz (scale samples by 3)
        # 3. Denoise using RNNoise (expects exactly 480 float32 samples)
        # 4. Resample back to 16kHz (subsample by 3)
        # 5. Convert float32 back to int16 PCM
        
        try:
            # Convert to float32 (-32768..32767 to -1.0..1.0)
            in_samples_16k = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            
            # Simple linear interpolation up-sampling (16kHz to 48kHz, factor 3)
            # This is extremely fast for 10ms chunks
            in_samples_48k = np.repeat(in_samples_16k, 3)
            
            if len(in_samples_48k) != 480:
                return frame_bytes
                
            # Prepare ctypes arrays
            in_ptr = in_samples_48k.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            out_samples_48k = np.zeros(480, dtype=np.float32)
            out_ptr = out_samples_48k.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            
            # Process frame
            self.lib.rnnoise_process_frame(self.state, out_ptr, in_ptr)
            
            # Down-sample back to 16kHz (take every 3rd sample)
            out_samples_16k = out_samples_48k[::3]
            
            # Convert float32 back to int16
            out_pcm = (out_samples_16k * 32768.0).clip(-32768, 32767).astype(np.int16)
            return out_pcm.tobytes()
        except Exception as e:
            # Return original chunk on processing failure
            print(f"[RNNoise process error] {e}")
            return frame_bytes

    def __del__(self):
        if hasattr(self, '_active') and self._active and hasattr(self, 'state') and self.state:
            try:
                self.lib.rnnoise_destroy(self.state)
            except Exception:
                pass
