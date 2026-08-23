import json
import typing as t

from sqlmesh.core.state_sync import StateSync
from sqlmesh.core.snapshot import Snapshot
from sqlmesh.utils.date import now, to_tstz
from sqlmesh.utils.pydantic import _expression_encoder
from sqlmesh.core.state_sync import Versions
from sqlmesh.core.state_sync.common import (
    EnvironmentsChunk,
    SnapshotsChunk,
    VersionsChunk,
    EnvironmentWithStatements,
    StateStream,
)
from sqlmesh.core.console import Console
from pathlib import Path
from sqlmesh.core.console import NoopConsole

import json_stream
from json_stream import streamable_dict, to_standard_types, streamable_list
from json_stream.writer import StreamableDict
from json_stream.base import StreamingJSONObject
from json_stream.dump import JSONStreamEncoder
from sqlmesh.utils.errors import SQLMeshError
from sqlglot import exp
from sqlmesh.utils.pydantic import DEFAULT_ARGS as PYDANTIC_DEFAULT_ARGS, PydanticModel


class SQLMeshJSONStreamEncoder(JSONStreamEncoder):
    def default(self, obj: t.Any) -> t.Any:
        if isinstance(obj, exp.Expr):
            return _expression_encoder(obj)

        return super().default(obj)


def _dump_pydantic_model(model: PydanticModel) -> t.Dict[str, t.Any]:
    dump_args: t.Dict[str, t.Any] = PYDANTIC_DEFAULT_ARGS
    return model.model_dump(mode="json", **dump_args)


def _export(state_stream: StateStream, importable: bool, console: Console) -> StreamableDict:
    """
    Return the state in a format 'json_stream' can stream to a file

    Args:
        state_stream: A stream of state to export
        console: A Console instance to print progress to
    """

    @streamable_list
    def _dump_snapshots(
        snapshot_stream: t.Iterable[Snapshot],
    ) -> t.Iterator[t.Dict[str, t.Any]]:
        console.update_state_export_progress(snapshot_count=0)
        for idx, snapshot in enumerate(snapshot_stream):
            yield _dump_pydantic_model(snapshot)
            console.update_state_export_progress(snapshot_count=idx + 1)

    @streamable_dict
    def _dump_environments(
        environment_stream: t.Iterable[EnvironmentWithStatements],
    ) -> t.Iterator[t.Tuple[str, t.Any]]:
        console.update_state_export_progress(environment_count=0)
        for idx, env in enumerate(environment_stream):
            yield env.environment.name, _dump_pydantic_model(env)
            console.update_state_export_progress(environment_count=idx + 1)

    @streamable_dict
    def _do_export() -> t.Iterator[t.Tuple[str, t.Any]]:
        yield "metadata", {"timestamp": to_tstz(now()), "file_version": 1, "importable": importable}

        for state_chunk in state_stream:
            if isinstance(state_chunk, VersionsChunk):
                versions = _dump_pydantic_model(state_chunk.versions)
                yield "versions", versions
                console.update_state_export_progress(
                    version_count=len(versions), versions_complete=True
                )

            if isinstance(state_chunk, SnapshotsChunk):
                yield "snapshots", _dump_snapshots(state_chunk)
                console.update_state_export_progress(snapshots_complete=True)

            if isinstance(state_chunk, EnvironmentsChunk):
                yield "environments", _dump_environments(state_chunk)
                console.update_state_export_progress(environments_complete=True)

    return _do_export()


def _import(
    state_sync: StateSync, data: t.Callable[[], StreamingJSONObject], clear: bool, console: Console
) -> None:
    """
    Load the state defined by the :data into the supplied :state_sync. The data is in the same format as written by dump()

    Args:
        state_sync: The StateSync that the user has requested to dump state from
        data: A factory function that produces new streaming JSON reader attached to the file we are loading state from.
            This is so each section of the file can have its own reader which allows it to be read in isolation / out-of-order
            This puts less reliance on downstream consumers performing operations in a certain order
        clear: Whether or not to clear the existing state before writing the new state
        console: A Console instance to print progress to
    """

    def _load_snapshots() -> t.Iterator[Snapshot]:
        stream = data()["snapshots"]

        console.update_state_import_progress(snapshot_count=0)
        for idx, raw_snapshot in enumerate(stream):
            snapshot = Snapshot.model_validate(to_standard_types(raw_snapshot))
            yield snapshot
            console.update_state_import_progress(snapshot_count=idx + 1)

        console.update_state_import_progress(snapshots_complete=True)

    def _load_environments() -> t.Iterator[EnvironmentWithStatements]:
        stream = data()["environments"]

        console.update_state_import_progress(environment_count=0)
        for idx, (_, raw_environment) in enumerate(stream.items()):
            environment = EnvironmentWithStatements.model_validate(
                to_standard_types(raw_environment)
            )
            yield environment
            console.update_state_import_progress(environment_count=idx + 1)

        console.update_state_import_progress(environments_complete=True)

    metadata = to_standard_types(data()["metadata"])

    timestamp = metadata["timestamp"]
    if not isinstance(timestamp, str):
        raise ValueError(f"'timestamp' contains an invalid value. Expecting str, got: {timestamp}")
    console.update_state_import_progress(
        timestamp=timestamp, state_file_version=metadata["file_version"]
    )

    versions = Versions.model_validate(to_standard_types(data()["versions"]))

    stream = StateStream.from_iterators(
        versions=versions, snapshots=_load_snapshots(), environments=_load_environments()
    )

    console.update_state_import_progress(versions=versions)

    state_sync.import_(stream, clear=clear)


def export_state(
    state_sync: StateSync,
    output_file: Path,
    local_snapshots: t.Optional[t.Dict[str, Snapshot]] = None,
    environment_names: t.Optional[t.List[str]] = None,
    console: t.Optional[Console] = None,
) -> None:
    console = console or NoopConsole()

    state_stream = (
        StateStream.from_iterators(
            versions=state_sync.get_versions(),
            snapshots=iter(local_snapshots.values()),
            environments=iter([]),
        )
        if local_snapshots
        else state_sync.export(environment_names=environment_names)
    )

    importable = False if local_snapshots else True

    json_stream = _export(state_stream=state_stream, importable=importable, console=console)
    with output_file.open(mode="w", encoding="utf8") as fh:
        json.dump(json_stream, fh, indent=2, cls=SQLMeshJSONStreamEncoder)


def import_state(
    state_sync: StateSync,
    input_file: Path,
    clear: bool = False,
    console: t.Optional[Console] = None,
) -> None:
    console = console or NoopConsole()

    # we need to peek into the file to figure out what state version we are dealing with
    with input_file.open("r", encoding="utf8") as fh:
        stream = json_stream.load(fh)
        if not isinstance(stream, StreamingJSONObject):
            raise SQLMeshError(f"Expected JSON object, got: {type(stream)}")

        try:
            metadata = stream["metadata"].persistent()
        except KeyError:
            raise SQLMeshError("Expecting a 'metadata' key to be present")

        if not isinstance(metadata, StreamingJSONObject):
            raise SQLMeshError("Expecting the 'metadata' key to contain an object")

        file_version = metadata.get("file_version")
        if file_version is None:
            raise SQLMeshError("Unable to determine state file format version from the input file")

        try:
            int(file_version)
        except ValueError:
            raise SQLMeshError(f"Unable to parse state file format version: {file_version}")

        if not metadata.get("importable", False):
            # this can happen if the state file was created from local unversioned snapshots that were not sourced from the project state database
            raise SQLMeshError("State file is marked as not importable. Aborting")

    handles: t.List[t.TextIO] = []

    def _new_handle() -> StreamingJSONObject:
        handle = input_file.open("r", encoding="utf8")
        handles.append(handle)
        stream = json_stream.load(handle)
        assert isinstance(stream, StreamingJSONObject)
        return stream

    try:
        _import(state_sync=state_sync, data=_new_handle, clear=clear, console=console)
    finally:
        for handle in handles:
            handle.close()
