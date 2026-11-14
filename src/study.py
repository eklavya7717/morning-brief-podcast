"""Study layer around the daily edition.

* no-repeat memory (``seen.json``): a story used in the last 14 days is not used again
* syllabus topic tags (rule based, no model needed)
* ``archive/<date>.json`` + ``cards.tsv`` (Anki-importable flashcards) + a searchable ``index.html``
* week / month in review material, a recall quiz, maxim / Article / terms of the day, Sunday answer drill
* a printable "study sheet" PDF
"""
from __future__ import annotations

import csv
import html
import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from src import llm

ROOT = Path(__file__).resolve().parents[1]
SEEN_TTL_DAYS = 14

# --------------------------------------------------------------------- topics
TOPICS = (
    ("Criminal law (BNS)", r"\bbns\b|bharatiya nyaya sanhita|\bipc\b|indian penal code|murder|rape|cheating|defamation|culpable"),
    ("Criminal procedure (BNSS)", r"\bbnss\b|bharatiya nagarik suraksha|\bcrpc\b|criminal procedure|\bbail\b|anticipatory|remand|chargesheet|quash|\bfir\b|investigation"),
    ("Evidence (BSA)", r"\bbsa\b|bharatiya sakshya|evidence act|admissib|confession|dying declaration|burden of proof"),
    ("Civil procedure (CPC)", r"\bcpc\b|civil procedure|order vii|injunction|res judicata|decree|\bsuit\b|plaint|execution"),
    ("Constitutional law", r"constitution|article \d+|fundamental right|writ|basic structure|federal|governor|legislat|parliament"),
    ("Contract & commercial", r"contract|arbitration|negotiable instrument|section 138|insolvency|\bibc\b|company law|specific performance"),
    ("Property & land", r"transfer of property|easement|mortgage|lease|land acquisition|partition|tenan|possession"),
    ("Family & succession", r"hindu succession|maintenance|divorce|custody|guardian|adoption|marriage|domestic violence|coparcener"),
    ("Service & administrative", r"service matter|compulsory retirement|disciplinary|recruitment|public servant|promotion|natural justice|writ of mandamus"),
    ("Tort, consumer & motor", r"negligence|motor vehicles|consumer|compensation|tort|accident"),
    ("Limitation", r"\blimitation\b|condonation of delay"),
    ("Environment", r"environment|pollution|forest|wildlife|green tribunal|cracker"),
    ("Judiciary & profession", r"collegium|appointment of judges|bar council|advocate|judicial officer|contempt|\bcji\b"),
    ("Rajasthan", r"rajasthan|jaipur|jodhpur"),
)


def topics_for(item: dict, limit: int = 3) -> list[str]:
    from src import syllabus

    return syllabus.match(item)["acts"][:limit] or ["General"]


def annotate(brief: dict) -> None:
    for sec in brief.get("sections", []):
        for item in sec.get("items", []):
            item["topics"] = topics_for(item)
    for item in brief.get("judiciary_capsule") or []:
        item["topics"] = topics_for(item)
    if brief.get("top_story"):
        brief["top_story"]["topics"] = topics_for(brief["top_story"])


# ------------------------------------------------------------ no-repeat memory
def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def filter_seen(items_by_source: dict, seen_path: Path, today: str) -> tuple[dict, int]:
    """Drop stories already used in the last 14 days (entries stamped today are ignored: re-runs stay whole)."""
    seen = _load(seen_path, {})
    dropped, out = 0, {}
    for name, items in items_by_source.items():
        kept = []
        for it in items or []:
            if seen.get(it.get("id"), today) != today and it.get("id") in seen:
                dropped += 1
            else:
                kept.append(it)
        out[name] = kept
    return out, dropped


def mark_seen(brief: dict, seen_path: Path, today: str) -> None:
    seen = _load(seen_path, {})
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    for sec in brief.get("sections", []):
        for item in sec.get("items", []):
            seen.setdefault(item["id"], today)
    Path(seen_path).write_text(json.dumps(seen, indent=0), encoding="utf-8")


# -------------------------------------------------------------------- archive
KEEP = ("id", "date", "source", "category", "title", "court", "case_name", "ratio", "summary", "url", "priority", "topics", "note", "syllabus")


def archive_edition(brief: dict, root: Path, date: str) -> Path:
    rows = []
    for sec in brief.get("sections", []):
        for item in sec.get("items", []):
            row = {k: item.get(k) for k in KEEP if k != "date"}
            row["date"] = date
            rows.append(row)
    d = Path(root) / "archive"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{date}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    return out


def load_archive(root: Path, date: str, days: int) -> list[dict]:
    start = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days)
    rows = []
    for f in sorted((Path(root) / "archive").glob("20??-??-??.json")):
        day = datetime.strptime(f.stem, "%Y-%m-%d")
        if start <= day < datetime.strptime(date, "%Y-%m-%d"):
            rows += _load(f, [])
    return rows


def recap_for(root: Path, date: str) -> dict | None:
    """Sunday -> the week in review; the 1st of a month -> the month in review."""
    dt = datetime.strptime(date, "%Y-%m-%d")
    if dt.day == 1:
        label, days, limit = "month", 31, 16
    elif dt.weekday() == 6:
        label, days, limit = "week", 7, 10
    else:
        return None
    legal = [r for r in load_archive(root, date, days) if r.get("category") in
             {"supreme-court", "high-court", "legislation", "judiciary"} and (r.get("ratio") or r.get("summary"))]
    legal.sort(key=lambda r: -(r.get("priority") or 0))
    seen, picked = set(), []
    for r in legal:
        key = (r.get("case_name") or r.get("title") or "").lower()[:60]
        if key not in seen:
            seen.add(key)
            picked.append(r)
    return {"label": label, "items": picked[:limit]} if len(picked) >= 4 else None


# ------------------------------------------------------------------ flashcards
def cards_append(brief: dict, root: Path, date: str) -> int:
    """Anki-importable (tab separated: front, back, tags, id). One card per ruling with a stated principle."""
    path = Path(root) / "cards.tsv"
    have = set()
    if path.exists():
        have = {line.rsplit("\t", 1)[-1].strip() for line in path.read_text(encoding="utf-8").splitlines()}
    rows = []
    for sec in brief.get("sections", []):
        for item in sec.get("items", []):
            note = item.get("note") or {}
            back = note.get("Principle") or item.get("ratio") or ((item.get("bullets") or [""])[0])
            if item["id"] in have or not back or not (item.get("case_name") or item.get("court")):
                continue
            front = f"{item.get('case_name') or item['title']} ({item.get('court') or 'court'}, {date}): what was held / the principle?"
            tags = " ".join(t.replace(" ", "_") for t in item.get("topics") or [])
            rows.append("\t".join([front.replace("\t", " "), back.replace("\t", " "), tags, item["id"]]))
    if rows:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(rows) + "\n")
    return len(rows)


# ------------------------------------------------------------- daily material
MAXIMS = (
    ("Audi alteram partem", "Hear the other side: no one should be condemned unheard. The core of natural justice."),
    ("Nemo judex in causa sua", "No one should be a judge in their own cause: the rule against bias."),
    ("Actus non facit reum nisi mens sit rea", "An act alone does not make a person guilty unless the mind is guilty: mens rea."),
    ("Ubi jus ibi remedium", "Where there is a right, there is a remedy."),
    ("Res ipsa loquitur", "The thing speaks for itself: negligence can be inferred from the very occurrence."),
    ("Ignorantia juris non excusat", "Ignorance of the law is no excuse."),
    ("Volenti non fit injuria", "To a willing person no injury is done: consent as a defence in tort."),
    ("Caveat emptor", "Let the buyer beware."),
    ("Ex turpi causa non oritur actio", "No action arises from an immoral or illegal cause."),
    ("Stare decisis", "To stand by what has been decided: the doctrine of precedent."),
    ("Ratio decidendi", "The reason for the decision: the binding part of a judgment."),
    ("Obiter dictum", "Said in passing: persuasive, not binding."),
    ("Sub judice", "Under judicial consideration."),
    ("Locus standi", "The right to approach the court."),
    ("Per incuriam", "Through lack of care: a ruling that overlooks binding law or a statute has weak precedential value."),
    ("Sub silentio", "Passed without argument or notice: a point not argued is not a binding decision on it."),
    ("Ultra vires", "Beyond the powers conferred."),
    ("Pari materia", "On the same subject: statutes on the same subject are read together."),
    ("Ejusdem generis", "Of the same kind: general words after specific ones take their meaning from them."),
    ("Expressio unius est exclusio alterius", "The express mention of one thing excludes others."),
    ("Noscitur a sociis", "A word is known by the company it keeps."),
    ("Generalia specialibus non derogant", "General provisions do not override special ones."),
    ("Lex posterior derogat priori", "A later law overrides an earlier one."),
    ("Nemo debet bis vexari", "No one should be troubled twice for the same cause: the idea behind protection against double jeopardy."),
    ("Doli incapax", "Incapable of crime, for example a very young child."),
    ("Injuria sine damno", "Violation of a legal right is actionable even without actual damage."),
    ("Damnum sine injuria", "Damage without violation of a legal right is not actionable."),
    ("Qui facit per alium facit per se", "He who acts through another acts himself."),
    ("Respondeat superior", "Let the master answer: an employer's vicarious liability."),
    ("Lis pendens", "A pending suit: a transfer of the disputed property during the case does not defeat the outcome."),
    ("Functus officio", "Having discharged the duty: authority is exhausted once the task is complete."),
    ("Delegatus non potest delegare", "A delegate cannot further delegate."),
    ("Habeas corpus", "Produce the body: the writ against unlawful detention."),
    ("Mandamus", "We command: the writ directing a public authority to perform a public duty."),
    ("Certiorari", "To be certified: the writ to quash an order of a lower body or tribunal."),
    ("Quo warranto", "By what authority: the writ testing a claim to a public office."),
    ("In personam and in rem", "Against a person, and against the world at large."),
)
ARTICLES = (
    ("14", "Equality before law and equal protection of the laws."),
    ("15", "No discrimination on grounds of religion, race, caste, sex or place of birth."),
    ("16", "Equality of opportunity in public employment."),
    ("17", "Abolition of untouchability."),
    ("19", "Freedoms of speech and expression, assembly, association, movement, residence and profession."),
    ("20", "Protection in respect of conviction: no ex post facto penal law, no double jeopardy, no self-incrimination."),
    ("21", "Protection of life and personal liberty except by procedure established by law."),
    ("21A", "Right to free and compulsory education for children aged six to fourteen."),
    ("22", "Protection against arrest and detention, including preventive detention."),
    ("25", "Freedom of conscience and the right to profess, practise and propagate religion."),
    ("32", "Right to constitutional remedies: approaching the Supreme Court for enforcement of fundamental rights."),
    ("39A", "Equal justice and free legal aid."),
    ("44", "Uniform civil code, a directive principle."),
    ("50", "Separation of the judiciary from the executive."),
    ("123", "The President's power to promulgate ordinances when Parliament is not in session."),
    ("124", "Establishment and constitution of the Supreme Court."),
    ("136", "Special leave to appeal to the Supreme Court."),
    ("141", "Law declared by the Supreme Court binds all courts in India."),
    ("142", "The Supreme Court's power to do complete justice in any cause or matter."),
    ("213", "The Governor's power to promulgate ordinances."),
    ("226", "High Courts' power to issue writs for fundamental and other legal rights."),
    ("227", "High Courts' superintendence over courts and tribunals within their jurisdiction."),
    ("233", "Appointment of district judges."),
    ("235", "Control over subordinate courts vested in the High Court."),
    ("246", "Distribution of legislative subjects between Parliament and State legislatures."),
    ("254", "Inconsistency between Union and State laws on concurrent subjects."),
    ("300A", "No person to be deprived of property except by authority of law."),
    ("311", "Safeguards for civil servants against dismissal, removal or reduction in rank."),
    ("324", "Superintendence and control of elections by the Election Commission."),
    ("356", "President's rule on failure of constitutional machinery in a State."),
    ("368", "Power of Parliament to amend the Constitution, and the procedure."),
)
TERMS = (
    ("adjournment", "स्थगन"), ("bail", "जमानत"), ("stay order", "स्थगन आदेश"), ("injunction", "निषेधाज्ञा"),
    ("plaintiff", "वादी"), ("defendant", "प्रतिवादी"), ("appellant", "अपीलार्थी"), ("respondent", "प्रत्यर्थी"),
    ("petitioner", "याचिकाकर्ता"), ("summons", "समन"), ("warrant", "वारंट"), ("evidence", "साक्ष्य"),
    ("witness", "साक्षी"), ("cross-examination", "प्रतिपरीक्षा"), ("examination-in-chief", "मुख्य परीक्षा"),
    ("charge sheet", "आरोप पत्र"), ("jurisdiction", "अधिकारिता"), ("decree", "डिक्री"), ("judgment", "निर्णय"),
    ("affidavit", "शपथ पत्र"), ("acquittal", "दोषमुक्ति"), ("conviction", "दोषसिद्धि"), ("sentence", "दण्डादेश"),
    ("cognizable offence", "संज्ञेय अपराध"), ("non-bailable", "अजमानतीय"), ("trial", "विचारण"), ("appeal", "अपील"),
    ("revision", "पुनरीक्षण"), ("review", "पुनर्विलोकन"), ("writ", "रिट"), ("limitation", "परिसीमा"),
    ("ex parte", "एकपक्षीय"), ("compensation", "प्रतिकर"), ("mortgage", "बंधक"), ("lease", "पट्टा"),
    ("will", "वसीयत"), ("partition", "बंटवारा"), ("easement", "सुखाधिकार"), ("negligence", "उपेक्षा"),
    ("arbitration", "माध्यस्थम्"), ("maintenance", "भरण-पोषण"), ("custody", "अभिरक्षा"),
    ("interim order", "अंतरिम आदेश"), ("precedent", "पूर्व-निर्णय"), ("fundamental rights", "मूल अधिकार"),
    ("public interest litigation", "जनहित याचिका"),
)


def daily_material(date: str) -> dict:
    n = datetime.strptime(date, "%Y-%m-%d").toordinal()
    maxim, article = MAXIMS[n % len(MAXIMS)], ARTICLES[n % len(ARTICLES)]
    terms = [TERMS[(n * 5 + i) % len(TERMS)] for i in range(5)]
    return {"maxim": maxim, "article": article, "terms": terms, "oldnew": OLD_NEW[n % len(OLD_NEW)]}


STUDY_SYSTEM = (
    "You write study material for an Indian law student preparing for the judicial services exams. "
    "Use ONLY the NOTES given; never add facts, case names or section numbers that are not in them."
)


def quiz(brief: dict, cfg: dict | None = None) -> list[tuple[str, str]]:
    """Five recall questions: model-written from the day's rulings, else simple 'what was held' cards."""
    rows = [i for s in brief.get("sections", []) for i in s.get("items", [])
            if (i.get("ratio") or i.get("bullets")) and (i.get("case_name") or i.get("court"))]
    rows.sort(key=lambda i: -(i.get("priority") or 0))
    rows = rows[:8]
    if not rows:
        return []
    notes = "\n".join(f"- {r.get('case_name') or r['title']} ({r.get('court') or ''}): "
                      f"{r.get('ratio') or ' '.join(r.get('bullets') or [])}" for r in rows)
    got = llm.chat(
        "Write exactly 5 short recall questions with one-line answers about these rulings. "
        "Format each as two lines: 'Q: ...' then 'A: ...'.\nNOTES:\n" + notes,
        system=STUDY_SYSTEM, max_tokens=700) if llm.available() else None
    pairs = re.findall(r"Q:\s*(.+?)\s*\n\s*A:\s*(.+?)(?=\n\s*Q:|\Z)", got or "", flags=re.S)
    if len(pairs) >= 3:
        from src.deepscript import guard
        ok = [(q.strip(), a.strip()) for q, a in pairs if guard(q + " " + a, notes, max_drop=0)]
        if len(ok) >= 3:
            return ok[:5]
    return [(f"In {r.get('case_name') or r['title']}, what principle did the court lay down?",
             r.get("ratio") or " ".join(r.get("bullets") or [])) for r in rows[:5]]


def drill(brief: dict, date: str) -> str | None:
    """Sunday only: a short answer-writing exercise built from the week's most important ruling."""
    if datetime.strptime(date, "%Y-%m-%d").weekday() != 6 or not llm.available():
        return None
    legal = [i for s in brief.get("sections", []) for i in s.get("items", []) if i.get("body")]
    if not legal:
        return None
    top = max(legal, key=lambda i: i.get("priority") or 0)
    got = llm.chat(
        "Using ONLY the source, write an answer-writing drill: (1) a three-sentence fact pattern, (2) the legal "
        "issue as one question, (3) a five-point outline of a model answer (rule, reasoning, application, "
        "conclusion). Plain text.\nSOURCE:\n" + top["body"][:9000],
        system=STUDY_SYSTEM, max_tokens=700)
    return f"{top['title']}\n\n{got}" if got else None


# ------------------------------------------------------------------ case notes
NOTE_FIELDS = ("Facts", "Issue", "Held", "Principle", "Provisions", "Exam angle")
NOTE_SYSTEM = (
    "You write case notes for an Indian judiciary (RJS) aspirant. Use ONLY the SOURCE: never add facts, "
    "case names, section numbers or holdings that are not in it. Be brief and exact; the principle (ratio) matters most."
)


def _parse_note(text: str) -> dict:
    out = {}
    pattern = r"(?im)^\s*\**(" + "|".join(NOTE_FIELDS) + r")\**\s*:\s*(.+?)(?=^\s*\**(?:" + "|".join(NOTE_FIELDS) + r")\**\s*:|\Z)"
    for key, val in re.findall(pattern, text, flags=re.S):
        out[key.title() if key != "Exam angle" else "Exam angle"] = re.sub(r"\s+", " ", val).strip(" *")
    return out


def _fallback_note(item: dict) -> dict:
    prov = [b for b in item.get("bullets") or [] if re.search(r"provision|section|article|act", b, re.I)]
    return {k: v for k, v in {
        "Facts": item.get("summary") or "",
        "Held": item.get("ratio") or ((item.get("bullets") or [""])[0]),
        "Principle": item.get("ratio") or "",
        "Provisions": "; ".join(prov[:2]),
    }.items() if v}


def case_notes(brief: dict, limit: int = 10) -> int:
    """Structured notes (facts, issue, held, principle, provisions, exam angle) for the top syllabus rulings."""
    from src.deepscript import guard

    legal = [i for s in brief.get("sections", []) for i in s.get("items", [])
             if i.get("category") in {"supreme-court", "high-court", "legislation"}
             and (i.get("syllabus") or {}).get("clusters") and (i.get("body") or i.get("ratio") or i.get("bullets"))]
    legal.sort(key=lambda i: -(i.get("priority") or 0))
    done = 0
    for it in legal[:limit]:
        source = (it.get("body") or "")[:9000] or " ".join([it.get("summary") or "", it.get("ratio") or ""] + (it.get("bullets") or []))
        note = {}
        if llm.available() and len(source.split()) >= 40:
            got = llm.chat(
                "Write a case note as exactly these labelled lines, each one or two short sentences: "
                "Facts: (brief facts) / Issue: (the legal question) / Held: (the decision) / Principle: (the ratio "
                "decidendi as a rule of law) / Provisions: (sections, articles and cases named in the source, else "
                "'not stated') / Exam angle: (which Act and how it could be asked, e.g. issue framing or ingredients).\n"
                f"HEADLINE: {it.get('title')}\nSOURCE:\n{source}",
                system=NOTE_SYSTEM, max_tokens=500)
            parsed = _parse_note(got or "")
            clean = {}
            for k, v in parsed.items():
                v = v.replace("‑", "-").replace("‐", "-")
                g = guard(v, f"{it.get('title')} {source} {' '.join((it.get('syllabus') or {}).get('acts') or [])}", max_drop=0.34)
                if g:  # unsupported section/year/number sentences are dropped; an emptied field is omitted
                    clean[k] = g
            if {"Facts", "Held", "Principle"} <= set(clean):
                note = clean
        it["note"] = note or _fallback_note(it)
        done += 1
    return done


OLD_NEW = (
    ("IPC 302, punishment for murder", "BNS 103"), ("IPC 300, murder", "BNS 101"), ("IPC 299, culpable homicide", "BNS 100"),
    ("IPC 304B, dowry death", "BNS 80"), ("IPC 307, attempt to murder", "BNS 109"), ("IPC 375 and 376, rape", "BNS 63 and 64"),
    ("IPC 420, cheating", "BNS 318(4)"), ("IPC 498A, cruelty by husband or relatives", "BNS 85 and 86"),
    ("IPC 34, common intention", "BNS 3(5)"), ("IPC 120B, criminal conspiracy", "BNS 61(2)"),
    ("CrPC 154, FIR", "BNSS 173"), ("CrPC 41, arrest without warrant", "BNSS 35"), ("CrPC 125, maintenance", "BNSS 144"),
    ("CrPC 161, police examination of witnesses", "BNSS 180"), ("CrPC 164, confession before a Magistrate", "BNSS 183"),
    ("CrPC 313, examination of the accused", "BNSS 351"), ("CrPC 437, bail in non-bailable cases", "BNSS 480"),
    ("CrPC 438, anticipatory bail", "BNSS 482"), ("CrPC 439, special bail powers of High Court and Sessions Court", "BNSS 483"),
    ("CrPC 482, inherent powers of the High Court", "BNSS 528"), ("Evidence Act 27, discovery", "BSA 23 (proviso)"),
    ("Evidence Act 32, dying declaration", "BSA 26"), ("Evidence Act 65B, electronic records", "BSA 63"),
    ("Evidence Act 106, fact especially within knowledge", "BSA 109"), ("Evidence Act 114, presumptions", "BSA 119"),
)


# ------------------------------------------------------------------ study sheet
CSS = """
@page{size:A4;margin:14mm}
body{font:11pt/1.45 Georgia,serif;color:#1a1a1a}
h1{font-size:20pt;margin:0 0 2pt}h2{font-size:12.5pt;border-bottom:1.5px solid #b8893a;padding-bottom:2pt;margin:14pt 0 5pt}
.sub{color:#666;font-size:9.5pt;margin-bottom:8pt}
.q{margin:5pt 0}.a{color:#555;font-size:10pt}
table{border-collapse:collapse;width:100%}td{padding:2pt 6pt;border-bottom:1px solid #ddd;font-size:10.5pt;vertical-align:top}
.tag{font-size:8.5pt;color:#7a5a1c}.pre{white-space:pre-wrap}
"""


def sheet_html(brief: dict, date: str, qa: list, daily: dict, drill_text: str | None) -> str:
    e = html.escape
    rows = []
    for sec in brief.get("sections", []):
        for it in sec.get("items", []):
            if it.get("topics") and (it.get("case_name") or it.get("ratio")):
                rows.append(f"<tr><td><b>{e(it.get('case_name') or it['title'])}</b> <span class='tag'>"
                            f"{e(' · '.join(it['topics']))}</span><br>{e(it.get('ratio') or '')}</td></tr>")
    by_topic: dict[str, int] = {}
    for sec in brief.get("sections", []):
        for it in sec.get("items", []):
            if it.get("category") not in {"supreme-court", "high-court", "legislation", "judiciary"}:
                continue  # only legal items count; general news would drown the syllabus topics
            for t in it.get("topics") or []:
                if t != "General":
                    by_topic[t] = by_topic.get(t, 0) + 1
    topic_line = " · ".join(f"{e(t)} ({n})" for t, n in sorted(by_topic.items(), key=lambda kv: -kv[1]))
    qs = "".join(f"<div class='q'>{i}. {e(q)}</div>" for i, (q, _) in enumerate(qa, 1))
    ans = "".join(f"<div class='a'>{i}. {e(a)}</div>" for i, (_, a) in enumerate(qa, 1))
    (mx, mm), (an, am) = daily["maxim"], daily["article"]
    terms = "".join(f"<tr><td>{e(en)}</td><td>{e(hi)}</td></tr>" for en, hi in daily["terms"])
    notes = []
    for sec in brief.get("sections", []):
        for it in sec.get("items", []):
            n = it.get("note")
            if n:
                body = "".join(f"<div><b>{k}:</b> {e(n[k])}</div>" for k in NOTE_FIELDS
                               if n.get(k) and not (k == "Principle" and n.get("Held") == n[k]))
                tag = e(" · ".join((it.get("syllabus") or {}).get("acts") or []))
                notes.append(f"<div class='q'><b>{e(it.get('case_name') or it['title'])}</b> <span class='tag'>{tag}</span>{body}</div>")
    notes_html = "<h2>Case notes for your syllabus</h2>" + "".join(notes) if notes else ""
    old, new = daily["oldnew"]
    oldnew_html = f"<h2>Old and new criminal codes</h2><div><b>{e(old)}</b> is now <b>{e(new)}</b>. <span class='tag'>Verify against the BPR&amp;D comparison tables.</span></div>"
    drill_html = f"<h2>Sunday answer-writing drill</h2><div class='pre'>{e(drill_text)}</div>" if drill_text else ""
    return (f"<!doctype html><meta charset='utf-8'><title>Study sheet {date}</title><style>{CSS}</style>"
            f"<h1>Study sheet</h1><div class='sub'>Eklavya's Morning Brief · {e(brief.get('weekday', ''))}, {e(brief.get('date_pretty', date))}"
            f" · AI-assisted notes: verify before citing</div>"
            f"<h2>Topics covered today</h2><div>{topic_line}</div>"
            f"<h2>Recall quiz</h2>{qs}<h2>Answers</h2>{ans}"
            f"<h2>Maxim of the day</h2><div><b>{e(mx)}</b>: {e(mm)}</div>"
            f"<h2>Article of the day</h2><div><b>Article {e(an)}</b>: {e(am)}</div>"
            f"{oldnew_html}{notes_html}<h2>Legal terms, English and Hindi</h2><table>{terms}</table>"
            f"<h2>Flashcards from today</h2><table>{''.join(rows) or '<tr><td>None today.</td></tr>'}</table>{drill_html}")


def render_study_pdf(html_text: str, out_dir: Path, config: dict) -> Path | None:
    from src.compose import _browser_print_pdf, _find_browser

    browser = _find_browser(config)
    if not browser:
        return None
    page, pdf = Path(out_dir) / "study.html", Path(out_dir) / "study.pdf"
    page.write_text(html_text, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="mb-study-") as tmp:
        _browser_print_pdf(browser, page, pdf, Path(tmp) / "profile")
    return pdf if pdf.exists() else None


# --------------------------------------------------------------------- site
SITE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eklavya's Morning Brief: archive</title><style>
:root{--bg:#fbf8f1;--fg:#1a1a1a;--mut:#6b6b6b;--ac:#9a6f1f;--card:#fff;--line:#e4dccb}
@media(prefers-color-scheme:dark){:root{--bg:#0e1828;--fg:#ece7da;--mut:#9aa6bd;--ac:#d6aa54;--card:#14233a;--line:#243756}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 Georgia,serif}main{max-width:820px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:.4em 0}input,select{font:inherit;padding:8px;margin:4px 4px 4px 0;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}
.m{color:var(--mut);font-size:.85rem}a{color:var(--ac)}.t{font-weight:bold}
</style></head><body><main><h1>Eklavya's Morning Brief: archive</h1>
<div class="m">__LINKS__</div>
<input id="q" placeholder="Search cases, principles, courts" size="32"><select id="t"><option value="">All topics</option></select>
<div id="n" class="m"></div><div id="out"></div>
<script>
const DATA=__DATA__;const q=document.getElementById('q'),t=document.getElementById('t'),out=document.getElementById('out'),n=document.getElementById('n');
[...new Set(DATA.flatMap(r=>r.topics||[]))].sort().forEach(x=>{const o=document.createElement('option');o.value=o.textContent=x;t.appendChild(o)});
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function draw(){const k=q.value.toLowerCase(),tp=t.value;
 const rows=DATA.filter(r=>(!tp||(r.topics||[]).includes(tp))&&(!k||JSON.stringify([r.title,r.case_name,r.court,r.ratio,r.summary]).toLowerCase().includes(k))).slice(0,150);
 n.textContent=rows.length+' shown';
 out.innerHTML=rows.map(r=>`<div class="card"><div class="t">${esc(r.case_name||r.title)}</div><div class="m">${r.date} · ${esc(r.court||r.category||'')} · ${(r.topics||[]).map(esc).join(', ')}</div><div>${esc(r.ratio||r.summary)}</div><div class="m"><a href="${r.url}">source</a></div></div>`).join('')}
q.oninput=t.onchange=draw;draw();
</script></main></body></html>"""


def build_site(root: Path, release_repo: str = "") -> Path:
    rows = []
    for f in sorted((Path(root) / "archive").glob("20??-??-??.json"), reverse=True)[:365]:
        rows += _load(f, [])
    links = ['<a href="feed.xml">podcast feed</a>']
    if (Path(root) / "cards.tsv").exists():
        links.append('<a href="cards.tsv">flashcards (Anki, tab-separated)</a>')
    links.append("PDFs: pdf/YYYY-MM-DD.pdf and study/YYYY-MM-DD.pdf (last 30 days)")
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    out = Path(root) / "index.html"
    out.write_text(SITE.replace("__DATA__", data).replace("__LINKS__", " · ".join(links)), encoding="utf-8")
    return out


# ---------------------------------------------------------------- orchestrate
def prepare(brief: dict, root: Path, date: str) -> None:
    """Before audio: topics + the week/month recap material the script may use."""
    annotate(brief)
    case_notes(brief)
    recap = recap_for(root, date)
    if recap:
        brief["recap"] = recap


def finish(brief: dict, config: dict, out_dir: Path, root: Path, date: str, artifacts: dict, log=print) -> None:
    """After audio/PDF: study sheet, archive, flashcards, seen-memory, archive page, dated PDF copies."""
    import shutil

    qa = quiz(brief, config.get("audio") or {})
    sheet = render_study_pdf(sheet_html(brief, date, qa, daily_material(date), drill(brief, date)), out_dir, config)
    artifacts["study_pdf"] = sheet
    archive_edition(brief, root, date)
    log(f"study: {cards_append(brief, root, date)} new flashcards")
    mark_seen(brief, Path(root) / "seen.json", date)
    pod = (config.get("delivery") or {}).get("podcast") or {}
    for key, folder in (("pdf", "pdf"), ("study_pdf", "study")):
        src = artifacts.get(key)
        if src and Path(src).exists():
            (Path(root) / folder).mkdir(exist_ok=True)
            shutil.copyfile(src, Path(root) / folder / f"{date}.pdf")
    build_site(root, pod.get("release_repo", ""))
