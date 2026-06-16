import re
from collections import Counter
from pathlib import Path

def simple_keywords(text, topk=8):
    tokens = re.findall(r"[a-zA-Z+#]{2,}", text.lower())
    stop = set("""a an the and of to in is are was were be been being for with on at from by this that which as into than then it its or not your our their you we i about over under out up down between among across via per vs vs.""".split())
    toks = [t for t in tokens if t not in stop]
    return [w for w,_ in Counter(toks).most_common(topk)]

def llm_answer(sub_query: str, corpus_path: str | None) -> str:
    """
    Offline 'LLM' stub:
    - looks for keywords in sub_query
    - searches a small local corpus for sentences containing those words
    - returns a 2-3 sentence blurb
    """
    if not corpus_path or not Path(corpus_path).exists():
        return f"(LLM stub) For query: '{sub_query}', trend: skills like Python, SQL, cloud and data engineering are frequently mentioned across career sources."

    corpus = Path(corpus_path).read_text(encoding="utf-8", errors="ignore")
    keys = simple_keywords(sub_query, topk=6)
    sents = re.split(r"(?<=[.!?])\s+", corpus)
    hits = []
    for s in sents:
        if any(k in s.lower() for k in keys):
            hits.append(s.strip())
        if len(hits) >= 3:
            break
    if not hits:
        return f"(LLM stub) No direct matches; overall interest rising for {', '.join(keys)}."
    return " ".join(hits[:3])
