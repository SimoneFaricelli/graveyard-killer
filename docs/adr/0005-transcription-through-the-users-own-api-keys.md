# Transcription through the user's own API keys

This fork replaces the local speech-to-text engines (Parakeet-TDT v3 and Whisper turbo) with the user's own Deepgram or OpenAI account. It supersedes [ADR 0001](0001-no-server-no-api-keys.md) for transcription only: downloads still happen on the user's computer, and the user's agent still does all the analysis and the Notion writes.

## Why

The user of this fork already pays for Deepgram and OpenAI and can manage API keys. For them the local engines cost more than they give: a 1 GB+ model download, a heavy install that differs by platform (MLX on Apple Silicon, CTranslate2 elsewhere) and minutes of CPU per long video, for a transcript that the providers return in seconds.

## Decision

- The post script sends the audio, as a 16 kHz mono mp3, to the provider named in the Library's Settings (Transcription → Provider), and to the other one when the first fails or has no key.
  - Deepgram: pre-recorded API, model `nova-3`, `smart_format`, the language hint or else language detection.
  - OpenAI: `gpt-4o-transcribe`, in pieces of at most 3 minutes. Each request's output is capped: on a 27-minute Italian clip (live test, 2026-11-12), 10-minute pieces silently lost their last 2 minutes each, while 5-minute pieces kept every sentence.
- The keys live in a file on the computer that runs the Worker, `~/.config/graveyard-killer/keys.env`, or in the environment. Not in the Library, which other agents and the phone read, and not in the chat: Setup creates the file and the user fills it in a text editor. A file, because a scheduled run does not inherit the shell's environment.
- Error messages name the HTTP status only: some provider errors echo part of the key.
- When every provider fails, the Entry is not finished (`transcription_failed`), so it never ends Processed without a transcript. A missing or refused key, no credit or a rate limit (HTTP 401, 402, 403, 429) blocks the rest of the run's downloads, as an Instagram challenge does for Instagram, so the download budget is not spent on posts that cannot be finished.

## Consequences

- Each transcribed minute costs the user money on their provider account, on top of their Claude plan. Creator captions are still used first and cost nothing.
- The audio of every saved video leaves the computer for the provider.
- No speech model download, and no MLX or CTranslate2 install.
- A run with no valid key reads no videos and says so in the Ping.
