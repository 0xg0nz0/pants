# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pants.backend.java.subsystems.junit import JUnit
from pants.core.goals.generate_lockfiles import GenerateToolLockfileSentinel
from pants.core.goals.test import (
    TestDebugAdapterRequest,
    TestDebugRequest,
    TestExtraEnv,
    TestExtraEnvVarsField,
    TestFieldSet,
    TestRequest,
    TestResult,
    TestSubsystem,
)
from pants.core.target_types import FileSourceField
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.engine.addresses import Addresses
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest
from pants.engine.fs import Digest, DigestSubset, MergeDigests, PathGlobs, RemovePrefix, Snapshot
from pants.engine.process import (
    FallibleProcessResult,
    InteractiveProcess,
    Process,
    ProcessCacheScope,
)
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import SourcesField, TransitiveTargets, TransitiveTargetsRequest
from pants.engine.unions import UnionRule
from pants.jvm.classpath import Classpath
from pants.jvm.goals import lockfile
from pants.jvm.jdk_rules import JdkEnvironment, JdkRequest, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, GenerateJvmToolLockfileSentinel
from pants.jvm.subsystems import JvmSubsystem
from pants.jvm.target_types import (
    JunitTestSourceField,
    JunitTestTimeoutField,
    JvmDependenciesField,
    JvmJdkField,
)
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JunitTestFieldSet(TestFieldSet):
    required_fields = (
        JunitTestSourceField,
        JvmJdkField,
    )

    sources: JunitTestSourceField
    timeout: JunitTestTimeoutField
    jdk_version: JvmJdkField
    dependencies: JvmDependenciesField
    extra_env_vars: TestExtraEnvVarsField


class JunitTestRequest(TestRequest):
    # TODO: Remove the type-ignore after adding a `skip` option to the subsystem.
    tool_subsystem = JUnit  # type: ignore[assignment]
    field_set_type = JunitTestFieldSet


class JunitToolLockfileSentinel(GenerateJvmToolLockfileSentinel):
    resolve_name = JUnit.options_scope


@dataclass(frozen=True)
class TestSetupRequest:
    field_set: JunitTestFieldSet
    is_debug: bool


@dataclass(frozen=True)
class TestSetup:
    process: JvmProcess
    reports_dir_prefix: str


@rule(level=LogLevel.DEBUG)
async def setup_junit_for_target(
    request: TestSetupRequest,
    jvm: JvmSubsystem,
    junit: JUnit,
    test_subsystem: TestSubsystem,
    test_extra_env: TestExtraEnv,
) -> TestSetup:

    jdk, transitive_tgts = await MultiGet(
        Get(JdkEnvironment, JdkRequest, JdkRequest.from_field(request.field_set.jdk_version)),
        Get(TransitiveTargets, TransitiveTargetsRequest([request.field_set.address])),
    )

    lockfile_request = await Get(GenerateJvmLockfileFromTool, JunitToolLockfileSentinel())
    classpath, junit_classpath, files = await MultiGet(
        Get(Classpath, Addresses([request.field_set.address])),
        Get(ToolClasspath, ToolClasspathRequest(lockfile=lockfile_request)),
        Get(
            SourceFiles,
            SourceFilesRequest(
                (dep.get(SourcesField) for dep in transitive_tgts.dependencies),
                for_sources_types=(FileSourceField,),
                enable_codegen=True,
            ),
        ),
    )

    input_digest = await Get(Digest, MergeDigests((*classpath.digests(), files.snapshot.digest)))

    toolcp_relpath = "__toolcp"
    extra_immutable_input_digests = {
        toolcp_relpath: junit_classpath.digest,
    }

    reports_dir_prefix = "__reports_dir"
    reports_dir = f"{reports_dir_prefix}/{request.field_set.address.path_safe_spec}"

    # Classfiles produced by the root `junit_test` targets are the only ones which should run.
    user_classpath_arg = ":".join(classpath.root_args())

    # Cache test runs only if they are successful, or not at all if `--test-force`.
    cache_scope = (
        ProcessCacheScope.PER_SESSION if test_subsystem.force else ProcessCacheScope.SUCCESSFUL
    )

    extra_jvm_args: list[str] = []
    if request.is_debug:
        extra_jvm_args.extend(jvm.debug_args)

    field_set_extra_env = await Get(
        EnvironmentVars, EnvironmentVarsRequest(request.field_set.extra_env_vars.value or ())
    )

    process = JvmProcess(
        jdk=jdk,
        classpath_entries=[
            *classpath.args(),
            *junit_classpath.classpath_entries(toolcp_relpath),
        ],
        argv=[
            *extra_jvm_args,
            "org.junit.platform.console.ConsoleLauncher",
            *(("--classpath", user_classpath_arg) if user_classpath_arg else ()),
            *(("--scan-class-path", user_classpath_arg) if user_classpath_arg else ()),
            "--reports-dir",
            reports_dir,
            *junit.args,
        ],
        input_digest=input_digest,
        extra_env={**test_extra_env.env, **field_set_extra_env},
        extra_jvm_options=junit.jvm_options,
        extra_immutable_input_digests=extra_immutable_input_digests,
        output_directories=(reports_dir,),
        description=f"Run JUnit 5 ConsoleLauncher against {request.field_set.address}",
        timeout_seconds=request.field_set.timeout.calculate_from_global_options(test_subsystem),
        level=LogLevel.DEBUG,
        cache_scope=cache_scope,
        use_nailgun=False,
    )
    return TestSetup(process=process, reports_dir_prefix=reports_dir_prefix)


@rule(desc="Run JUnit", level=LogLevel.DEBUG)
async def run_junit_test(
    test_subsystem: TestSubsystem,
    batch: JunitTestRequest.Batch[JunitTestFieldSet, Any],
) -> TestResult:
    field_set = batch.single_element

    test_setup = await Get(TestSetup, TestSetupRequest(field_set, is_debug=False))
    process_result = await Get(FallibleProcessResult, JvmProcess, test_setup.process)
    reports_dir_prefix = test_setup.reports_dir_prefix

    xml_result_subset = await Get(
        Digest, DigestSubset(process_result.output_digest, PathGlobs([f"{reports_dir_prefix}/**"]))
    )
    xml_results = await Get(Snapshot, RemovePrefix(xml_result_subset, reports_dir_prefix))

    return TestResult.from_fallible_process_result(
        process_result,
        address=field_set.address,
        output_setting=test_subsystem.output,
        xml_results=xml_results,
    )


@rule(level=LogLevel.DEBUG)
async def setup_junit_debug_request(
    batch: JunitTestRequest.Batch[JunitTestFieldSet, Any]
) -> TestDebugRequest:
    setup = await Get(TestSetup, TestSetupRequest(batch.single_element, is_debug=True))
    process = await Get(Process, JvmProcess, setup.process)
    return TestDebugRequest(
        InteractiveProcess.from_process(process, forward_signals_to_process=False, restartable=True)
    )


@rule
async def setup_junit_debug_adapter_request(
    _: JunitTestRequest.Batch[JunitTestFieldSet, Any],
) -> TestDebugAdapterRequest:
    raise NotImplementedError(
        "Debugging JUnit tests using a debug adapter has not yet been implemented."
    )


@rule
def generate_junit_lockfile_request(
    _: JunitToolLockfileSentinel, junit: JUnit
) -> GenerateJvmLockfileFromTool:
    return GenerateJvmLockfileFromTool.create(junit)


def rules():
    return [
        *collect_rules(),
        *lockfile.rules(),
        UnionRule(TestFieldSet, JunitTestFieldSet),
        UnionRule(GenerateToolLockfileSentinel, JunitToolLockfileSentinel),
        *JunitTestRequest.rules(),
    ]
