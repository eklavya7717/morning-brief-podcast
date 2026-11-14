"""RJS Mains syllabus map: which cluster/Act a story belongs to, and how much it should count.

Mirrors the user's roadmap clusters (weights follow its priority column). Old and new criminal codes are
both matched, since case law still cites IPC/CrPC/Evidence Act. Pure rules, no model needed.
Always check the official Rajasthan High Court schedule: it beats this list.
"""
from __future__ import annotations

import re

# (cluster, boost, [(act label, regex)])
CLUSTERS = (
    ("Civil procedure", 12, [
        ("CPC", r"code of civil procedure|\bcpc\b|order (?:i|ii|vi|vii|viii|ix|xi|xiv|xx|xxi|xxxix|xli|xlvii)\b|res judicata|"
                r"\bplaint\b|written statement|ex parte decree|temporary injunction|execution of (?:a )?decree|\bdecree\b|"
                r"civil suit|second appeal|civil revision|framing of issues|court fees|suits valuation"),
    ]),
    ("Evidence and crime", 12, [
        ("BSA / Evidence", r"bharatiya sakshya|\bbsa\b|evidence act|dying declaration|extra-judicial confession|confession|"
                           r"circumstantial evidence|last seen|burden of proof|electronic evidence|65b|admissib|"
                           r"test identification|hostile witness"),
        ("BNS / IPC", r"bharatiya nyaya sanhita|\bbns\b|indian penal code|\bipc\b|murder|culpable homicide|common intention|"
                      r"conspiracy|abetment|cheating|criminal breach of trust|forgery|rape|dowry death|attempt to murder"),
        ("BNSS / CrPC", r"bharatiya nagarik suraksha|\bbnss\b|\bcrpc\b|code of criminal procedure|\bbail\b|anticipatory|"
                        r"framing of charge|discharge|quashing|section 482|section 528|chargesheet|investigation|remand|"
                        r"acquit|conviction|sentenc|remission|custodial|\bfir\b"),
    ]),
    ("Obligations", 8, [
        ("Contract / Specific Relief", r"indian contract act|\bcontract\b|specific relief|specific performance|indemnity|guarantee|"
                                       r"bailment|\bagency\b|breach of contract|void agreement|liquidated damages|readiness and willingness"),
        ("Sale of Goods / Partnership", r"sale of goods|partnership act|\bpartnership\b|\bpartner\b"),
    ]),
    ("Property", 8, [
        ("Transfer of Property", r"transfer of property|\btpa\b|mortgage|\blease\b|gift deed|sale deed|lis pendens|part performance|"
                                 r"actionable claim|easement"),
        ("Limitation / Registration", r"limitation act|\blimitation\b|condonation of delay|registration act|unregistered|adverse possession"),
    ]),
    ("Personal, local and constitutional", 8, [
        ("Hindu / Muslim law", r"hindu marriage act|hindu succession|hindu adoption|hindu minority|coparcen|joint family|"
                               r"muslim law|mohammedan|talaq|\bmehr\b|\bdower\b|waqf|wakf|maintenance|divorce|child custody|custody of (?:the )?(?:child|minor)|guardians? and wards"),
        ("Rajasthan laws", r"rajasthan rent control|rajasthan land revenue|rajasthan tenancy|rajasthan court fees|rajasthan excise|"
                           r"rajasthan high court|rajasthan"),
        ("Constitution", r"constitution|article \d+[a-z]?\b|fundamental right|writ petition|writ of|basic structure|"
                         r"judicial service|district judge|separation of powers|federalism|collegium|appointment of judges|judicial appointment|equality before law|right to life|personal liberty"),
    ]),
    ("Special criminal Acts", 8, [
        ("NI Act s.138", r"negotiable instruments|section 138|cheque dishonour|cheque bounce"),
        ("Probation / JJ / POCSO / DV", r"probation of offenders|juvenile justice|\bpocso\b|protection of children from sexual|domestic violence"),
        ("NDPS / SC-ST / PC / others", r"\bndps\b|narcotic|sc/st|scheduled castes and (?:the )?scheduled tribes|prevention of corruption|"
                                       r"excise act|arms act|dowry prohibition|information technology act|\bit act\b"),
    ]),
    ("Interpretation", 4, [
        ("Interpretation of statutes", r"interpretation of statute|literal rule|purposive|general clauses act|ejusdem|noscitur|"
                                       r"harmonious construction|mischief rule|retrospective|retroactive"),
    ]),
)
LANDMARK_RE = re.compile(
    r"constitution bench|five[- ]judge|seven[- ]judge|nine[- ]judge|larger bench|overrul|reference to (?:a )?larger|"
    r"landmark|settles? the law|clarif(?:ies|ied) the law|laid down guidelines|issues? guidelines|guidelines", re.I)
NO_MATCH_PENALTY = 10


def _text(item: dict) -> str:
    parts = [item.get("title"), item.get("summary"), " ".join(item.get("bullets") or []), item.get("case_name"),
             item.get("ratio"), " ".join(item.get("tags") or []), (item.get("body") or "")[:1500]]
    return " ".join(p for p in parts if p).lower()


def match(item: dict) -> dict:
    """{'clusters': [...], 'acts': [...], 'landmark': bool, 'boost': int} for one story."""
    text = _text(item)
    clusters, acts, weights = [], [], []
    for cluster, weight, entries in CLUSTERS:
        hit = [label for label, rx in entries if re.search(rx, text)]
        if hit:
            clusters.append(cluster)
            acts += hit
            weights.append(weight)
    landmark = bool(LANDMARK_RE.search(text))
    if weights:
        boost = max(weights) + 2 * min(len(weights) - 1, 2)
    else:
        boost = -NO_MATCH_PENALTY
    if landmark:
        boost += 8
    return {"clusters": clusters, "acts": acts, "landmark": landmark, "boost": boost}
