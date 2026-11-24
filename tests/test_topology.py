from aredn_fleet.discovery import crawl
from aredn_fleet.topology import Topology, plan_rollout


def chain(*names, kind="RF"):
    t = Topology()
    for a, b in zip(names, names[1:]):
        t.add_link(a, b, kind)
    return t


def flat(plan):
    return [n for w in plan.waves for n in w]


def test_chain_goes_outermost_first_root_last():
    t = chain("root", "a", "b", "c")
    plan = plan_rollout(t, "root", ["root", "a", "b", "c"])
    assert plan.waves == [["c"], ["b"], ["a"], ["root"]]


def test_tree_parallel_leaves_share_a_wave():
    t = Topology()
    t.add_link("root", "hub")
    for leaf in ("l1", "l2", "l3"):
        t.add_link("hub", leaf)
    plan = plan_rollout(t, "root", ["hub", "l1", "l2", "l3"], max_parallel=3)
    assert sorted(plan.waves[0]) == ["l1", "l2", "l3"]
    assert plan.waves[1] == ["hub"]


def test_redundant_path_node_not_held_back():
    # root - a - c and root - b - c : a and b each have a backup, neither cuts c.
    t = Topology()
    for a, b in [("root", "a"), ("root", "b"), ("a", "c"), ("b", "c")]:
        t.add_link(a, b)
    plan = plan_rollout(t, "root", ["a", "b", "c"], max_parallel=3)
    # a and b may not go down together (jointly they cut c), c is farthest.
    order = flat(plan)
    assert order[0] == "c"
    assert all(not {"a", "b"} <= set(w) for w in plan.waves)


def test_joint_cut_prevents_parallel_when_target_pending():
    t = Topology()
    for a, b in [("root", "a"), ("root", "b"), ("a", "c"), ("b", "c")]:
        t.add_link(a, b)
    plan = plan_rollout(t, "root", ["a", "b"], max_parallel=2)
    assert all(len(w) == 1 for w in plan.waves)


def test_dependent_selected_node_is_done_before_its_gateway():
    t = Topology()
    t.add_link("root", "relay")
    t.add_link("relay", "far")
    t.add_link("root", "other")
    plan = plan_rollout(t, "root", ["relay", "far", "other"], max_parallel=2)
    order = flat(plan)
    assert order.index("far") < order.index("relay")


def test_unselected_dependent_produces_warning():
    t = chain("root", "relay", "far")
    plan = plan_rollout(t, "root", ["relay"])
    assert plan.warnings and "far" in plan.warnings[0]


def test_unreachable_selected_nodes_are_skipped():
    t = chain("root", "a")
    t.add_link("island1", "island2")
    plan = plan_rollout(t, "root", ["a", "island1"])
    assert plan.unreachable == {"island1"}
    assert flat(plan) == ["a"]


def test_excluding_tunnels_changes_reachability():
    t = chain("root", "a")
    t.add_link("root", "remote", "TUN")
    plan = plan_rollout(t, "root", ["a", "remote"], exclude_link_kinds=["TUN"])
    assert plan.unreachable == {"remote"}


def test_crawl_with_fake_fetcher():
    mesh = {
        "localnode": "km6slf-hub",
        "km6slf-hub": ["km6slf-a", "km6slf-b"],
        "km6slf-a": ["km6slf-hub", "km6slf-c"],
        "km6slf-b": ["km6slf-hub"],
        "km6slf-c": ["km6slf-a"],
    }

    def fake(name):
        real = mesh[name] if name == "localnode" else name
        return {
            "node": real.upper(),
            "api_version": "2.0",
            "node_details": {"firmware_version": "4.26.7.0", "model": "test"},
            "link_info": {f"10.0.0.{i}": {"hostname": f"{n}.local.mesh", "linkType": "RF"}
                          for i, n in enumerate(mesh[real])},
        }

    res = crawl("localnode", fetch=fake)
    assert set(res.nodes) == {"km6slf-hub", "km6slf-a", "km6slf-b", "km6slf-c"}
    assert len(res.topology.links) == 3
    plan = plan_rollout(res.topology, "km6slf-hub", sorted(res.nodes))
    assert flat(plan)[0] == "km6slf-c" and flat(plan)[-1] == "km6slf-hub"
