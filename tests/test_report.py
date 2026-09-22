from pathlib import Path

from openpyxl import load_workbook

from su_crawler.models import CollectionConfig, Product, Source
from su_crawler.report import SHEETS, export_report


def _config(tmp_path):
    return CollectionConfig("t", [Product("p1", "제품 1")], [Source("s1", "출처 1", "web", "https://shop.example/p", ["p1"])], str(tmp_path), str(tmp_path))


def test_evidence_first_workbook(tmp_path):
    cfg = _config(tmp_path)
    out = export_report(cfg, {"id": "r1", "status": "done", "started_at": "2026-01-01T00:00:00Z", "demo": True},
        [{"id": "t1", "product_id": "p1", "source_id": "s1", "status": "failed", "attempts": 0, "reason": "가격 없음", "updated_at": "2026-01-01T00:00:00Z"}],
        [{"id": "o0", "task_id": "t1", "product_id": "p1", "source_id": "s1", "source_name": "출처 1", "source_url": "https://shop.example/p", "collected_at": "2026-01-01T00:00:00Z", "status": "verified", "reason": "", "amount": "0", "currency": "KRW", "unit": "ea", "raw_fields": {"price": "0", "name": "=HYPERLINK(\"bad\")"}, "evidence": {"amount": {"location": "#price", "raw": "0"}}, "evidence_path": "evidence/a.html", "evidence_sha256": "abc", "locator": "#price", "extraction_method": "dom", "comparable": True, "comparison_key": "p1|KRW|ea", "freshness": "observed"},
         {"id": "o1", "task_id": "t1", "product_id": "p1", "source_id": "s1", "source_name": "출처 1", "source_url": "https://shop.example/p", "collected_at": "2026-01-01T00:00:00Z", "status": "review", "reason": "단위 미확인", "amount": None, "raw_fields": {"price": "문의"}, "evidence": {}, "evidence_path": "", "evidence_sha256": "", "locator": "", "extraction_method": "dom", "comparable": False, "freshness": "observed"}], out := tmp_path / "report.xlsx")
    assert out.exists()
    wb = load_workbook(out, read_only=True, data_only=False)
    assert wb.sheetnames == list(SHEETS)
    assert wb["Price Comparison"]["E3"].value == 0
    assert wb["Price Comparison"]["F3"].value == "0"
    assert wb["Observation History"]["H4"].value is None
    assert wb["Review Required"]["G3"].value is None
    assert wb["Review Required"]["H3"].value is None
    assert wb["Observation History"]["Q3"].value.startswith('{')  # formula-looking raw source stays text
    assert wb["Run Summary"]["A1"].value == "Demo data — not market prices"
    assert wb["Collection Status"].max_row == 3  # banner + header + planned task


def test_comparison_uses_latest_source_observation_and_group_statistics(tmp_path):
    cfg = CollectionConfig("t", [Product("p1", "제품 1")], [
        Source("s1", "출처 1", "web", "https://a.example", ["p1"]),
        Source("s2", "출처 2", "web", "https://b.example", ["p1"]),
    ], str(tmp_path), str(tmp_path))
    base = {"task_id": "t", "product_id": "p1", "status": "verified", "reason": "", "currency": "KRW", "unit": "ea", "raw_fields": {"price": "원문"}, "evidence": {}, "evidence_path": "", "evidence_sha256": "", "locator": "", "extraction_method": "dom", "comparable": True, "comparison_key": "same", "freshness": "observed"}
    observations = [
        dict(base, id="old", source_id="s1", source_name="출처 1", source_url="https://a.example", amount="10", normalized_amount="5", calculation="/2", collected_at="2026-01-01T00:00:00Z"),
        dict(base, id="new", source_id="s1", source_name="출처 1", source_url="https://a.example", amount="20", normalized_amount="10", calculation="/2", collected_at="2026-01-02T00:00:00Z"),
        dict(base, id="other", source_id="s2", source_name="출처 2", source_url="https://b.example", amount="40", normalized_amount="20", calculation="/2", collected_at="2026-01-01T00:00:00Z"),
        dict(base, id="bad", source_id="s2", source_name="출처 2", source_url="https://b.example", amount="NaN", collected_at="2026-01-03T00:00:00Z"),
    ]
    out = export_report(cfg, {"id": "r"}, [], observations, tmp_path / "stats.xlsx")
    ws = load_workbook(out, read_only=True, data_only=True)["Price Comparison"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2
    assert {r[4] for r in rows} == {20, 40}  # old source row and NaN excluded
    assert {r[13] for r in rows} == {2}
    assert {r[14] for r in rows} == {20}
    assert {r[15] for r in rows} == {40}
    assert {r[16] for r in rows} == {30}


def test_rental_report_separates_estimates_unknowns_and_deposit_kinds(tmp_path):
    cfg = _config(tmp_path)
    cfg.products[0].price_profile = "rental"
    base = {"task_id": "t", "product_id": "p1", "source_id": "s1", "source_name": "Synthetic",
            "source_url": "https://example.test/car", "status": "verified", "amount": "590000",
            "currency": "KRW", "price_profile": "rental", "value_origin": "observed", "source_visibility": "visible",
            "verification_level": "evidence_validated", "freshness": "observed", "comparable": True,
            "comparison_key": "synthetic", "collected_at": "2026-01-01T00:00:00Z",
            "raw_fields": {"price": "590000"}, "rental_conditions": {
                "price_basis": "monthly", "term_months": 36, "deposit_amount": "0", "advance_amount": "2000000",
                "upfront_deposit_amount": "1000000", "deposit_installment_amount": "3000000", "deposit_installment_months": 12}}
    observations = [dict(base, id="observed"),
        dict(base, id="estimate", value_origin="calculator_estimate", derived_amount="612345", status="review", amount=None,
             raw_fields={"displayed_deposit": "approximately 10 million"}, comparable=False, reason="calculated estimate"),
        # Even malformed flags cannot leak an estimate or hidden amount into comparison.
        dict(base, id="hidden", source_visibility="hidden"),
        dict(base, id="bad-estimate", value_origin="calculator_estimate", derived_amount="600000"),
        dict(base, id="unknown", status="review", comparable=False, amount=None, rental_conditions={"price_basis": "monthly"}),
        dict(base, id="daily", status="review", comparable=False, rental_conditions={"price_basis": "daily"}),
        dict(base, id="review", status="review", comparable=False, reason="conflicting terms"),
    ]
    out = export_report(cfg, {"id": "r"}, [], observations, tmp_path / "rental.xlsx")
    wb = load_workbook(out, data_only=False)
    assert wb.sheetnames == [*SHEETS, "Rental Quotes"]
    ws = wb["Rental Quotes"]
    data = [dict(zip([c.value for c in ws[1]], row)) for row in ws.iter_rows(min_row=2, values_only=True)]
    assert data[0]["Observed Monthly Price"] == 590000
    assert data[0]["Estimated Monthly Price (not observed)"] is None
    assert data[0]["Deposit Amount"] == 0
    assert data[0]["Advance Amount"] == 2000000
    assert data[0]["Upfront Deposit Amount"] == 1000000
    assert data[0]["Deposit Installment Amount"] == 3000000
    assert data[0]["Deposit Installment Months"] == 12
    assert data[1]["Observed Monthly Price"] is None
    assert data[1]["Estimated Monthly Price (not observed)"] == 612345
    assert data[1]["Displayed Deposit (raw)"] == "approximately 10 million"
    assert data[2]["Observed Monthly Price"] is None
    assert data[3]["Observed Monthly Price"] is None
    assert data[4]["Term (months)"] is None and data[4]["Deposit Amount"] is None
    assert data[5]["Observed Monthly Price"] is None
    assert data[6]["Observed Monthly Price"] == 590000
    assert wb["Price Comparison"].max_row == 2
    assert wb["Observation History"]["V3"].value == 612345
    assert wb["Observation History"]["H3"].value is None
    assert wb["Observation History"]["Z8"].value == 590000
    assert wb["Observation History"]["H8"].value is None
    assert ws.freeze_panes == "C2"


def test_report_retains_capture_artifacts_and_excludes_unconfirmed_html(tmp_path):
    cfg = _config(tmp_path)
    base = {"task_id": "t", "product_id": "p1", "source_id": "s1", "status": "verified",
            "value_origin": "observed", "amount": "12", "currency": "USD",
            "comparable": True, "comparison_key": "same", "freshness": "observed",
            "collected_at": "2026-09-22T00:00:00Z", "raw_fields": {"price": "12"}}
    artifacts = {"receipt": {"path": str(tmp_path / "capture.json"), "sha256": "receipt-hash"},
                 "screenshot": {"path": str(tmp_path / "capture.png"), "sha256": "image-hash"}}
    rows = [dict(base, id="static", evidence_mode="static_html", source_visibility="unconfirmed"),
            dict(base, id="rendered", evidence_mode="rendered_dom", source_visibility="visible",
                 evidence_artifacts=artifacts),
            dict(base, id="unconfirmed", evidence_mode="rendered_dom", source_visibility="unconfirmed")]
    output = export_report(cfg, {"id": "r"}, [], rows, tmp_path / "artifacts.xlsx")
    wb = load_workbook(output, read_only=True, data_only=True)
    try:
        evidence_sheet = wb["Source Evidence"]
        headers = next(evidence_sheet.iter_rows(values_only=True))
        evidence = [dict(zip(headers, values)) for values in evidence_sheet.iter_rows(min_row=2, values_only=True)]
        assert evidence[0]["Screenshot File"] is None
        assert evidence[1]["Evidence Mode"] == "rendered_dom"
        assert evidence[1]["Capture Receipt"] == artifacts["receipt"]["path"]
        assert evidence[1]["Receipt SHA-256"] == "receipt-hash"
        assert evidence[1]["Screenshot File"] == artifacts["screenshot"]["path"]
        assert evidence[1]["Screenshot SHA-256"] == "image-hash"
        assert wb["Price Comparison"].max_row == 2

    finally:
        wb.close()
