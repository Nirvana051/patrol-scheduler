"""VLM 评测脚本：人工复核过的检查当标注。"""
import importlib.util
from pathlib import Path

from tests.conftest import init_robot
from tests.test_e2e_run import make_task, wait_run

spec = importlib.util.spec_from_file_location('eval_vlm', Path(__file__).resolve().parent.parent / 'scripts' / 'eval_vlm.py')
eval_vlm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_vlm)


def test_truth_answer():
    assert eval_vlm.truth_answer('yes', 1) == 'yes' and eval_vlm.truth_answer('yes', 0) == 'no'
    assert eval_vlm.truth_answer('no', 1) == 'no' and eval_vlm.truth_answer('no', 0) == 'yes'


def test_evaluate_uses_human_verdicts(app_client, mock_robot):
    init_robot(app_client, '1')
    tid = make_task(app_client, nodes=('3', '5'))
    run = wait_run(app_client, app_client.post(f'/api/tasks/{tid}/run').json()['id'])
    insp = run['inspections']
    assert [i['answer'] for i in insp] == ['yes', 'no']
    # 人工复核：第一条判通过（真值 yes），第二条判通过（期望 yes → 真值 yes，与模型的 no 不一致）
    for i in insp:
        app_client.put(f"/api/inspections/{i['id']}/verdict", json={'passed': True})
    from app.vlm.base import MockVlm
    rep = eval_vlm.evaluate(app_client.ctx.db, app_client.ctx.cfg, provider=MockVlm('yes'))
    assert rep['samples'] == 2 and rep['correct'] == 2 and rep['accuracy'] == 1.0
    rep2 = eval_vlm.evaluate(app_client.ctx.db, app_client.ctx.cfg, provider=MockVlm('no'))
    assert rep2['correct'] == 0 and len(rep2['errors']) == 2 and rep2['by_waypoint']['点3']['accuracy'] == 0
    assert eval_vlm.evaluate(app_client.ctx.db, app_client.ctx.cfg, provider=MockVlm('yes'), days=0)['samples'] in (0, 2)
