from app.executor.inspection import pick_tts
from app.vlm.base import MockVlm, build_user_prompt, parse_yes_no


def test_parse_yes_no_json_and_text():
    assert parse_yes_no('{"answer": "yes", "reason": "门关着"}') == 'yes'
    assert parse_yes_no('{"answer":"no"}') == 'no'
    assert parse_yes_no('{"answer":"unknown"}') == 'unknown'
    assert parse_yes_no('是，柜门已关好。') == 'yes'
    assert parse_yes_no('不是。门开着') == 'no'
    assert parse_yes_no('No, the door is open.') == 'no'
    assert parse_yes_no('Yes') == 'yes'
    assert parse_yes_no('画面里看不到消防栓，无法判断') == 'unknown'
    assert parse_yes_no('') == 'unknown'
    assert parse_yes_no('门未关好') == 'no'            # 否定词优先


def test_pick_tts_branches():
    tpl = {'expected': 'yes', 'on_pass': '{name}通过', 'on_fail': '{name}不通过', 'on_unknown': '{name}未知'}
    assert pick_tts(tpl, 'yes', 'A') == ('A通过', True)
    assert pick_tts(tpl, 'no', 'A') == ('A不通过', False)
    assert pick_tts(tpl, 'unknown', 'A') == ('A未知', None)
    assert pick_tts({'expected': 'no', 'on_pass': 'ok'}, 'no', 'B') == ('ok', True)
    text, passed = pick_tts({}, 'error', 'C')          # 默认模版兜底
    assert passed is None and '人工复核' in text


def test_mock_vlm_alternates():
    v = MockVlm('alternate')
    a = [v.ask_yes_no([(b'x', 'image/jpeg')], 'q').answer for _ in range(4)]
    assert a == ['yes', 'no', 'yes', 'no']
    assert MockVlm('no').ask_yes_no([], 'q').answer == 'no'


def test_build_user_prompt_mentions_angles():
    s = build_user_prompt('门是否关好', 190, 230, forward_deg=180, waypoint_name='消防栓')
    assert '190°' in s and '230°' in s and '+10°' in s and '+50°' in s and '消防栓' in s
