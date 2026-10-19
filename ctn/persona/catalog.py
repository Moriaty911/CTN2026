"""Versioned, local persona content. This module never creates platform accounts."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re

from .models import Persona

GROUP_ALLOCATION = {
    "transport_logistics":9, "retail_food":9, "tourism_leisure":9,
    "education_sport":9, "healthcare_support":9, "technology_digital":9,
    "agriculture_trades":9, "office_community":9, "medical_public_health":2,
    "law_policy":2, "independent_commentary":4,
}
DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "config/personas/yes.v1.json"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate persona JSON key")
        result[key] = value
    return result


STORY_FIELDS = {"age","gender","look","grew_up","life","covid","vaccine","vote_2025","interests","follows",
                "posting","personal_reasons","still_unsure","gossip"}


def validate_story(story):
    """Optional backstory: voice and consistency data only, never evidence."""
    if type(story) is not dict or set(story) != STORY_FIELDS:
        raise ValueError("invalid persona story fields")
    if type(story["age"]) is not int or not 18 <= story["age"] <= 90 or type(story["gossip"]) is not bool:
        raise ValueError("invalid persona story age or trait")
    if story["vote_2025"] not in ("Hawthorne","Castillo","did not vote"):
        raise ValueError("invalid 2025 vote")
    for key in ("gender","look","grew_up","life","covid","vaccine","still_unsure"):
        if not isinstance(story[key],str) or not story[key].strip():raise ValueError("missing persona story "+key)
    for key in ("interests","follows","personal_reasons"):
        if type(story[key]) is not list or not story[key] or any(type(s) is not str or not s.strip() for s in story[key]):
            raise ValueError("missing persona story "+key)
    if type(story["posting"]) is not dict or set(story["posting"]) != {"when","habits"}:
        raise ValueError("invalid persona posting habits")


def load_catalog(path=None):
    path = Path(path or os.environ.get("CTN_PERSONA_FILE") or DEFAULT_CATALOG)
    data = json.loads(path.read_text(), object_pairs_hook=_unique_keys)
    if type(data) is not dict or set(data) != {"schema_version","catalog_version","side","personas"}:
        raise ValueError("invalid persona catalog fields")
    if data["schema_version"] != "ctn-personas.v1" or data["side"] != "yes":
        raise ValueError("expected a YES persona catalog")
    if not isinstance(data["catalog_version"],str) or not data["catalog_version"].strip():
        raise ValueError("missing catalog version")
    rows = data["personas"]
    if not isinstance(rows,list) or len(rows) != 80:
        raise ValueError("persona catalog must contain exactly 80 profiles")
    fields = {"persona_id","version","side","occupation_group","candidate_username","name",
              "identity","stance","voice","topic_tags","knowledge_boundaries","example_posts"}
    ids, names, handles, examples = set(), set(), set(), set()
    for row in rows:
        if type(row) is not dict or not (fields <= set(row) <= fields | {"story"}):
            raise ValueError("invalid persona profile fields")
        if "story" in row:validate_story(row["story"])
        for key, seen in (("persona_id",ids),("name",names),("candidate_username",handles)):
            value = row[key]
            if not isinstance(value,str) or not value.strip() or value.casefold() in seen:
                raise ValueError("missing or duplicate persona " + key)
            seen.add(value.casefold())
        if not re.fullmatch(r"yes_\d{3}",row["persona_id"]) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}",row["candidate_username"]):
            raise ValueError("invalid persona identifier")
        if type(row["version"]) is not int or row["version"] < 1 or row["side"] != "yes":
            raise ValueError("invalid persona version or side")
        if row["occupation_group"] not in GROUP_ALLOCATION:
            raise ValueError("unknown occupation group")
        identity=row["identity"]
        if type(identity) is not dict or set(identity) != {"occupation","home","household","background","fixed_experiences"}:
            raise ValueError("invalid identity fields")
        if any(not isinstance(identity[k],str) or not identity[k].strip() for k in ("occupation","home","household","background")):
            raise ValueError("missing fixed identity")
        stance=row["stance"]
        if type(stance) is not dict or set(stance) != {"position","main_reasons","open_questions"} or stance["position"] != "support_public_access_vaccination_proof":
            raise ValueError("persona does not support YES")
        voice=row["voice"]
        if type(voice) is not dict or set(voice) != {"style","instructions"} or any(not isinstance(v,str) or not v.strip() for v in voice.values()):
            raise ValueError("invalid persona voice")
        for sequence in (identity["fixed_experiences"],stance["main_reasons"],stance["open_questions"],row["topic_tags"],row["knowledge_boundaries"]):
            if type(sequence) is not list or not sequence or any(type(s) is not str or not s.strip() for s in sequence):
                raise ValueError("missing persona content")
        posts=row["example_posts"]
        if type(posts) is not list or len(posts)<4 or any(type(p) is not dict or set(p)!={"kind","text"} for p in posts):
            raise ValueError("invalid sample posts")
        if Counter(p["kind"] for p in posts)["daily"]<2 or Counter(p["kind"] for p in posts)["referendum"]<2:
            raise ValueError("need two daily and two referendum examples")
        for post in posts:
            if post['kind'] not in ('daily','referendum'):raise ValueError('invalid sample kind')
            text=post["text"]
            if type(text) is not str or not text.strip() or len(text)>280 or text.casefold() in examples:
                raise ValueError("invalid or duplicate sample post")
            if not text.isascii():
                raise ValueError("persona samples must use plain English text")
            examples.add(text.casefold())
    if Counter(r["occupation_group"] for r in rows) != Counter(GROUP_ALLOCATION):
        raise ValueError("persona occupation allocation differs from agreed 80-person roster")
    return data


def persona_from_record(row):
    return Persona(persona_id=row["persona_id"], version=row["version"],
        occupation_group=row["occupation_group"], archetype=row["occupation_group"],
        name=row["name"], bio=row["identity"]["background"],
        tone_instruction=row["voice"]["instructions"], assigned_side=row["side"],
        topic_tags=tuple(row["topic_tags"]), profile_json=canonical_json(row))


def render_preview(data):
    lines=["# 80 YES personas", "", "Generated from the JSON catalog; edit the JSON, not this preview.", ""]
    for row in data["personas"]:
        lines += [f"## {row['persona_id']} — {row['name']}", "",
            f"Group: {row['occupation_group']} · Voice: {row['voice']['style']}", "",
            f"Candidate handle: {row['candidate_username']} · Profile version: {row['version']}", "",
            f"Home: {row['identity']['home']} · Household: {row['identity']['household']}", "",
            row["identity"]["background"], "", "YES reasons:"]
        lines += ["- "+s for s in row["stance"]["main_reasons"]]
        lines += ["", "Open implementation questions:"]+["- "+s for s in row["stance"]["open_questions"]]
        lines += ["", "Fixed experiences:"]+["- "+s for s in row["identity"]["fixed_experiences"]]
        lines += ["", row["voice"]["instructions"], "", "Examples:"]
        lines += [f"- [{p['kind']}] {p['text']}" for p in row["example_posts"]]
        lines += ['', 'Knowledge boundaries:']+['- '+s for s in row['knowledge_boundaries']]
        story=row.get('story')
        if story:
            lines += ['', f"Story: {story['age']}, {story['gender']}; 2025 vote: {story['vote_2025']}; gossip: {'yes' if story['gossip'] else 'no'}",
                      '', story['grew_up']+' '+story['life'], '', 'COVID: '+story['covid'], '', 'Vaccine: '+story['vaccine'],
                      '', 'Personal reasons:']+['- '+s for s in story['personal_reasons']]+['', 'Still unsure: '+story['still_unsure']]
        lines += [""]
    return "\n".join(lines)+"\n"


def render_profiles(data, daily_posts=None):
    """Readable profile sheet in the style of the organisers' citizen profiles."""
    daily_posts=daily_posts or {}
    groups={}
    for row in data["personas"]:groups.setdefault(row["occupation_group"],[]).append(row)
    lines=["# 80 YES personas: profiles", "",
           f"Catalog `{data['catalog_version']}`. Generated from `config/personas/yes.v1.json` and "
           "`config/personas/daily-posts.v1.json`; edit the JSON, not this file. Backstories are fictional voice "
           "and consistency data, never evidence.", "", "## Overview", "",
           "| ID | Name | Age | Job | Home | 2025 vote | Gossip |", "|---|---|---|---|---|---|---|"]
    for row in data["personas"]:
        s=row.get("story") or {}
        lines.append(f"| {row['persona_id']} | {row['name']} | {s.get('age','')} | {row['identity']['occupation']} | {row['identity']['home']} | {s.get('vote_2025','')} | {'yes' if s.get('gossip') else ''} |")
    lines.append("")
    for group,rows in groups.items():
        lines += [f"## {group.replace('_',' ').title()}", ""]
        for row in rows:
            s=row.get("story") or {};ident=row["identity"]
            lines += [f"### {row['persona_id']} · {row['name']}", "",
                      f"@{row['candidate_username']} · {ident['occupation']} · {ident['home']}", "",
                      "| | |", "|---|---|",
                      f"| **Age / gender** | {s.get('age','')} / {s.get('gender','')} |",
                      f"| **Looks** | {s.get('look','')} |",
                      f"| **Household** | {ident['household']} |",
                      f"| **Grew up** | {s.get('grew_up','')} |",
                      f"| **Life now** | {s.get('life','')} |",
                      f"| **COVID-19** | {s.get('covid','')} |",
                      f"| **H2D2 vaccine** | {s.get('vaccine','')} |",
                      f"| **2025 presidential vote** | {s.get('vote_2025','')} |",
                      f"| **Interests** | {', '.join(s.get('interests',[]))} |",
                      f"| **Follows** | {', '.join(s.get('follows',[]))} |",
                      f"| **Posts when** | {(s.get('posting') or {}).get('when','')} |",
                      f"| **Posting style** | {row['voice']['style']}: {(s.get('posting') or {}).get('habits','')} {row['voice']['instructions']} |",
                      f"| **Passes on rumours** | {'yes' if s.get('gossip') else 'no'} |", "",
                      "**Why YES**", ""]
            lines += ["- "+t for t in row["stance"]["main_reasons"]+s.get("personal_reasons",[])]
            lines += ["", "**Still unsure about**", "", "- "+s.get("still_unsure",""), "",
                      "**Everyday posts (initialise)**", ""]
            lines += ["- "+t for t in list(daily_posts.get(row["persona_id"],[]))+[p["text"] for p in row["example_posts"] if p["kind"]=="daily"]]
            lines += ["", "**Referendum examples**", ""]+["- "+p["text"] for p in row["example_posts"] if p["kind"]=="referendum"]+[""]
    return "\n".join(lines)+"\n"
