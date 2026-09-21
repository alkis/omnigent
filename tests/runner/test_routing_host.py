"""Routing hosts for colocated children do not confer host ownership."""

from omnigent.entities import Conversation
from omnigent.runner.routing_host import session_routing_hosts


def conversation(session_id, *, parent=None, host=None, runner="runner_shared"):
    return Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=parent or session_id,
        parent_conversation_id=parent,
        runner_id=runner,
        host_id=host,
    )


class Store:
    def __init__(self, *conversations):
        self.rows = {conv.id: conv for conv in conversations}
        self.reads = []

    def get_conversations(self, ids):
        self.reads.append(set(ids))
        return {sid: self.rows[sid] for sid in ids if sid in self.rows}


def test_batch_resolves_nested_children_without_per_child_queries():
    root = conversation("root", host="host_a")
    parents = [conversation(f"parent_{i}", parent=root.id) for i in range(20)]
    children = [conversation(f"child_{i}", parent=p.id) for i, p in enumerate(parents)]
    store = Store(root, *parents, *children)

    assert session_routing_hosts(children, store) == {child.id: "host_a" for child in children}
    assert store.reads == [{p.id for p in parents}, {root.id}]
    assert all(child.host_id is None for child in children)


def test_loaded_ancestors_and_top_level_sessions_require_no_extra_reads():
    root = conversation("root", host="host_a")
    child = conversation("child", parent=root.id)
    local = conversation("local")
    store = Store()

    assert session_routing_hosts([root, child, local], store) == {
        "root": "host_a",
        "child": "host_a",
        "local": None,
    }
    assert store.reads == []


def test_runner_rebind_does_not_route_an_old_child_to_the_new_parent_host():
    root = conversation("root", host="host_new", runner="runner_new")
    child = conversation("child", parent=root.id)
    unbound = conversation("unbound", parent=root.id, runner=None)
    own_host = conversation("own", parent=root.id, host="host_own")

    assert session_routing_hosts([child, unbound, own_host], Store(root)) == {
        "child": None,
        "unbound": None,
        "own": "host_own",
    }


def test_missing_ancestors_and_cycles_stop_without_guessing_a_host():
    first = conversation("first", parent="second")
    second = conversation("second", parent=first.id)
    missing = conversation("missing", parent="deleted")
    store = Store(first, second)

    assert session_routing_hosts([first, missing], store) == {"first": None, "missing": None}
    assert store.reads == [{"second", "deleted"}]
