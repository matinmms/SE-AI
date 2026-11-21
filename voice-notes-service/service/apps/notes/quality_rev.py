"""Note quality revision 2 ("v4.1" for the teaching pipeline, "v2.1" for the lecturer's-words one).

Admin trial first: only staff accounts listed in NOTE_QUALITY_V21_PHONE_NUMBERS get it; every other
account keeps the plain v4 / v2 behaviour untouched. What the revision changes, from the audit in
docs/product/note-quality-v2-v4-plan.md (Phase 1):

* the note never talks about the transcript ("روشن نیست"، "از گفتار روشن نیست"),
* exam remarks of one unit become one box (not three in a row) and repeats are dropped,
* the summary box has one line per section of the whole lecture, not the first few units,
* numbers and names that look like mishearings are left out, a teaching story is kept as an
  example, and a worked example whose board numbers are lost is rebuilt and marked as TesaNote's.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

from services.auth.phone import InvalidPhoneNumberError, normalize_iranian_mobile_number

QUALITY_REV = "2"
QUALITY_LABEL_V4 = "v4.1"
QUALITY_LABEL_V2 = "v2.1"
# Revision 3 ("v4.2" for the teaching pipeline, "v2.2" for the lecturer's-words one): everything in
# revision 2 plus the changes from the QA reviews (docs/product/note-qa-and-v42-plan.md, sections 8 and 15).
QUALITY_REV_V42 = "3"
QUALITY_LABEL_V42 = "v4.2"
QUALITY_LABEL_V22 = "v2.2"


def _staff_phone_listed(user: Any, setting_name: str) -> bool:
    if not getattr(user, "is_staff", False):
        return False
    phone = str(getattr(user, "phone_number", "") or "").strip()
    if not phone:
        return False
    try:
        phone = normalize_iranian_mobile_number(phone)
    except InvalidPhoneNumberError:
        return False
    for raw in getattr(settings, setting_name, []) or []:
        try:
            if normalize_iranian_mobile_number(str(raw)) == phone:
                return True
        except InvalidPhoneNumberError:
            continue
    return False


def should_use_quality_rev(*, user: Any) -> bool:
    """Staff accounts listed in NOTE_QUALITY_V21_PHONE_NUMBERS get the revision."""
    if not getattr(settings, "NOTE_QUALITY_V21_ENABLED", False) or user is None:
        return False
    return _staff_phone_listed(user, "NOTE_QUALITY_V21_PHONE_NUMBERS")


def should_use_quality_rev_v42(*, user: Any, teaching_v4: bool = True) -> bool:
    """Revision 3 (v4.2 main note, v2.2 comparison note).

    Admin trial first: the staff accounts in NOTE_QUALITY_V42_PHONE_NUMBERS get both notes. Everyone else
    keeps v4.1 until NOTE_QUALITY_V42_FOR_ALL is switched on, which then applies to the v4 main note only.
    NOTE_QUALITY_V42_ENABLED (and the revision's kill switch NOTE_QUALITY_V21_ENABLED) turn it off.
    """
    if user is None or not getattr(settings, "NOTE_QUALITY_V42_ENABLED", False):
        return False
    if not getattr(settings, "NOTE_QUALITY_V21_ENABLED", False):
        return False
    if _staff_phone_listed(user, "NOTE_QUALITY_V42_PHONE_NUMBERS"):
        return True
    return bool(teaching_v4 and getattr(settings, "NOTE_QUALITY_V42_FOR_ALL", False))


def should_use_quality_rev_v41(*, user: Any, teaching_v4: bool) -> bool:
    """v4.1 for every user's main note: the v4 teaching pipeline with the quality revision.

    Only a note that is really written by the v4 pipeline gets it; a user still on an older
    pipeline keeps that pipeline untouched. NOTE_QUALITY_V21_ENABLED is the kill switch for the
    whole revision and NOTE_QUALITY_V41_FOR_ALL for the all-users part.
    """
    if user is None or not teaching_v4:
        return False
    if not getattr(settings, "NOTE_QUALITY_V21_ENABLED", False):
        return False
    return bool(getattr(settings, "NOTE_QUALITY_V41_FOR_ALL", False))


def quality_rev_on(context: dict[str, Any] | None) -> bool:
    """Revision 2 or 3: revision 3 includes everything revision 2 does."""
    return bool(context) and str(context.get("quality_rev", "") or "") in {QUALITY_REV, QUALITY_REV_V42}


def quality_rev_v42(context: dict[str, Any] | None) -> bool:
    return bool(context) and str(context.get("quality_rev", "") or "") == QUALITY_REV_V42


# --------------------------------------------------------------------------------------
# Prompt rules
# --------------------------------------------------------------------------------------

QUALITY_REV_PROMPT = (
    "NOTE QUALITY RULES (they override anything above that conflicts with them). "
    "(1) NEVER TALK ABOUT THE SOURCE: the student must never read that something was unclear, "
    "not stated, cut off, inaudible, or lost in the recording or transcript -- no sentence such as "
    "\"it is not clear from the speech\" or \"the exact numbers are not stated\". When a detail "
    "cannot be recovered, write the point without it, or rebuild it as described in (3). "
    "(2) NUMBERS AND NAMES: keep a number, id, file name, score or foreign phrase only when it "
    "makes sense in its context and agrees with the rest of the lecture. A made-up looking id, a "
    "score that contradicts the others, or a sentence in another language that is not a real "
    "example is almost always a mishearing: keep the point, leave the item out. Never copy a "
    "garbled number just because it is in the transcript, and never rescale or simplify the "
    "lecturer's numbers (30, 80, 90 stay 30, 80, 90; never 3, 8, 9). "
    "(3) WORKED EXAMPLES: when the lecturer works an example with numbers but the numbers cannot "
    "be recovered, rebuild a small example with the same structure and clearly consistent "
    "values, work every step to the result, and set example_ai to true. Never leave a worked "
    "example as a bare list of steps and never say its numbers are missing. When the lecturer "
    "gives his own example (his array, his two strings), use it with his values first and "
    "never label it rebuilt; add at most one rebuilt example per topic, only when his cannot be "
    "recovered. "
    "(4) TEACHING STORIES: a story, anecdote, paper or case the lecturer tells to make a course "
    "point is course material, not an aside. Keep it as ONE unit, written as a complete example "
    "in the example field with its lesson in one closing sentence, never split into fragments, "
    "never put in an off-topic unit and never marked as outside the exam unless the lecturer said "
    "so. Only logistics, greetings and jokes that teach nothing about the course are off-topic. "
    "(5) EXAM REMARKS: whatever the lecturer says he will ask or wants in the exam is always "
    "kept, with its meaning exact, as an [[exam:important]] remark; one marker per distinct exam "
    "point, never cut one remark into several markers and never repeat an exam point already "
    "stated in an earlier unit. "
    "(6) FIGURES NEED A MEANING: write a figure only together with what it measures, in the "
    "lecturer's own words. Never add a unit or noun he did not say (parameters, tokens, percent, "
    "seconds, words) and never turn an unclear figure into a statement about size or accuracy. "
    "Never write a sentence that only lists figures that \"were mentioned\" or says what they "
    "\"meant\"; if the sentence around a figure is unclear, leave the figure out. "
    "(7) GARBLED STRETCHES: speech-to-text sometimes turns a stretch into near-words. A foreign "
    "word or name that appears once, without explanation, in such a stretch is a mishearing: do "
    "not make it a term, an example or a claim. Never write a sentence that contradicts itself "
    "(for example \"built X and compared it with the existing X\"); choose the reading that fits "
    "the rest of the lecture or leave the point out. "
    "(8) SCOPE OF A PART: the intended scope describes the whole topic. When a scope item is not "
    "in the excerpts you were given, leave it out silently -- another part of the note covers "
    "it; never write that it was not explained or not covered. "
    "(9) ASSIGNMENTS AND QUIZZES: keep every concrete item the lecturer gave -- the exact words, "
    "sentences, counts and deadlines (for example which word must appear in which role). A list "
    "of roles without the words they belong to is useless to the student. "
    "(12) EACH CONCEPT ONCE: a concept, definition, rule or step is written exactly once in the "
    "note, in the place where it belongs. note, list_items, key_idea and ai_added must ADD "
    "something the body does not say (a pitfall, a condition, a consequence, a new case); they "
    "never restate the body, a list never repeats the paragraph above it, and a later part "
    "that returns to something already written adds only what is new or leaves it out. "
    "(10) CODE: when the lecturer walks through an implementation -- arrays and their initial "
    "values, loops, conditions, updates, what is returned or printed -- even dictated in Persian "
    "and partly garbled, write it as ONE code_blocks entry (pseudocode unless he named a "
    "language) that does exactly the steps he described, in his order, with his variable "
    "names; it is the lecturer's content, not an addition. Never spread an implementation over "
    "formula boxes: a formula is a definition or a recurrence, never a single condition "
    "(A[j] < A[i]), an assignment (max index = i), a test (P[i] > 0) or a list of elements. "
    "(11) CLASSROOM TALK: questions to the class (\"any questions?\", \"shall we move on?\"), "
    "waiting for answers and calling on students are left out; a remark about what the course "
    "covers (for example \"in compression we only do Huffman\") goes into an off-topic unit. "
)


QUALITY_REV_V42_PROMPT = (
    "REVISION 3 RULES (they add to the rules above and win over any conflict). "
    "(13) QUIZZES, HOMEWORK AND IMPORTANCE: whenever the lecturer announces a quiz, an assignment, a "
    "project, a deadline or an exam date, or says that a part is important or \"half the course\", keep it "
    "as its own remark in his exact words -- an [[exam:important]] remark for a quiz, exam or importance, "
    "and the concrete items for homework. Never drop it as class talk, even when it comes at the end of "
    "the lecture. An exam date, an exam rule (open book, one page) and a review remark (\"this repeats\", "
    "\"the questions come from this slide\", \"this will come\") are exam remarks too: never file them "
    "in an off-topic unit. "
    "(14) NOTHING THE LECTURER DID NOT SAY: ai_added is always an empty array. Never add a definition, "
    "case, formula, condition, name, court case, interface detail (colours, icons, buttons) or example "
    "figure he did not give. When the recording ends before he reaches a point, the note ends there too: "
    "never complete a topic from general knowledge. Never write a sentence about what the lecture did or "
    "did not cover. When the numbers of his example are lost, describe its structure in words without "
    "figures; never rebuild an example and never set example_ai to true. "
    "(15) GARBLED STRETCHES: a passage that is mostly near-words yields no figures, dates, interface facts "
    "or names; write only what is certain and leave the rest out. When a technical term is clearly a "
    "mishearing of a known one (a recognizer spelling of an English term), write the correct term, never "
    "the misspelling. "
    "(16) NO LABELS IN TEXT: never write the words important, reconstructed or ai, nor a label such as "
    "\"reconstructed example:\", inside any text field; never end a list with a heading or a section "
    "title; never leave a label without its content. "
    "(17) ONE IDEA ONCE: key_idea is a one-line takeaway used only in the closing summary, so the body, "
    "note and list never repeat it; leave note empty when it would restate the body. "
    "(19) NO UNEXPLAINED ABBREVIATIONS: a Latin abbreviation or short capital-letter token (such as a "
    "three-letter code) that the lecturer does not clearly define, or that does not fit the sentence, is a "
    "recognizer mishearing: leave it out; never put it in parentheses as if it were a term. "
    "(20) EVERY NAMED ITEM AND LAB FACT: when the lecturer names the kinds of something (types of enzyme, "
    "kinds of ends, buffers, ladders, polymerases), each kind he names is written with what he says about "
    "it; a practical remark (\"in the lab we use heat instead of the enzyme\") is kept next to the item it "
    "concerns, never dropped as an aside. A remark belongs to the topic it was said about, even when it "
    "was said later. "
    "(21) NO EVIDENCE TALK AND NO SOLVED EXTRAS: never write a sentence about the evidence, the speech "
    "or what \"can be confirmed\"; never give a worked example a separate solution or any text of "
    "your own; never use [[added:...]] -- a statement you doubt is written as the lecturer said it or "
    "left out, never corrected inline. "
    "(22) DICTATED PROCEDURES: when the lecturer dictates a method, a procedure or the steps of "
    "an experiment (\"first ..., then ...\"), write every step he gave, in his order, as one ordered list "
    "in the unit; never replace it with a remark that it was described, and never keep only its "
    "problems or exceptions. A list has at least two items; a single item is a sentence of the body. "
    "(24) WORDS OF ANOTHER LANGUAGE STAY EXACTLY AS SAID, and this rule wins over (2), (7) and (19): when the "
    "lecturer says a word, a term, a name, an example sentence, a verse, a quotation or a whole phrase in a "
    "language other than the lecture's own (Arabic, English, French, German, Latin or any other), the note "
    "keeps it in that language and its own script, exactly as he said it: Arabic in Arabic letters (with the "
    "vowel marks the transcript gives), English in Latin letters with its usual casing. Never translate it "
    "into the note's language in its place, never write it in Persian letters, never shorten it, never "
    "correct it to what you think it should be, and never leave it out because it appears only once or "
    "looks unfamiliar. A translation or explanation goes beside it only when the lecturer gave one. In a "
    "language, literature, religion, law or philosophy lecture the Arabic and foreign examples ARE the "
    "content: each one he analyses is written in full, in his order, together with what he says about it. "
    "The only foreign words to leave out are clear garbage (a string that is no word of any language and "
    "fits nowhere), never a real term. A term the transcript spelled phonetically in Persian letters is "
    "restored to its real spelling when the lecturer clearly said a foreign word; when you cannot tell "
    "which word it was, keep the transcript wording. "
)

QUALITY_REV_V42_V2_PROMPT = (
    "(18) LECTURER'S WORDS ONLY: every sentence is the lecturer's own, in his words and order; add no "
    "sentence of yours, no comment and no bridge. A point he repeated is written once, in his clearest "
    "wording; a definition, an example or a step already written is never written again in other words. "
    "(23) SAME LECTURE, SAME MANNER: the note must teach the way the lecturer taught. Keep the order of "
    "his topics and of the points inside each topic; open a topic the way he opened it (his question, his "
    "problem, his reminder of the last session); keep his own examples with his own values, his analogies "
    "and comparisons, his warnings (\"be careful\", \"a common mistake\"), his own words for a term, and "
    "every remark he made about the exam, homework or what to memorise or not to memorise, in the unit "
    "where he said it. Do not reorder, merge or rename his topics to look tidier, do not move a remark to "
    "another topic, and do not add a recap, a conclusion or a closing summary he did not give. Every "
    "foreign word, term, example and quotation he used is kept in the language he used it in (rule 24), "
    "even when it breaks the flow of the Persian sentence. "
)


# The two notes have different visions. v2.2 is the lecturer's own account, so it takes rules 14 and 21 as
# written above (no additions of its own, ever). v4.2 is the note where TesaNote teaches in its own way:
# it may add what a good teacher adds, in the marked places (ai_added and example_ai) and under a budget,
# so rules 14 and 21 are replaced by these two for it.
_RULE_14_V4 = (
    "(14) TESANOTE TEACHES IN ITS OWN WAY, BUT NEVER PUTS WORDS IN THE LECTURER'S MOUTH: the lecturer's "
    "content is written first and in full. On top of it you may add what a good teacher adds -- a clearer "
    "definition of something he left implicit, a small worked example that makes his point concrete, a "
    "pitfall, a condition or a consequence -- ONLY in ai_added (one short addition per unit at most, in "
    "plain language for a student who finds the subject hard). An addition must be correct, agree with "
    "everything he said and add something the body does not say; it never contradicts him, never "
    "completes a topic he did not reach, and never invents a fact about the lecture itself (a name, a "
    "court case, an interface detail, a figure he supposedly gave). When the numbers of his worked "
    "example are lost, rebuild a small example with the same structure and consistent numbers, work it "
    "to the result and set example_ai to true; when his own numbers are clear, use his and never set "
    "example_ai. Never write a sentence about what the lecture did or did not cover. "
)
_RULE_21_V4 = (
    "(21) NO EVIDENCE TALK: never write a sentence about the evidence, the speech or what \"can be "
    "confirmed\"; an addition or a rebuilt example is written as plain teaching, never as a remark "
    "about how it came about (the flags ai_added and example_ai mark it for the reader); never use "
    "[[added:...]] -- a statement you doubt is written as the lecturer said it or left out, never "
    "corrected inline. "
)


def _swap_rule(text: str, start: str, end: str, replacement: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + replacement + text[j:]


QUALITY_REV_V42_PROMPT_V4 = _swap_rule(
    _swap_rule(QUALITY_REV_V42_PROMPT, "(14) NOTHING THE LECTURER", "(15) GARBLED", _RULE_14_V4),
    "(21) NO EVIDENCE TALK", "(22) DICTATED", _RULE_21_V4,
)


def quality_rev_prompt(context: dict[str, Any] | None) -> str:
    if not quality_rev_on(context):
        return ""
    if quality_rev_v42(context):
        if str((context or {}).get("note_pedagogy", "") or "") == "v4":
            extra = QUALITY_REV_V42_PROMPT_V4
        else:
            extra = QUALITY_REV_V42_PROMPT + QUALITY_REV_V42_V2_PROMPT
        return QUALITY_REV_PROMPT + extra
    return QUALITY_REV_PROMPT


_STORY_REPLACEMENTS = (
    (
        "jokes and personal stories",
        "jokes and personal asides that teach nothing about the course (a story the lecturer "
        "tells to make a course point is course material, never noise)",
    ),
    (
        "jokes, personal stories, class",
        "jokes and personal asides that teach nothing, class",
    ),
)


def apply_story_rule(prompt: str, context: dict[str, Any] | None) -> str:
    """Planner/writer prompts call every personal story noise; under the revision a teaching story is not."""
    if not quality_rev_on(context):
        return prompt
    for old, new in _STORY_REPLACEMENTS:
        prompt = prompt.replace(old, new)
    return prompt


def add_quality_rules_to_writer(prompt: str, context: dict[str, Any] | None) -> str:
    """Insert the rules right before the writer's OUTPUT section so the cached prefix stays per-lecture."""
    if not quality_rev_on(context):
        return prompt
    rules = quality_rev_prompt(context)
    marker = "OUTPUT: return JSON only"
    if marker in prompt:
        return prompt.replace(marker, f"{rules}{marker}", 1)
    return f"{prompt}{rules}"


# --------------------------------------------------------------------------------------
# Meta-commentary scrub
# --------------------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟۔])\s+")

# A sentence that points at the recording or transcript...
_SOURCE_REFERENCE = re.compile(
    r"گفتار|ضبط|صوت|ترنسکریپت|ترانسکریپت|رونوشت|متن پیاده|پیاده[‌ ]?سازی|شنیده|"
    r"\b(?:transcript|recording|audio|speech|lecture said)\b",
    re.IGNORECASE,
)
# ...and says something is unclear, missing or unrecoverable.
_UNCLEAR_CUE = re.compile(
    r"روشن\s+نیست|مشخص\s+نیست|واضح\s+نیست|روشن\s+نمی[‌ ]?شود|مشخص\s+نمی[‌ ]?شود|"
    r"روشن\s+نمی[‌ ]?کند|بیان\s+نشده|ذکر\s+نشده|قابل[‌ ]?(?:بازیابی|تشخیص|فهم|شنیدن)|"
    r"ناخوانا|نامفهوم|مبهم|"
    r"\b(?:unclear|not clear|not stated|cannot be recovered|inaudible|not specified)\b",
    re.IGNORECASE,
)
# Statements that are meta on their own: «قابل‌بازیابی» and «بدون رابطهٔ قابل…» in the transcript sense,
# «عبارت … روشن نمی‌کند این عدد …».
_ALWAYS_META = re.compile(
    r"قابل[‌ ]?بازیابی|روشن\s+نمی[‌ ]?کند\s+(?:که\s+)?این\s+(?:عدد|مقدار|رقم)|"
    r"(?:عدد|اعداد|مقدار|مقادیر)\s+[^.!?؟]{0,40}(?:بدون|بی)\s+(?:رابطه|ارتباط)|"
    # «… در این گفتار توضیح داده نشده‌اند», «در این بخش جملهٔ مشخصی … ارائه نشده است»
    r"(?:توضیح\s+داده|ارائه|مطرح|بیان|ذکر|آورده|گفته)\s*[‌ ]?نشده|"
    r"^\s*در\s+(?:این\s+)?گفتار|در\s+این\s+گفتار|"
    r"نمی[‌ ]?توان\s+(?:اعلام|تعیین)\s+کرد|"
    r"از\s+[^.!?؟]{0,30}بازسازی\s*[‌ ]?شده|"
    r"در\s+این\s+بخش\s+[^.!?؟]{0,60}تأکید\s+می[‌ ]?شود|"
    # Revision 3 (seen in v2.2 reviews): «… در متن گفتارِ ارائه‌شده تأیید نمی‌شود», «عددی … از شواهد موجود
    # به دست نمی‌آید», «بازسازی نمادین:», «این مثال به‌صورت کلی بازسازی شده است، چون در شواهد …».
    r"(?:متن|محتوای)\s+گفتار|تأیید\s+نمی[‌ ]?شود|تایید\s+نمی[‌ ]?شود|"
    r"(?:از|در)\s+شواهد\s+(?:موجود|ارائه)|شواهد\s+(?:موجود|ارائه)[‌ ]?شده|"
    r"(?:مثال|نمونه)\s*[^.!?؟]{0,30}بازسازی\s*[‌ ]?(?:شده|نمادین)|بازسازی\s+نمادین|"
    # Revision 3 (2026-11-18 run): a sentence about a number of the recording, not about the subject:
    # «عدد ۱۲۰۰۰ … معنای روشنی ندارد», «… به‌عنوان تاریخ یا بزرگ‌نمایی به کار نرفته است».
    r"(?:عدد|اعداد|مقدار|مقادیر|رقم)\s[^.!?؟]{0,60}(?:معنا|مفهوم)ی?\s+(?:روشن|مشخص|واضح)ی?\s+ندارد|"
    r"(?:عدد|اعداد|مقدار|مقادیر|رقم)\s[^.!?؟]{0,80}به\s+کار\s+نرفته",
)


def is_meta_sentence(sentence: str) -> bool:
    text = str(sentence or "")
    if not text.strip():
        return False
    if _ALWAYS_META.search(text):
        return True
    return bool(_SOURCE_REFERENCE.search(text) and _UNCLEAR_CUE.search(text))


def scrub_meta_text(text: str) -> str:
    """Drop sentences that talk about the transcript instead of teaching, paragraph by paragraph."""
    source = str(text or "")
    if not source.strip():
        return source
    paragraphs = re.split(r"(\n\s*\n)", source)
    cleaned: list[str] = []
    changed = False
    for paragraph in paragraphs:
        if not paragraph.strip():
            cleaned.append(paragraph)
            continue
        kept = [part for part in _SENTENCE_SPLIT.split(paragraph) if not is_meta_sentence(part)]
        if len(kept) != len(_SENTENCE_SPLIT.split(paragraph)):
            changed = True
        cleaned.append(" ".join(part.strip() for part in kept if part.strip()))
    if not changed:
        return source
    result = "".join(cleaned)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def scrub_unit(unit: dict[str, Any], supported: set[str] | None = None) -> dict[str, Any]:
    """A copy of a content unit with meta-commentary removed from every text field.

    With `supported` (the numbers the transcript supports), sentences that carry a decimal or a
    three-digit-plus figure the transcript never contained are dropped from the explanation text:
    that is how a mishearing turns into a stated fact. Rebuilt examples are exempt.
    """
    scrubbed = dict(unit)
    for key in ("body", "note", "example", "key_idea"):
        if isinstance(scrubbed.get(key), str):
            scrubbed[key] = scrub_meta_text(scrubbed[key])
    for key in ("list_items", "ai_added"):
        if isinstance(scrubbed.get(key), list):
            scrubbed[key] = [
                item
                for item in (scrub_meta_text(str(entry or "")) for entry in scrubbed[key])
                if str(item).strip()
            ]
    if supported is not None and not scrubbed.get("example_ai"):
        for key in ("body", "note"):
            if isinstance(scrubbed.get(key), str):
                scrubbed[key] = drop_unsupported_figures(scrubbed[key], supported)
        if isinstance(scrubbed.get("list_items"), list):
            kept = [item for item in scrubbed["list_items"] if not _risky_unsupported(str(item or ""), supported)]
            scrubbed["list_items"] = kept or scrubbed["list_items"]
    return scrubbed


# --------------------------------------------------------------------------------------
# Exam boxes
# --------------------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[\w؀-ۿ]+", str(text or "").lower()) if len(token) > 1}


def _similar(left: str, right: str, threshold: float = 0.7) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return False
    overlap = len(a & b) / min(len(a), len(b))
    return overlap >= threshold


def merge_exam_statements(
    statements: list[tuple[str, str]],
    earlier: list[str],
) -> list[tuple[str, str]]:
    """One box per status for a unit, without repeats of what the note already said.

    `earlier` holds every exam sentence already placed in the note; it is extended in place.
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for status, sentence in statements:
        text = str(sentence or "").strip().rstrip(":：").strip()
        if not text:
            continue
        if any(_similar(text, previous) for previous in earlier):
            continue
        earlier.append(text)
        if status not in grouped:
            grouped[status] = []
            order.append(status)
        grouped[status].append(text if text[-1] in ".!?؟۔" else f"{text}.")
    return [(status, " ".join(grouped[status])) for status in order]


# --------------------------------------------------------------------------------------
# Summary box that covers the whole note
# --------------------------------------------------------------------------------------

MAX_SUMMARY_ITEMS = 14


def _first_sentence(text: str) -> str:
    return _SENTENCE_SPLIT.split(str(text or "").strip(), maxsplit=1)[0].strip()


def full_coverage_takeaways(
    units: list[dict[str, Any]],
    *,
    concept_key: Any,
) -> list[str]:
    """One takeaway per section (topic) of the note, in reading order, spread over the whole lecture.

    The line is the section's key idea when it has one; otherwise the opening sentence of the
    section's unit that is most about the section title (the first sentence of the first unit
    was often a leftover from the topic before).
    """
    taught = [unit for unit in units if str(unit.get("content_type", "") or "").strip().lower() != "off-topic"]

    def group(by_subsection: bool) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
        order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for unit in taught:
            group_key = str(unit.get("topic_id", "") or "") or concept_key(
                str(unit.get("section_title", "") or unit.get("concept_title", "") or "")
            )
            if by_subsection:
                group_key = f"{group_key}|{concept_key(str(unit.get('concept_title', '') or ''))}"
            if not group_key:
                continue
            if group_key not in grouped:
                grouped[group_key] = []
                order.append(group_key)
            grouped[group_key].append(unit)
        return order, grouped

    order, grouped = group(by_subsection=False)
    by_subsection = False
    if len(order) < 3:
        # A note with one or two long sections (v2.1 algorithms: LIS, LCS) still gets a
        # summary: one line per subsection instead of per section.
        order, grouped = group(by_subsection=True)
        by_subsection = True
    if len(order) > MAX_SUMMARY_ITEMS:
        step = len(order) / MAX_SUMMARY_ITEMS
        order = [order[int(index * step)] for index in range(MAX_SUMMARY_ITEMS)]
    takeaways: list[str] = []
    for group_key in order:
        members = grouped[group_key]
        first = members[0]
        title_fields = ("concept_title", "section_title") if by_subsection else ("section_title", "concept_title")
        title = next((str(first.get(field, "") or "").strip() for field in title_fields if str(first.get(field, "") or "").strip()), "")
        key_idea = next((str(unit.get("key_idea", "") or "").strip() for unit in members if str(unit.get("key_idea", "") or "").strip()), "")
        sentence = key_idea
        if not sentence:
            title_tokens = _sentence_tokens(title)
            candidates = [_first_sentence(unit.get("body", "")) for unit in members]
            candidates = [candidate for candidate in candidates if candidate]
            if candidates:
                sentence = max(
                    enumerate(candidates),
                    key=lambda item: (len(_sentence_tokens(item[1]) & title_tokens), -item[0]),
                )[1]
        if title and sentence and concept_key(sentence) != concept_key(title):
            takeaways.append(f"{title}: {sentence}")
        elif title or sentence:
            takeaways.append(title or sentence)
    return [item for item in takeaways if item]


# --------------------------------------------------------------------------------------
# Numbers that the transcript does not contain
# --------------------------------------------------------------------------------------

_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUMBER = re.compile(r"\d+(?:[.,٫]\d+)?")


def _numbers(text: str) -> set[str]:
    normalized = str(text or "").translate(_DIGIT_TRANSLATION)
    return {match.group(0).replace("٫", ".").replace(",", ".") for match in _NUMBER.finditer(normalized)}


_WORD_UNITS = {
    "صفر": 0, "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6, "هفت": 7, "هشت": 8, "نه": 9,
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15, "پونزده": 15, "شانزده": 16,
    "هفده": 17, "هجده": 18, "نوزده": 19, "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70,
    "هشتاد": 80, "نود": 90, "صد": 100, "یکصد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500,
    "ششصد": 600, "هفتصد": 700, "هشتصد": 800, "نهصد": 900,
}
_WORD_SCALES = {"هزار": 1_000, "میلیون": 1_000_000, "میلیارد": 1_000_000_000}
_DECIMAL_WORD = re.compile(r"(\d+)\s*ممیز\s*(\d+)")
_SPACED_DECIMAL = re.compile(r"(\d+)\.\s+(\d+)")


def _spoken_numbers(text: str) -> set[str]:
    """Numbers the lecturer said in words ("نود و دو" -> 92); every partial reading counts."""
    found: set[str] = set()
    tokens = re.findall(r"[\w؀-ۿ]+", str(text or "").replace("\u200c", " "))
    total, current, active = 0, 0, False
    for token in [*tokens, ""]:
        if token in _WORD_UNITS:
            current += _WORD_UNITS[token]
            active = True
        elif token in _WORD_SCALES and active:
            total += (current or 1) * _WORD_SCALES[token]
            current = 0
        elif token == "و" and active:
            continue
        else:
            if active:
                found.add(str(total + current))
            total, current, active = 0, 0, False
    return found


def supported_numbers(transcript_text: str) -> set[str]:
    """Every number the transcript supports, written in digits or spoken in words."""
    text = str(transcript_text or "").translate(_DIGIT_TRANSLATION)
    text = _DECIMAL_WORD.sub(r"\1.\2", text)
    text = _SPACED_DECIMAL.sub(r"\1.\2", text)
    return _numbers(text) | _spoken_numbers(text)


def _risky_unsupported(sentence: str, supported: set[str]) -> bool:
    for number in _numbers(sentence):
        digits = number.replace(".", "")
        if number in supported:
            continue
        if "." in number or len(digits) >= 3:
            return True
    return False


def drop_unsupported_figures(text: str, supported: set[str]) -> str:
    """Remove sentences with a decimal / 3+ digit figure the transcript does not support.

    Never empties a text: when every sentence would go, the text is left as written.
    """
    sentences = [part for part in _SENTENCE_SPLIT.split(str(text or "")) if part.strip()]
    kept = [sentence for sentence in sentences if not _risky_unsupported(sentence, supported)]
    if not kept or len(kept) == len(sentences):
        return str(text or "")
    return " ".join(kept)


def numbers_missing_from_source(note_text: str, transcript_text: str) -> list[str]:
    """Numbers of two digits or more (or decimals) in the note that never occur in the transcript.

    A report only -- a rebuilt example legitimately has new numbers -- but a spread of them in
    non-example text is the sign of a mishearing kept as a fact.
    """
    source = supported_numbers(transcript_text)
    missing = sorted(
        number for number in _numbers(note_text) - source if len(number.replace(".", "")) >= 2
    )
    return missing[:60]


# --------------------------------------------------------------------------------------
# Phase 2 (v4.1): a note a student can learn from -- fewer, fuller sections
# --------------------------------------------------------------------------------------

MIN_UNIT_WORDS = 120
MAX_MERGED_UNIT_WORDS = 450
MAX_ADDITIONS_PER_TOPIC = 2


def _unit_text_fields(unit: dict[str, Any]) -> list[str]:
    parts = [str(unit.get(key, "") or "") for key in ("body", "note", "example")]
    parts.extend(str(item or "") for item in unit.get("list_items", []) or [])
    return [part for part in parts if part.strip()]


def unit_word_count(unit: dict[str, Any]) -> int:
    return sum(len(part.split()) for part in _unit_text_fields(unit))


def _topic_key(unit: dict[str, Any]) -> str:
    return str(unit.get("topic_id", "") or "") or str(unit.get("section_title", "") or "").strip()


def _is_off_topic(unit: dict[str, Any]) -> bool:
    return str(unit.get("content_type", "") or "").strip().lower() == "off-topic"


def _can_merge(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _is_off_topic(left) or _is_off_topic(right):
        return False
    if not _topic_key(left) or _topic_key(left) != _topic_key(right):
        return False
    # One list and one example per unit: two of either keep their own subsections.
    if left.get("list_items") and right.get("list_items"):
        return False
    if str(left.get("example", "") or "").strip() and str(right.get("example", "") or "").strip():
        return False
    return unit_word_count(left) + unit_word_count(right) <= MAX_MERGED_UNIT_WORDS


def _merge_two(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    merged["body"] = "\n\n".join(
        part for part in (str(left.get("body", "") or "").strip(), str(right.get("body", "") or "").strip()) if part
    )
    if not left.get("list_items") and right.get("list_items"):
        merged["list_items"] = list(right.get("list_items") or [])
        merged["list_ordered"] = bool(right.get("list_ordered"))
    merged["note"] = " ".join(
        part for part in (str(left.get("note", "") or "").strip(), str(right.get("note", "") or "").strip()) if part
    )
    if not str(left.get("example", "") or "").strip() and str(right.get("example", "") or "").strip():
        merged["example"] = right.get("example", "")
        merged["example_ai"] = bool(right.get("example_ai"))
    for key in ("formulas", "tables", "code_blocks", "ai_added"):
        merged[key] = [*(left.get(key) or []), *(right.get(key) or [])]
    merged["slide_pages"] = sorted({*(left.get("slide_pages") or []), *(right.get("slide_pages") or [])})[:3]
    merged["key_idea"] = str(left.get("key_idea", "") or "") or str(right.get("key_idea", "") or "")
    return merged


def merge_thin_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold units under ~120 words into a neighbour of the same topic.

    The audited v4 note averaged ~95 words per heading: a heading, a highlighted line, two
    sentences and a box, over and over. A thin unit joins the unit before it (or, when it opens
    its topic, the one after it), so a section reads as continuous teaching.
    """
    result: list[dict[str, Any]] = []
    for unit in units:
        previous = result[-1] if result else None
        if (
            previous is not None
            and (unit_word_count(unit) < MIN_UNIT_WORDS or unit_word_count(previous) < MIN_UNIT_WORDS)
            and _can_merge(previous, unit)
        ):
            result[-1] = _merge_two(previous, unit)
            continue
        result.append(dict(unit))
    return result


def budget_ai_additions(units: list[dict[str, Any]], *, topic_wide: bool = False) -> list[dict[str, Any]]:
    """At most one TesaNote addition per unit and two per topic, never one that repeats the unit.

    The audited note had 22 additions, nearly one per heading, many restating the body. With
    `topic_wide` (revision 3) an addition is also compared with every other unit of its topic,
    because the 2026-11-18 reviews found additions repeating the paragraph of the neighbouring unit.
    """
    topic_sentences: dict[str, list[str]] = {}
    if topic_wide:
        for unit in units:
            topic_sentences.setdefault(_topic_key(unit), []).extend(
                sentence
                for part in _unit_text_fields(unit)
                for sentence in _SENTENCE_SPLIT.split(part)
                if sentence.strip()
            )
    per_topic: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    for unit in units:
        updated = dict(unit)
        additions = [str(item or "").strip() for item in unit.get("ai_added", []) or [] if str(item or "").strip()]
        topic = _topic_key(unit)
        kept: list[str] = []
        if additions and per_topic.get(topic, 0) < MAX_ADDITIONS_PER_TOPIC:
            sentences = topic_sentences.get(topic) if topic_wide else None
            if sentences is None:
                sentences = [
                    sentence
                    for part in _unit_text_fields(unit)
                    for sentence in _SENTENCE_SPLIT.split(part)
                    if sentence.strip()
                ]
            for addition in additions:
                if any(_similar(addition, sentence, threshold=0.6) for sentence in sentences):
                    continue
                kept.append(addition)
                per_topic[topic] = per_topic.get(topic, 0) + 1
                break
        updated["ai_added"] = kept
        result.append(updated)
    return result


def section_intro_text(unit: dict[str, Any], *, label: str) -> str:
    concepts = [str(item or "").strip() for item in unit.get("topic_key_concepts", []) or [] if str(item or "").strip()]
    if len(concepts) < 2:
        return ""
    return f"{label}: {'، '.join(concepts[:5])}"


_V4_SHAPE_REPLACEMENTS = (
    (
        "WRITE LEAN. A long note is not a good note: the student cannot afford many "
        "pages. Every sentence must carry new information. Do not announce what you are "
        "about to say, do not recap what you just said, do not pad with generic "
        "background, do not repeat a point across body, note, example, list_items and "
        "key_idea, and do not add analogies or examples the lecturer did not need. Short "
        "sentences, one idea per paragraph, at most three sentences per paragraph. The "
        "budget in the user message is a size to land in; reach it with teaching, not with "
        "words. ",
        "WRITE COMPLETE, NOT LONG. The student must be able to read a topic as one continuous "
        "explanation, not a string of headings and one-line boxes. Every sentence must carry "
        "new information: do not announce what you are about to say, do not recap, do not pad "
        "with generic background and do not repeat a point across body, note, example, "
        "list_items and key_idea. But explain fully: a paragraph may run to about five "
        "sentences when the reasoning needs it, and the WHY, the steps and the worked example "
        "of a concept stay together. Inside every topic follow this order: (a) the problem "
        "this topic solves, in one or two sentences, at the start of the first unit; (b) the "
        "idea in plain words, then the precise definition; (c) how it works: ordered steps, "
        "the formula with its symbols; (d) a worked example with numbers; (e) the typical "
        "mistake or exam trap, in note. The budget in the user message is a size to land in; "
        "reach it with teaching, not with words. ",
    ),
    (
        "- units: an array of concept units, one per distinct concept, definition, rule, "
        "procedure, case or worked example, never folding two distinct concepts into one",
        "- units: an array of 2 to 4 units for this topic (each becomes a subsection), each "
        "one a concept taught completely -- its definition, procedure, worked example and "
        "trap belong in the SAME unit, never in units of their own; a unit under about 120 "
        "words is too thin and must be joined to its neighbour; never fold two unrelated "
        "concepts into one unit",
    ),
    (
        "- body: the teaching text, a few short paragraphs separated by a blank line",
        "- body: the teaching text, full paragraphs of up to about five sentences separated "
        "by a blank line",
    ),
    (
        "most core concepts gain from one well-chosen addition (the intuition, the "
        "missing step or the typical mistake) -- but never as filler and never to repeat "
        "body; [] when the lecturer's own teaching already passes the test",
        "at most ONE per unit and about one per topic, only for the why, the intuition, a "
        "missing step or the typical mistake, never a sentence that restates body, note or "
        "example; [] in most units",
    ),
    (
        "never restate the definition word for word, and \"\" for off-topic units",
        "never restate the definition word for word, and \"\" for off-topic units; it is "
        "shown at the END of the topic as the one line to remember, so write it as a "
        "takeaway, not as an introduction",
    ),
)

_PLANNER_SHAPE_REPLACEMENTS = (
    (
        "a full lecture hour typically has 8 to 16 topics, and a plan "
        "with only a handful almost certainly merged different subjects.",
        "a full lecture hour typically has 6 to 10 topics, each taught in 2 to 4 "
        "subsections, and a plan with fewer than 5 almost certainly merged different "
        "subjects. A point the lecturer made in a sentence or two, or in well under two "
        "minutes, is NOT a topic of its own: put it inside the neighbouring topic it serves "
        "(for example, choosing between models on validation data belongs to the topic on "
        "validation data).",
    ),
)


def _replace_all(prompt: str, replacements: tuple[tuple[str, str], ...]) -> str:
    for old, new in replacements:
        prompt = prompt.replace(old, new)
    return prompt


def apply_v4_shape_rules(prompt: str, context: dict[str, Any] | None) -> str:
    return _replace_all(prompt, _V4_SHAPE_REPLACEMENTS) if quality_rev_on(context) else prompt


PLANNER_TOPIC_RULE = (
    " NEVER MAKE A TOPIC OUT OF CLASS TALK OR UNCLEAR SPEECH: questions to the class, "
    "logistics, chatter and garbled stretches are not topics; their chunks belong to the "
    "neighbouring course topic, and no topic title may describe the recording (for example "
    "«بخش نامفهوم» or «گفت‌وگوهای کلاسی»). "
)


def apply_planner_shape_rules(prompt: str, context: dict[str, Any] | None) -> str:
    return _replace_all(prompt, _PLANNER_SHAPE_REPLACEMENTS) + PLANNER_TOPIC_RULE if quality_rev_on(context) else prompt


_META_TOPIC_TITLE = re.compile(
    r"نامفهوم|ناواضح|گفت[‌ ]?و[‌ ]?گوهای\s+کلاسی|گفت[‌ ]?و[‌ ]?گوی\s+کلاسی|پرسش\s+و\s+پاسخ\s+کلاسی|"
    r"\b(?:unclear|inaudible|class(?:room)?\s+(?:talk|chatter|discussion))\b",
    re.IGNORECASE,
)


def dissolve_meta_topics(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A planner topic about class talk or unclear speech hands its chunks to the nearest real topic.

    The algorithms note ended with «گفت‌وگوهای کلاسی و بخش نامفهوم», a section that repeated a
    point from section 1 under a heading about the recording.
    """
    meta = [topic for topic in topics if _META_TOPIC_TITLE.search(str(topic.get("title", "") or ""))]
    real = [dict(topic) for topic in topics if topic not in meta]
    if not meta or not real:
        return topics
    for topic in meta:
        for chunk in topic.get("transcript_chunk_indexes") or []:
            chunk = int(chunk)
            nearest = min(
                real,
                key=lambda candidate: min(
                    (abs(int(other) - chunk) for other in candidate.get("transcript_chunk_indexes") or []),
                    default=10**9,
                ),
            )
            indexes = sorted({*(int(i) for i in nearest.get("transcript_chunk_indexes") or []), chunk})
            nearest["transcript_chunk_indexes"] = indexes
    for position, topic in enumerate(real):
        topic["order"] = position
    return real


# --------------------------------------------------------------------------------------
# Phase 3 (v2.1): the lecturer's own words, in lecture order
# --------------------------------------------------------------------------------------

# A written passage under this share of the spoken words (fillers included) is a paraphrase or a
# summary, not the lecturer's sentences. The audited v2 note was 37% of the spoken words.
MIN_V2_WORD_RATIO = 0.5
MIN_V2_SOURCE_WORDS = 150
V2_RETRY_GAIN = 1.15


def is_leftover_topic(topic: dict[str, Any]) -> bool:
    """The planner's catch-all for chunks it did not place («سایر موارد مطرح‌شده»)."""
    return str(topic.get("topic_id", "") or "").startswith("uncovered-content")


def time_order_topics(topics: list[dict[str, Any]], all_chunk_indexes: list[int]) -> list[dict[str, Any]]:
    """Make every topic one continuous stretch of the lecture, in the order it was spoken.

    The planner lists, per topic, every chunk where it is discussed; the note then moved
    sentences from the middle of the lecture into earlier sections (the sentiment lexicon ended up
    inside Naive Bayes) and cut a story from its end. A topic now owns the chunks from where it
    first appears up to where the next topic first appears; the first topic also takes any
    chunks before it.
    """
    all_chunks = sorted({int(index) for index in all_chunk_indexes})
    if not all_chunks:
        return topics
    keyed: list[tuple[int, dict[str, Any]]] = []
    for topic in topics:
        indexes = [int(index) for index in topic.get("transcript_chunk_indexes") or [] if int(index) in set(all_chunks)]
        if not indexes or is_leftover_topic(topic):
            # The planner's "uncovered content" leftovers are absorbed by the neighbouring ranges.
            continue
        keyed.append((min(indexes), topic))
    if not keyed:
        return topics
    keyed.sort(key=lambda item: item[0])
    # Two topics starting on the same chunk: the first listed keeps it, the other starts with the next chunk.
    used_starts: set[int] = set()
    adjusted: list[tuple[int, dict[str, Any]]] = []
    for start, topic in keyed:
        while start in used_starts and start < all_chunks[-1]:
            start = next((chunk for chunk in all_chunks if chunk > start), start)
            if start == all_chunks[-1]:
                break
        if start in used_starts:
            continue
        used_starts.add(start)
        adjusted.append((start, topic))
    result: list[dict[str, Any]] = []
    for position, (start, topic) in enumerate(adjusted):
        begin = all_chunks[0] if position == 0 else start
        end = adjusted[position + 1][0] if position + 1 < len(adjusted) else all_chunks[-1] + 1
        updated = dict(topic)
        updated["transcript_chunk_indexes"] = [chunk for chunk in all_chunks if begin <= chunk < end]
        updated["order"] = position
        result.append(updated)
    return result


_REPEATED_TALK_SEGMENT = re.compile(r"REPEATED TALK:.*?an exam hint\)\.\s", re.DOTALL)

_V2_LECTURER_REPLACEMENT = (
    "REPEATED TALK: a point is written ONCE. When the lecturer explains a point a second time "
    "(in other words, or again later), keep the clearest version in the place where he first "
    "said it and fold in only what the second telling ADDS (a new example, a condition, a "
    "detail); never write the same point again in other words. Never shorten a passage that "
    "explains a point for the first time. "
)

_V2_WRITER_REPLACEMENTS = (
    (
        "Since you are given every mention of this topic together, write ONE coherent, "
        "complete treatment of it -- merge the separate mentions into flowing prose "
        "instead of repeating the definition or restating the same point once per "
        "mention. ",
        "The excerpts are one continuous stretch of the lecture in the order it was spoken: "
        "write them in that same order, as the lecturer's own explanation, without regrouping "
        "or moving points. ",
    ),
    (
        "Never shorten the note by dropping a point; keep "
        "it readable by writing each point tightly and removing only spoken repetition, "
        "filler and restarts. ",
        "Never shorten the note by dropping a point or a sentence of explanation; keep the "
        "lecturer's own sentences and remove only filler, restarts and a sentence said twice. "
        "Your text should usually run to well over half the length of the spoken excerpt. ",
    ),
)

_V2_PLANNER_REPLACEMENTS = (
    (
        "A professor commonly returns to the same topic more than once during a lecture "
        "(introduces it, moves on to something else, then comes back to it later with more "
        "detail, a related example, or a correction). You MUST recognize every such return "
        "and group ALL of it under one topic, identified once, instead of creating a "
        "separate topic each time it is mentioned. ",
        "FOLLOW THE LECTURE IN TIME. Each topic is ONE continuous stretch of the lecture; the "
        "writer reads the stretches in order and never moves a sentence from one place of the "
        "lecture to another. When the lecturer comes back to an earlier subject, it stays in "
        "the stretch where it was said. THE LECTURER'S OWN STRUCTURE WINS: when the lecturer "
        "names a structure (for example three data sets: train, development and test; two "
        "kinds of evaluation; the steps of a method), the topics and their titles follow that "
        "structure and never replace it with a different count or grouping. START A NEW TOPIC "
        "AT THE EXACT CHUNK WHERE THE LECTURER MOVES TO A NEW NAMED SUBJECT (for example from "
        "Naive Bayes to sentiment analysis, even though the two are related): a topic never "
        "carries a stretch that belongs to the next subject, and the cut falls on the chunk "
        "where the new subject is first mentioned. A TOPIC IS AT MOST ABOUT 15 MINUTES OF THE "
        "LECTURE: a longer subject is split at the lecturer's own steps (definition, recurrence, "
        "implementation, reconstructing the answer, running time, a worked example), each step "
        "its own topic with its own title. ",
    ),
    (
        "personal anecdotes, classroom",
        "personal anecdotes that teach nothing about the course (a story told to make a "
        "course point is course material), classroom",
    ),
)

_V2_USER_REPLACEMENTS = (
    (
        "- transcript_chunk_indexes: array of integers -- EVERY chunk_index below where this "
        "topic is discussed, even when the indexes are not contiguous",
        "- transcript_chunk_indexes: array of integers -- the chunk_index where this topic "
        "STARTS and every chunk through where it ends (one continuous stretch; the next topic "
        "starts where this one ends)",
    ),
)


def apply_v2_writer_rules(prompt: str, context: dict[str, Any] | None) -> str:
    if not quality_rev_on(context):
        return prompt
    prompt = _REPEATED_TALK_SEGMENT.sub(_V2_LECTURER_REPLACEMENT, prompt)
    return _replace_all(prompt, _V2_WRITER_REPLACEMENTS)


def apply_v2_planner_rules(system_prompt: str, user_prompt: str, context: dict[str, Any] | None) -> tuple[str, str]:
    if not quality_rev_on(context):
        return system_prompt, user_prompt
    return (
        _replace_all(system_prompt, _V2_PLANNER_REPLACEMENTS) + PLANNER_TOPIC_RULE,
        _replace_all(user_prompt, _V2_USER_REPLACEMENTS),
    )


def v2_length_hint_text(*, source_words: int, written_words: int) -> str:
    """User-prompt prefix for the one re-run of a window that came out far below the spoken length."""
    return (
        "LENGTH CORRECTION: your previous answer for these exact excerpts was "
        f"about {written_words} words for {source_words} spoken words, so it paraphrased or "
        "summarised the lecturer. Write it again keeping the lecturer's own sentences: drop only "
        "filler, restarts and a point said twice (keep the clearest telling), finish broken sentences, and keep every "
        "explanation, example and remark in the order it was spoken. The result should be about "
        f"{int(source_words * 0.7)} words.\n"
    )


def v2_part_is_short(*, source_text: str, written_words: int) -> bool:
    source_words = len(str(source_text or "").split())
    return source_words >= MIN_V2_SOURCE_WORDS and written_words < source_words * MIN_V2_WORD_RATIO


# --------------------------------------------------------------------------------------
# Repeats inside a section and passages a part left out
# --------------------------------------------------------------------------------------

MIN_REPEAT_TOKENS = 6
MIN_SHARED_TOKENS = 6
REPEAT_OVERLAP = 0.6
V42_REPEAT_OVERLAP = 0.5  # revision 3: a point written in other words is still the same point
EXAMPLE_REPEAT_OVERLAP = 0.8  # worked examples reuse their numbers and symbols legitimately
KEY_IDEA_REPEAT_OVERLAP = 0.85  # the one-line takeaway may restate its own topic, but not word for word


def _sentence_tokens(sentence: str) -> set[str]:
    return {token for token in re.findall(r"[\w؀-ۿ]+", str(sentence or "").replace("\u200c", "").lower()) if len(token) > 1}


def _is_repeat(tokens: set[str], earlier: list[set[str]], threshold: float = REPEAT_OVERLAP) -> bool:
    if len(tokens) < MIN_REPEAT_TOKENS:
        return False
    for previous in earlier:
        if len(previous) < MIN_REPEAT_TOKENS:
            continue
        shared = len(tokens & previous)
        if shared >= MIN_SHARED_TOKENS and shared / min(len(tokens), len(previous)) >= threshold:
            return True
    return False


def _dedupe_text(text: str, earlier: list[set[str]], own: list[set[str]], threshold: float) -> str:
    paragraphs_out: list[str] = []
    for paragraph in str(text or "").split("\n\n"):
        kept: list[str] = []
        for sentence in [part for part in _SENTENCE_SPLIT.split(paragraph) if part.strip()]:
            tokens = _sentence_tokens(sentence)
            if _is_repeat(tokens, earlier, threshold):
                continue
            kept.append(sentence)
            own.append(tokens)
        if kept:
            paragraphs_out.append(" ".join(kept))
    return "\n\n".join(paragraphs_out)


def drop_repeated_sentences(units: list[dict[str, Any]], *, overlap: float = REPEAT_OVERLAP) -> list[dict[str, Any]]:
    """Each concept is written once in the whole note.

    A sentence that restates an earlier one (even reworded: it shares most of its content words)
    is dropped, wherever it sits -- the same point as a paragraph, then as a step list, a
    warning, a takeaway and again in a later section. The first occurrence stays; the later one
    goes. Short sentences, worked examples (which reuse numbers) and the one-line key idea of a
    topic (which may restate its own topic, but never word for word) are judged more gently.
    A unit left with nothing to show is removed.
    """
    seen: list[set[str]] = []
    result: list[dict[str, Any]] = []
    for unit in units:
        if _is_off_topic(unit):
            result.append(unit)
            continue
        updated = dict(unit)
        own: list[set[str]] = []
        for key in ("body", "note"):
            if str(updated.get(key, "") or "").strip():
                updated[key] = _dedupe_text(updated[key], seen, own, overlap)
        if isinstance(updated.get("list_items"), list):
            items = []
            for item in updated["list_items"]:
                tokens = _sentence_tokens(str(item or ""))
                if _is_repeat(tokens, seen, overlap):
                    continue
                items.append(item)
                own.append(tokens)
            updated["list_items"] = items
        if str(updated.get("example", "") or "").strip():
            updated["example"] = _dedupe_text(updated["example"], seen, own, EXAMPLE_REPEAT_OVERLAP)
        if isinstance(updated.get("ai_added"), list):
            added = []
            for item in updated["ai_added"]:
                tokens = _sentence_tokens(str(item or ""))
                if _is_repeat(tokens, [*seen, *own]):
                    continue
                added.append(item)
            updated["ai_added"] = added
        key_idea = str(updated.get("key_idea", "") or "")
        if key_idea and _is_repeat(_sentence_tokens(key_idea), [*seen, *own], KEY_IDEA_REPEAT_OVERLAP):
            updated["key_idea"] = ""
        seen.extend(own)
        has_content = any(
            updated.get(field)
            for field in ("body", "note", "example", "list_items", "formulas", "code_blocks", "tables", "key_idea", "ai_added")
        )
        if has_content or not any(unit.get(field) for field in ("body", "note", "example", "list_items")):
            result.append(updated)
    return result


_PASSAGE_STOPWORDS = frozenset(
    """این اون آن که را با از به در برای یک دو سه هم هر چه چی چیز چیزی میشه میکنیم کنیم کنید کنم بکنیم
    داریم دارید دارم داره دارن باشه باشد باشید باشیم بشه بود بوده هست هستن هستند است نیست اینجا اونجا
    اینها اینا اونا اونها حالا دیگه دیگر مثلا خب بله آره یعنی ولی اما اگر اگه چون پس بعد قبل خیلی همه
    همین همون وقتی کجا کدوم چقدر چرا چجوری شما ما من تو رو باید شده شد کرد کرده کردیم کردن کنه توی روی
    بین بیشتر کمتر میتونیم میتونه گفتیم گفتم میگیم""".split()
)
MIN_PASSAGE_TOKENS = 12
PASSAGE_COVERAGE_RATIO = 0.75


def _passage_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[؀-ۿA-Za-z]{3,}", str(text or "").replace("\u200c", ""))
        if token not in _PASSAGE_STOPWORDS
    }


def passages_missing_from_written(chunk_texts: list[str], written_text: str) -> list[str]:
    """Spoken passages whose words barely appear in what was written for them.

    Each passage is scored by the share of its content words found in the written part; one far
    below the part's median coverage was left out (the agent/instrument explanation vanished
    from v2.1 this way). Colloquial speech lowers every passage alike, so the comparison is
    relative to the part itself, never an absolute bar.
    """
    written = str(written_text or "").replace("\u200c", "")
    scored: list[tuple[str, float]] = []
    for text in chunk_texts:
        tokens = _passage_tokens(text)
        if len(tokens) < MIN_PASSAGE_TOKENS:
            continue
        scored.append((text, sum(1 for token in tokens if token in written) / len(tokens)))
    if len(scored) < 2:
        return []
    ordered = sorted(score for _, score in scored)
    median = ordered[len(ordered) // 2]
    return [text for text, score in scored if score < median * PASSAGE_COVERAGE_RATIO]


def v2_missing_passages_hint_text(passages: list[str]) -> str:
    quoted = "\n".join(f"- «{' '.join(str(passage).split()[:25])} …»" for passage in passages[:4])
    return (
        "MISSING PASSAGES: your previous answer for these exact excerpts left out what the "
        "lecturer said in the passages starting below. Write the part again and include them "
        "in the order spoken, with their examples; leave a passage out only when it is noise "
        f"or garbled speech-to-text with no recoverable point.\n{quoted}\n"
    )


# --------------------------------------------------------------------------------------
# Formula boxes: no duplicates, no code fragments
# --------------------------------------------------------------------------------------

_TEXT_IN_FORMULA = re.compile(r"\\text\{[^{}]*\}|\\(?:quad|qquad|,|;|!)")


def _formula_key(formula: str) -> str:
    text = _TEXT_IN_FORMULA.sub("", str(formula or ""))
    text = re.sub(r"\s+", "", text).lower()
    for pattern, symbol in (("\\leq", "≤"), ("\\geq", "≥"), ("\\le", "≤"), ("\\ge", "≥"), ("\\neq", "≠"), ("\\ne", "≠"), ("<=", "≤"), (">=", "≥"), ("!=", "≠")):
        text = text.replace(pattern, symbol)
    return text.replace("{", "").replace("}", "")


def is_formula_fragment(formula: str) -> bool:
    """A single condition, an assignment, a test or a bare list of elements is not a formula."""
    key = _formula_key(formula)
    if not key:
        return True
    if "\\rightarrow" in key or "\\implies" in key:
        return True  # «DP[i] > DP[max] ⇒ max = i» is a line of code
    if "=" not in key:
        # «A[j] ≤ A[i]», «P[i] > 0», «X_1, …, X_{n-1}»; a bound such as «LCS(X,Y) ≤ min(n,m)» stays.
        return "\\ldots" in key or ("[" in key and len(key) <= 24)
    left, right = key.split("=", 1)
    # «Z = (Z_1, …, Z_l)» only lists elements.
    return "\\ldots" in right and right.startswith("(") and not any(op in right for op in ("+", "\\max", "\\min", "\\sum"))


_ARITH_EVAL = re.compile(r"\([\s\d\.,]+\)\s*=\s*[\d\.]+")
_FN_WORDS = ("min", "max", "log", "sum", "floor", "ceil", "mod", "gcd")
_PERSIAN_LETTER = re.compile(r"[؀-ۿ]")
_SINGLE_VALUE = re.compile(r"[\d.,٫]+\s*[A-Za-z]{0,3}")


def is_trivial_formula(formula: str) -> bool:
    """A formula box that adds nothing: a few symbols, plain arithmetic, or a label equal to one value.

    «min(10, 3) = 3», «240 + 12 = 252», «3/1 = 3» and «S_{prokaryote} = 70S» belong in a sentence or a
    table, not in a box of their own. A recurrence, a bound or a definition with several symbols is not trivial.
    """
    raw = str(formula or "")
    text = re.sub(r"\\[a-zA-Z]+", " ", raw)
    compact = re.sub(r"[\s{}$]", "", text)
    if not compact or len(compact) <= 3:
        return True
    if _ARITH_EVAL.search(compact):
        return True
    letters = re.sub(r"(?i)" + "|".join(_FN_WORDS), "", compact)
    if not re.search(r"[A-Za-z]", letters) and re.search(r"\d", compact):
        return True  # pure numbers and operators, e.g. 2 + 3 = 5
    if "=" in raw and _PERSIAN_LETTER.search(raw):
        # A symbol labelled with a Persian word set equal to a single value («S_{پروکاریوت‌ها}=70S»).
        right = raw.rsplit("=", 1)[1].strip().strip("$ ")
        if _SINGLE_VALUE.fullmatch(right):
            return True
    return False


def clean_unit_formulas(units: list[dict[str, Any]], *, drop_trivial: bool = False) -> list[dict[str, Any]]:
    """Drop formula fragments and formulas that repeat one already shown in the note.

    Revision 3 (`drop_trivial`) also drops boxes that hold only arithmetic or a label equal to one value.
    """
    from difflib import SequenceMatcher

    seen: list[str] = []
    result: list[dict[str, Any]] = []
    for unit in units:
        formulas = unit.get("formulas") or []
        if not formulas:
            result.append(unit)
            continue
        kept: list[Any] = []
        for formula in formulas:
            text = formula.get("formula", "") if isinstance(formula, dict) else str(formula or "")
            key = _formula_key(text)
            if is_formula_fragment(text) or (drop_trivial and is_trivial_formula(text)):
                continue
            if any(key == other or SequenceMatcher(None, key, other).ratio() >= 0.9 for other in seen):
                continue
            seen.append(key)
            kept.append(formula)
        updated = dict(unit)
        updated["formulas"] = kept
        result.append(updated)
    return result


# --------------------------------------------------------------------------------------
# Revision 3 ("v4.2" / "v2.2"): cleaning that never depends on the model obeying a prompt
# --------------------------------------------------------------------------------------

# Not inside an [[exam:important]] marker: those are still needed to build the exam boxes.
_LEAKED_LATIN = re.compile(r"(?<![\w\u0600-\u06FF:])(?:important|excluded|reconstructed)(?![\w\]])", re.IGNORECASE)
_LEAKED_AI_LABEL = re.compile(r"(?<=مثال)\s+ai(?![\w])")
_LABEL_ONLY = re.compile(r"^\s*(?:نمونهٔ|نمونه)\s*بازسازی[\u200c ]?شده\s*[:：]?\s*$")
# `[[added:...]]` is the model's own addition; revision 3 allows none, so a stray one is removed whole.
_ADDED_MARKER = re.compile(r"\s*\[\[added:.*?\]\]", re.DOTALL)
_FILE_NAME_TITLE = re.compile(r"^\s*(?:voice|vn|rec|aud|audio|img|mov|wa)\b[\s_-]*\d|\b\d{6}_\d{6}\b|\.(?:mp3|m4a|wav|mp4|ogg|webm|aac|flac)\s*$", re.IGNORECASE)


_CODE_TITLE = re.compile(
    r"^\s*(?:(?:session|lecture|lec|class|recording|new recording|جلسه|جلسهٔ|کلاس|ضبط|ویس|فایل)[\s#:_-]*\d{1,4}"  # «Session 3», «جلسه ۴»
    r"|[\d۰-۹]{1,4}"  # a bare number
    r"|[\d۰-۹]{1,4}\s*[-/._]\s*[\d۰-۹]{1,2}\s*[-/._]\s*[\d۰-۹]{1,4}(?:[\sT]+[\d۰-۹]{1,2}[:.][\d۰-۹]{2}(?::[\d۰-۹]{2})?)?"  # a date, maybe with a time
    r"|[\d۰-۹]{1,2}[:.][\d۰-۹]{2}(?:[:.][\d۰-۹]{2})?)\s*$",  # a time
    re.IGNORECASE,
)
_PLACEHOLDER_TEXT = re.compile(
    r"اینجا بنویس|متن (?:این )?(?:بخش|قسمت) را (?:اینجا )?بنویس|مثال و حل (?:آن )?را (?:اینجا )?بنویس|"
    r"write (?:the )?(?:section )?(?:text )?here|lorem ipsum|\[\s*(?:text|section text|example)\s*\]",
    re.IGNORECASE,
)


def looks_like_file_name(title: str) -> bool:
    """«Voice 261104_150555-1», «lecture3.m4a», «Session 3», «1404/08/20» or «14:30» is not a title."""
    value = str(title or "")
    return bool(_FILE_NAME_TITLE.search(value) or _CODE_TITLE.match(value))


def has_placeholder_text(text: str) -> bool:
    """A template instruction (\"write the section text here\") that reached the note instead of content."""
    return bool(_PLACEHOLDER_TEXT.search(str(text or "")))


def strip_leaked_labels(text: str) -> str:
    """Remove generation markers that leaked into Persian running text, and labels with nothing after them."""
    source = str(text or "")
    if not source.strip():
        return source
    if has_placeholder_text(source):
        kept = [part for part in re.split(r"\n\s*\n", source) if part.strip() and not has_placeholder_text(part)]
        source = "\n\n".join(kept)
        if not source.strip():
            return ""
    if not re.search(r"[\u0600-\u06FF]", source):
        return source
    parts = re.split(r"(\n\s*\n)", source)
    cleaned: list[str] = []
    for part in parts:
        if not part.strip():
            cleaned.append(part)
            continue
        if _LABEL_ONLY.match(part):
            continue
        part = _LEAKED_AI_LABEL.sub("", _LEAKED_LATIN.sub("", _ADDED_MARKER.sub("", part)))
        cleaned.append(re.sub(r"[ \t]{2,}", " ", part).strip())
    return re.sub(r"\n{3,}", "\n\n", "".join(cleaned)).strip()


def clean_units_v42(
    units: list[dict[str, Any]], *, concept_key: Any, keeps_lecturer_wording: bool = True
) -> list[dict[str, Any]]:
    """Revision 3: nothing of the note's own, no labels in text, and no list ending with a heading.

    The model is told this in the prompt; this makes it hold when the model does not obey: for v2.2
    (keeps_lecturer_wording) additions of the note and rebuilt examples are removed; for both, leaked markers are stripped, and a last list item that
    is just the title of a section or subsection is dropped.
    """
    titles = {
        concept_key(str(value or ""))
        for unit in units
        for value in (unit.get("section_title"), unit.get("concept_title"))
        if str(value or "").strip()
    }
    result: list[dict[str, Any]] = []
    for unit in units:
        # A few lines of class chatter (music, a request to repeat a word) is not study material.
        if (
            str(unit.get("content_type", "") or "").strip().lower() == "off-topic"
            and len(str(unit.get("body", "") or "")) < 280
        ):
            continue
        updated = dict(unit)
        if keeps_lecturer_wording:
            # v2.2: the lecturer's own account. Nothing of the note's own, no rebuilt example.
            updated["ai_added"] = []
            if updated.get("example_ai"):
                updated["example"] = ""
                updated["example_ai"] = False
        # v4.2 keeps its additions and rebuilt examples (marked by the flags and capped by the
        # addition budget); only the leaked labels inside the text are removed below.
        for key in ("body", "note", "example", "key_idea"):
            if isinstance(updated.get(key), str):
                updated[key] = strip_leaked_labels(updated[key])
        if isinstance(updated.get("list_items"), list):
            items = [strip_leaked_labels(str(item or "")) for item in updated["list_items"]]
            items = [item for item in items if item.strip()]
            while items and concept_key(items[-1]) in titles:
                items.pop()
            updated["list_items"] = items
            # A list of one is a sentence: fold it into the body instead of printing a one-bullet list.
            if len(items) == 1:
                updated["body"] = _fold_single_item(str(updated.get("body", "") or ""), items[0])
                updated["list_items"] = []
        # A lead that announces a list («…:») with no list after it keeps its sentence, not the colon.
        if (
            not updated.get("list_items")
            and not any(updated.get(key) for key in ("tables", "code_blocks", "formulas"))
            and isinstance(updated.get("body"), str)
        ):
            updated["body"] = _drop_orphan_colon(updated["body"])
        if not keeps_lecturer_wording and _is_empty_example(updated):
            updated["example"] = ""
        result.append(updated)
    return result


def _fold_single_item(body: str, item: str) -> str:
    item = item.strip()
    body = body.strip()
    if not body:
        return item
    if _tokens(item) <= _tokens(body):
        return body  # the body already says it
    body = re.sub(r"\s*[:：]\s*$", ".", body)
    if body and body[-1] not in ".!?؟۔":
        body += "."
    return f"{body} {item}"


_TRAILING_COLON = re.compile(r"\s*[:：]\s*$")


def _drop_orphan_colon(body: str) -> str:
    """Replace a closing «:» of the body (a lead for a list that is not there) with a full stop."""
    text = body.rstrip()
    if not _TRAILING_COLON.search(text):
        return body
    stripped = _TRAILING_COLON.sub("", text).rstrip()
    if not stripped:
        return ""
    return stripped if stripped[-1] in ".!?؟۔" else stripped + "."


_EXAMPLE_FACT = re.compile(r"[\d۰-۹]|[A-Za-z]{3,}")


def _is_empty_example(unit: dict[str, Any]) -> bool:
    """A v4.2 example box with no figure and no named case that only restates the body.

    A rebuilt example (`example_ai`) is TesaNote's own teaching and is never dropped here.
    """
    example = str(unit.get("example", "") or "").strip()
    if not example or unit.get("example_ai") or _EXAMPLE_FACT.search(example):
        return False
    body = " ".join(
        part for part in (str(unit.get("body", "") or ""), *[str(i or "") for i in unit.get("list_items", []) or []]) if part
    )
    return bool(body) and _similar(example, body, threshold=0.8)
