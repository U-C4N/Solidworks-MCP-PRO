"""Scratch ownership, real image verification and route boundaries without CAD."""
import asyncio
from contextlib import nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from solidworks_mcp_pro.render_validation import (
    RenderValidationError,
    validate_render_live,
    validation_exit_code,
    verify_render_image,
)


class Document:
    def __init__(self, title, path=""):
        self.title, self.path = title, path

    def GetTitle(self):
        return self.title

    def GetPathName(self):
        return self.path


class Session:
    def __init__(self, documents=()):
        self.documents = list(documents)
        self._model = self.documents[0] if self.documents else None
        self.closed = []
        self.mutations = []
        self.failure = None
        self.volume = 8000
        self.close_works = True
        self._sw = SimpleNamespace(CloseDoc=self.close_doc)

    def list_documents(self):
        return {"ok": True, "count": len(self.documents),
                "documents": [{"title": d.GetTitle(), "path": d.GetPathName()} for d in self.documents]}

    def _open_documents(self):
        return list(self.documents)

    def run_guarded(self, function, **kwargs):
        self.mutations.append(function.__name__)
        return function(**kwargs)

    def new_part(self):
        self._model = Document("Part1")
        self.documents.append(self._model)
        if self.failure == "create_after_new_document":
            raise RuntimeError("creation failed after native document appeared")
        return {"ok": True, "title": self._model.GetTitle()}

    def add_box(self, **kwargs):
        assert kwargs == {"width_mm": 40, "height_mm": 25, "depth_mm": 8}
        if self.failure == "box":
            raise RuntimeError("box failed")
        return {"ok": True}

    def save_part(self, path):
        Path(path).write_bytes(b"native solid fixture double")
        self._model.path = path
        self._model.title = Path(path).name
        return {"ok": True, "path": path}

    def get_mass_properties(self):
        return {"ok": True, "mass_properties": {"volume_mm3": self.volume}}

    def close_doc(self, title):
        self.closed.append(title)
        if self.close_works:
            self.documents = [d for d in self.documents if d.GetTitle() != title]


def write_png(directory, width, height):
    directory.mkdir()
    path = directory / "probe.png"
    Image.new("RGB", (width, height), (32, 100, 210)).save(path)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class Backend:
    def __init__(self, session=None):
        self.session = session or Session()
        self.upstream = SimpleNamespace(_session=self.session)
        self.calls = []
        self.behavior = "success"
        self.render_started = asyncio.Event()
        self.release_render = asyncio.Event()
        self.was_closed = False

    async def _workflow_native(self, function):
        return function()

    async def visualize(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if operation != "render":
            return {"ok": True, "available": True}
        self.render_started.set()
        if self.behavior == "wait":
            await self.release_render.wait()
        if self.behavior == "macro_failure":
            raise RenderValidationError("VISUALIZE_MACRO_RUN_FAILED", "RunMacro2 returned false")
        size = (kwargs["width"], kwargs["height"]) if self.behavior != "wrong_size" else (16, 16)
        image = write_png(kwargs["output_dir"], *size)
        if self.behavior == "new_user_document":
            user_document = Document("UserPart", "C:/user/UserPart.sldprt")
            self.session.documents.append(user_document)
            self.session._model = user_document
        execution = "solidworks_in_process_macro" if self.behavior != "unknown_execution" else "unknown"
        return {"ok": True, "images": [image], "execution": execution}

    def close(self):
        self.was_closed = True


async def test_addin_acceptance_verifies_file_and_only_closes_its_fixture(tmp_path):
    backend = Backend()
    result = await validate_render_live(tmp_path / "run", backend=backend, lease_factory=nullcontext)
    assert result["ok"] and result["status"] == "passed" and result["render_verified"]
    assert result["addin_render_verified"] and result["macro_render_verified"]
    assert not result["project_render_verified"] and not result["alpha_tested"]
    assert result["images"][0]["width"] == 640
    assert result["images"][0]["height"] == 360
    assert len(result["images"][0]["sha256"]) == 64
    assert backend.session.closed == ["RenderProbe.sldprt"]
    assert not backend.session.documents and backend.session._model is None
    assert not backend.was_closed  # Injected backends remain caller-owned.
    assert json.loads(Path(result["evidence_path"]).read_text())["ok"]
    assert validation_exit_code(result) == 0


async def test_user_documents_block_before_any_mutation_or_render(tmp_path):
    user = Document("CustomerDesign", "C:/user/customer.sldasm")
    backend = Backend(Session([user]))
    result = await validate_render_live(tmp_path / "blocked", backend=backend, lease_factory=nullcontext)
    assert result["status"] == "blocked" and result["code"] == "OPEN_DOCUMENTS_PRESENT"
    assert not result["render_verified"] and validation_exit_code(result) == 2
    assert not backend.calls and not backend.session.mutations and not backend.session.closed
    assert backend.session._model is user and backend.session.documents == [user]
    assert result["cleanup"]["verified"] and not result["cleanup"]["attempted"]


async def test_incomplete_document_inventory_never_means_empty(tmp_path):
    backend = Backend()
    backend.session.list_documents = lambda: {"ok": True, "count": 0}
    result = await validate_render_live(tmp_path / "blocked", backend=backend, lease_factory=nullcontext)
    assert result["status"] == "blocked" and result["code"] == "DOCUMENT_INVENTORY_UNKNOWN"
    assert not backend.session.mutations and not backend.calls


async def test_partial_geometry_failure_closes_identified_unsaved_fixture(tmp_path):
    backend = Backend()
    backend.session.failure = "box"
    result = await validate_render_live(tmp_path / "failure", backend=backend, lease_factory=nullcontext)
    assert result["status"] == "failed" and not result["render_verified"]
    assert backend.session.closed == ["Part1"] and not backend.session.documents
    assert result["cleanup"]["verified"] and validation_exit_code(result) == 1


async def test_failed_creation_never_closes_an_unidentified_document(tmp_path):
    backend = Backend()
    backend.session.failure = "create_after_new_document"
    result = await validate_render_live(tmp_path / "failure", backend=backend, lease_factory=nullcontext)
    assert result["status"] == "indeterminate" and result["code"] == "FIXTURE_CLEANUP_UNVERIFIED"
    assert len(backend.session.documents) == 1 and not backend.session.closed
    assert not result["cleanup"]["verified"] and result["primary_failure"]["error"]


async def test_non_solid_fixture_fails_before_renderer(tmp_path):
    backend = Backend()
    backend.session.volume = 0
    result = await validate_render_live(tmp_path / "failure", backend=backend, lease_factory=nullcontext)
    assert result["code"] == "FIXTURE_VOLUME_MISMATCH" and not backend.calls
    assert result["cleanup"]["verified"]


async def test_macro_failure_is_not_accepted_or_silently_fallen_back(tmp_path):
    backend = Backend()
    backend.behavior = "macro_failure"
    result = await validate_render_live(tmp_path / "macro", backend=backend, lease_factory=nullcontext)
    assert result["code"] == "VISUALIZE_MACRO_RUN_FAILED" and result["status"] == "failed"
    assert not result["ok"] and not result["render_verified"]
    assert not result["project_render_verified"] and validation_exit_code(result) == 1
    assert [name for name, _ in backend.calls].count("render") == 1
    assert result["cleanup"]["verified"]


@pytest.mark.parametrize("behavior,code", [("wrong_size", "RENDER_OUTPUT_INVALID"),
                                            ("unknown_execution", "RENDER_ROUTE_UNVERIFIED")])
async def test_reported_render_success_needs_image_and_route_verification(tmp_path, behavior, code):
    backend = Backend()
    backend.behavior = behavior
    result = await validate_render_live(tmp_path / "failure", backend=backend, lease_factory=nullcontext)
    assert not result["ok"] and not result["render_verified"] and result["code"] == code
    assert result["cleanup"]["verified"]


async def test_closed_fixture_is_verified_and_foreign_new_document_is_left_alone(tmp_path):
    backend = Backend()
    backend.behavior = "new_user_document"
    result = await validate_render_live(tmp_path / "run", backend=backend, lease_factory=nullcontext)
    assert result["ok"] and result["cleanup"]["remaining_document_count"] == 1
    assert backend.session.closed == ["RenderProbe.sldprt"]
    assert backend.session._model.GetTitle() == "UserPart"
    assert backend.session.documents == [backend.session._model]


async def test_unverified_close_fails_overall_even_after_verified_png(tmp_path):
    backend = Backend()
    backend.session.close_works = False
    result = await validate_render_live(tmp_path / "run", backend=backend, lease_factory=nullcontext)
    assert result["render_verified"] and not result["ok"] and result["status"] == "indeterminate"
    assert result["code"] == "FIXTURE_CLEANUP_UNVERIFIED" and validation_exit_code(result) == 1


async def test_project_route_preserves_source_and_does_not_import_native_backend(tmp_path, monkeypatch):
    from solidworks_mcp_pro import backend as native
    monkeypatch.setattr(native, "NativeBackend", lambda: pytest.fail("Project route must not attach to CAD"))
    source = tmp_path / "source.svpj"
    source.write_bytes(b"source package double")
    original = source.read_bytes()

    def render(**kwargs):
        assert kwargs["project_path"] == source
        return {"ok": True, "renderer": {"native_execution": True},
                "images": [write_png(kwargs["output_directory"], kwargs["width"], kwargs["height"])]}

    result = await validate_render_live(tmp_path / "project", route="project", project_path=source,
        project_renderer=render, lease_factory=nullcontext)
    assert result["ok"] and result["project_render_verified"] and result["render_verified"]
    assert not result["addin_render_verified"] and not result["macro_render_verified"]
    assert result["execution"] == "native_offline_renderer_project_copy"
    assert result["source_project"]["unchanged"] and source.read_bytes() == original
    assert not result["cleanup"]["attempted"]


async def test_source_mutation_is_indeterminate_despite_decodable_project_render(tmp_path):
    source = tmp_path / "source.svpj"
    source.write_bytes(b"source")

    def render(**kwargs):
        source.write_bytes(b"changed by unrelated actor")
        return {"ok": True, "renderer": {"native_execution": True},
                "images": [write_png(kwargs["output_directory"], kwargs["width"], kwargs["height"])]}

    result = await validate_render_live(tmp_path / "project", route="project", project_path=source,
        project_renderer=render, lease_factory=nullcontext)
    assert result["render_verified"] and not result["ok"]
    assert result["status"] == "indeterminate" and result["code"] == "SOURCE_PROJECT_CHANGED"


async def test_cancellation_holds_lease_until_render_and_cleanup_finish(tmp_path):
    backend = Backend()
    backend.behavior = "wait"
    events = []

    class Lease:
        def __enter__(self):
            events.append("acquire")

        def __exit__(self, *args):
            assert not backend.session.documents
            events.append("release")

    task = asyncio.create_task(validate_render_live(tmp_path / "cancel", backend=backend, lease_factory=Lease))
    await backend.render_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not backend.session.closed and events == ["acquire"]
    backend.release_render.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["acquire", "release"]
    evidence = json.loads((tmp_path / "cancel" / "evidence.json").read_text())
    assert evidence["render_verified"] and evidence["cleanup"]["verified"]


def test_image_outside_job_or_changed_hash_cannot_pass(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    image = write_png(tmp_path / "elsewhere", 128, 128)
    with pytest.raises(RenderValidationError, match="inside this probe"):
        verify_render_image({"ok": True, "images": [image]}, owned, 128, 128)
    image = write_png(owned / "nested", 128, 128)
    image["sha256"] = "0" * 64
    with pytest.raises(RenderValidationError, match="hash changed"):
        verify_render_image({"ok": True, "images": [image]}, owned, 128, 128)


@pytest.mark.parametrize("kwargs", [
    {"route": "auto"}, {"width": True}, {"passes": 0}, {"width": 8192},
    {"denoiser": 1}, {"project_path": Path("existing.svpj")}, {"route": "project"},
])
async def test_invalid_request_has_no_native_or_filesystem_effect(tmp_path, kwargs):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        await validate_render_live(output, **kwargs)
    assert not output.exists()


async def test_existing_output_is_preserved(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "evidence.json"
    marker.write_text("retain this")
    with pytest.raises(ValueError, match="new absolute"):
        await validate_render_live(output)
    assert marker.read_text() == "retain this"


def test_cli_requires_live_before_invoking_any_probe(monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_render_live.py"
    spec = importlib.util.spec_from_file_location("render_live_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "validate_render_live", lambda *a, **k: pytest.fail("No live opt-in"))
    with pytest.raises(SystemExit) as exc:
        module.main([])
    assert exc.value.code == 2


@pytest.mark.parametrize("evidence", [
    {"ok": False, "render_verified": True, "status": "passed"},
    {"ok": True, "render_verified": False, "status": "passed"},
    {"ok": True, "render_verified": True, "status": "indeterminate"},
])
def test_cli_cannot_succeed_for_unverified_or_incomplete_acceptance(evidence):
    assert validation_exit_code(evidence) == 1
