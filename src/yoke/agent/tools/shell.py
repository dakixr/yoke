"""Shell command building utilities for platform-appropriate execution."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import subprocess

COMMAND_TOOL_NAME = "exec_command"


def default_shell_executable(env: dict[str, str]) -> str:
    """Return the appropriate shell executable path for the current platform."""
    if os.name != "nt":
        return (
            env.get("YOKE_SHELL")
            or env.get("SHELL")
            or shutil.which("bash")
            or shutil.which("sh")
            or "/bin/bash"
        )
    if shell_override := env.get("YOKE_SHELL"):
        return shell_override
    if pwsh := shutil.which("pwsh.exe") or shutil.which("pwsh"):
        return pwsh
    if powershell := shutil.which("powershell.exe") or shutil.which("powershell"):
        return powershell
    return env.get("ComSpec") or "cmd.exe"


def build_shell_command(
    command: str,
    env: dict[str, str],
    *,
    shell: str | None = None,
    login: bool = True,
) -> list[str] | str:
    """Build a platform-appropriate shell command list for subprocess."""
    shell_exe = shell or default_shell_executable(env)
    if os.name != "nt":
        return [shell_exe, "-lc" if login else "-c", command]

    shell_name = Path(shell_exe).name.lower()
    if shell_name in {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}:
        return build_powershell_command(command, env, shell_exe, shell_name)
    if shell_name in {"cmd.exe", "cmd"}:
        return build_cmd_command(shell_exe, command)
    return [shell_exe, "-lc", command]


def build_cmd_command(shell_exe: str, command: str) -> str:
    """Build a cmd.exe command line without escaping the nested command."""
    prefix = subprocess.list2cmdline([shell_exe, "/d", "/s", "/c"])
    if command.lstrip().startswith('"'):
        command = f'"{command}"'
    return f"{prefix} {command}"


def build_powershell_command(
    command: str,
    env: dict[str, str],
    shell_exe: str,
    shell_name: str,
) -> list[str]:
    """Build a PowerShell command list for the given command and shell."""
    if shell_name in {"powershell.exe", "powershell"}:
        command = rewrite_powershell_command(command)
    env["YOKE_COMMAND_TOOL_COMMAND"] = command
    has_active_python_env = bool(env.get("VIRTUAL_ENV") or env.get("CONDA_PREFIX"))
    output_encoding_setup = powershell_output_encoding_setup()
    python_path_setup = prepend_python_alias_bin_to_path()
    heredoc_helper = powershell_heredoc_helper()
    if has_active_python_env:
        ps_command = (
            "$ErrorActionPreference = 'Stop'; "
            f"{output_encoding_setup}"
            f"{heredoc_helper}"
            f"{python_path_setup}"
            f"{invoke_expression_with_exit_propagation()}"
        )
    else:
        ps_command = (
            "$ErrorActionPreference = 'Stop'; "
            f"{output_encoding_setup}"
            f"{heredoc_helper}"
            "if (Test-Path -LiteralPath $PROFILE) { . $PROFILE }; "
            f"{python_path_setup}"
            f"{invoke_expression_with_exit_propagation()}"
        )
    return [
        shell_exe,
        "-NoLogo",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_command,
    ]


def powershell_output_encoding_setup() -> str:
    """Return PowerShell that uses UTF-8 without a BOM for native pipelines."""
    return (
        "$yokeUtf8NoBom = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::OutputEncoding = $yokeUtf8NoBom; "
        "$OutputEncoding = $yokeUtf8NoBom; "
    )


def powershell_heredoc_helper() -> str:
    """Return PowerShell that feeds heredocs to native stdin without a BOM."""
    return (
        "function global:Invoke-YokeHeredoc { "
        "param([string]$EncodedBody, [string]$CommandText); "
        "$errors = $null; "
        "$tokens = @([System.Management.Automation.PSParser]::Tokenize("
        "$CommandText, [ref]$errors) | Where-Object { "
        "$_.Type -in @('Operator','Command','CommandArgument',"
        "'CommandParameter','String','Variable') "
        "}); "
        "if ($tokens.Count -lt 1) { throw 'Heredoc command is empty' }; "
        "if ($tokens[0].Type -eq 'Operator' -and $tokens[0].Content -eq '&') { "
        "$tokens = @($tokens[1..($tokens.Count - 1)]) }; "
        "$fileName = if ($tokens[0].Type -eq 'Variable') { "
        "Get-Variable -Name $tokens[0].Content -ValueOnly "
        "} else { $tokens[0].Content }; "
        "if ($fileName -eq 'python' -or $fileName -eq 'python3') { "
        "$fileName = $env:YOKE_PYTHON_EXECUTABLE }; "
        "$cmd = '\"' + $fileName + '\"'; "
        "if ($tokens.Count -gt 1) { foreach ($token in "
        "$tokens[1..($tokens.Count - 1)]) { "
        "$argument = if ($token.Type -eq 'Variable') { "
        "Get-Variable -Name $token.Content -ValueOnly "
        "} else { $token.Content }; "
        "$cmd += ' \"' + $argument.Replace('\"', '\"\"') + '\"' "
        "} }; "
        "$tmp = [System.IO.Path]::GetTempFileName(); "
        "try { [System.IO.File]::WriteAllBytes($tmp, "
        "[System.Convert]::FromBase64String($EncodedBody)); "
        "$cmd += ' < \"' + $tmp + '\"'; "
        "& $env:ComSpec /d /s /c $cmd; "
        "$global:LASTEXITCODE = $LASTEXITCODE "
        "} finally { Remove-Item -LiteralPath $tmp "
        "-ErrorAction SilentlyContinue } "
        "}; "
    )


def prepend_python_alias_bin_to_path() -> str:
    """Return PowerShell that exposes yoke's Python as python/python3."""
    return (
        "if ($env:YOKE_PYTHON_EXECUTABLE) { "
        "function global:python { "
        "if ($MyInvocation.ExpectingInput) { "
        "$input | & $env:YOKE_PYTHON_EXECUTABLE @args "
        "} else { & $env:YOKE_PYTHON_EXECUTABLE @args } }; "
        "function global:python3 { "
        "if ($MyInvocation.ExpectingInput) { "
        "$input | & $env:YOKE_PYTHON_EXECUTABLE @args "
        "} else { & $env:YOKE_PYTHON_EXECUTABLE @args } } "
        "}; "
    )


def invoke_expression_with_exit_propagation() -> str:
    """Return PowerShell that preserves exceptions and native exit codes."""
    return (
        "$global:LASTEXITCODE = 0; "
        "$yokeErrorActionPreference = $ErrorActionPreference; "
        "$yokeErrorCount = $Error.Count; "
        "$yokePowerShellError = $null; "
        "try { "
        "$ErrorActionPreference = 'Continue'; "
        "Invoke-Expression $env:YOKE_COMMAND_TOOL_COMMAND; "
        "$yokeExitCode = $LASTEXITCODE; "
        "$yokeNewErrorCount = [Math]::Max(0, $Error.Count - $yokeErrorCount); "
        "if ($yokeNewErrorCount -gt 0) { "
        "$yokePowerShellError = $Error | "
        "Select-Object -First $yokeNewErrorCount | "
        "Where-Object { $_.FullyQualifiedErrorId -notlike 'NativeCommandError*' } | "
        "Select-Object -First 1 "
        "} "
        "} finally { "
        "$ErrorActionPreference = $yokeErrorActionPreference "
        "}; "
        "if ($null -ne $yokePowerShellError) { throw $yokePowerShellError }; "
        "if ($yokeExitCode -ne 0) { exit $yokeExitCode }"
    )


def rewrite_powershell_command(command: str) -> str:
    """Rewrite a command string to be compatible with PowerShell syntax."""
    command = rewrite_cmd_c_for_powershell(command)
    command = rewrite_legacy_powershell_chain_operators(command)
    command = rewrite_bash_heredocs(command)
    stripped = command.lstrip()
    if stripped.startswith("& "):
        return command
    quoted_command = re.match(
        r'^(?P<indent>\s*)(?P<quoted>(?P<quote>["\']).+?(?P=quote))(?=\s)',
        command,
    )
    if quoted_command is not None:
        indent = quoted_command.group("indent")
        quoted = quoted_command.group("quoted")
        remainder = command[quoted_command.end() :]
        return f"{indent}& {quoted}{remainder}"
    return command


def rewrite_cmd_c_for_powershell(command: str) -> str:
    """Run cmd /c payloads through cmd.exe so cmd quoting is preserved."""
    match = re.match(
        r"^(?P<indent>\s*)cmd(?:\.exe)?\s+/c\s+(?P<payload>.+)$",
        command,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return command
    payload = match.group("payload")
    quoted_payload = quote_powershell_single_quoted(payload)
    return f"{match.group('indent')}& $env:ComSpec /d /s /c {quoted_payload}"


def rewrite_legacy_powershell_chain_operators(command: str) -> str:
    """Replace bash-style && operators with PowerShell-compatible guards."""
    if "&&" not in command:
        return command
    parts = split_unquoted_token(command, "&&")
    if len(parts) == 1:
        return command
    rewritten = parts[-1].strip()
    for part in reversed(parts[:-1]):
        rewritten = (
            f"{part.strip()}; $yokeSegmentSucceeded = $?; "
            "if ($yokeSegmentSucceeded -or "
            "($LASTEXITCODE -eq 0 -and $Error.Count -gt 0 -and "
            "$Error[0].FullyQualifiedErrorId -like 'NativeCommandError*')) "
            f"{{ {rewritten} }}"
        )
    return rewritten


def split_unquoted_token(command: str, token: str) -> list[str]:
    """Split a command on tokens that are outside quoted strings."""
    parts: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote is not None:
            if char == "`":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if command.startswith("@'", index) or command.startswith('@"', index):
            terminator = f"\n{command[index + 1]}@"
            terminator_index = command.find(terminator, index + 2)
            if terminator_index == -1:
                index = len(command)
            else:
                index = terminator_index + len(terminator)
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if command.startswith(token, index):
            parts.append(command[start:index])
            index += len(token)
            start = index
            continue
        index += 1
    parts.append(command[start:])
    return parts


def split_last_unquoted_token(command: str, token: str) -> tuple[str, str] | None:
    """Split a command at its last unquoted token occurrence."""
    parts = split_unquoted_token(command, token)
    if len(parts) == 1:
        return None
    return token.join(parts[:-1]), parts[-1]


def quote_powershell_single_quoted(value: str) -> str:
    """Quote a string for use as a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def rewrite_bash_heredocs(command: str) -> str:
    """Rewrite simple bash heredocs as PowerShell stdin pipelines."""
    lines = command.splitlines()
    rewritten_lines: list[str] = []
    index = 0
    heredoc_pattern = re.compile(
        r"^(?P<prefix>.+?)\s*<<\s*(?P<quote>['\"]?)(?P<marker>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?P=quote)\s*$"
    )
    while index < len(lines):
        line = lines[index]
        match = heredoc_pattern.match(line)
        if match is None:
            rewritten_lines.append(line)
            index += 1
            continue

        marker = match.group("marker")
        body_start = index + 1
        body_end = body_start
        while body_end < len(lines) and lines[body_end] != marker:
            body_end += 1
        if body_end >= len(lines):
            rewritten_lines.append(line)
            index += 1
            continue

        prefix = match.group("prefix").rstrip()
        prelude = ""
        trailing_command = split_last_unquoted_token(prefix, ";")
        if trailing_command is not None:
            prelude, prefix = trailing_command
            prelude = f"{prelude}; "
            prefix = prefix.strip()
        body = "\n".join(lines[body_start:body_end])
        encoded_body = base64.b64encode(body.encode("utf-8")).decode("ascii")
        quoted_prefix = quote_powershell_single_quoted(prefix)
        rewritten_lines.append(
            f"{prelude}Invoke-YokeHeredoc '{encoded_body}' {quoted_prefix}; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
        )
        index = body_end + 1
    return "\n".join(rewritten_lines)
