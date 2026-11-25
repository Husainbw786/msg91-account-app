"""AI backend used by the extraction step (recon/extract.py). Reconciliation never calls it.

viaSocket AI is used when VIASOCKET_ORG_ID / VIASOCKET_PROJECT_ID / VIASOCKET_EMBED_SECRET are set (no model key
needed). Otherwise an Anthropic key (ANTHROPIC_API_KEY) works directly. RECON_AI=off disables AI.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol


log = logging.getLogger("recon.ai")
DEFAULT_MODEL = "claude-opus-5-5"
TOL = 100


class AIBackend(Protocol):
    def json(self, task: str, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> Optional[dict]:
        ...


@dataclass
class AIStats:
    calls: int = 0
    failures: int = 0
    accepted: int = 0
    rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    log: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.log.append(msg)


class ClaudeBackend:
    """Anthropic SDK backend: structured JSON output, refusal-aware, server-side fallback enabled."""

    def __init__(self, model: Optional[str] = None, stats: Optional[AIStats] = None):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3, timeout=300.0)
        self.model = model or os.environ.get("RECON_AI_MODEL", DEFAULT_MODEL)
        self.stats = stats or AIStats()

    def json(self, task, system, user, schema, effort="medium", max_tokens=16000):
        a = self._anthropic
        self.stats.calls += 1
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except a.RateLimitError as e:
            self.stats.failures += 1
            self.stats.note(f"{task}: rate limited ({e.status_code}); skipped")
            return None
        except a.APIStatusError as e:
            self.stats.failures += 1
            self.stats.note(f"{task}: API error {e.status_code}; skipped")
            return None
        except a.APIConnectionError:
            self.stats.failures += 1
            self.stats.note(f"{task}: could not reach the AI service; skipped")
            return None
        u = getattr(resp, "usage", None)
        if u:
            self.stats.input_tokens += getattr(u, "input_tokens", 0) or 0
            self.stats.output_tokens += getattr(u, "output_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            self.stats.failures += 1
            self.stats.note(f"{task}: the model declined; skipped")
            return None
        if resp.stop_reason == "max_tokens":
            self.stats.failures += 1
            self.stats.note(f"{task}: answer was cut off; skipped")
            return None
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self.stats.failures += 1
            self.stats.note(f"{task}: answer was not valid JSON; skipped")
            return None


class ViaSocketAIBackend:
    """viaSocket AI (POST /embed/ai/message): runs on viaSocket's model accounts, billed as viaSocket AI credits.
    Structure work goes to a model that follows rules (Claude Sonnet 5.5); row labelling at volume to a cheap
    one (GPT-6 Luna). Override with RECON_AI_STRUCTURE_MODEL / RECON_AI_TYPES_MODEL as 'service:model'."""

    supports_attachments = True

    TASK_MODELS = {"extract-structure": ("RECON_AI_STRUCTURE_MODEL", "anthropic:claude-sonnet-5-5"),
                   "vision-ledger": ("RECON_AI_VISION_MODEL", "anthropic:claude-sonnet-5-5"),
                   "extract-types": ("RECON_AI_TYPES_MODEL", "openai:gpt-6-luna")}

    def __init__(self, stats: Optional[AIStats] = None):
        from . import viasocket
        self.vs = viasocket
        self.stats = stats or AIStats()
        self.model = "viaSocket AI"

    def json(self, task, system, user, schema, effort="medium", max_tokens=16000, attachments=None):
        import uuid
        env, default = self.TASK_MODELS.get(task, ("RECON_AI_MODEL_VIASOCKET", "anthropic:claude-sonnet-5-5"))
        service, model = os.environ.get(env, default).split(":", 1)
        self.stats.calls += 1
        body = {"message": user, "prompt": system, "response_type": "json_schema",
                "json_schema": {"name": re.sub(r"[^a-z0-9_]", "_", task.lower()), "schema": schema, "strict": True},
                "service": service, "model": model, "thread_id": f"recon-{task}-{uuid.uuid4().hex[:20]}"}
        if attachments:
            body["attachments"] = attachments[:10]           # images by https URL (viaSocket fetches them)
        try:
            data = self.vs.ask_ai(self.vs.app_user(), body)
        except self.vs.ViaSocketError as e:
            self.stats.failures += 1
            reason = "AI credits are exhausted" if e.status == 402 else f"error {e.status}: {str(e)[:120]}"
            self.stats.note(f"{task}: viaSocket AI {reason}; skipped")
            return None
        except Exception as e:  # network
            self.stats.failures += 1
            self.stats.note(f"{task}: could not reach viaSocket AI ({type(e).__name__}); skipped")
            return None
        content = (data or {}).get("content")
        if (data or {}).get("finish_reason") == "truncated":
            self.stats.failures += 1
            self.stats.note(f"{task}: answer was cut off; skipped")
            return None
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                self.stats.failures += 1
                self.stats.note(f"{task}: answer was not valid JSON; skipped")
                return None
        return content if isinstance(content, dict) else None


def default_backend(stats: Optional[AIStats] = None):
    """viaSocket AI when its keys are set (preferred), else Anthropic when a key is set, else None."""
    if os.environ.get("RECON_AI", "").lower() in ("off", "0", "false", "no"):
        return None
    from . import viasocket
    if viasocket.configured():
        return ViaSocketAIBackend(stats=stats)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_PROFILE")):
        return None
    try:
        return ClaudeBackend(stats=stats)
    except Exception as exc:  # SDK missing or misconfigured: run without AI
        log.warning("AI disabled: %s", exc)
        return None


def _col_letter(c: int) -> str:
    s, c = "", c + 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_index(letter: str) -> Optional[int]:
    letter = (letter or "").strip().upper()
    if not letter.isalpha() or len(letter) > 3:
        return None
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1
