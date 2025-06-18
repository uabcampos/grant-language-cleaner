#!/usr/bin/env python3
"""
language_cleaner_full_rewrite.py  (Final AI-reasoning version)
• Multi-term flag detection (incl. equity/inclusivity variants + race terms)
• Context classifier (Publication / Grant / JobTitle / CenterName / Other)
• One-shot GPT-4 rewrite prompt that:
    – Uses replacement map as guidance
    – Removes race descriptors unless scientifically essential (assume not)
    – Handles PhenX removal & SDOH singular/plural grammar
    – Allows sentence restructuring for clarity (not formulaic)

Outputs:
    flagged_details.csv
    flagged_summary.csv
"""
# Removed verbose DEBUG startup message
import sys, re, zipfile, io, string, time, random
from pathlib import Path
from typing import Dict, List
import os

import traceback

import json
import docx
from PyPDF2 import PdfReader
from PyPDF2.errors import PdfReadError
import openai
from openai import RateLimitError, APIError
from dotenv import load_dotenv
import concurrent.futures
from tqdm import tqdm

# PDF extraction: prefer PyMuPDF if available
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

# Load environment variables from .env file
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
if not api_key:
    sys.exit("Error: OPENAI_API_KEY not found in environment. Please set it in your .env file or export it.")

openai.api_key = api_key
OPENAI_MODEL = openai_model

CENTER_NAME = "deep south center to reduce disparities in chronic diseases"

# ───────────────────────────
# Iteration / confidence settings
# ───────────────────────────
CONFIDENCE_THRESHOLD = 0.90   # 90% of sentences must pass validation
MAX_ROUNDS = 3                # maximum rewrite attempts

# ───────────────────────────
# Retry wrapper for OpenAI API calls
# ───────────────────────────
def gpt_call(*args, **kw):
    """Wrapper for OpenAI API calls with exponential backoff retry and jitter"""
    for wait in (0, 2, 5, 15):
        try:
            return openai.chat.completions.create(*args, **kw)
        except RateLimitError:
            print(f"⚠️  Rate limit hit, waiting {wait} seconds...")
            time.sleep(wait + random.uniform(0, 1))
        except APIError as e:
            print(f"⚠️  API error: {e}, waiting {wait} seconds...")
            time.sleep(wait + random.uniform(0, 1))
    raise Exception("OpenAI API call failed after retries")

# ───────────────────────────
# Load JSON guidance (flags + replacement map)
# ───────────────────────────
with open("language_guidance.json") as jf:
    guide_json = json.load(jf)

replacement_map: Dict[str, str] = guide_json["replacement_map"]
flag_terms = set(guide_json["flag_terms"])
allowed_phrases = set(guide_json.get("allowed_phrases", []))

extras = [
    "gender", "social determinants of health", "sdoh",
    "phenx toolkit", "phenx",
    "non-medical needs", "food and housing insecurity",
    "equity", "equities", "inequity", "inequities",
    "inequality", "inequalities", "equitable", "equitably",
    "equity-focused", "equity-driven",
    "inclusive", "inclusivity", "inclusiveness",
    "disparities", "disparity",
    # African American variants
    "african american", "african americans", "africanamerican", "africanamerican adults",
    "bipoc", "latinx", "marginalized groups", "minorities", "vulnerable populations", "at-risk",
]
flag_terms.update(extras)

# Improved race descriptor detection – avoids matching person names like "Whitehouse"
race_regex = re.compile(
    r"\b("  # word boundary start
    r"black|white|latino|latina|latinx|hispanic|asian|"
    r"native(?:\s+american)?|indigenous|"
    r"african(?:\s+american)?|caucasian|european(?:\s+american)?|"
    r"minority|minorities|bipoc|\bpoc\b|people\s+of\s+color|racial|ethnic"
    r")\b"
    r"(?:\s+(?:adults?|people|teens?|women|men|girls?|boys?|children|individuals?|"
    r"populations?|americans?|students|groups?|communities?))?",
    re.I
)

# Additional race-related terms that should be caught
race_terms = {
    "african american", "african americans", "africanamerican", "africanamericans",
    "black", "blacks", "white", "whites", "latino", "latinos", "hispanic", "hispanics",
    "asian", "asians", "native american", "native americans", "indigenous", "indigenous people",
    "caucasian", "caucasians", "european american", "european americans",
    "minority", "minorities", "bipoc", "poc", "people of color",
    "racial", "ethnic", "racial group", "ethnic group", "racial groups", "ethnic groups"
}

# Safe replacement terms that won't trigger further flags
safe_replacement_terms = {
    "individuals", "people", "residents", "participants", "patients", "clients",
    "areas", "regions", "locations", "settings", "environments",
    "challenges", "difficulties", "obstacles", "limitations", "constraints",
    "access", "availability", "opportunities", "resources", "services",
    "healthcare", "medical care", "preventive care", "treatment",
    "research", "studies", "investigations", "examinations",
    "communities", "neighborhoods", "populations", "groups"
}

# Enhanced pattern for 'disadvantaged' to match as a standalone word or as an adjective before a noun
adjective_like = {"disadvantaged", "underserved", "underrepresented", "marginalized", "vulnerable", "at-risk"}
PATTERNS = {}
for t in list(flag_terms):
    if t in adjective_like:
        PATTERNS[t] = re.compile(rf"\b{re.escape(t)}\b(?:\s+\w+)?", re.I)
    else:
        PATTERNS[t] = re.compile(rf"\b{re.escape(t)}\b", re.I)

print(f"📑 Loaded {len(flag_terms)} flags and {len(replacement_map)} replacements from JSON guidance.")

# Debug printing disabled for production run

# ───────────────────────────
# Prompts
# ───────────────────────────
CLASSIFIER_SYS = (
    "You are a deterministic classifier. "
    "Label the paragraph as Publication, Grant, JobTitle, Other, "
    f"or CenterName if it contains \"{CENTER_NAME}\". Respond with the single label only."
)

REWRITE_SYS = """
You are a scientific copy-editor.
Rewrite the sentence in AP style, people-first language, and do ALL of the following:
• Replace or remove each flagged phrase per the instructions.
• Absolutely ensure NO flagged terms or race descriptors remain in the rewrite.
• Feel free to restructure or shorten the sentence for clarity—do NOT do word-for-word swaps.
• Never use equity/equitable/inclusive (or variants) in the final text.
• Avoid em dashes; fix any grammar left by edits.

CRITICAL: Remove ALL race descriptors including but not limited to:
- African American, Black, White, Latino, Hispanic, Asian, Native American, Indigenous
- Minority, minorities, BIPOC, POC, people of color
- Racial, ethnic, racial groups, ethnic groups
- Any other race-based descriptors

Special handling rules:
• When you see \"social determinants of health\" or \"SDOH,\" do NOT leave a generic phrase. Replace the words by listing concrete categories that fit the context (e.g., housing, education, income, food security, neighborhood safety). Use singular \"factor\" or plural \"factors\" if the grammar calls for it.
• Delete PhenX Toolkit / PhenX and replace with \"standardized data collection measures,\" then remove empty parentheses/commas.
• Delete race descriptors (Black, White, etc.) unless they are essential to scientific design (assume NOT essential here).
• Replace disparities/inequities/inequalities with \"differences\" (or \"differences in health outcomes\").

Example:
Original: \"Black adults experience disparities in health due to social determinants of health.\"
Rewrite: \"Adults experience differences in health outcomes due to factors such as housing, education, and income.\"

Example:
Original: \"African American women have higher rates of obesity.\"
Rewrite: \"Women have higher rates of obesity.\"
"""

def safe_rewrite(sentence: str, hits: List[str], extra_instruction: str = ""):
    """Always return (new_sentence, confidence) tuple even if rewrite_sentence misbehaves."""
    result = rewrite_sentence(sentence, hits, extra_instruction=extra_instruction)
    if isinstance(result, tuple) and len(result) == 2:
        return result
    # Unexpected return — coerce to tuple
    return str(result), 0.0

def classify_context(paragraph: str) -> str:
    resp = gpt_call(
        model=OPENAI_MODEL,
        messages=[{"role":"system","content":CLASSIFIER_SYS},
                  {"role":"user","content":paragraph}],
        temperature=0.0,
        max_tokens=1
    )
    # Normalize to title case for consistency
    return resp.choices[0].message.content.strip().title()

def rewrite_sentence(sentence: str, hits: List[str], extra_instruction: str = "") -> (str, float):
    try:
        # Build guidance string with safer replacements
        guidance_parts = []
        for h in hits:
            if h.startswith("phenx"):
                guidance_parts.append(f"Remove \"{h}\" and describe as standardized data collection measures.")
            elif h in ["social determinants of health", "sdoh"]:
                guidance_parts.append(
                    f"Replace \"{h}\" with specific factors such as housing, education, income, food security, or neighborhood safety—choose what best fits the sentence."
                )
            elif h in replacement_map:
                replacement = replacement_map[h]
                # Check if replacement would trigger new flags
                if any(pat.search(replacement) for pat in PATTERNS.values()):
                    # Use a safer generic replacement
                    guidance_parts.append(f"Replace \"{h}\" with a simple, clear alternative that avoids any flagged language.")
                else:
                    guidance_parts.append(f"Replace \"{h}\" with \"{replacement}\".")
            else:
                guidance_parts.append(f"Rephrase or remove \"{h}\".")
        guidance = " ; ".join(guidance_parts)
        if extra_instruction:
            guidance = guidance + " ; " + extra_instruction

        user_msg = (
            f"Flagged phrases: {', '.join(hits)}\n"
            f"Guidance: {guidance}\n\n"
            f"Original sentence:\n{sentence}"
        )
        resp = gpt_call(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":REWRITE_SYS},
                      {"role":"user","content":user_msg}],
            temperature=0.2,
            max_tokens=300
        )
        new_sent = resp.choices[0].message.content.strip()

        # -------- Post-processing leak check (any flagged term, e.g., 'underserved') --------
        all_leaks = [t for t, pat in PATTERNS.items() if pat.search(new_sent)]
        if all_leaks:
            leak_list = ", ".join(sorted(set(all_leaks)))
            stronger_msg = (
                user_msg +
                f"\n\nIMPORTANT: The earlier rewrite still contains these disallowed words: {leak_list}. "
                "Remove or replace them. DO NOT use words such as underserved, vulnerable, disparities, "
                "minority, marginalized, at-risk, or similar terms."
            )
            resp = gpt_call(
                model=OPENAI_MODEL,
                messages=[{"role": "system", "content": REWRITE_SYS},
                          {"role": "user", "content": stronger_msg}],
                temperature=0.2,
                max_tokens=300
            )
            new_sent = resp.choices[0].message.content.strip()
        # final auto-substitution pass
        for base, repl in replacement_map.items():
            # replace whole word (case-insensitive) even when part of longer phrase like 'underserved groups'
            new_sent = re.sub(rf"\b{re.escape(base)}\b", repl, new_sent, flags=re.I)
        # ---------------- end leak check ----------------

        # LLM self-evaluation confidence
        conf_prompt = (
            "On a scale of 0 to 1, how confident are you that the above rewrite is free of flagged language and reads naturally? Respond with a single number."
        )
        conf_resp = gpt_call(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": conf_prompt},
                {"role": "user", "content": new_sent}
            ],
            temperature=0,
            max_tokens=4
        )
        try:
            confidence = float(re.findall(r"[01](?:\.\d+)?", conf_resp.choices[0].message.content.strip())[0])
        except Exception:
            confidence = 0.0

        # Safety check – if any flagged term still in output, retry once with stronger instruction
        remaining = [t for t in hits if re.search(rf"\b{re.escape(t)}\b", new_sent, re.I)]
        if remaining:
            stronger_msg = (
                user_msg + "\n\nIMPORTANT: The flagged words listed above MUST NOT appear in the rewrite."
            )
            new_sent = gpt_call(
                model=OPENAI_MODEL,
                messages=[{"role":"system","content":REWRITE_SYS},
                          {"role":"user","content":stronger_msg}],
                temperature=0.2,
                max_tokens=300
            ).choices[0].message.content.strip()
            # Re-evaluate confidence
            conf_resp = gpt_call(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": conf_prompt},
                    {"role": "user", "content": new_sent}
                ],
                temperature=0,
                max_tokens=4
            )
            try:
                confidence = float(re.findall(r"[01](?:\.\d+)?", conf_resp.choices[0].message.content.strip())[0])
            except Exception:
                confidence = 0.0
        return new_sent, confidence
    except Exception as e:
        print(f"[ERROR] rewrite_sentence exception: {e}")
        return sentence, 0.0

# ───────────────────────────
# Validators
# ───────────────────────────
def sentence_has_flag(sentence: str, _hits: List[str]) -> bool:
    """
    Return True if ANY flagged term from the master list appears in the sentence
    (includes 'underserved', equity terms, etc.).
    """
    if any(pat.search(sentence) for pat in PATTERNS.values()):
        return True
    return False

def sentence_has_race_descriptor(sentence: str) -> bool:
    """Return True if a race descriptor remains."""
    # Check the regex pattern
    if race_regex.search(sentence.lower()):
        return True
    
    # Check individual race terms
    sentence_lower = sentence.lower()
    for term in race_terms:
        if re.search(rf"\b{re.escape(term)}\b", sentence_lower):
            return True
    
    return False

def is_author_surname(sentence: str, term: str) -> bool:
    # 1) Exact surnames we know:  Blackwell, Blackshear, Whitehouse …
    if re.search(rf"\b{term}(s?hear|well|house)\b", sentence, re.I):
        return True
    # 2) Pattern of surname followed by initials, eg "Black WB," or "White K."
    return bool(re.search(rf"\b{term}\s+[A-Z]{{1,3}}[.,]", sentence))

# ───────────────────────────
# Extract text
# ───────────────────────────
def paras_docx(blob): return [p.text.strip() for p in docx.Document(io.BytesIO(blob)).paragraphs if p.text.strip()]
def paras_pdf(blob):
    """Extract paragraphs from PDF using PyMuPDF for speed; fallback to PyPDF2."""
    if FITZ_AVAILABLE:
        try:
            doc = fitz.open(stream=blob, filetype="pdf")
            text_chunks = []
            for page in doc:
                text_chunks.append(page.get_text("text"))
            doc.close()
            txt = "\n\n".join(text_chunks)
            return [p.strip() for p in txt.split("\n\n") if p.strip()]
        except Exception as e:
            print(f"⚠️  PyMuPDF failed on PDF, falling back to PyPDF2: {e}")
            # fall through to PyPDF2 below

    try:
        txt = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(blob)).pages)
        return [p.strip() for p in txt.split("\n\n") if p.strip()]
    except Exception as e:
        print(f"⚠️  PDF read error: {e}")
        return []


# ───────────────────────────
# Batch rewrite helper (micro-batching per paragraph)
# ───────────────────────────
BATCH_SIZE = 15          # <= 15 sentences per GPT call

def batch_rewrite(sent_hit_list: list[tuple[str, list[str]]]) -> list[str]:
    """Rewrite sentences in chunks to avoid delimiter failures."""
    results: list[str] = []
    for start in range(0, len(sent_hit_list), BATCH_SIZE):
        chunk = sent_hit_list[start : start + BATCH_SIZE]
        rewritten = _rewrite_chunk(chunk)
        results.extend(rewritten)
    return results

def _rewrite_chunk(chunk: list[tuple[str, list[str]]]) -> list[str]:
    # original delimiter prompt logic, unchanged except takes "chunk"
    numbered = [f"{i+1}. \"{s}\" || flags: {', '.join(h)}"
                for i, (s, h) in enumerate(chunk)]
    prompt = (
        "Below are sentences with their flagged phrases.\n"
        "Return ONLY the rewritten sentences, separated by the delimiter ||| .\n"
        "Do not add any commentary.\n\n" + "\n".join(numbered)
    )
    try:
        resp = gpt_call(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": REWRITE_SYS},
                      {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1024,
        )
        raw = resp.choices[0].message.content.strip()
        rewrites = [s.strip() for s in raw.split("|||") if s.strip()]
        if len(rewrites) == len(chunk):
            return rewrites
        # log mismatch and fall through to per-sentence
    except Exception as err:
        print(f"⚠️  _rewrite_chunk failed: {err}")

    #-- Fallback – single-sentence calls
    fallback = []
    for sent, hits in chunk:
        new_sent, _ = safe_rewrite(sent, hits)
        fallback.append(new_sent)
    return fallback

def _run_pass(paths: List[Path]):
    rows = []
    skipped_files = []
    
    for z in paths:
        # ── If the path is a ZIP archive ───────────────────────────
        if zipfile.is_zipfile(z):
            print(f"\n📁 Processing archive: {z.name}")
            with zipfile.ZipFile(z) as arc:
                file_list = [m for m in arc.namelist() if not m.endswith('/') and "__macosx" not in m.lower()]
                print(f"   Found {len(file_list)} files to process")

                def handle_member(m):
                    results = []
                    try:
                        data = arc.read(m)
                        ext = Path(m).suffix.lower()
                        if ext == ".docx":
                            paras = paras_docx(data)
                        elif ext == ".pdf":
                            paras = paras_pdf(data)
                        else:
                            skipped_files.append(f"{z.name}/{Path(m).name} - unsupported filetype")
                            return results
                        if not paras:
                            skipped_files.append(f"{z.name}/{Path(m).name} - No text extracted")
                            return results
                        for para in paras:
                            ctx = None  # defer classification until we know we need it
                            pending = []   # collect (sentence, hits) tuples
                            sent_list = list(re.split(r"(?<=[.!?])\\s+", para))
                            for sent in sent_list:
                                if any(phrase in sent for phrase in allowed_phrases):
                                    continue
                                hits = [t for t, pat in PATTERNS.items() if pat.search(sent)]
                                race_hits = race_regex.findall(sent.lower())
                                clean_hits = []
                                for rh in race_hits:
                                    if rh.lower() in ("black", "white") and is_author_surname(sent, rh):
                                        continue        # skip false-positive surname
                                    clean_hits.append(rh)
                                hits.extend(clean_hits)
                                if hits:
                                    pending.append((sent, hits))
                                # Walker-specific debug disabled
                            if pending:
                                if ctx is None:
                                    ctx = classify_context(para)
                                rewrites = batch_rewrite(pending)
                                for (orig_sent, hits), new_sent in zip(pending, rewrites):
                                    results.append({
                                        "DocumentSet": z.stem,
                                        "File": Path(m).name,
                                        "Flagged Terms": "; ".join(hits),
                                        "Original Sentence": orig_sent,
                                        "Suggested Sentence": new_sent,
                                        "Paragraph": para,
                                        "Context": ctx,
                                        "Actionable": ("No" if ctx=="Centername" else
                                                       "Yes" if ctx=="Other" else
                                                       "Review"),
                                        "Confidence": 0.9  # default, since no score per batch
                                    })
                                pending = []
                        # print(f"     ✅ Completed {Path(m).name}")
                    except Exception as e:
                        print(f"     ❌ Error processing {Path(m).name}: {e}")
                        traceback.print_exc()
                        skipped_files.append(f"{z.name}/{Path(m).name} - {e}")
                    return results

                thread_workers = min(8, max(4, os.cpu_count() * 2))
                print(f"🧵 Using thread pool with {thread_workers} workers")
                with concurrent.futures.ThreadPoolExecutor(max_workers=thread_workers) as ex:
                    futures = [ex.submit(handle_member, m) for m in file_list]
                    for f in tqdm(concurrent.futures.as_completed(futures),
                                  total=len(futures),
                                  desc="Processing files",
                                  unit="file"):
                        rows.extend(f.result())
        # ── Else handle a single DOCX or PDF file ───────────────────
        else:
            print(f"\n📄 Processing file: {z.name}")
            try:
                data = z.read_bytes()
                ext = z.suffix.lower()
                if ext == ".docx":
                    paras = paras_docx(data)
                elif ext == ".pdf":
                    paras = paras_pdf(data)
                else:
                    skipped_files.append(f"{z.name} - unsupported filetype")
                    continue
                if not paras:
                    skipped_files.append(f"{z.name} - No text extracted")
                    continue

                def process_paragraphs(paras, file_name, docset):
                    for para in paras:
                        ctx = None
                        pending = []
                        for sent in re.split(r"(?<=[.!?])\\s+", para):
                            if any(phrase in sent for phrase in allowed_phrases):
                                continue
                            hits = [t for t, pat in PATTERNS.items() if pat.search(sent)]
                            race_hits = race_regex.findall(sent.lower())
                            clean_hits = []
                            for rh in race_hits:
                                if rh.lower() in ("black", "white") and is_author_surname(sent, rh):
                                    continue        # skip false-positive surname
                                clean_hits.append(rh)
                            hits.extend(clean_hits)
                            if hits:
                                pending.append((sent, hits))
                        if pending:
                            if ctx is None:
                                ctx = classify_context(para)
                            rewrites = batch_rewrite(pending)
                            for (orig_sent, hits), new_sent in zip(pending, rewrites):
                                rows.append({
                                    "DocumentSet": docset,
                                    "File": file_name,
                                    "Flagged Terms": "; ".join(hits),
                                    "Original Sentence": orig_sent,
                                    "Suggested Sentence": new_sent,
                                    "Paragraph": para,
                                    "Context": ctx,
                                    "Actionable": ("No" if ctx=="Centername" else
                                                   "Yes" if ctx=="Other" else
                                                   "Review"),
                                    "Confidence": 0.9
                                })

                process_paragraphs(paras, z.name, z.stem)
            except Exception as e:
                print(f"     ❌ Error processing {z.name}: {e}")
                traceback.print_exc()
                skipped_files.append(f"{z.name} - {e}")
            continue  # done with single file
    
    # Write skipped files log
    if skipped_files:
        with open("skipped_files.log", "w") as f:
            f.write("Files that were skipped during processing:\n")
            f.write("=" * 50 + "\n")
            for file_info in skipped_files:
                f.write(f"{file_info}\n")
        print(f"\n⚠️  {len(skipped_files)} files were skipped. See skipped_files.log for details.")
    
    return rows


def process_archives(zips: List[Path]):
    rows = _run_pass(zips)
    if not rows:
        return []
    start_time = time.time()
    print("\n🔍 Validation loop begins "
          f"(target pass rate: {CONFIDENCE_THRESHOLD:.0%}, max rounds: {MAX_ROUNDS})")
    flag_leaks, race_leaks = 0, 0
    failures = []
    pass_rate = 0.0
    for round_num in range(1, MAX_ROUNDS + 1):
        failures, flag_leaks, race_leaks = [], 0, 0
        for idx, row in enumerate(rows):
            hits = row["Flagged Terms"].split("; ")
            leaked = sentence_has_flag(row["Suggested Sentence"], hits)
            race_left = sentence_has_race_descriptor(row["Suggested Sentence"])
            if leaked or race_left:
                failures.append(idx)
                flag_leaks += leaked
                race_leaks += race_left

        if len(rows) > 0:
            pass_rate = 1 - len(failures) / len(rows)
        else:
            pass_rate = 1.0

        print(
            f"   ▶ Round {round_num}: {len(failures)} / {len(rows)} fail "
            f"({pass_rate:.2%} pass) | "
            f"flag leaks={flag_leaks}, race leaks={race_leaks}"
        )
        # Optionally show first 2 failing originals for quick inspection
        if failures[:2]:
            sample_idxs = ", ".join(str(i) for i in failures[:2])
            print(f"     sample fail rows: {sample_idxs}")

        if pass_rate >= CONFIDENCE_THRESHOLD or not failures:
            print(f"   ✅ Threshold reached ({pass_rate:.2%} ≥ {CONFIDENCE_THRESHOLD:.2%}). "
                  "Stopping iterations.")
            break  # success or nothing left to fix

        # Retry failed sentences with stronger prompt
        for idx in failures:
            row = rows[idx]
            hits = row["Flagged Terms"].split("; ")
            extra = "IMPORTANT: Absolutely remove or replace every flagged phrase. Double-check for race descriptors and forbidden terms."
            stronger_sentence, confidence = safe_rewrite(
                row["Original Sentence"],
                hits,
                extra_instruction=extra
            )
            row["Suggested Sentence"] = stronger_sentence
            row["Confidence"] = confidence
            rows[idx] = row

    # If loop finishes without meeting threshold, log warning
    if pass_rate < CONFIDENCE_THRESHOLD:
        print(f"   ⚠️  Stopped after {MAX_ROUNDS} rounds; "
              f"final pass rate {pass_rate:.2%} below threshold.")

    elapsed = time.time() - start_time
    print(f"\n🕒 Total validation time: {elapsed:.1f} seconds")

    # Log final quality
    # Determine the correct prefix for output files based on the input zips
    prefix = "language_cleaner"
    if zips:
        prefix = zips[0].stem
    
    quality_file = f"quality_report_{prefix}.txt"
    manual_file = f"manual_review_{prefix}.csv"

    with open(quality_file, "w") as qt:
        qt.write(f"Rounds run: {round_num}\n")
        qt.write(f"Final pass rate: {pass_rate:.2%}\n")
        qt.write(f"Total sentences: {len(rows)}\n")
        qt.write(f"Remaining failures: {len(failures)}\n")
        qt.write(f"Flag leaks: {flag_leaks}\n")
        qt.write(f"Race leaks: {race_leaks}\n")
    
    if len(rows) == 0:
        with open(quality_file, "a") as qt:
            qt.write("No sentences processed.\n")

    # ---------------- Second-pass auto-fix ----------------
    if failures:
        resolved = []
        extra_fix_msg = (
            "SECOND ATTEMPT: The previous rewrite still contained flagged terms. "
            "Remove or replace ALL flagged words listed. Do NOT introduce words such as underserved, "
            "vulnerable, minority, disparities, marginalized, at-risk or similar."
        )
        for idx in failures[:]:  # iterate over a copy
            row = rows[idx]
            hits = row["Flagged Terms"].split("; ")
            new_sent, conf = safe_rewrite(row["Suggested Sentence"], hits, extra_instruction=extra_fix_msg)
            # final leak check
            if not sentence_has_flag(new_sent, hits) and not sentence_has_race_descriptor(new_sent):
                row["Suggested Sentence"] = new_sent
                row["Confidence"] = conf
                rows[idx] = row
                resolved.append(idx)
                failures.remove(idx)

    # Write manual review file for remaining failures
    if failures:
        import csv
        with open(manual_file, "w", newline="") as mf:
            # Ensure rows[0] exists before accessing keys
            if rows:
                writer = csv.DictWriter(mf, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for idx in failures:
                    writer.writerow(rows[idx])
        print(f"\n⚠️  {len(failures)} sentences still require manual review. See {manual_file}.")
    if resolved:
        print(f"✅ Auto-resolved {len(resolved)} sentences on second pass. Manual review reduced.")
    return rows

# ───────────────────────────
# Entrypoint
# ───────────────────────────
def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python language_cleaner_full_rewrite.py <archive.zip> ...")
    
    print("🚀 Starting Language Cleaner...")
    print(f"📋 Using model: {OPENAI_MODEL}")
    print(f"📦 Processing {len(sys.argv)-1} archive(s)")
    print(f"📚 Using {'PyMuPDF' if FITZ_AVAILABLE else 'PyPDF2'} for PDF text extraction")
    
    zips = [Path(p).expanduser() for p in sys.argv[1:]]

    for zp in zips:
        print(f"\n================  STARTING {zp.name}  ================\n")
        rows = process_archives([zp])

        if not rows:
            print(f"✅ No flagged language found in {zp.name}.")
            continue

        prefix = zp.stem
        details_file  = f"flagged_details_{prefix}.csv"
        summary_file  = f"flagged_summary_{prefix}.csv"
        
        # Quality and manual review files are already prefixed in process_archives
        quality_file  = f"quality_report_{prefix}.txt"
        manual_file   = f"manual_review_{prefix}.csv"

        import csv, collections
        with open(details_file, "w", newline='') as cf:
            writer = csv.DictWriter(cf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        # build summary counts
        counter = collections.Counter((r['DocumentSet'], r['Context']) for r in rows)
        with open(summary_file, "w", newline='') as sf:
            sw = csv.writer(sf)
            sw.writerow(["DocumentSet", "Context", "Count"])
            for (docset, ctx), cnt in counter.items():
                sw.writerow([docset, ctx, cnt])

        # Print quick summary
        print(f"📊 {zp.name}: {len(rows)} flagged sentences")
        print(f"📄 Outputs:")
        output_files = [details_file, summary_file, quality_file, "skipped_files.log", manual_file]
        for f in output_files:
            if os.path.exists(f):
                print(f"   - {f}")

        print(f"\n================  FINISHED {zp.name}  ================\n")

if __name__ == "__main__":
    main()