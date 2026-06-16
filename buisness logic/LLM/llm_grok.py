"""xAI Grok LLM adapter (OpenAI-compatible chat API)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()


class GrokLLM:
    def __init__(self, model: str, json_mode: bool, api_key: str | None = None):
        self.model_name = model
        self.json_mode = json_mode
        self.api_key = api_key or os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
        if not self.api_key:
            raise RuntimeError("XAI_API_KEY or GROK_API_KEY not set in the environment")

    def generate(self, system: str, user: str) -> str:
        url = "https://api.x.ai/v1/chat/completions"
        messages = []
        if (system or "").strip():
            messages.append({"role": "system", "content": system.strip()})
        messages.append({"role": "user", "content": (user or "").strip()})

        body: dict = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.1,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choices = payload.get("choices") or []
            if not choices:
                return ""
            return (choices[0].get("message") or {}).get("content") or ""
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 or "quota" in err_body.lower():
                print(f"[Grok quota/rate limit] {err_body[:120]}…")
                return ""
            print(f"[Grok HTTP {e.code} — fallback] {err_body[:120]}…")
            return ""
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "rate limit" in err.lower():
                print(f"[Grok quota exceeded] {err[:120]}…")
                return ""
            print(f"[Grok error — fallback] {err[:120]}…")
            return ""
