import math

from app.planning.graph import NavGraph, quat_to_yaw, sort_ids
from mock_gateway.robot_sim import load_maps

MAP = load_maps()['map_demo_20260903_220000']


def test_quat_to_yaw_roundtrip():
    for yaw in (0.0, 0.5, -1.2, 3.0):
        q = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
        assert abs(quat_to_yaw(*q) - yaw) < 1e-9


def test_sort_ids_numeric():
    assert sort_ids(['10', '2', '1', '21', 'x']) == ['1', '2', '10', '21', 'x']


def test_graph_shortest_path_and_length():
    g = NavGraph.from_api(MAP)
    assert len(g) == 45
    p = g.shortest_path('1', '3')
    assert p == ['1', '2', '3']
    p2 = g.shortest_path('1', '20')
    assert p2[0] == '1' and p2[-1] == '20'
    # 环路上两个方向都能到，最短路应不长于半个周长
    assert g.path_length(p2) <= 44 + 1e-6
    assert g.shortest_path('5', '5') == ['5']
    assert g.components() == 1


def test_graph_branch_reachable():
    g = NavGraph.from_api(MAP)
    p = g.shortest_path('1', '45')            # 东侧死胡同末端
    assert p is not None and p[-1] == '45'
    assert g.shortest_path('1', 'nope') is None


def test_nearest():
    g = NavGraph.from_api(MAP)
    nid, d = g.nearest(0.1, -0.1)
    assert nid == '1' and d < 0.2


def test_from_rows_matches_api():
    import json
    rows = []
    for nid, w in MAP.items():
        p, q = w['pose']['position'], w['pose']['orientation']
        rows.append({'node_id': nid, 'x': p['x'], 'y': p['y'], 'z': p['z'], 'yaw': quat_to_yaw(q['x'], q['y'], q['z'], q['w']),
                     'neighbors': json.dumps(w['neighbors'])})
    g1, g2 = NavGraph.from_api(MAP), NavGraph.from_rows(rows)
    assert g1.shortest_path('1', '30') == g2.shortest_path('1', '30')
