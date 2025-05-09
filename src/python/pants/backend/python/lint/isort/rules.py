# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from dataclasses import dataclass

from pants.backend.python.lint.isort.skip_field import SkipIsortField
from pants.backend.python.lint.isort.subsystem import Isort
from pants.backend.python.target_types import PythonSourceField
from pants.backend.python.util_rules import pex
from pants.backend.python.util_rules.pex import (
    VenvPexProcess,
    create_venv_pex,
    determine_venv_pex_resolve_info,
)
from pants.core.goals.fmt import FmtResult, FmtTargetsRequest
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.partitions import PartitionerType
from pants.engine.fs import MergeDigests
from pants.engine.intrinsics import merge_digests
from pants.engine.process import ProcessExecutionFailure, execute_process_or_raise
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.target import FieldSet, Target
from pants.option.global_options import KeepSandboxes
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class IsortFieldSet(FieldSet):
    required_fields = (PythonSourceField,)

    source: PythonSourceField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipIsortField).value


class IsortRequest(FmtTargetsRequest):
    field_set_type = IsortFieldSet
    tool_subsystem = Isort
    partitioner_type = PartitionerType.DEFAULT_SINGLE_PARTITION


def generate_argv(
    source_files: tuple[str, ...], isort: Isort, *, is_isort5: bool
) -> tuple[str, ...]:
    args = [*isort.args]
    if is_isort5 and len(isort.config) == 1:
        explicitly_configured_config_args = [
            arg
            for arg in isort.args
            if (
                arg.startswith("--sp")
                or arg.startswith("--settings-path")
                or arg.startswith("--settings-file")
                or arg.startswith("--settings")
            )
        ]
        # TODO: Deprecate manually setting this option, but wait until we deprecate
        #  `[isort].config` to be a string rather than list[str] option.
        if not explicitly_configured_config_args:
            args.append(f"--settings={isort.config[0]}")
    args.extend(source_files)
    return tuple(args)


@rule(desc="Format with isort", level=LogLevel.DEBUG)
async def isort_fmt(
    request: IsortRequest.Batch, isort: Isort, keep_sandboxes: KeepSandboxes
) -> FmtResult:
    isort_pex_get = create_venv_pex(**implicitly(isort.to_pex_request()))
    config_files_get = find_config_file(isort.config_request(request.snapshot.dirs))
    isort_pex, config_files = await concurrently(isort_pex_get, config_files_get)

    # Isort 5+ changes how config files are handled. Determine which semantics we should use.
    is_isort5 = False
    if isort.config:
        isort_pex_info = await determine_venv_pex_resolve_info(isort_pex)
        isort_info = isort_pex_info.find("isort")
        is_isort5 = isort_info is not None and isort_info.version.major >= 5

    input_digest = await merge_digests(
        MergeDigests((request.snapshot.digest, config_files.snapshot.digest))
    )
    description = f"Run isort on {pluralize(len(request.files), 'file')}."
    result = await execute_process_or_raise(
        **implicitly(
            VenvPexProcess(
                isort_pex,
                argv=generate_argv(request.files, isort, is_isort5=is_isort5),
                input_digest=input_digest,
                output_files=request.files,
                description=description,
                level=LogLevel.DEBUG,
            )
        )
    )

    if b"Failed to pull configuration information" in result.stderr:
        raise ProcessExecutionFailure(
            -1,
            result.stdout,
            result.stderr,
            description,
            keep_sandboxes=keep_sandboxes,
        )

    return await FmtResult.create(request, result)


def rules():
    return (
        *collect_rules(),
        *IsortRequest.rules(),
        *pex.rules(),
    )
