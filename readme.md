Language-Cleaner README

(version: 2025-06-18)

⸻

1. What this tool does

language_cleaner_updated.py is a CLI micro-service that scans Word (DOCX) and PDF files for language that may trigger compliance review under the 2025 Executive Order 14151, Alabama SB129, Project 2025, or similar “DEI–sensitive” funding rules.
It then:
	1.	Detects 150 + watch-list terms (equity, inclusivity, SDOH, PhenX, race descriptors, “disadvantaged,” etc.).
	2.	Calls GPT-4o once per paragraph (micro-batched) to rewrite every flagged sentence using neutral, outcome-focused phrasing drawn from language_guidance.json.
	3.	Self-audits with deterministic validators → rewrites any failures until ≥ 90 % of sentences pass.
	4.	Writes per-archive CSVs:

file	contents
flagged_details_<archive>.csv	full side-by-side sentence rewrites + metadata
flagged_summary_<archive>.csv	counts by DocumentSet / Context
quality_report_<archive>.txt	pass-rate, leak counts, timing
manual_review_<archive>.csv	any sentences that still need eyes


⸻

2. Key features
	•	JSON-driven policy – All terms & preferred rewrites live in language_guidance.json; no code edits for new rules.
	•	Paragraph micro-batching – One GPT call per paragraph, not per sentence → ~4× faster.
	•	Threaded I/O – DOCX/PDF parsed in a thread pool (≤ 8 workers).
	•	Fast PDF extraction – Uses PyMuPDF (GPU/CUDA/MPS) if available, falls back to PyPDF2.
	•	Adaptive retry – Exponential back-off with jitter on OpenAI rate limits.
	•	Progress bar – Live tqdm updates as each file finishes.
	•	Per-ZIP isolation – Finishes one archive, writes CSVs, then moves on (review while it runs).
	•	No heavy deps – Pure csv instead of pandas; no seaborn/matplotlib.

⸻

3. Installation

git clone https://github.com/your-org/grant-language-flag.git
cd grant-language-flag
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # openai, python-docx, PyMuPDF, PyPDF2, tqdm, python-dotenv

Create a .env with:

OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini        # default if omitted

(PyMuPDF is optional but recommended for PDF speed.)

⸻

4. Usage

python language_cleaner_updated.py  1_CoreProjects.zip  2_CenterBiosketches.zip

Console output:

🚀 Starting Language Cleaner...
📋 Using model: gpt-4o-mini
📚 Using PyMuPDF for PDF text extraction

================  STARTING 2_CenterBiosketches.zip ================
🧵 Using thread pool with 8 workers
Processing files:  35%|█████▍              | 8/23 [00:15<00:29,  1.02 file/s]
▶ Round 1: 5 / 142 fail (96.48% pass) | flag leaks=3, race leaks=2
✅ Threshold reached (≥ 90%).
🕒 Total validation time: 12.7 s
📊 2_CenterBiosketches.zip: 137 flagged sentences
📄 Outputs:
   - flagged_details_2_CenterBiosketches.csv
   - flagged_summary_2_CenterBiosketches.csv
   - quality_report_2_CenterBiosketches.txt
   - manual_review_2_CenterBiosketches.csv
===================================================================


⸻

5. Outputs explained

flagged_details_<archive>.csv

DocumentSet	File	Flagged Terms	Original Sentence	Suggested Sentence	Context	Actionable	Confidence


	•	Context = Publication / Grant / JobTitle / CenterName / Other
	•	Actionable:
	•	No – center name or formal title (do not change)
	•	Review – publication or grant title (likely must stay)
	•	Yes – everything else

flagged_summary_<archive>.csv

Simple pivot for quick counts.

quality_report_<archive>.txt

Rounds run, final pass-rate, leak counts, elapsed time.

manual_review_<archive>.csv

Sentences that couldn’t be fully cleaned after three rounds.

⸻

6. Adding new terms / phrases
	1.	Edit language_guidance.json

{
  "replacement_map": {
    "bipoc": "populations with limited access to care",
    "disadvantaged": "populations with limited access to healthcare resources",
    "culturally relevant": "developed with input from community partners"
  },
  "flag_terms": [
    "bipoc",
    "disadvantaged",
    "culturally relevant"
  ]
}

	2.	No code changes needed – rerun the script.

(“extras” list in the script handles low-risk one-word adds; JSON is canonical.)

⸻

7. Troubleshooting

Symptom	Fix
AttributeError: module 'openai' has no attribute '...'	Make sure openai>=1.12 is installed (pip install --upgrade openai).
Progress bar doesn’t move	Likely an exception inside a thread; check skipped_files.log or console stack-trace.
PyMuPDF missing	pip install PyMuPDF or ignore, the code will fallback.
Cost concerns	Switch OPENAI_MODEL=gpt-3.5-turbo-0125; accuracy drops slightly but cost ≈ 10× cheaper.
Need more strictness	Raise CONFIDENCE_THRESHOLD to 0.95 or add more forbidden stems to sentence_has_flag.



Feel free to adapt for internal grant-prep workflows; please retain attribution if redistributing.