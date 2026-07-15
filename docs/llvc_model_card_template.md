# LLVC Model Card Template

This document provides a template for versioning and documenting Low Latency Voice Conversion (LLVC) models developed for Keira.

## Model Details

- **Model Version**: `llvc-pilot-vX.Y.Z`
- **Trained By**: Keira AI Team
- **Base Architecture**: LLVC (Low Latency Voice Conversion) Causal CNN / Transformer
- **Target Speaker Profile**: [e.g. Brand Voice - Female, Warm]
- **Release Date**: YYYY-MM-DD

## Intended Use

- **Primary Application**: Real-time voice conversion for call center agents.
- **Out of Scope**: Non-realtime generation, offline deepfakes, or unauthorized speaker replication.

## Training Dataset

### Source Corpus
- **Dataset Name**: [e.g., LibriTTS, Keira Representative Recordings]
- **Total Duration**: [e.g., 24.5 hours]
- **Sample Rate / Format**: 16 kHz Mono 16-bit PCM
- **Speaker Splits**: Disjoint splits to prevent validation contamination.

### Teacher (RVC) Target Corpus
- **RVC Teacher Version**: [e.g., Keira-RVC-v2.1]
- **Conversion Settings**: `pitch_shift=[X]`, `index_rate=[Y]`

## Evaluation & Metrics

### Latency Performance
- **Median Inference Latency**: [X] ms
- **95th Percentile Latency**: [Y] ms
- **Duration Drift**: [Z] ms / minute

### Quality Metrics
- **Speaker Similarity (Cosine Similarity / d-vector)**: [e.g., 0.85]
- **Word Error Rate (WER) degradation**: [e.g., +1.2%]
- **Naturalness MOS (Mean Opinion Score)**: [e.g., 3.8 / 5.0]

## Safety and Limitations

- **Watchdog Failsafe**: Triggered automatically on stream silent interval >= 2.0 seconds.
- **Fail-Closed Policy**: If the model server drops, the call is hung up immediately to protect the agent's raw voice privacy.
