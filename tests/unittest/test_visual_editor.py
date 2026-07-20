import json

from mineru.cli.visual_editor import recover_table_span_from_model


def test_recovers_empty_table_body_from_same_page_model_output(tmp_path):
    doc_stem = "estimate"
    (tmp_path / f"{doc_stem}_model.json").write_text(
        json.dumps(
            [[
                {
                    "type": "table",
                    "bbox": [0.1, 0.2, 0.9, 0.8],
                    "content": "<table><tr><td>Recovered</td></tr></table>",
                }
            ]],
        ),
        encoding="utf-8",
    )
    block = {
        "type": "table",
        "bbox": [10, 20, 90, 80],
        "blocks": [{"type": "table_body", "bbox": [10, 20, 90, 80], "lines": []}],
    }

    span = recover_table_span_from_model(tmp_path, doc_stem, 0, block, (100, 100))

    assert span is not None
    assert span["html"] == "<table><tr><td>Recovered</td></tr></table>"
    assert block["blocks"][0]["lines"][0]["spans"][0] is span


def test_does_not_use_unrelated_model_table(tmp_path):
    doc_stem = "estimate"
    (tmp_path / f"{doc_stem}_model.json").write_text(
        json.dumps(
            [[
                {
                    "type": "table",
                    "bbox": [0.6, 0.6, 0.9, 0.9],
                    "content": "<table><tr><td>Other</td></tr></table>",
                }
            ]],
        ),
        encoding="utf-8",
    )
    block = {
        "type": "table",
        "bbox": [10, 10, 30, 30],
        "blocks": [{"type": "table_body", "bbox": [10, 10, 30, 30], "lines": []}],
    }

    assert recover_table_span_from_model(tmp_path, doc_stem, 0, block, (100, 100)) is None
    assert block["blocks"][0]["lines"] == []


def test_recovers_full_page_table_despite_low_iou(tmp_path):
    # Model bbox spans the whole page; the middle.json block bbox is trimmed
    # to the OCR-detected rows, so IoU (0.09) stays well below the old 0.5
    # cutoff even though the smaller bbox sits entirely inside the larger one
    # (containment == 1.0). The new max(IoU, containment) score must still
    # recover this candidate.
    doc_stem = "fullpage"
    (tmp_path / f"{doc_stem}_model.json").write_text(
        json.dumps(
            [[
                {
                    "type": "table",
                    "bbox": [0.0, 0.0, 1.0, 1.0],
                    "content": "<table><tr><td>FullPage</td></tr></table>",
                }
            ]],
        ),
        encoding="utf-8",
    )
    block = {
        "type": "table",
        "bbox": [10, 10, 40, 40],
        "blocks": [{"type": "table_body", "bbox": [10, 10, 40, 40], "lines": []}],
    }

    span = recover_table_span_from_model(tmp_path, doc_stem, 0, block, (100, 100))

    assert span is not None
    assert span["html"] == "<table><tr><td>FullPage</td></tr></table>"
    assert block["blocks"][0]["lines"][0]["spans"][0] is span


def test_selects_best_scoring_candidate_among_multiple_tables(tmp_path):
    # Two table candidates on the same page: a poorly-overlapping one listed
    # first (score ~0.04, below the 0.3 multi-candidate floor) and the
    # correct match listed second (score 1.0, fully contained). The function
    # must pick the best-scoring candidate, not the first one in list order.
    doc_stem = "multi"
    (tmp_path / f"{doc_stem}_model.json").write_text(
        json.dumps(
            [[
                {
                    "type": "table",
                    "bbox": [0.4, 0.4, 0.9, 0.9],
                    "content": "<table><tr><td>Bad</td></tr></table>",
                },
                {
                    "type": "table",
                    "bbox": [0.0, 0.0, 0.6, 0.6],
                    "content": "<table><tr><td>Good</td></tr></table>",
                },
            ]],
        ),
        encoding="utf-8",
    )
    block = {
        "type": "table",
        "bbox": [0, 0, 50, 50],
        "blocks": [{"type": "table_body", "bbox": [0, 0, 50, 50], "lines": []}],
    }

    span = recover_table_span_from_model(tmp_path, doc_stem, 0, block, (100, 100))

    assert span is not None
    assert span["html"] == "<table><tr><td>Good</td></tr></table>"


def test_returns_none_when_overlap_is_below_score_threshold(tmp_path):
    # Candidate bbox barely clips the corner of the block bbox: a non-zero
    # but tiny intersection (score ~0.0025) that must still fall below the
    # 0.05 single-candidate floor, distinct from the zero-overlap case
    # already covered by test_does_not_use_unrelated_model_table.
    doc_stem = "belowthreshold"
    (tmp_path / f"{doc_stem}_model.json").write_text(
        json.dumps(
            [[
                {
                    "type": "table",
                    "bbox": [0.48, 0.48, 0.9, 0.9],
                    "content": "<table><tr><td>TooFar</td></tr></table>",
                }
            ]],
        ),
        encoding="utf-8",
    )
    block = {
        "type": "table",
        "bbox": [10, 10, 50, 50],
        "blocks": [{"type": "table_body", "bbox": [10, 10, 50, 50], "lines": []}],
    }

    assert recover_table_span_from_model(tmp_path, doc_stem, 0, block, (100, 100)) is None
    assert block["blocks"][0]["lines"] == []
