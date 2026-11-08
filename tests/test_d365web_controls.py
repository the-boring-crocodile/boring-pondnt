"""Controls beyond fields, buttons and grids: tree controls (web_tree).

The page route (_js) is stubbed: these pin what the client asks the page and when it clicks. The page
library's tree reading runs against the fake page in a browser (test_d365web_client.py), MODEL_LIB's
tree op in node (test_d365web_model_js.py), and both live on a dev box."""
import time

import pytest

from boring_pondnt.d365web_client import Config, D365WebError, WebClient, check_steps


class Page:
    def __init__(self):
        self.events = []

    def click(self, x, y):
        self.events.append(("click", x, y))

    def url(self):
        return "/?mi=SysConfiguration"


class Tree:
    """Roots Bank (collapsed, children Cheques and Fees once expanded) and Activity (a leaf, selected)."""

    def __init__(self):
        self.expanded, self.selected = set(), "5"
        self.nodes = {"5": (None, "Activity"), "28": (None, "Bank"), "29": ("28", "Cheques"), "30": ("28", "Fees")}
        self.calls = []

    def js(self, op, *args):
        self.calls.append((op, args))
        if op == "treeNode":
            tree, parent, label = args
            kids = [i for i, (p, _) in self.nodes.items() if p == parent and (p is None or p in self.expanded)]
            hits = [i for i in kids if self.nodes[i][1].lower() == label.lower()]
            if len(hits) != 1:
                return {"error": "not_found", "what": "node", "tree": "TreeKeys",
                        "options": [self.nodes[i][1] for i in kids]}
            i = hits[0]
            has_kids = any(p == i for p, _ in self.nodes.values())
            return {"tree": "TreeKeys", "id": i, "label": self.nodes[i][1], "x": 10, "y": int(i), "bx": 2, "by": int(i),
                    "expanded": (i in self.expanded) if has_kids else None, "selected": self.selected == i}
        if op == "scope":
            return {"scope": "form", "form": "SysConfiguration"}
        if op == "context":
            return {"forms": ["SysConfiguration"], "dialog": "", "record": {"rows": {"tree:TreeKeys": self.selected}},
                    "pane": {"tree_open": len(self.expanded)}, "fields": "x"}
        return []


def client(monkeypatch, tree, backend="model"):
    web = WebClient(Config(url="https://example.test", backend=backend))
    page = Page()
    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_js", tree.js)
    monkeypatch.setattr(web, "_wait_idle", lambda: True)
    monkeypatch.setattr(web, "_after_step", lambda before, **kw: kw)
    monkeypatch.setattr(time, "sleep", lambda *a: None)
    return web, page


def clicking(page, tree):
    real = page.click

    def click(x, y):  # a click does what D365 would do: the expand button (x 2) or the label
        real(x, y)
        node = str(y)
        if x == 2:
            tree.expanded.add(node)
        else:
            tree.selected = node
    page.click = click


@pytest.mark.parametrize("backend,via", [("model", "input"), ("input", None)])
def test_a_path_expands_its_parents_and_selects_the_last_node_by_clicks(monkeypatch, backend, via):
    tree = Tree()
    web, page = client(monkeypatch, tree, backend)
    clicking(page, tree)
    result = web._op_tree("", ["Bank", "Fees"])
    assert page.events == [("click", 2, 28), ("click", 10, 30)] and tree.selected == "30"
    assert result["tree"] == {"name": "TreeKeys", "selected": ["Bank", "Fees"]} and result.get("via") == via
    assert result["changed"] == ["record", "pane"]
    assert not any(op.startswith("model:") for op, _ in tree.calls)  # the tree's commands need node objects


def test_an_expanded_parent_is_not_clicked_again(monkeypatch):
    tree = Tree()
    tree.expanded.add("28")
    web, page = client(monkeypatch, tree)
    clicking(page, tree)
    web._op_tree("", ["Bank", "Cheques"])
    assert page.events == [("click", 10, 29)]  # a second click on the button would collapse it


def test_an_already_selected_node_sends_nothing_and_says_unchanged(monkeypatch):
    tree = Tree()
    web, page = client(monkeypatch, tree)
    result = web._op_tree("", ["Activity"])
    assert page.events == []
    assert result["unchanged"] is True


def test_a_missing_label_is_refused_with_the_labels_on_offer(monkeypatch):
    tree = Tree()
    web, page = client(monkeypatch, tree)
    clicking(page, tree)
    with pytest.raises(D365WebError) as caught:
        web._op_tree("", ["Bank", "Loans"])
    assert caught.value.code == "not_found" and "under Bank" in caught.value.message
    assert caught.value.details["options"] == ["Cheques", "Fees"]
    assert tree.selected == "5"  # nothing selected; the parent was expanded on the way


def test_an_ambiguous_label_is_refused():
    web = WebClient(Config(url="https://example.test"))
    try:
        web._js = lambda op, *a: {"error": "ambiguous", "what": "node", "tree": "T", "options": ["A", "A"]}
        web._ensure = lambda: Page()
        web._wait_idle = lambda: True
        with pytest.raises(D365WebError) as caught:
            web._op_tree("", ["A"])
        assert caught.value.code == "refused" and "more than one" in caught.value.message
    finally:
        web.shutdown()


@pytest.mark.parametrize("path", [[], "Bank", [""], ["Bank", 3], ["x"] * 21])
def test_a_bad_path_is_refused_before_anything_runs(path):
    with pytest.raises(D365WebError):
        check_steps([{"op": "tree", "path": path}])


def test_a_scenario_takes_a_tree_step():
    check_steps([{"op": "tree", "path": ["Bank", "${child}"], "tree": "TreeKeys"}], {"child": "Fees"})


# --- reference groups ---------------------------------------------------------------------------------

def test_a_reference_group_by_its_own_name_is_refused_with_its_fields(monkeypatch):
    web = WebClient(Config(url="https://example.test"))
    page = Page()
    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_js", lambda op, *a: [] if op == "messages" else {"scope": "form"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Worker", "role": "ReferenceGroup", "x": 1, "y": 2,
                                                    "parts": ["Worker_DirPerson_FK_Name"], "value": ""})
    with pytest.raises(D365WebError) as caught:
        web._op_set([("Worker", "Lars Giusti")])
    assert caught.value.code == "refused" and caught.value.details["parts"] == ["Worker_DirPerson_FK_Name"]
    assert page.events == []


# --- file upload ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["press", "set"])
def test_a_file_upload_control_is_refused_without_a_click(monkeypatch, op):
    web = WebClient(Config(url="https://example.test"))
    page = Page()
    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_js", lambda op_, *a: [] if op_ == "messages" else {"scope": "dialog"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "UploadControlBrowseButton", "x": 1, "y": 2,
                                                    "upload": "UploadControl", "value": ""})
    with pytest.raises(D365WebError) as caught:
        web._op_press("UploadControlBrowseButton", 0) if op == "press" else web._op_set([("UploadControl", "C:/x.pdf")])
    assert caught.value.code == "refused" and caught.value.details["upload"] == "UploadControl"
    assert "file picker" in caught.value.message and page.events == []
