"""
FastAPI app for the KPCL GMS assistant.
  GET  /, /dashboard   dashboard UI (the entry page, as in the Cescom POC)
  GET  /api/dashboard  ?fy=2024-25&station=RTPS (both optional): fixed read-only summary queries
  GET  /api/mis-report ?fy=2023-24&station=RTPS&grain=monthly|weekly|daily|all: the dashboard selection as an Excel MIS report
  GET  /api/answer-report/{id}  an AI-mode report answer as Excel (the id comes with the answer; kept for a few hours)
  GET  /insights       key insights as KPI cards (GET /api/insights?fy=2024-25)
  GET  /chat           chat UI (AI mode)
  POST /api/ask        SSE: status (one per real pipeline stage) -> sql (admins only) -> answer_chunk ... -> chart -> table
                       -> done
  POST /api/ask_once   same pipeline, plain JSON (scripts, evaluation)
  GET  /health         liveness, data period, whether the LLM gateway (and the voice services) are configured

VOICE INSTANCE (this copy of the app, webapp/kpcl-voice-app, port 8001). Everything above is unchanged, plus:
  {"question": ..., "mode": "voice"} on /api/ask(_once) adds spoken_answer (app/spoken.py, built from the verified
  answer with no model call) to the response / the SSE done event.
  POST /api/voice/transcribe     raw 16 kHz WAV body -> {"text"}            (qwen3-asr)
  POST /api/voice/speak          {"text", "speed"?} -> audio/wav            (Qwen3-TTS, officer voice)
  POST /api/voice/speak/stream   same body -> audio/pcm s16le mono, rate in X-Audio-Sample-Rate
  GET  /api/voice/filler         {"filler"} -- "Let me check that.", spoken while the answer is worked out
  GET  /api/voice/config         what the page may use (speech in, speech out, streaming, speed)
Every speak failure is a 503 carrying spoken_text, so the page falls back to the browser's own voice.

The read-only database connection and the LLM client are created once in lifespan and reused.
SQL, timings, routes, entities and error details are shown only when KPCL_SHOW_TECHNICAL_DETAILS=true.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import answer_report
from app.config import settings
from app.dashboard_data import DashboardNotFound, build_dashboard, build_insights
from app.db import get_db, init_db, shutdown_db
from app.guardrails import REFUSALS
from app.llm import init_llm_client, shutdown_llm_client
from app.mis_report import ReportUnderReview, build_mis_report, data_version
from app.xlsx_view import workbook_view
from app.models import AskRequest, AskResponse
from app.pipeline import answer_question
from app.protection import Protection
from app.semantic_layer import load_semantic_layer, router_index
from app import spoken, voice, voice_guard

_STATIC = Path(__file__).resolve().parent / "static"
_TECH = settings.kpcl_show_technical_details
_log = logging.getLogger(__name__)
# The speech-detection model (Silero VAD on ONNX Runtime Web) is served from /static/vendor/vad.
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_llm_client()
    voice.init_voice_client()
    load_semantic_layer()
    router_index()
    warm = asyncio.create_task(_warm_tts_cache())
    yield
    warm.cancel()
    shutdown_db()
    await shutdown_llm_client()
    await voice.shutdown_voice_client()


app = FastAPI(title="KPCL GMS Assistant", lifespan=lifespan, docs_url="/docs" if _TECH else None, redoc_url=None,
              openapi_url="/openapi.json" if _TECH else None)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
app.add_middleware(Protection)
# At most this many questions are answered at once (each may hold one call to the shared gateway); the rest wait
# up to KPCL_QUEUE_WAIT_SECONDS, then get a polite "busy" answer instead of piling up.
_SLOTS = asyncio.Semaphore(settings.kpcl_max_concurrent_questions)


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError):
    if _TECH:
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    too_long = any("too_long" in str(e.get("type", "")) for e in exc.errors())
    return JSONResponse(status_code=400, content={"detail": REFUSALS["too_long" if too_long else "empty"]})


UNDER_REVIEW = ("This report is under review: one of its figures did not pass the report's own add-up check, so it is not "
                "being handed out. The check has been logged. The answer on screen is unaffected; please try again later.")


@app.exception_handler(ReportUnderReview)
async def _under_review(request: Request, exc: ReportUnderReview):
    """A report whose own integrity checks failed is never downloaded or previewed."""
    _log.error("report withheld on %s: %s", request.url.path.split("/api/")[-1].split("/")[0], exc)
    return JSONResponse(status_code=409, content={"detail": "under_review", "message": UNDER_REVIEW},
                        headers={"Cache-Control": "no-store"})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    _log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": REFUSALS["error"]})


@app.get("/health")
async def health():
    period = load_semantic_layer()["data_period"]
    return {"status": "ok", "data_from": period["data_from"], "data_to": period["data_to"],
            "llm_configured": settings.llm_configured, "technical": _TECH,
            "voice": {"speech_in": settings.asr_configured, "speech_out": settings.tts_configured}}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_ui():
    return (_STATIC / "dashboard.html").read_text(encoding="utf-8")


@app.get("/insights", response_class=HTMLResponse)
async def insights_ui():
    return (_STATIC / "insights.html").read_text(encoding="utf-8")


@app.get("/chat", response_class=HTMLResponse)
async def chat_ui():
    return (_STATIC / "chat.html").read_text(encoding="utf-8")


@app.get("/api/insights")
async def insights_api(fy: str | None = None):
    """Key insights for one FY as KPI cards (same reviewed queries as the dashboard). Unknown FY is a 404."""
    if fy is not None and len(fy) > 10:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    try:
        return await build_insights(get_db(), fy, settings.kpcl_query_timeout_seconds)
    except DashboardNotFound as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/api/dashboard")
async def dashboard_api(fy: str | None = None, station: str | None = None):
    """Fixed, reviewed read-only queries (app/dashboard_data.py). An unknown FY or station is a 404, never a query."""
    if (fy is not None and len(fy) > 10) or (station is not None and len(station) > 20):
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    try:
        return await build_dashboard(get_db(), fy, station, settings.kpcl_query_timeout_seconds)
    except DashboardNotFound as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})


_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/mis-report")
async def mis_report_api(fy: str | None = None, station: str | None = None, grain: str = "monthly"):
    """The current dashboard selection as an Excel MIS report (app/mis_report.py). Unknown FY, station or grain is a 404."""
    if (fy is not None and len(fy) > 10) or (station is not None and len(station) > 20) or len(grain) > 10:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    try:
        data, name = await build_mis_report(get_db(), fy, station, grain, settings.kpcl_query_timeout_seconds)
    except DashboardNotFound as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    return Response(content=data, media_type=_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/mis-report/view")
async def mis_report_view_api(fy: str | None = None, station: str | None = None, grain: str = "monthly"):
    """The same workbook as /api/mis-report, rendered for the on-screen viewer (app/xlsx_view.py)."""
    if (fy is not None and len(fy) > 10) or (station is not None and len(station) > 20) or len(grain) > 10:
        return JSONResponse(status_code=404, content={"detail": "Not found."})
    try:
        wb, name = await build_mis_report(get_db(), fy, station, grain, settings.kpcl_query_timeout_seconds, as_workbook=True)
    except DashboardNotFound as exc:
        return JSONResponse(status_code=404, content={"detail": str(exc)})
    view = await asyncio.to_thread(workbook_view, wb)
    return JSONResponse({"name": name, **view}, headers={"Cache-Control": "no-store"})


@app.get("/api/answer-report/{report_id}/view")
async def answer_report_view_api(report_id: str):
    """The same workbook as /api/answer-report/{id}, rendered for the on-screen viewer."""
    kept = answer_report.get(report_id, data_version(get_db()))
    if kept is None:
        return JSONResponse(status_code=404, content={"detail": "This report is no longer available. Please ask the question again."})
    wb, name = await answer_report.build_answer_report(get_db(), kept, settings.kpcl_query_timeout_seconds, as_workbook=True)
    view = await asyncio.to_thread(workbook_view, wb)
    return JSONResponse({"name": name, **view}, headers={"Cache-Control": "no-store"})


@app.get("/api/answer-report/{report_id}")
async def answer_report_api(report_id: str):
    """Excel for one answer the server kept (app/answer_report.py). Unknown or expired ids are a 404."""
    kept = answer_report.get(report_id, data_version(get_db()))
    if kept is None:
        return JSONResponse(status_code=404, content={"detail": "This report is no longer available. Please ask the question again."})
    data, name = await answer_report.build_answer_report(get_db(), kept, settings.kpcl_query_timeout_seconds)
    return Response(content=data, media_type=_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def _run(question: str, progress=None, mode: str = "text") -> AskResponse:
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=settings.kpcl_queue_wait_seconds)
    except asyncio.TimeoutError:
        _log.warning("busy: no free slot for %r", question[:200])
        return AskResponse(question=question, answerable=False, answer=REFUSALS["busy"])
    try:
        r = await answer_question(question, get_db(), settings.kpcl_max_rows, settings.kpcl_query_timeout_seconds,
                                  progress=progress)
    except Exception:  # noqa: BLE001
        _log.exception("pipeline failed for %r", question[:200])
        return AskResponse(question=question, answerable=False, answer=REFUSALS["error"])
    finally:
        _SLOTS.release()
    if r.error:
        _log.warning("answer error for %r: %s", question[:200], r.error)
    report_id = ""
    if answer_report.wants_report(question, r.route, r.answerable, r.columns, r.rows):
        report_id = answer_report.keep(question, r.answer, r.route, r.columns, r.rows, r.truncated, r.chart, r.entities,
                                       total=r.total_row, version=data_version(get_db()))
    said = _spoken_for_voice(question, r, report_id) if mode == "voice" else ""
    return AskResponse(spoken_answer=said,
        question=r.question, answerable=r.answerable, answer=r.answer, route=r.route if _TECH else "",        sql=r.sql if _TECH else "", columns=r.columns, rows=[list(x) for x in r.rows], truncated=r.truncated,
        table=r.table, chart=r.chart, notes=r.notes, entities=r.entities if _TECH else {},
        timing_ms=r.timing_ms if _TECH else {}, error=r.error if _TECH else "",
        verification=(r.verification + ("; " + " | ".join(r.review_notes) if r.review_notes else "")) if _TECH else "",
        report_id=report_id)


_DATA_ROUTES = ("metric:", "report:", "verified:", "llm")


def _spoken_for_voice(question: str, r, report_id: str) -> str:
    """What the voice says, under the voice guardrails (app/voice_guard.py), in this order:
      1. a refusal that may come from mishearing -> "I heard: ... Could you say it again?";
      2. a model-written answer the second check did not pass -> only "the answer is on your screen";
      3. any answer from the data -> "For <place>, <period>: " first, then the spoken form, whose every figure must be a
         screen figure with its unit -- otherwise "the figures are on your screen" (never an unchecked number)."""
    try:
        again = voice_guard.ask_again(question, r.route)
        if again:
            return again
        if r.route == "llm" and r.verification not in ("ok", "fixed"):
            return voice_guard.MODEL_UNCHECKED
        base = spoken.spoken_answer(question, r.answer, r.route, rows=len(r.rows), has_chart=bool(r.chart),
                                    has_report=bool(report_id))
        if not r.route.startswith(_DATA_ROUTES) or not r.answerable:
            return base
        base = voice_guard.add_units(base)
        if not (voice_guard.figures_match_screen(base, r.answer) and voice_guard.figures_have_units(base)):
            _log.warning("spoken figures withheld for %r: %r", question[:200], base[:200])
            base = voice_guard.FIGURES_ON_SCREEN + (" " + spoken.REPORT_NOTE if report_id else "")
        return voice_guard.readback(question, r.answer) + base
    except Exception:  # noqa: BLE001 -- the spoken form is best-effort; the screen answer stands
        _log.exception("spoken answer failed for %r", question[:200])
        return voice_guard.FIGURES_ON_SCREEN if r.answerable else "Sorry, I couldn't answer that. Please ask again."


@app.post("/api/ask_once", response_model=AskResponse)
async def ask_once(req: AskRequest):
    return await _run(req.question, mode=req.mode)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _chunks(text: str) -> list[str]:
    """Split the answer into word-sized pieces, keeping line breaks, so the page can type it out."""
    return re.findall(r"\n|[^ \n]+ ?| ", text)


@app.post("/api/ask")
async def ask_stream(req: AskRequest):
    async def events():
        stages: asyncio.Queue[str] = asyncio.Queue()
        task = asyncio.create_task(_run(req.question, progress=stages.put_nowait, mode=req.mode))
        sent = set()
        while True:
            getter = asyncio.create_task(stages.get())
            finished, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter in finished:
                stage = getter.result()
                if stage not in sent:
                    sent.add(stage)
                    yield _sse("status", {"stage": stage})
                continue
            getter.cancel()
            break
        while not stages.empty():
            stage = stages.get_nowait()
            if stage not in sent:
                sent.add(stage)
                yield _sse("status", {"stage": stage})
        res = task.result()
        if _TECH:
            yield _sse("sql", {"sql": res.sql, "route": res.route})
        if res.spoken_answer:
            # Voice mode: what to say goes first, so the voice can start while the text is still typing out.
            yield _sse("spoken", {"spoken_answer": res.spoken_answer, "answerable": res.answerable})
        yield _sse("status", {"stage": "answering"})
        pieces = _chunks(res.answer)
        # Paced so a short answer reads like typing and a long one still finishes in about two seconds. Words are
        # grouped so no pause is shorter than 25 ms: Windows timers tick every ~15 ms, and shorter sleeps return at once.
        group = max(1, -(-len(pieces) // 80))
        for i in range(0, len(pieces), group):
            yield _sse("answer_chunk", {"text": "".join(pieces[i:i + group])})
            await asyncio.sleep(0.03 if group == 1 else 0.025)
        yield _sse("chart", {"chart": res.chart})
        yield _sse("table", {"table": res.table if res.answerable else None})
        yield _sse("done", {"answerable": res.answerable, "timing_ms": res.timing_ms, "truncated": res.truncated,
                            "technical": _TECH, "route": res.route,
                            "verification": res.verification if _TECH else "", "report_id": res.report_id,
                            "spoken_answer": res.spoken_answer})
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ================================================================================================ voice
# Speech in and out around the unchanged pipeline (the Meghalaya voice agent's contract, KPCL data and persona).

# The speech-to-text prompt. Measured on the voice stress test (Indian-English voices): a comma list of names made the
# model echo the list on silence and mishear years ("twenty twenty-one twenty-two" -> 2020-21); one plain paragraph
# of the stations, reservoirs, suppliers and terms (and nothing about how years are spoken) hears names best.
ASR_CONTEXT = (
    "Spoken questions to the KPCL (Karnataka Power Corporation Limited) generation data assistant. Power stations: "
    "Raichur Thermal Power Station (RTPS), Bellary thermal (BTPS), Yeramarus thermal (YTPS), Yelahanka combined cycle "
    "(YCCPP), Bidadi waste to energy, Sharavathi (SGS), Nagjhari power house (NPH), Varahi underground power house "
    "(VUGPH), Mahatma Gandhi hydro electric station (MGHE), Linganamakki power house, Gerusoppa power house, Supa dam "
    "power house, Kodasalli, Kadra dam, Mani dam power house, Alamatti dam power house, Ghataprabha, Bhadra, Munirabad, "
    "Shiva hydro electric station, Shimsha, Mallapur mini hydel, Sirwar mini hydel, Kalmala mini hydel, Ganekal mini "
    "hydel, Malaprabha mini hydel, Kappatagudda wind farm, Yalesandra solar, Itnal solar, Yapaldinni solar, "
    "Shivasamudra 5 MW and 10 MW solar, Chandapur solar. Reservoirs: Linganamakki, Talakalale, Supa, Mani, Gerusoppa, "
    "Alamatti. Coal: Singareni, Mahanadi, Western Coalfields. Terms: PLF, PAF, CEA target, running hours, forced outage "
    "hours, back-down, shear pin failure, Tungabhadra, MU.")
_PROMPT_WORDS = set(re.findall(r"[a-z]+", ASR_CONTEXT.lower()))
_STATION_WORDS = {"raichur", "bellary", "yeramarus", "yelahanka", "bidadi", "sharavathi", "nagjhari", "varahi", "linganamakki",
                  "gerusoppa", "supa", "kodasalli", "kadra", "mani", "alamatti", "ghataprabha", "bhadra", "munirabad",
                  "shiva", "shimsha", "mallapur", "sirwar", "kalmala", "ganekal", "malaprabha", "kappatagudda",
                  "yalesandra", "itnal", "yapaldinni", "shivasamudra", "chandapur", "talakalale", "singareni", "mahanadi"}


def _is_prompt_echo(text: str) -> bool:
    """On silence or noise the model can return its prompt. A transcript made of the prompt's own words that names
    four or more stations is that echo, not a question ("compare RTPS, BTPS and YTPS" names three)."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    if len(words) < 8:
        return False
    in_prompt = sum(w in _PROMPT_WORDS for w in words) / len(words)
    return in_prompt >= 0.85 and len(_STATION_WORDS & set(words)) >= 4


@app.get("/api/voice/config")
async def voice_config():
    return {"speech_in": settings.asr_configured, "speech_out": settings.tts_configured,
            "stream": settings.tts_configured and settings.tts_stream_enabled,
            "speed": settings.tts_configured and settings.tts_speed_enabled,
            "sample_rate": settings.tts_stream_sample_rate}


def _rejected(kind: str, message: str, heard: str = "") -> dict:
    return {"text": "", "heard": heard, "confirm": None, "rejected": kind, "message": message}


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request, language: str = "en", session_id: str = "default", expect: str = ""):
    """One spoken question as a WAV body -> {"text", "heard", "confirm", "rejected", "message"}.
      text      the cleaned question to ask ("" = nothing to ask)
      heard     what the speech model wrote, before clean-up
      confirm   {"prompt", "options": [{"label", "question"}]} when the clean-up was unsure: ask before answering
      rejected  "too_long" | "language" | "duplicate" | "echo" | "no_speech" | "too_short" | null, with "message" to
                say (none for duplicate / echo / no_speech / too_short: those are noise, not a question)
    expect=confirm: the panel is waiting for an answer to "Did you mean ...?", so one word ("Sirwar", "yes") is fine;
    expect=bargein: words said over a reply ("stop"); the panel itself ignores fragments and the reply's own echo.
    413 over the byte cap, 503 when speech-to-text is down (the page then offers typing instead)."""
    if not re.fullmatch(r"[a-z]{2,3}", language or ""):
        language = "en"
    session = re.sub(r"[^A-Za-z0-9._:-]", "", session_id)[:64] or "default"
    audio = await request.body()
    if not audio:
        return JSONResponse(status_code=400, content={"detail": "No audio was received."})
    if len(audio) > settings.voice_max_audio_bytes:
        return JSONResponse(status_code=413, content={"detail": "That recording is too long. Please ask one question at a time."})
    secs = voice_guard.wav_seconds(audio)
    if secs is not None and secs > settings.voice_max_seconds:
        return _rejected("too_long", f"That was longer than {int(settings.voice_max_seconds)} seconds. Please ask one short question at a time.")
    voiced = voice_guard.voiced_seconds(audio)
    if voiced is not None and voiced < voice_guard.MIN_VOICED_SECONDS:
        _log.info("transcribe: %.2f s of speech in the recording; not sent to the speech model", voiced)
        return _rejected("no_speech", "")
    hint = ASR_CONTEXT if settings.asr_context_hints_enabled else None
    try:
        raw = await voice.transcribe(audio, "audio.wav", language=language, prompt=hint)
    except voice.VoiceBusy:
        return JSONResponse(status_code=503, headers={"Retry-After": "5"},
                            content={"detail": "The speech service is busy. Please try again in a moment."})
    except voice.VoiceUnavailable as exc:
        _log.warning("transcription failed: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Speech recognition is unavailable right now."})
    raw = (raw or "").strip()
    if hint and _is_prompt_echo(raw):
        _log.info("transcribe: dropped an echo of the ASR prompt (no speech)")
        return _rejected("echo", "")
    if not raw:
        return {"text": "", "heard": "", "confirm": None, "rejected": None, "message": ""}
    if not voice_guard.language_ok(raw):
        return _rejected("language", "I can only take questions in English. Please ask again in English.", raw)
    if len(raw) > settings.voice_max_transcript_chars:
        return _rejected("too_long", "That was a long question. Please ask one thing at a time.", raw)
    cleaned, notes = spoken.clean_transcript_ex(raw)
    if expect not in ("confirm", "bargein") and voice_guard.too_short(cleaned, voiced, _PROMPT_WORDS | {"fy", "the", "of", "in", "at"}):
        _log.info("transcribe: dropped a fragment that is not a question: %r", raw[:80])
        return _rejected("too_short", "", raw)
    if voice_guard.repeats.seen(session, cleaned):
        return _rejected("duplicate", "", raw)
    confirm = voice_guard.confirmation(cleaned, notes)
    if notes or cleaned.lower() != raw.lower():
        from app.semantic_layer import unknown_names
        voice_guard.record_mishearing({"heard": raw, "cleaned": cleaned, "notes": notes,
                                       "unknown_names": unknown_names(cleaned)})
    return {"text": cleaned, "heard": raw, "confirm": confirm, "rejected": None, "message": ""}


@app.get("/api/voice/health")
async def voice_health(refresh: bool = False):
    """Speech in / speech out reachability (checked at most every two minutes unless refresh=true)."""
    return await voice_guard.speech_health(force=refresh)


_TTS_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_TTS_CACHE_MAX_ITEMS, _TTS_CACHE_MAX_CHARS = 64, 160
_PCM_HEADERS = {"X-Audio-Sample-Rate": str(settings.tts_stream_sample_rate), "X-Audio-Format": "s16le; channels=1",
                "Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def _cache_key(text: str, speed: float | None, streamed: bool):
    if len(text) > _TTS_CACHE_MAX_CHARS:
        return None                                # only short fixed phrases (fillers, apologies) are cached
    return (streamed, text, speed, settings.tts_model, settings.tts_task_type, settings.tts_ref_audio_path,
            settings.kpcl_voice_instructions, settings.tts_language, settings.tts_stream_sample_rate)


def _cache_put(key, audio: bytes) -> None:
    if key is None or not audio:
        return
    _TTS_CACHE[key] = audio
    _TTS_CACHE.move_to_end(key)
    while len(_TTS_CACHE) > _TTS_CACHE_MAX_ITEMS:
        _TTS_CACHE.popitem(last=False)


async def _warm_tts_cache() -> None:
    """Synthesise the filler phrases once at startup (streamed form), so the first one plays at once."""
    if not (settings.tts_configured and settings.tts_stream_enabled):
        return
    for phrase in spoken.FILLERS:
        said = spoken.pronounce(phrase)
        try:
            _cache_put(_cache_key(said, None, True), b"".join([c async for c in voice.stream_speech(said)]))
        except Exception as exc:  # noqa: BLE001 -- best effort; the first request fills it instead
            _log.info("TTS warm-up skipped: %s", exc)
            return


def _speak_input(body: dict) -> tuple[str, float | None] | JSONResponse:
    text = str(body.get("text") or "").strip()
    if not text or len(text) > 2000:
        return JSONResponse(status_code=400, content={"detail": "Nothing to say."})
    speed = body.get("speed")
    try:
        speed = None if speed in (None, "") else min(2.0, max(0.5, float(speed)))
    except (TypeError, ValueError):
        speed = None
    return text, speed


def _unavailable(said: str, detail: str, retry: bool = False) -> JSONResponse:
    return JSONResponse(status_code=503, headers={"Retry-After": "5"} if retry else None,
                        content={"detail": detail, "spoken_text": said})


@app.post("/api/voice/speak")
async def voice_speak(request: Request):
    """Text -> a whole WAV clip in the officer voice. 503 + spoken_text when it can't (the page then uses the
    browser voice with exactly that text)."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Nothing to say."})
    parsed = _speak_input(body if isinstance(body, dict) else {})
    if isinstance(parsed, JSONResponse):
        return parsed
    text, speed = parsed
    said = spoken.pronounce(text)
    if not settings.tts_configured:
        return _unavailable(said, "Voice output is not enabled.")
    key = _cache_key(said, speed, False)
    audio = _TTS_CACHE.get(key) if key else None
    if audio is None:
        try:
            audio = await voice.speak(said, speed)
        except voice.VoiceBusy:
            return _unavailable(said, "The speech service is busy.", retry=True)
        except voice.VoiceUnavailable as exc:
            _log.warning("TTS failed: %s", exc)
            return _unavailable(said, "Voice output is unavailable right now.")
        _cache_put(key, audio)
    media = "audio/wav" if audio[:4] == b"RIFF" else "audio/mpeg" if audio[:3] == b"ID3" else "audio/wav"
    return Response(content=audio, media_type=media, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/speak/stream")
async def voice_speak_stream(request: Request):
    """Text -> raw PCM as the model decodes it (first audio ~0.8 s). The first chunk is awaited before the 200 is
    sent, so any failure the page can still act on is a clean 503 with spoken_text. 404 when streaming is off."""
    if not settings.tts_stream_enabled:
        return JSONResponse(status_code=404, content={"detail": "Streamed voice output is off; use /api/voice/speak."})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Nothing to say."})
    parsed = _speak_input(body if isinstance(body, dict) else {})
    if isinstance(parsed, JSONResponse):
        return parsed
    text, speed = parsed
    said = spoken.pronounce(text)
    if not settings.tts_configured:
        return _unavailable(said, "Voice output is not enabled.")
    key = _cache_key(said, speed, True)
    cached = _TTS_CACHE.get(key) if key else None
    if cached is not None:
        async def cached_body():
            for i in range(0, len(cached), 16384):
                yield cached[i:i + 16384]
        return StreamingResponse(cached_body(), media_type="audio/pcm", headers=_PCM_HEADERS)
    stream = voice.stream_speech(said, speed)
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        return _unavailable(said, "Voice output is unavailable right now.")
    except voice.VoiceBusy:
        return _unavailable(said, "The speech service is busy.", retry=True)
    except voice.VoiceUnavailable as exc:
        _log.warning("TTS stream failed before audio: %s", exc)
        return _unavailable(said, "Voice output is unavailable right now.")

    async def body_iter():
        kept = [first] if key is not None else None
        try:
            yield first
            async for chunk in stream:
                if kept is not None:
                    kept.append(chunk)
                yield chunk
            if kept is not None:
                _cache_put(key, b"".join(kept))          # only a complete clip is cached
        except voice.VoiceUnavailable as exc:
            _log.warning("TTS stream cut short: %s", exc)
        finally:
            try:
                await stream.aclose()                    # frees the gateway slot when the page stops listening
            except RuntimeError:
                pass
    return StreamingResponse(body_iter(), media_type="audio/pcm", headers=_PCM_HEADERS)


_LAST_FILLER: "OrderedDict[str, str]" = OrderedDict()


@app.get("/api/voice/filler")
async def voice_filler(session_id: str = "default"):
    """A short phrase spoken the moment a question is captured, never the same twice in a row per session."""
    sid = re.sub(r"[^A-Za-z0-9._:-]", "", session_id)[:64] or "default"
    last = _LAST_FILLER.get(sid)
    choice = next(f for f in spoken.FILLERS if f != last)
    _LAST_FILLER[sid] = choice
    _LAST_FILLER.move_to_end(sid)
    while len(_LAST_FILLER) > 5000:
        _LAST_FILLER.popitem(last=False)
    return {"filler": choice}
