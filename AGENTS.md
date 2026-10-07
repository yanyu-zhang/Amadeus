# Repository instructions

- Use uv to manage Python, environments, dependencies, and run commands.
- Do not write or add unit tests. The user explicitly requested removal of all unit tests and no future unit tests.
- Verify changes with static checks and targeted manual runtime checks.
- Meeting audio processing and summaries use local models only. Optimize transcription and summaries for Chinese.
- Target the user's 24GB Apple Silicon Mac mini; prefer small local language models.
- Never print secrets or commit `.env`, meeting recordings, or transcripts.
- Meeting summaries list each speaker and synthesize their main topics, views, questions, and responses in concise prose. Do not mechanically repeat each utterance or infer decisions or assignments from casual conversation. Validate source references internally; keep detailed timestamps and quotes in the transcript.
