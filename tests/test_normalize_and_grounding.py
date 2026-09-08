"""Deterministic parts of the pipeline: period parsing, unit/scale normalisation, quote grounding, numeric judge."""
from factlayer import normalize
from factlayer.extract import ground
from factlayer.link import deterministic
from factlayer.pdf import make_chunks


def test_period_labels_resolve_with_indian_fiscal_year():
    assert normalize.parse_period("FY24") == ("2023-04-01", "2024-03-31", "fiscal_year")
    assert normalize.parse_period("FY 2023-24") == ("2023-04-01", "2024-03-31", "fiscal_year")
    assert normalize.parse_period("2024-25") == ("2024-04-01", "2025-03-31", "fiscal_year")
    assert normalize.parse_period("Q4 FY24") == ("2024-01-01", "2024-03-31", "quarter")
    assert normalize.parse_period("H1:2025-26") == ("2025-04-01", "2025-09-30", "half_year")
    assert normalize.parse_period("as at 31 March 2024") == ("2024-03-31", "2024-03-31", "point_in_time")


def test_period_labels_respect_other_conventions():
    assert normalize.parse_period("2024", fy_start_month=1) == ("2024-01-01", "2024-12-31", "calendar_year")
    assert normalize.parse_period("FY24", fy_start_month=7) == ("2023-07-01", "2024-06-30", "fiscal_year")


def test_units_and_scales_become_comparable():
    assert normalize.unit_norm("Rs.") == normalize.unit_norm("₹") == normalize.unit_norm("INR") == "INR"
    assert normalize.value_norm(8141.9, "crore") == 8141.9 * 1e7
    assert normalize.value_norm(81.419, "billion") == 81.419 * 1e9
    assert normalize.attribute_key("Total Revenue from Operations") == "revenue_from_operations"


def test_number_in_text_handles_indian_grouping_and_decimals():
    assert normalize.number_in_text(8141, "revenue of Rs 8,141 crore")
    assert normalize.number_in_text(6.5, "grew by 6.5 per cent")
    assert not normalize.number_in_text(8141, "revenue of Rs 7,225 crore")


PAGE = "FY24\n\n₹8,142 Cr ₹127Cr / 1.6% ₹76Cr / 0.9%\nFY24 revenue from services EBITDA / EBITDA margin Adj. EBITDA\n"


def test_exact_quote_is_located_with_offsets():
    fact = {"page": 1, "kind": "numeric", "value_num": 8142, "quote": "₹8,142 Cr ₹127Cr / 1.6%", "confidence": 0.9}
    ground(fact, [PAGE])
    assert fact["grounding"] == "exact"
    assert PAGE[fact["quote_start"]:fact["quote_end"]] == "₹8,142 Cr ₹127Cr / 1.6%"


def test_slide_tile_quote_is_grounded_as_window_and_flagged():
    fact = {"page": 1, "kind": "numeric", "value_num": 8142, "quote": "₹8,142 Cr\nFY24 revenue from services", "confidence": 0.9}
    ground(fact, [PAGE])
    assert fact["grounding"] == "window"
    assert "quote_not_contiguous" in fact["flags"]
    assert "8,142" in PAGE[fact["quote_start"]:fact["quote_end"]]


def test_missing_quote_and_wrong_number_are_flagged():
    fact = {"page": 1, "kind": "numeric", "value_num": 9999, "quote": "this sentence is not on the page at all", "confidence": 0.9}
    ground(fact, [PAGE])
    assert fact["grounding"] == "unverified"
    assert "quote_not_found" in fact["flags"] and "value_not_in_quote" in fact["flags"]


def test_wrong_page_number_is_corrected():
    fact = {"page": 2, "kind": "numeric", "value_num": 8142, "quote": "₹8,142 Cr ₹127Cr", "confidence": 0.9, "_chunk_pages": (1, 2)}
    ground(fact, [PAGE, "another page"])
    assert fact["page"] == 1 and any(f.startswith("page_corrected") for f in fact["flags"])


def _fact(**kw):
    base = {"kind": "numeric", "attribute_key": "revenue_from_operations", "unit": "INR", "period_start": "2023-04-01",
            "period_end": "2024-03-31", "basis": "", "scope": "", "value_text": "", "scale": "crore"}
    base.update(kw)
    return base


def test_deterministic_judge_only_speaks_when_context_is_identical():
    a = _fact(value_norm=8141.9e7, value_text="8,141.9")
    b = _fact(value_norm=8142e7, value_text="8,142", scale="crore")
    r = deterministic(a, b)
    assert r and r["type"] == "corroborates" and r["method"] == "deterministic"
    # different period -> stays silent (the model must reason about it)
    assert deterministic(a, _fact(value_norm=7225e7, period_start="2022-04-01", period_end="2023-03-31")) is None
    # same period, values differ -> silent too: could be scope/vintage, not for arithmetic to decide
    assert deterministic(a, _fact(value_norm=7000e7)) is None


def test_chunks_carry_page_markers_and_respect_size():
    pages = ["a" * 5000, "b" * 5000, "", "c" * 9000]
    chunks = make_chunks("doc", pages, max_chars=10000)
    assert [(c.page_start, c.page_end) for c in chunks] == [(1, 2), (4, 4)]
    assert "[[page 1]]" in chunks[0].text and "[[page 4]]" in chunks[1].text
