#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from application.config_generator import render_law_command
from domain.defaults import Defaults, load_defaults


OUTPUT_FILENAME = "start-production.sh"
CONDA_ENVIRONMENT = "casino-py3.11"
REPO_ROOT = Path(__file__).resolve().parents[1]


def discover_configs(config_dir: Path) -> list[Path]:
    if not config_dir.exists():
        raise ValueError(f"config directory does not exist: {config_dir}")
    if not config_dir.is_dir():
        raise ValueError(f"config path is not a directory: {config_dir}")

    configs = sorted(path for path in config_dir.glob("*.yaml") if path.is_file())
    if not configs:
        raise ValueError(f"no YAML configs found in: {config_dir}")
    return configs


def config_reference(config_path: Path, repo_root: Path) -> str:
    resolved_config = config_path.resolve()
    resolved_root = repo_root.resolve()
    try:
        relative_path = resolved_config.relative_to(resolved_root)
    except ValueError:
        return str(resolved_config)
    return f"$PWD/{relative_path.as_posix()}"


def render_tmux_command(
    session_name: str,
    config_path: Path,
    repo_root: Path,
    defaults: Defaults,
) -> str:
    law_command = render_law_command(
        config_reference(config_path, repo_root),
        defaults.workflow,
        defaults.htcondor_extra_args,
    )
    inner_command = " && ".join(
        (
            "source ~/.bashrc",
            f"cd {shlex.quote(str(repo_root.resolve()))}",
            f"conda activate {shlex.quote(CONDA_ENVIRONMENT)}",
            "source ./setup.sh",
            law_command,
        )
    )
    return (
        f"tmux new-session -d -s {shlex.quote(session_name)} "
        f"bash -lc {shlex.quote(inner_command)}"
    )


def generate_launch_script(config_dir: Path, repo_root: Path) -> tuple[Path, int]:
    configs = discover_configs(config_dir)
    defaults = load_defaults(repo_root)
    commands = [
        render_tmux_command(str(index), config, repo_root, defaults)
        for index, config in enumerate(configs, start=1)
    ]
    output_path = config_dir / OUTPUT_FILENAME
    output_path.write_text(
        "#!/usr/bin/env bash\n\n" + "\n".join(commands) + "\n",
        encoding="utf-8",
    )
    return output_path, len(commands)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate detached tmux commands for CASINO sample productions."
    )
    parser.add_argument(
        "config_path",
        type=Path,
        help="Sample YAML config file or directory containing YAML configs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_path = args.config_path

    if input_path.is_file():
        if input_path.suffix.lower() != ".yaml":
            parser.error(f"config file must have a .yaml suffix: {input_path}")
        defaults = load_defaults(REPO_ROOT)
        print(render_tmux_command("1", input_path, REPO_ROOT, defaults))
        return 0

    try:
        output_path, command_count = generate_launch_script(input_path, REPO_ROOT)
    except ValueError as exc:
        parser.error(str(exc))

    noun = "command" if command_count == 1 else "commands"
    print(f"Wrote {command_count} production {noun} to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
