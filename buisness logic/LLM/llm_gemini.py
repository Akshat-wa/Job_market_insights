from __future__ import annotations
import google.generativeai as genai
import os
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

def safety_cfg(safety: Optional[str]):
    if safety == "strict":
        return [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SEXUAL", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
                {"category": "HARM_CATEGORY_SELF_HARM", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}]
    if safety == "relaxed":
        return [{"category": c, "threshold": "BLOCK_NONE"} for c in [
            "HARM_CATEGORY_DANGEROUS_CONTENT","HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_HARASSMENT","HARM_CATEGORY_SEXUAL","HARM_CATEGORY_SELF_HARM"
        ]]
    return None

def _is_quota_or_rate_limit(err: str, exc: Exception) -> bool:
    err_l = (err or "").lower()
    name = type(exc).__name__
    return (
        "429" in err
        or "quota" in err_l
        or "rate limit" in err_l
        or "resourceexhausted" in name.lower()
        or "too many requests" in err_l
    )

class GeminiLLM:
    def __init__(self, model: str, json_mode: bool, safety: Optional[str]):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in the environment")
        genai.configure(api_key=api_key)
        self.model_name = model          # store the *name*
        self.json_mode = json_mode
        self.safety = safety_cfg(safety)
    
    def generate(self, system, user):
        generation_config = None
        if self.json_mode:
            generation_config = {"response_mime_type": "application/json"}

        try:
            model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=(system or "").strip() or None
            )
            resp = model.generate_content(
                (user or "").strip(),
                safety_settings=self.safety,
                generation_config=generation_config
            )
            try:
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    from LLM.llm_status import record_provider_outcome
                    record_provider_outcome("gemini", "ok")
                return text
            except Exception as e:
                if _is_quota_or_rate_limit(str(e), e):
                    from LLM.llm_status import record_provider_outcome
                    record_provider_outcome("gemini", "quota_exhausted", str(e))
                    print(f"[Gemini quota/rate limit on response.text] {str(e)[:120]}…")
                    return ""
                raise
        except Exception as e:
            err = str(e)
            if _is_quota_or_rate_limit(err, e):
                from LLM.llm_status import record_provider_outcome
                record_provider_outcome("gemini", "quota_exhausted", err)
                print(f"[Gemini quota exceeded — fallback] {err[:120]}…")
                return ""
            from LLM.llm_status import record_provider_error
            record_provider_error("gemini", err)
            print(f"[Gemini error — fallback] {err[:120]}…")
            return ""
