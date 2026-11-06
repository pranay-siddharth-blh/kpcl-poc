# KPCL GMS Assistant — Voice instance

A separate copy of the KPCL app (`../kpcl-ai-app`, port 8000, untouched) with a hands-free **voice agent** added,
modelled on the Meghalaya voice agent (`D:\Meghalaya_voice\...`, read-only reference). It serves the same dashboard,
insights and chat pages on **port 8001**. In the chat, the round voice button beside **Ask** starts a spoken
conversation, and every spoken answer also appears in the chat with its chart, table and View / Download report.

## Run

```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Set-Location "D:\KPCL POC\KPCL POC\webapp\kpcl-voice-app"; python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001/chat and press the round voice button. The browser asks to **Allow** the microphone the
first time. The microphone only works on `127.0.0.1` / `localhost`, or over HTTPS. A plain `http://<LAN-IP>` address
can't use it. Speech in and out run on the internal model gateway, so the VPN must be connected; without it the page
falls back to the browser's own voice, and typed questions still work.

Tests (offline, gateway mocked for voice): `python -m pytest tests -q -p no:cacheprovider` (the 548 inherited tests
plus 52 in `tests/test_voice.py`). The live stress runner defaults to port 8001 and writes
`STRESS_TEST_QUESTIONS_VOICE.md` here, never the original's file.

Voice stress test (spoken questions, VPN needed): `python tests/run_voice_stress.py`.
- **What it does:** every stress-bank question plus a generated bank, spoken by Indian-English Windows voices (Ravi and
  Heera), through the real speech-to-text and this pipeline.
- **Result:** 597/602 pass with the guardrails on (594 before them, 496 before the transcript fixes); details in HANDOFF.md C.7 and C.8.
- **Output:** `VOICE_STRESS_TEST_QUESTIONS.md`.

## How a spoken turn works

```
browser                                                server (this app)                         gateway (VPN)
mic → Silero VAD (in the page; loudness fallback)
    → 16 kHz WAV ──────────────► POST /api/voice/transcribe ─────────────────────────────► qwen3-asr
                                   (station names as a prompt; an echo of the prompt is dropped;
                                    "R.T.P.S." → RTPS, "twenty twenty-three twenty-four" → 2023-24)
    ◄── question text
    → GET /api/voice/filler ("Let me check that.") → spoken at once (pre-synthesised, cached)
    → POST /api/ask {question, mode:"voice"}  ─────► the unchanged pipeline (same answers as port 8000)
         SSE: status … spoken{spoken_answer} → answer_chunk … → chart → table → done
    → per sentence: POST /api/voice/speak/stream ──────────────────────────────────────► Qwen3-TTS (Base, voice
         ◄── raw PCM, played as it arrives (next sentence fetched while this one plays)    cloned from the officer clip)
    → listening again; speaking over the reply interrupts it and becomes the next question
```

### What the voice says (`app/spoken.py`)
The spoken answer is built from the **verified answer text, with no model call**:
- the bold headline first;
- at most one supporting sentence that adds a new figure (or "followed by" the next two items of a list);
- figures rounded the way an officer says them (8,310.351 MU → "8,310 million units"; 16,561,495 tonnes → "about 1.66 crore
  tonnes");
- screen-only notes are not read;
- "The full details, and the report, are on your screen" when there is a table or chart.

A test checks that every spoken figure is a rounding of a figure on screen. This keeps KPCL's rule that figures come
only from the data. It also saves the 1–3 s a model rewrite would add: the Meghalaya agent needed a number guard
after its model once said "3.06 lakh" for 30.6 lakh.

`pronounce()` then makes the text speakable for the voice model:
- codes are spelled out (RTPS → "R T P S", RTPS-U8 → "R T P S unit 8", SUPA → "Supa");
- "FY 2024-25" → "financial year twenty twenty-four, twenty-five";
- units become words ("%" → "percent", "W50" → "week 50").

### Voice
Meghalaya's officer reference clip (`voices/officer_serena.wav`, a synthetic Qwen3-TTS preset voice) is cloned
(`TTS_TASK_TYPE=Base`) and styled by the **KPCL persona** (`kpcl_voice_instructions` in `app/config.py`): a composed
senior KPCL officer, medium-low pitch, clear neutral Indian English, slower for figures. The Meghalaya persona line
(`TTS_INSTRUCTIONS`) is merged into `.env` but deliberately **not used**. The gateway currently serves only the Base
(cloning) model; preset voices return 400. Measured: first audio about 1.1–1.7 s after a sentence is sent; speech to
text 0.1–1.2 s.

## Voice guardrails (2026-10-02)

| Guardrail | Where | What it does |
|---|---|---|
| Show what was heard | `voice-agent.js` `heardWindow`, panel `.vm-heard` | "I heard: …" for 2 s with a countdown, **That's not what I said** (cancels; the voice says sorry and listens) and **Ask now** |
| Read back station and period | `voice_guard.readback`, `main._spoken_for_voice` | every data answer starts "For Raichur Thermal Power Station, March 2025: …" (unit, reservoir, category, "all of KPCL", "As on 30 April 2025", default FY) |
| Confirm when unsure | `spoken.clean_transcript_ex` notes → `voice_guard.confirmation` → transcribe `confirm` | "Did you mean A, or B?" for a loose name match, a repaired year, or a known confusable pair (Shiva / Sirwar). Answer by voice ("Sirwar", "the second one", "no") or tap a button |
| Ambiguous years | same | "FY 2024", "financial year 24", "FY24" → "Did you mean FY 2023-24, or FY 2024-25?" |
| Ask again on failure | `voice_guard.ask_again` | unknown station, couldn't confirm, off-topic → "I heard: <question>. … Could you say it again?" |
| Same figure on screen and in speech | `voice_guard.figures_match_screen` | every spoken figure must be a screen figure (rounded as said); otherwise the voice says "The figures are on your screen" and no number |
| Stricter for model answers | `main._spoken_for_voice` | a model-written answer is read out only if the second check passed (`ok` / `fixed`); otherwise "The answer is on your screen …" |
| Units required | `voice_guard.add_units`, `figures_have_units` | each spoken figure needs its unit after it, its unit in the label ("lakh tons: 65.33"), or, for a whole number, what it counts named earlier in the sentence ("unit trips … was 33", "Rakes: 270"). Heat rate and frequency, which the data always keeps in kcal/kWh and Hz, get the unit said. A bare decimal → "The figures are on your screen" |
| Injection on transcripts | the pipeline's own `guardrails.screen_question` | spoken "ignore previous instructions", "delete …", "sabotage …", "API key" are refused like typed ones (tests) |
| Language check | `voice_guard.language_ok` | a transcript mostly not in Latin or Kannada script is dropped: "I can only take questions in English…" |
| Length limits | transcribe + `voice-agent.js` | at most 30 s of audio (recorder stops at 30 s; server checks the WAV header) and 300 characters of transcript |
| Repeat detection | `voice_guard.RepeatGuard` | the same question from the same session within 4 s is answered once |
| Microphone indicator and auto-off | panel `.vm-mic`, `voice-agent.js` | "Microphone on/off" in the panel and dock; off after 3 minutes without a question, and as soon as the tab is hidden, with a notice |
| Visible fallback | `voice-agent.js` `sttDown`, panel `.vm-typebox` | speech-to-text down (503, or the health check) → microphone released, "type your question instead" box, answers still spoken, **Try voice again** |
| Timeouts | `voice-agent.js` | speech-to-text 20 s, "Still working on it." once after 8 s, stop waiting after 60 s (the answer still lands on screen), TTS first audio 20 s |
| Health check | `GET /api/voice/health`, panel dots | speech in (model list) and voice (one short word, first audio only), cached 2 minutes, polled every 3 minutes while voice is open; voice down → the browser's own voice |
| Misheard-names list | `app/voice_lexicon.json`, `voice_guard.lexicon`, `tests/review_mishearings.py` | reviewed entries only; at load, an entry that is (or sounds exactly like) a real name, an ordinary word, or points to an unknown name is ignored. Collection from real officers is off by default (`VOICE_COLLECT_MISHEARINGS=true`, text only, consent required) |

| No speech, no question | `voice_guard.voiced_seconds`, `too_short`; transcribe | a recording with under 0.35 s clearly above its own background (fan, typing, a cough, a chime) never reaches the speech model (`rejected: no_speech`); a one-word transcript ("PLF", what the model writes for noise), or 2–3 hint words from under a second of sound, is dropped silently (`too_short`, the panel shows "I only heard …"). `expect=confirm` / `expect=bargein` allow one word ("Sirwar", "stop") |
| Talking over a reply | `voice-agent.js` `onVadSpeechStart` → `checkBargeIn` | the reply is turned down (not stopped) while someone talks; when they stop, their words are transcribed: the reply's own echo, a fragment or a cough → the reply carries on at full volume; a real question or "stop" → the reply ends and that is handled. Active from the moment the answer is being looked up to the end of the reply; without the speech detector, interrupt by tapping |
| No self-echo, quiet start | `voice-agent.js` `start`, `listen`, `isEchoOf` (`voice-text.js`) | listening starts only when the speech detector is ready (the start chime is never a question); 0.3 s of quiet after each reply; a transcript that repeats four or more words of the last reply in order, begun while it played, is ignored; speech detector thresholds 0.6 / 600 ms; the chat is given one question at a time |

Debugging on a real machine: in the browser console run `window.__voiceDebug = true`, then use voice; the speech-detector events are logged as `[voice] <ms> speech start | speech end | misfire | vad start/pause`.

Tests: `tests/test_voice_guards.py`. Browser checks (headless Edge, fake microphone): normal turn, Shiva/Sirwar confirmation (tap Sirwar), "That's not what I said", speech-to-text forced down → type instead, hidden tab → microphone off; no console errors or CSP violations.

## Files added or changed in this copy (everything else is identical to `../kpcl-ai-app`)

| File | What |
|---|---|
| `app/voice.py` | gateway client: `transcribe`, `speak` (WAV), `stream_speech` (PCM); own concurrency cap; `VoiceUnavailable` / `VoiceBusy` |
| `app/spoken.py` | spoken answer, `pronounce`, `clean_transcript`, fillers |
| `app/main.py` | `mode:"voice"` on `/api/ask(_once)` → `spoken_answer` (and an early `spoken` SSE event); `/api/voice/{config,transcribe,speak,speak/stream,filler}`; short-phrase TTS cache warmed at startup; `.wasm`/`.mjs` types |
| `app/models.py` | `AskRequest.mode`, `AskResponse.spoken_answer` |
| `app/config.py` | `ASR_*`, `TTS_*`, `KPCL_VOICE_INSTRUCTIONS`, `VOICE_*` settings (read from the shared `webapp/.env`) |
| `app/protection.py` | `microphone=(self)`; CSP `script-src` adds `'wasm-unsafe-eval'` (WebAssembly for the speech detector, not JavaScript eval); 10 MB body cap on `/api/voice/transcribe` only; separate per-IP budget for `/api/voice/*` (240/min) |
| `app/static/chat.html`, `chat.js` | voice button, voice stylesheet and module; `ask(text, {mode:'voice'})` resolves as soon as the spoken form arrives; spoken questions carry a mic mark; badge "AI VOICE MODE" |
| `app/static/voice/` | `voice-agent.js` (engine, ported from Meghalaya), `pcm-stream.js`, `voice-text.js`, `voice-ui.js` (panel), `voice.css` |
| `app/static/vendor/vad/` | Silero VAD 0.0.31 + ONNX Runtime Web 1.30.0 (copied from Meghalaya, SHA-256 checked against `VERSIONS.txt`, about 16 MB) |
| `voices/officer_serena.wav` | the officer reference clip (copied from Meghalaya) |
| `app/voice_guard.py`, `app/voice_lexicon.json` | the voice guardrails and the reviewed misheard-names list |
| `tests/test_voice.py`, `tests/test_voice_guards.py`, `tests/run_voice_stress.py`, `tests/review_mishearings.py`, `tests/make_wavs.ps1` | voice tests, the spoken stress test, the misheard-names review tool; `tests/run_stress.py` targets 8001 and writes its own report |

## Settings (`webapp/.env`, shared with the original app, which ignores every voice key)
The Meghalaya `.env` was merged into `webapp/.env` on 2026-10-02 under a marked block. Four keys that the original
app also reads were **not** copied, so it is unaffected: `SQL_VERIFY_MODEL`, `SQL_VERIFY_BASE_URL`,
`SQL_VERIFY_API_KEY` and `AI_MODEL_CA_BUNDLE_PATH`. Its CA path is relative to Meghalaya's folder. Voice uses:
- `ASR_MODEL`, `ASR_BASE_URL`, `ASR_API_KEY`, `ASR_CONTEXT_HINTS_ENABLED`;
- `TTS_ENABLED`, `TTS_MODEL`, `TTS_BASE_URL`, `TTS_API_KEY`, `TTS_TIMEOUT_SECONDS`, `TTS_TASK_TYPE`;
- `TTS_REF_AUDIO_PATH`, which resolves against this folder;
- `TTS_X_VECTOR_ONLY_MODE`, `TTS_STREAM_ENABLED`.

Optional: `KPCL_VOICE_INSTRUCTIONS`, `VOICE_RATE_LIMIT_PER_MINUTE`, `VOICE_MAX_CONCURRENT_CALLS`, `VOICE_MAX_AUDIO_BYTES`.

## Panel controls
- **Full panel:** an orb that reacts to your voice and to the reply, captions, mute, back to chat (chevron down) and end.
- **Docked bar:** replaces the composer, with mute, expand and End.
- **Settings:** officer voice or device voice, speed 0.85× / 1× / 1.15×, hands-free or hold-to-talk, captions, sounds.
- **Keyboard:** Esc ends, M mutes, Space holds to talk (hold-to-talk) or interrupts a reply.
- **Preferences** are remembered in the browser (`kpcl-voice-prefs`).

## Known limits
- English only. Kannada speech in and out are not supported by these models here; the page's Kannada switch
  translates the panel's labels only.
- The microphone needs `localhost` or HTTPS (see Run).
- With the browser's own voice (fallback), barge-in is by tapping the orb: the operating system's speech bypasses echo
  cancellation.
- Kept reports, the TTS cache and rate limits are per process (one uvicorn worker).
- A real-room microphone test by a person is still needed. The automated browser test used Edge with a fake microphone
  (a recorded question): it heard, answered, spoke and returned to listening, with no console errors or
  security-policy violations.
