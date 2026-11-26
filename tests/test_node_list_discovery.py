"""Discovery also uses the start node's node list (the field mesh, 2026-11-26).

The four nodes near the laptop only listed each other as neighbours; the rest of the mesh
was reached some other way (a tunnel, or links without names). The start node's node
list still names them, and their own neighbour lists usually link them back in."""

from aredn_fleet.discovery import crawl
from aredn_fleet.topology import hop_distances, plan_rollout

# name -> [(neighbour, link type)]; "far-*" link back to the near set only from their end.
MESH = {
    "green": [("blue", "RF"), ("mant", "RF")],
    "blue": [("mant", "RF"), ("green", "RF")],
    "mant": [("green", "RF"), ("blue", "RF"), ("black", "RF")],
    "black": [("mant", "RF")],
    "far-a": [("mant", "TUN"), ("far-b", "RF")],
    "far-b": [("far-a", "RF")],
    "far-c": [],  # answers, but nothing read links to it
}
NODE_LIST = sorted(MESH)  # LAN devices (cameras etc.) are not in the node list


def fake(name):
    real = "green" if name == "localnode" else name
    if real == "gone":
        raise OSError("timed out")
    return {
        "node": real.upper(),
        "api_version": "2.0",
        "node_details": {"firmware_version": "4.26.10.0", "model": "test"},
        "link_info": {f"10.0.0.{i}": {"hostname": f"{n}.local.mesh", "linkType": t}
                      for i, (n, t) in enumerate(MESH[real])},
    }


def test_walk_alone_misses_the_far_nodes():
    res = crawl("localnode", fetch=fake)
    assert set(res.nodes) == {"green", "blue", "mant", "black"}
    assert res.from_list == []


def test_node_list_finds_and_connects_the_rest():
    res = crawl("localnode", fetch=fake, list_nodes=lambda n: [x.upper() for x in NODE_LIST])
    assert set(res.nodes) == set(MESH)
    assert res.from_list == ["far-a", "far-b", "far-c"]
    dist = hop_distances(res.topology.adjacency(), "green")
    assert dist["far-a"] == 2 and dist["far-b"] == 3
    assert "far-c" not in dist
    # Upgrades skip the node with no known path; the rest still go edge-inward.
    plan = plan_rollout(res.topology, "green", sorted(res.nodes))
    assert "far-c" in plan.unreachable
    order = [n for wave in plan.waves for n in wave]
    assert order.index("far-b") < order.index("far-a") < order.index("mant")


def test_listed_node_that_does_not_answer_is_reported():
    res = crawl("localnode", fetch=fake, list_nodes=lambda n: NODE_LIST + ["gone"])
    assert "gone" in res.errors


def test_node_list_failure_is_harmless():
    def broken(n):
        raise OSError("no nodes=1 on this firmware")
    res = crawl("localnode", fetch=fake, list_nodes=broken)
    assert set(res.nodes) == {"green", "blue", "mant", "black"}
