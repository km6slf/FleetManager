from aredn_fleet.routing import (BABEL, DUAL, OLSR, UNKNOWN, migration_blocks, routing_from_daemons,
                                 routing_of)
from aredn_fleet.topology import Topology


def test_routing_from_version():
    assert routing_of("3.23.8.0") == OLSR
    assert routing_of("3.25.2.0") == OLSR
    assert routing_of("3.25.5.0") == DUAL and routing_of("3.25.10.0") == DUAL
    assert routing_of("4.26.1.0") == BABEL and routing_of("4.26.10.0") == BABEL
    assert routing_of("20261122-5e4131bf") == BABEL
    assert routing_of("babel-20250601-abcdef12") == BABEL
    assert routing_of("20250601-abcdef12") == UNKNOWN
    assert routing_of("") == UNKNOWN


def test_routing_from_daemons():
    assert routing_from_daemons("olsrd\nbabeld\n") == DUAL
    assert routing_from_daemons("olsrd\n") == OLSR
    assert routing_from_daemons("babeld\n") == BABEL
    assert routing_from_daemons("") is None


def chain():
    # root(babel) - mid - edge
    t = Topology()
    t.add_link("root", "mid", "RF")
    t.add_link("mid", "edge", "RF")
    return t


def test_olsr_only_cannot_jump_to_babel_only():
    b = migration_blocks(chain(), "root", ["edge"], "4.26.10.0",
                         {"root": DUAL, "mid": DUAL, "edge": OLSR})
    assert "OLSR + Babel release" in b["edge"]
    assert migration_blocks(chain(), "root", ["edge"], "3.25.10.0",
                            {"root": DUAL, "mid": DUAL, "edge": OLSR}) == {}


def test_babel_only_node_behind_olsr_only_node_would_be_unreachable():
    b = migration_blocks(chain(), "root", ["edge"], "4.26.10.0",
                         {"root": DUAL, "mid": OLSR, "edge": DUAL})
    assert "path back" in b["edge"]


def test_inner_node_going_babel_only_strands_olsr_nodes_behind_it():
    b = migration_blocks(chain(), "root", ["mid"], "4.26.10.0",
                         {"root": DUAL, "mid": DUAL, "edge": OLSR})
    assert "edge" in b["mid"] and "cut off" in b["mid"]


def test_all_dual_mesh_moves_freely():
    assert migration_blocks(chain(), "root", ["mid", "edge"], "4.26.10.0",
                            {"root": BABEL, "mid": DUAL, "edge": DUAL}) == {}


def test_redundant_paths_are_still_caught():
    t = Topology()
    for a, b in [("root", "a"), ("root", "b"), ("a", "edge"), ("b", "edge")]:
        t.add_link(a, b, "RF")
    p = {"root": DUAL, "a": DUAL, "b": DUAL, "edge": OLSR}
    assert migration_blocks(t, "root", ["a"], "4.26.10.0", p) == {}       # b still carries it
    both = migration_blocks(t, "root", ["a", "b"], "4.26.10.0", p)
    assert set(both) == {"a", "b"} and all("edge" in w for w in both.values())


def test_unknown_nightly_is_treated_cautiously():
    b = migration_blocks(chain(), "root", ["edge"], "4.26.10.0",
                         {"root": BABEL, "mid": BABEL, "edge": UNKNOWN},
                         versions={"edge": "20250101-abc1234"})
    assert "unknown for firmware 20250101-abc1234" in b["edge"]


def test_firmware_not_read_says_so_not_nightly():
    b = migration_blocks(chain(), "root", ["edge"], "4.26.10.0",
                         {"root": BABEL, "mid": BABEL, "edge": UNKNOWN}, versions={"edge": ""})
    assert "isn't known" in b["edge"] and "nightly" not in b["edge"]


def test_blocked_olsr_node_still_protects_its_path():
    # The OLSR-only edge node is selected but blocked, so it stays OLSR-only: the dual node in
    # front of it must not go Babel-only either.
    b = migration_blocks(chain(), "root", ["mid", "edge"], "4.26.10.0",
                         {"root": BABEL, "mid": DUAL, "edge": OLSR})
    assert "OLSR + Babel release" in b["edge"]
    assert "edge" in b["mid"] and "cut off" in b["mid"]


def test_only_the_last_bridge_before_olsr_nodes_is_held_back():
    t = Topology()
    for a, b in [("root", "a"), ("a", "b"), ("b", "edge")]:
        t.add_link(a, b, "RF")
    blk = migration_blocks(t, "root", ["a", "b"], "4.26.10.0",
                           {"root": BABEL, "a": DUAL, "b": DUAL, "edge": OLSR})
    assert set(blk) == {"b"} and "edge" in blk["b"]     # a can go Babel-only; b bridges for edge
