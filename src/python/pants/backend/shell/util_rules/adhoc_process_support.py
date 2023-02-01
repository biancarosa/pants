# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import dataclasses
import itertools
import logging
import os
import re
import shlex
from dataclasses import dataclass
from textwrap import dedent  # noqa: PNT20

from pants.backend.shell.subsystems.shell_setup import ShellSetup
from pants.backend.shell.target_types import (
    ShellCommandExecutionDependenciesField,
    ShellCommandOutputDependenciesField,
    ShellCommandOutputDirectoriesField,
    ShellCommandOutputFilesField,
    ShellCommandOutputsField,
)
from pants.backend.shell.util_rules.builtin import BASH_BUILTIN_COMMANDS
from pants.base.deprecated import warn_or_error
from pants.build_graph.address import Address
from pants.core.goals.package import BuiltPackage, PackageFieldSet
from pants.core.target_types import FileSourceField
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.core.util_rules.system_binaries import (
    BashBinary,
    BinaryNotFoundError,
    BinaryPathRequest,
    BinaryPaths,
)
from pants.engine.addresses import Addresses, UnparsedAddressInputs
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest
from pants.engine.fs import EMPTY_DIGEST, CreateDigest, Digest, Directory, MergeDigests, Snapshot
from pants.engine.process import Process
from pants.engine.rules import Get, MultiGet, collect_rules, rule, rule_helper
from pants.engine.target import (
    FieldSetsPerTarget,
    FieldSetsPerTargetRequest,
    SourcesField,
    Target,
    TransitiveTargets,
    TransitiveTargetsRequest,
)
from pants.util.frozendict import FrozenDict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellCommandProcessRequest:
    description: str
    address: Address
    shell_name: str
    interactive: bool
    working_directory: str | None
    command: str
    timeout: int | None
    tools: tuple[str, ...]
    input_digest: Digest
    immutable_input_digests: FrozenDict[str, Digest] | None
    append_only_caches: FrozenDict[str, str] | None
    output_files: tuple[str, ...]
    output_directories: tuple[str, ...]
    fetch_env_vars: tuple[str, ...]
    supplied_env_var_values: FrozenDict[str, str] | None


@rule_helper
async def _execution_environment_from_dependencies(shell_command: Target) -> Digest:

    runtime_dependencies_defined = (
        shell_command.get(ShellCommandExecutionDependenciesField).value is not None
    )

    any_dependencies_defined = (
        shell_command.get(ShellCommandOutputDependenciesField).value is not None
    )

    # If we're specifying the `dependencies` as relevant to the execution environment, then include
    # this command as a root for the transitive dependency search for execution dependencies.
    maybe_this_target = (shell_command.address,) if not runtime_dependencies_defined else ()

    # Always include the execution dependencies that were specified
    if runtime_dependencies_defined:
        runtime_dependencies = await Get(
            Addresses,
            UnparsedAddressInputs,
            shell_command.get(ShellCommandExecutionDependenciesField).to_unparsed_address_inputs(),
        )
    elif any_dependencies_defined:
        runtime_dependencies = Addresses()
        warn_or_error(
            "2.17.0.dev0",
            (
                "Using `dependencies` to specify execution-time dependencies for "
                "`experimental_shell_command` "
            ),
            (
                "To clear this warning, use the `output_dependencies` and `execution_dependencies`"
                "fields. Set `execution_dependencies=()` if you have no execution-time "
                "dependencies."
            ),
            print_warning=True,
        )
    else:
        runtime_dependencies = Addresses()

    transitive = await Get(
        TransitiveTargets,
        TransitiveTargetsRequest(itertools.chain(maybe_this_target, runtime_dependencies)),
    )

    all_dependencies = (
        *(i for i in transitive.roots if i is not shell_command),
        *transitive.dependencies,
    )

    sources, pkgs_per_target = await MultiGet(
        Get(
            SourceFiles,
            SourceFilesRequest(
                sources_fields=[tgt.get(SourcesField) for tgt in all_dependencies],
                for_sources_types=(SourcesField, FileSourceField),
                enable_codegen=True,
            ),
        ),
        Get(
            FieldSetsPerTarget,
            FieldSetsPerTargetRequest(PackageFieldSet, all_dependencies),
        ),
    )

    packages = await MultiGet(
        Get(BuiltPackage, PackageFieldSet, field_set) for field_set in pkgs_per_target.field_sets
    )

    dependencies_digest = await Get(
        Digest, MergeDigests([sources.snapshot.digest, *(pkg.digest for pkg in packages)])
    )

    return dependencies_digest


def _parse_outputs_from_command(
    shell_command: Target, description: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    outputs = shell_command.get(ShellCommandOutputsField).value or ()
    output_files = shell_command.get(ShellCommandOutputFilesField).value or ()
    output_directories = shell_command.get(ShellCommandOutputDirectoriesField).value or ()
    if outputs and (output_files or output_directories):
        raise ValueError(
            "Both new-style `output_files` or `output_directories` and old-style `outputs` were "
            f"specified in {description}. To fix, move all values from `outputs` to "
            "`output_files` or `output_directories`."
        )
    elif outputs:
        output_files = tuple(f for f in outputs if not f.endswith("/"))
        output_directories = tuple(d for d in outputs if d.endswith("/"))
    return output_files, output_directories


def _shell_tool_safe_env_name(tool_name: str) -> str:
    """Replace any characters not suitable in an environment variable name with `_`."""
    return re.sub(r"\W", "_", tool_name)


@rule_helper
async def _shell_command_tools(
    shell_setup: ShellSetup.EnvironmentAware, tools: tuple[str, ...], rationale: str
) -> dict[str, str]:

    search_path = shell_setup.executable_search_path
    tool_requests = [
        BinaryPathRequest(
            binary_name=tool,
            search_path=search_path,
        )
        for tool in sorted({*tools, *["mkdir", "ln"]})
        if tool not in BASH_BUILTIN_COMMANDS
    ]
    tool_paths = await MultiGet(
        Get(BinaryPaths, BinaryPathRequest, request) for request in tool_requests
    )

    paths: dict[str, str] = {}

    for binary, tool_request in zip(tool_paths, tool_requests):
        if binary.first_path:
            paths[_shell_tool_safe_env_name(tool_request.binary_name)] = binary.first_path.path
        else:
            raise BinaryNotFoundError.from_request(
                tool_request,
                rationale=rationale,
            )

    return paths


@rule
async def prepare_shell_command_process(
    shell_setup: ShellSetup.EnvironmentAware,
    shell_command: ShellCommandProcessRequest,
    bash: BashBinary,
) -> Process:

    description = shell_command.description
    address = shell_command.address
    shell_name = shell_command.shell_name
    interactive = shell_command.interactive
    if not interactive:
        working_directory = _parse_working_directory(shell_command.working_directory or "", address)
    elif shell_command.working_directory is not None:
        working_directory = shell_command.working_directory
    else:
        raise ValueError("Working directory must be not be `None` for interactive processes.")
    command = shell_command.command
    timeout: int | None = shell_command.timeout
    tools = shell_command.tools
    output_files = shell_command.output_files
    output_directories = shell_command.output_directories
    fetch_env_vars = shell_command.fetch_env_vars
    supplied_env_vars = shell_command.supplied_env_var_values or FrozenDict()
    append_only_caches = shell_command.append_only_caches or FrozenDict()
    immutable_input_digests = shell_command.immutable_input_digests

    if interactive:
        command_env = {
            "CHROOT": "{chroot}",
        }
    else:
        resolved_tools = await _shell_command_tools(shell_setup, tools, f"execute {description}")
        tools = tuple(tool for tool in sorted(resolved_tools))

        command_env = {"TOOLS": " ".join(tools), **resolved_tools}

    extra_env = await Get(EnvironmentVars, EnvironmentVarsRequest(fetch_env_vars))
    command_env.update(extra_env)

    if supplied_env_vars:
        command_env.update(supplied_env_vars)

    input_snapshot = await Get(Snapshot, Digest, shell_command.input_digest)

    if interactive or not working_directory or working_directory in input_snapshot.dirs:
        # Needed to ensure that underlying filesystem does not change during run
        work_dir = EMPTY_DIGEST
    else:
        work_dir = await Get(Digest, CreateDigest([Directory(working_directory)]))

    input_digest = await Get(Digest, MergeDigests([shell_command.input_digest, work_dir]))

    if interactive:
        _working_directory = working_directory or "."
        relpath = os.path.relpath(
            _working_directory or ".", start="/" if os.path.isabs(_working_directory) else "."
        )
        boot_script = f"cd {shlex.quote(relpath)}; " if relpath != "." else ""
    else:
        # Setup bin_relpath dir with symlinks to all requested tools, so that we can use PATH, force
        # symlinks to avoid issues with repeat runs using the __run.sh script in the sandbox.
        bin_relpath = ".bin"
        boot_script = ";".join(
            dedent(
                f"""\
                $mkdir -p {bin_relpath}
                for tool in $TOOLS; do $ln -sf ${{!tool}} {bin_relpath}; done
                export PATH="$PWD/{bin_relpath}"
                """
            ).split("\n")
        )

    proc = Process(
        argv=(bash.path, "-c", boot_script + command, shell_name),
        description=f"Running {description}",
        env=command_env,
        input_digest=input_digest,
        output_directories=output_directories,
        output_files=output_files,
        timeout_seconds=timeout,
        working_directory=working_directory,
        append_only_caches=append_only_caches,
        immutable_input_digests=immutable_input_digests,
    )

    if not interactive:
        return _output_at_build_root(proc, bash)
    else:
        # `InteractiveProcess`es don't need to be wrapped since files aren't being captured.
        return proc


def _output_at_build_root(process: Process, bash: BashBinary) -> Process:

    working_directory = process.working_directory or ""

    output_directories = process.output_directories
    output_files = process.output_files
    if working_directory:
        output_directories = tuple(os.path.join(working_directory, d) for d in output_directories)
        output_files = tuple(os.path.join(working_directory, d) for d in output_files)

    cd = f"cd {shlex.quote(working_directory)} && " if working_directory else ""
    shlexed_argv = " ".join(shlex.quote(arg) for arg in process.argv)
    new_argv = (bash.path, "-c", f"{cd}{shlexed_argv}")

    return dataclasses.replace(
        process,
        argv=new_argv,
        working_directory=None,
        output_directories=output_directories,
        output_files=output_files,
    )


def _parse_working_directory(workdir_in: str, address: Address) -> str:
    """Convert the `workdir` field into something that can be understood by `Process`."""

    reldir = address.spec_path

    if workdir_in == ".":
        return reldir
    elif workdir_in.startswith("./"):
        return os.path.join(reldir, workdir_in[2:])
    elif workdir_in.startswith("/"):
        return workdir_in[1:]
    else:
        return workdir_in


def rules():
    return collect_rules()
