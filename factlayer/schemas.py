"""JSON schemas handed to the model (structured outputs) + the prompts that go with them.

The schema is deliberately generic: subject / attribute / value / unit / period / basis / scope. Nothing here
knows about Delhivery, the RBI, revenue, or inflation - the documents decide what counts as a fact.
"""
from __future__ import annotations

DOC_META_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "publisher": {"type": "string", "description": "Organisation that authored/published the document"},
        "doc_type": {"type": "string", "description": "e.g. annual_report, prospectus, earnings_presentation, economic_survey, central_bank_annual_report, imf_staff_report, press_release, other"},
        "publication_date": {"type": "string", "description": "ISO date (YYYY-MM-DD, or YYYY-MM if the day is unknown); empty if unknown"},
        "period_covered_start": {"type": "string", "description": "ISO date the document primarily reports on, empty if not applicable"},
        "period_covered_end": {"type": "string"},
        "fiscal_year_convention": {"type": "string", "description": "How the document labels years, e.g. 'Indian fiscal year April-March; FY24 = 1 Apr 2023 - 31 Mar 2024' or 'calendar years'"},
        "default_currency": {"type": "string", "description": "ISO code, e.g. INR, USD; empty if none"},
        "default_scale": {"type": "string", "description": "Scale most numbers are quoted in: none, thousand, lakh, million, crore, billion"},
        "primary_subject": {"type": "string", "description": "The main entity the document is about, e.g. 'Delhivery Limited' or 'India'"},
        "data_vintage_notes": {"type": "string", "description": "Anything about estimates vs actuals, provisional data, revisions, cut-off dates"},
        "summary": {"type": "string", "description": "Two sentences on what the document is"},
    },
    "required": ["title", "publisher", "doc_type", "publication_date", "period_covered_start", "period_covered_end",
                 "fiscal_year_convention", "default_currency", "default_scale", "primary_subject",
                 "data_vintage_notes", "summary"],
}

FACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer", "description": "The [[page N]] marker the quote sits under"},
                    "kind": {"type": "string", "enum": ["numeric", "statement"]},
                    "subject": {"type": "string", "description": "Entity the fact is about (company, country, person, segment, index). Use the full proper name."},
                    "attribute": {"type": "string", "description": "What is being stated about the subject, as a short noun phrase, e.g. 'Revenue from operations', 'Headline CPI inflation', 'Registered office address', 'Board position'"},
                    "value_text": {"type": "string", "description": "The value exactly as written for numbers (e.g. '8,141.9'), or the statement itself for non-numeric facts"},
                    "value_num": {"type": ["number", "null"], "description": "Parsed number in the units/scale stated, null for statements"},
                    "unit": {"type": "string", "description": "INR, USD, %, percentage points, persons, count, ratio, days, tonnes ... empty if none"},
                    "scale": {"type": "string", "enum": ["none", "thousand", "lakh", "million", "crore", "billion", "trillion"]},
                    "period_label": {"type": "string", "description": "Period exactly as the document labels it: 'FY24', 'Q4 FY24', '2024-25', 'as at 31 March 2024', 'H1:2025-26'; empty if timeless"},
                    "period_start": {"type": "string", "description": "ISO date the period starts (resolve fiscal labels using the document's convention); empty if unknown"},
                    "period_end": {"type": "string", "description": "ISO date the period ends; for point-in-time facts equals period_start"},
                    "period_type": {"type": "string", "enum": ["fiscal_year", "quarter", "half_year", "calendar_year", "point_in_time", "range", "none"]},
                    "basis": {"type": "string", "description": "Measurement basis if stated: consolidated, standalone, provisional, advance estimate, revised estimate, projection, target, actual, nominal, real, y-o-y ... empty if none"},
                    "scope": {"type": "string", "description": "Any other qualifier that changes the meaning: segment, region, price base, definition, exclusions"},
                    "quote": {"type": "string", "description": "VERBATIM span copied from the text (<= 300 chars) that contains the value. Never paraphrase."},
                    "confidence": {"type": "number", "description": "0-1: how sure you are the fact is stated exactly like this"},
                },
                "required": ["page", "kind", "subject", "attribute", "value_text", "value_num", "unit", "scale",
                             "period_label", "period_start", "period_end", "period_type", "basis", "scope",
                             "quote", "confidence"],
            },
        }
    },
    "required": ["facts"],
}

RELATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "a": {"type": "string", "description": "fact id"},
                    "b": {"type": "string", "description": "fact id"},
                    "type": {"type": "string", "enum": ["corroborates", "contradicts", "reconciled", "unrelated"]},
                    "reconciliation": {"type": "string", "enum": ["", "period", "scope", "unit", "rounding", "vintage", "estimate_vs_actual", "definition", "other"]},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string", "description": "2-4 sentences a reader could verify against the two quotes"},
                },
                "required": ["a", "b", "type", "reconciliation", "confidence", "reasoning"],
            },
        }
    },
    "required": ["relations"],
}

DOC_META_SYSTEM = """You are cataloguing a PDF for a fact knowledge base. From the excerpt (the first pages and the last page) \
identify what the document is. Be precise about dates and about the year-labelling convention, because later stages \
use them to decide whether two numbers refer to the same period. If something is not stated, leave it empty rather than guessing."""

EXTRACT_SYSTEM = """You extract facts from documents into a knowledge layer that will later be compared across documents.

A FACT is a specific, checkable claim: a number with its unit and period, or a concrete statement (who holds which \
position, where an office is, when something happened, what a policy/target is). Not opinions, not boilerplate.

Rules:
- Prefer facts likely to be stated elsewhere too: headline financials, KPIs, macro indicators, dates, people/roles, \
addresses, counts, targets, growth rates. Skip trivia and repetitive line items; if a table has 40 rows take the totals \
and the notable ones. At most {max_facts} facts per request - choose the most important.
- One fact = one value. Split "revenue grew 13% to Rs 8,141 crore" into two facts (growth rate, level); split a KPI \
tile like "Rs 127 Cr / 1.6% EBITDA / EBITDA margin" into two facts; for "improved from 38 to 31 days" record the \
current value (31) and, if useful, the prior value as a separate fact with its own period.
- Copy `quote` VERBATIM from the text (<= 300 characters), including the number as written. Never paraphrase or fix typos.
- `page` must be the [[page N]] marker under which the quote appears.
- Resolve `period_start`/`period_end` to ISO dates using the document context below. A point-in-time fact (as at date) \
has start = end. Leave empty only when the text gives no time reference at all.
- `subject` is the entity the fact is about; use full names ("Delhivery Limited", "India", "Reserve Bank of India"). \
For a person, subject is the person, attribute is the role/event.
- Fill `basis`/`scope` whenever the text qualifies the number (consolidated vs standalone, provisional, estimate, \
projection, segment, constant prices...). These qualifiers are what make later comparisons fair.
- Keep `unit` and `scale` separate: "Rs 8,141 crore" -> value_num 8141, unit INR, scale crore. Percentages: unit "%", scale none.
- `confidence` below 0.7 when the text is ambiguous, garbled, or the value is inferred from a table with unclear headers.

Document context:
{doc_context}"""

ADJUDICATE_SYSTEM = """You compare facts that were extracted from different documents and decide how they relate.

For every pair of facts from DIFFERENT documents that talk about the same thing, output one relation:
- corroborates: both state the same fact about the same period/scope, even if worded or formatted differently \
(rounding within normal reporting precision counts as corroboration).
- contradicts: same subject, attribute, period and scope, yet the values/claims genuinely disagree and the documents \
give no qualifier that explains the gap. Also use for a state change that is NOT explained by time (e.g. two documents \
of the same date disagree on a person's role).
- reconciled: the values differ but the difference is explained by context - different periods (FY22 vs FY24), \
different scope (consolidated vs standalone, segment vs total), different units or scale, different data vintage \
(provisional vs revised estimate; a projection made earlier vs the later actual), a definition difference, or a \
person's role legitimately changing between the two documents' dates. Name the reason in `reconciliation` and \
explain it concretely in `reasoning`, citing the qualifiers or dates that resolve it.
- unrelated: the facts only look similar (different attribute or different entity). Output these only when the \
pair was obviously a false match; otherwise omit the pair.

Be rigorous: quote the numbers and periods in `reasoning`; make sure the arithmetic of any "rounding" claim actually \
works; do not call two figures corroborating if their periods differ. Documents publish at different times - a later \
document may revise an earlier one; that is "reconciled/vintage", not a contradiction, when the later doc says so or the \
numbers are official revisions. If a mismatch has no such explanation, call it a contradiction and say what you would \
need to check.

Only compare facts across documents. Skip pairs within the same document."""
