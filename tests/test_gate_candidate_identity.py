import pytest
from sift.gating.verdict import evaluate_gate


def benchmark(candidate_rate=1.0):
    return {
        'run_summary': {
            'baseline': {'pass_rate_programmatic': {'mean': 1.0}},
            'candidate': {'pass_rate_programmatic': {'mean': candidate_rate}},
            'delta': {'pass_rate_programmatic': str(1.0 - candidate_rate)},
        },
        'comparison_summary': {
            'label_a': 'baseline', 'label_b': 'candidate',
            'a_wins': 0, 'b_wins': 3, 'ties': 0, 'valid': 3, 'failed': 0,
        },
    }


def test_second_side_candidate_has_correct_baseline_and_win_rate():
    result = evaluate_gate(benchmark(), candidate='candidate', min_win_rate=0.5)
    assert (result.candidate, result.baseline) == ('candidate', 'baseline')
    assert result.axes['comparison'].win_rate == 1.0
    assert result.decision == 'PASS'


def test_second_side_candidate_regression_is_not_inverted():
    result = evaluate_gate(benchmark(0.5), candidate='candidate', min_win_rate=0.5)
    assert result.axes['programmatic'].delta == -0.5
    assert result.axes['programmatic'].verdict == 'FAIL'
    assert result.decision == 'FAIL'


def test_swapping_sides_preserves_candidate_verdict():
    original = benchmark(0.5)
    swapped = benchmark(0.5)
    swapped['comparison_summary'].update(label_a='candidate', label_b='baseline', a_wins=3, b_wins=0)
    swapped['run_summary']['delta']['pass_rate_programmatic'] = '-0.5'
    a = evaluate_gate(original, candidate='candidate', min_win_rate=0.5)
    b = evaluate_gate(swapped, candidate='candidate', min_win_rate=0.5)
    assert (a.decision, a.candidate, a.baseline) == (b.decision, b.candidate, b.baseline)
    assert a.axes['comparison'].win_rate == b.axes['comparison'].win_rate == 1.0
    assert a.axes['programmatic'] == b.axes['programmatic']


def test_unknown_candidate_is_not_accepted():
    with pytest.raises(ValueError, match='candidate'):
        evaluate_gate(benchmark(), candidate='not-present')


def test_directory_sort_order_does_not_invert_a_real_regression():
    from sift.execution.benchmark import summarize_benchmark
    def entry(rate):
        return {'pass_rate': rate, 'pass_rate_programmatic': rate, 'time_seconds': 1, 'tokens': 1}
    data = {'run_summary': summarize_benchmark({'a-candidate': [entry(0.5)], 'z-baseline': [entry(1.0)]}),
            'comparison_summary': {'label_a': 'z-baseline', 'label_b': 'a-candidate', 'a_wins': 0, 'b_wins': 3, 'ties': 0}}
    result = evaluate_gate(data, candidate='a-candidate', min_win_rate=0.5)
    assert result.axes['programmatic'].delta == -0.5
    assert result.decision == 'FAIL'


def test_default_comparison_labels_and_configuration_ids_select_the_same_candidate():
    data = {'run_summary': {'with_skill': {'pass_rate_programmatic': {'mean': 1.0}}, 'without_skill': {'pass_rate_programmatic': {'mean': 1.0}}, 'delta': {'pass_rate_programmatic': '0'}},
            'comparison_summary': {'label_a': 'with', 'label_b': 'without', 'a_wins': 0, 'b_wins': 3, 'ties': 0},
            'runs': [{'configuration': 'without_skill', 'eval_id': 1, 'result': {'total': 1, 'passed': 1}}]}
    by_label = evaluate_gate(data, candidate='without', min_win_rate=0.5, must_pass_ids=[1])
    by_configuration = evaluate_gate(data, candidate='without_skill', min_win_rate=0.5, must_pass_ids=[1])
    assert by_label.decision == by_configuration.decision == 'PASS'
    assert by_label.axes['comparison'] == by_configuration.axes['comparison']
    assert by_label.axes['programmatic'] == by_configuration.axes['programmatic']
    assert by_label.axes['must_pass'].verdict == by_configuration.axes['must_pass'].verdict


def test_delta_without_side_identity_cannot_accept_an_arbitrary_candidate():
    with pytest.raises(ValueError):
        evaluate_gate({'run_summary': {'delta': {'pass_rate_programmatic': '+0.50'}}}, candidate='phantom')


def test_unlabelled_comparisons_do_not_award_first_side_wins_to_second_side():
    data = {'run_summary': {'with_skill': {'pass_rate_programmatic': {'mean': 1}}, 'without_skill': {'pass_rate_programmatic': {'mean': 1}}},
            'comparison_summary': {'a_wins': 3, 'b_wins': 0, 'ties': 0}}
    result = evaluate_gate(data, candidate='without_skill', min_win_rate=0.5)
    assert result.decision == 'UNCLEAR'
    assert result.axes['comparison'].win_rate is None


@pytest.mark.parametrize('bad', ['NaN', float('inf'), True])
def test_invalid_means_are_not_treated_as_non_regression(bad):
    data = benchmark()
    data['run_summary']['candidate']['pass_rate_programmatic']['mean'] = bad
    with pytest.raises(ValueError):
        evaluate_gate(data, candidate='candidate', min_win_rate=0.5)


def test_calibration_comparison_labels_mark_the_selected_configuration():
    data = {'run_summary': {'with_skill': {'pass_rate_programmatic': {'mean': 1}}, 'without_skill': {'pass_rate_programmatic': {'mean': 1}}},
            'comparison_summary': {'label_a': 'with', 'label_b': 'without', 'a_wins': 0, 'b_wins': 3, 'ties': 0},
            'calibration_summary': {'sides': {'without': {'total': 1, 'high_score_suspects': [{'eval': 1}]}}}}
    result = evaluate_gate(data, candidate='without_skill', min_win_rate=0.5)
    assert any('[candidate without]' in message for message in result.warnings)


def test_comparison_labels_cannot_supply_direction_for_an_unbound_legacy_delta():
    data = {'run_summary': {'delta': {'pass_rate_programmatic': '+0.50'}},
            'comparison_summary': {'label_a': 'baseline', 'label_b': 'candidate', 'a_wins': 0, 'b_wins': 3, 'ties': 0}}
    result = evaluate_gate(data, candidate='candidate', min_win_rate=0.5)
    assert result.axes['programmatic'].verdict == 'N/A'
    assert result.axes['programmatic'].applicable
    assert result.decision == 'UNCLEAR'
