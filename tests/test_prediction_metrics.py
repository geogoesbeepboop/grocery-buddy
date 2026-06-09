"""Pure-logic tests for the prediction precision/recall metric.

The headline guarantee: recall is NOT pinned to 1.0 (the old eval's bug) — buying
something the predictor never flagged lowers recall, as it should.
"""
from grocery_buddy.evals import prediction_metrics


def test_perfect_match():
    m = prediction_metrics({"milk", "eggs"}, {"milk", "eggs"})
    assert m["precision"] == 1.0 and m["recall"] == 1.0 and m["f1"] == 1.0


def test_recall_is_not_pinned_to_one():
    # Predicted only milk; the user actually bought milk AND bread → recall 0.5.
    m = prediction_metrics({"milk"}, {"milk", "bread"})
    assert m["recall"] == 0.5
    assert m["precision"] == 1.0
    assert m["fn"] == 1  # bread is a real miss


def test_false_positive_lowers_precision():
    # We flagged kale that was never bought.
    m = prediction_metrics({"milk", "kale"}, {"milk"})
    assert m["precision"] == 0.5
    assert m["recall"] == 1.0
    assert m["fp"] == 1


def test_empty_predicted_gives_no_precision():
    m = prediction_metrics(set(), {"milk"})
    assert m["precision"] is None
    assert m["recall"] == 0.0


def test_empty_relevant_gives_no_recall():
    m = prediction_metrics({"milk"}, set())
    assert m["recall"] is None
    assert m["precision"] == 0.0
