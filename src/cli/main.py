from __future__ import annotations

import argparse
import getpass
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from application import DEFAULT_DRIVER_ROOT, CasinoApplication
from application.chain_resolver import (
    AmbiguousChain,
    DrpremixUnresolved,
    NoChainFound,
    WmlhegsUnresolved,
    resolve_chain,
)
from application.config_generator import ConfigWriter, build_config, parse_campaign_location, render_law_command
from application.config_lister import available_years, configs_for_year
from cli.prompts import QuestionaryPrompter
from domain.defaults import load_defaults
from infrastructure.auth_status import auth_status_lines
from infrastructure.das import DASClient, DASError
from infrastructure.mcm import fetch_text
from infrastructure.mcm_auth import MCM_BASE, McMAuthSession, default_cookiefile

CARDS_DIR = "cards/delphes"
_RUN_OPTIONS = ["Run1", "Run2", "Run3", "Run4"]
_REFINE_SEARCH = "↻ Refine search"
MCM_CHAIN_URL = "https://cms-pdmv-prod.web.cern.ch/mcm/chained_requests?prepid={prepid}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download McM setup scripts and derive step plans.")
    subparsers = parser.add_subparsers(dest="command", required=False)

    for name, help_text, overwrite_help in (
        ("download", "Download setup scripts from McM into artifacts/mcm-commands/<campaign>/<year>/<sample>/...",
         "Overwrite existing downloaded files"),
        ("build-step-runners", "Create a step plan for each production step", "Overwrite existing step plan files"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("config", nargs="?", help="Path to the sample config YAML file")
        sub.add_argument("--all", action="store_true", help="Process every config under configs/")
        sub.add_argument("--driver-root", default=str(DEFAULT_DRIVER_ROOT), help="Driver-command root directory")
        sub.add_argument("--overwrite", action="store_true", help=overwrite_help)
        if name == "download":
            sub.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
            sub.add_argument("--verify-certs", action="store_true", help="Enable TLS certificate verification")

    list_parser = subparsers.add_parser("list-configs", help="Browse existing configs by year")
    list_parser.add_argument("year", nargs="?", help="Config year directory, e.g. run3/2024")

    create = subparsers.add_parser("create-config", help="Create a sample config interactively")
    create.add_argument("--dataset")
    create.add_argument("--mode", choices=["full-chain", "delphes"])
    create.add_argument("--card")
    create.add_argument("--no-delphes-nano", action="store_true", dest="no_delphes_nano",
                        help="Delphes: emit only the delphes-tree step (skip NanoAOD ntuplization)")
    create.add_argument("--total-events", type=int, dest="total_events")
    create.add_argument("--memory", type=int)
    create.add_argument("--cores", type=int)
    create.add_argument("--events-per-job-mini", type=int, dest="events_per_job_mini")
    create.add_argument("--events-per-job-nano", type=int, dest="events_per_job_nano")
    create.add_argument("--gridpack")
    create.add_argument("--drpremix")
    create.add_argument("--generator", "--wmlhegs", dest="wmlhegs",
                        help="Generator request prep-id (wmLHEGS or direct GS; --wmlhegs is kept as an alias)")
    create.add_argument("--chain")
    create.add_argument("--output-root", dest="output_root")
    create.add_argument("--config-dir", dest="config_dir")
    create.add_argument("--force", action="store_true")
    create.add_argument("--run", action="store_true")
    create.add_argument("--print-only", action="store_true", dest="print_only")
    create.add_argument("--workflow", choices=["htcondor", "local"])
    create.add_argument("--no-validate", action="store_true", dest="no_validate")

    login = subparsers.add_parser("mcm-login", help="Interactive CERN-SSO login (Kerberos + 2FA) writing the McM cookie")
    login.add_argument("--cookiefile", default=None,
                       help="Where to save the cookie (default $MCM_SSO_COOKIEFILE or ~/.mcm-sso-cookie.txt)")
    login.add_argument("--user", default=None, help="CERN username for kinit when no Kerberos ticket exists (default $USER)")
    return parser


def _repo_root(app: CasinoApplication, repo_root) -> Path:
    return Path(repo_root) if repo_root is not None else app.config_loader.repo_root_from_directory(Path.cwd())


def _resolve_config_dir(campaign, prompter, interactive, printer):
    """configs/<era>/<year> for a campaign; prompts for whatever cannot be derived from the name."""
    era, year = parse_campaign_location(campaign)
    if era and year:
        return f"{era}/{year}"
    if not interactive:
        printer(f"could not derive run/year from campaign {campaign!r}; "
                "pass --config-dir (e.g. --config-dir run3/2024)")
        return None
    if not era:
        era = _RUN_OPTIONS[prompter.choice("please enter run number", _RUN_OPTIONS)].lower()
    while not year:
        year = prompter.text("please enter year").strip()
    return f"{era}/{year}"


def build_launch_command(config_path, workflow: str, extra_args=()) -> list[str]:
    # Everything reaches law as positional argv so the shell never interpolates the requirements expression.
    inner = 'source ./setup.sh && poetry run law run RunSample --config "$1" --workflow "$2" "${@:3}"'
    return ["bash", "-lc", inner, "bash", str(config_path), workflow, *extra_args]


def _ask_int(prompter, interactive: bool, explicit, label: str, default: int) -> int:
    if explicit:
        return explicit
    return int(prompter.text(label, default=str(default))) if interactive else default


def handle_create_config(app, args, prompter, repo_root, printer, das=None, fetcher=None, auth_session=None) -> int:
    root = _repo_root(app, repo_root)
    defaults = load_defaults(root)
    das = das or DASClient(datatier=defaults.das_datatier)
    fetcher = fetcher or fetch_text
    auth_session = auth_session or McMAuthSession()
    interactive = not args.dataset

    dataset = args.dataset or _pick_dataset_interactive(prompter, das, printer)
    if dataset is None:
        printer("No dataset selected.")
        return 1

    mode = args.mode or ["full-chain", "delphes"][
        prompter.choice("Mode", ["full-chain (->MiniAOD+Nano)", "delphes"], default=0)
    ]
    card = args.card
    include_delphes_nano = True
    if mode == "delphes":
        if not card:
            cards = sorted(p.name for p in (root / CARDS_DIR).glob("*.tcl"))
            card = f"{CARDS_DIR}/{cards[prompter.choice('Delphes card', cards)]}"
        include_delphes_nano = (
            prompter.confirm("Include delphes-nano step (NanoAOD ntuplization)?", default=False)
            if interactive else not args.no_delphes_nano
        )

    def sso_unavailable(exc):
        printer(f"McM SSO resolution unavailable ({exc}) — falling back to proxy/DAS. "
                "Run `casino mcm-login` (Kerberos + 2FA), provide --drpremix, or enter the prep-id when prompted.")

    try:
        chain = resolve_chain(dataset, mode, das=das, fetcher=fetcher, auth_session=auth_session, prefer_auth=True,
                              wmlhegs=args.wmlhegs, drpremix=args.drpremix, chain=args.chain,
                              on_auth_unavailable=sso_unavailable)
    except (WmlhegsUnresolved, DrpremixUnresolved, AmbiguousChain, NoChainFound) as exc:
        chain = _recover_resolution(exc, args, prompter, das, dataset, mode, printer, interactive, fetcher)
        if chain is None:
            return 1

    total_events = _ask_int(prompter, interactive, args.total_events, "total_events", defaults.total_events)
    memory = _ask_int(prompter, interactive, args.memory, "memory_per_job GB", defaults.memory_per_job_gb)
    cores = _ask_int(prompter, interactive, args.cores, "cores_per_job", defaults.cores_per_job)
    mini_step, nano_step = ("delphes-tree", "delphes-nano") if mode == "delphes" else ("MiniAODv6", "NanoAODv15")
    events_per_job_mini = _ask_int(
        prompter, interactive, args.events_per_job_mini, f"{mini_step} events_per_job", defaults.events_per_job_mini,
    )
    ask_nano = interactive and (mode == "full-chain" or include_delphes_nano)
    events_per_job_nano = _ask_int(
        prompter, ask_nano, args.events_per_job_nano, f"{nano_step} events_per_job", defaults.events_per_job_nano,
    )

    gridpack = args.gridpack
    if interactive and not gridpack and prompter.confirm("Add custom gridpack?"):
        gridpack = prompter.text("Gridpack URI (root://...)") or None

    config = build_config(
        chain, mode=mode, total_events=total_events, cores=cores, memory=memory,
        events_per_job_mini=events_per_job_mini, events_per_job_nano=events_per_job_nano,
        defaults=defaults, card=card, output_root=args.output_root, gridpack=gridpack,
        include_delphes_nano=include_delphes_nano,
    )

    config_dir = args.config_dir or _resolve_config_dir(chain.campaign, prompter, interactive, printer)
    if config_dir is None:
        return 1
    suffix = "_Delphes" if mode == "delphes" else ""
    config_path = root / "configs" / config_dir / f"{chain.sample_name}{suffix}.yaml"
    writer = ConfigWriter()
    try:
        writer.write(config, config_path, force=args.force)
    except FileExistsError as exc:
        if not (interactive and prompter.confirm(f"{config_path.name} exists. Overwrite?")):
            printer(str(exc))
            return 1
        writer.write(config, config_path, force=True)
    printer(f"wrote: {config_path.relative_to(root)}")

    if not args.no_validate:
        rc = _validate(app, config_path, root, gridpack, printer)
        if rc != 0:
            return rc

    workflow = args.workflow or defaults.workflow
    extra_args = shlex.split(defaults.htcondor_extra_args) if workflow == "htcondor" else []
    printer(render_law_command(f"$PWD/{config_path.relative_to(root)}", workflow, defaults.htcondor_extra_args))
    if args.run or (interactive and not args.print_only and prompter.confirm("Start now?")):
        subprocess.run(build_launch_command(config_path, workflow, extra_args), cwd=root, check=False)
    return 0


def _pick_dataset_interactive(prompter, das, printer):
    """Search DAS and let the user refine until a dataset is picked; None on an empty query."""
    while True:
        query = prompter.text(f"Sample search (DAS, {das.datatier}; * = wildcard)").strip()
        if not query:
            return None
        try:
            results = das.search_datasets(query)
        except DASError as exc:
            printer(str(exc))
            continue
        if not results:
            printer(f"No datasets matched {query!r} — refine your search (empty to cancel).")
            continue
        printer(f"{len(results)} dataset(s) matched.")
        index = prompter.choice("Select dataset", results + [_REFINE_SEARCH])
        if index < len(results):
            return results[index]


def _recover_resolution(exc, args, prompter, das, dataset, mode, printer, interactive, fetcher):
    """Interactive: prompt for the missing piece and retry once. Non-interactive: fail with the hint."""
    if not interactive:
        printer(str(exc))
        return None
    retry = dict(das=das, fetcher=fetcher, chain=args.chain)
    if isinstance(exc, WmlhegsUnresolved):
        generator = prompter.text("Generator prep-id (wmLHEGS or GS; see McM chain page)") or None
        try:
            return resolve_chain(dataset, mode, wmlhegs=generator, **retry)
        except WmlhegsUnresolved as retry_exc:
            printer(str(retry_exc))
            return None
    if isinstance(exc, DrpremixUnresolved):
        printer(f"DR-Premix prep-id for ({exc.campaign}, {exc.variant}) — "
                f"see {MCM_CHAIN_URL.format(prepid=exc.chain_prepid)}")
        return resolve_chain(dataset, mode, drpremix=prompter.text("DR-Premix prep-id") or None, **retry)
    if isinstance(exc, AmbiguousChain):
        retry["chain"] = exc.candidates[prompter.choice("Multiple chains — pick", exc.candidates)]
        return resolve_chain(dataset, mode, **retry)
    printer(str(exc))
    return None


def _validate(app, config_path, root, gridpack, printer) -> int:
    driver_root = root / "artifacts" / "mcm-commands"
    try:
        app.download(config_path, driver_root, 30, False, False)
        check_gridpack = bool(gridpack) and shutil.which("gfal-stat") is not None
        if gridpack and not check_gridpack:
            printer("note: gfal-stat unavailable; skipping gridpack pre-check (verified at job time).")
        app.build_step_runners_for_config(config_path, root, driver_root, overwrite=True, check_gridpack=check_gridpack)
    except Exception as exc:
        printer(f"validation failed (config kept): {exc}")
        return 1
    printer("validated: download OK  build-step-runners OK")
    return 0


def handle_list_configs(app, args, prompter, repo_root, printer) -> int:
    root = _repo_root(app, repo_root)
    year = getattr(args, "year", None)
    if not year:
        years = available_years(root)
        if not years:
            printer("No configs found under configs/.")
            return 0
        year = years[prompter.choice("Select year", [f"{rel}   ({n} configs)" for rel, n in years])][0]
    summaries = configs_for_year(root, year)
    if not summaries:
        printer(f"No configs in configs/{year}.")
        return 0
    options = [f"{s.name}  —  {s.mode} · {s.total_events} evt · {s.n_steps} steps" for s in summaries] + ["Back"]
    index = prompter.choice(
        f"configs/{year} — select a config to show its launch command", options, default=len(options) - 1,
    )
    if index < len(summaries):
        printer(summaries[index].law_command)
    return 0


def _kerberos_ticket_present() -> bool:
    try:
        return subprocess.run(["klist", "-s"]).returncode == 0
    except OSError:
        return False


def _prompt_otp(attempt: int) -> str:
    label = "CERN 2FA code (authenticator app): " if attempt == 1 else f"2FA code (attempt {attempt}): "
    try:
        return input(label)
    except EOFError:
        return ""


def _default_login(prompt_otp, cookiefile, printer):
    from infrastructure.mcm_sso_login import login_with_2fa

    return login_with_2fa(prompt_otp, url=MCM_BASE, cookiefile=cookiefile, printer=printer)


def handle_mcm_login(args, printer, prompt_otp=None, login_fn=None, authenticator=None,
                     ticket_present=None, kinit_fn=None, prompt_user=None) -> int:
    """Kerberos + 2FA login writing an McM cookie. The callables are injectable for tests."""
    cookiefile = default_cookiefile(getattr(args, "cookiefile", None))
    ticket_present = ticket_present or _kerberos_ticket_present
    prompt_otp = prompt_otp or _prompt_otp
    login_fn = login_fn or _default_login
    authenticator = authenticator or (lambda cf: McMAuthSession(cookiefile=cf).is_authenticated())

    def default_kinit(user: str) -> bool:
        try:
            subprocess.run(["kinit", user])  # password is typed on the tty, never seen here
        except OSError as exc:
            printer(f"could not run kinit: {exc}")
            return False
        return ticket_present()

    def default_prompt_user() -> str:
        default_user = os.environ.get("USER") or getpass.getuser()
        try:
            return input(f"CERN username [{default_user}]: ").strip() or default_user
        except EOFError:
            return ""

    if not ticket_present():
        user = getattr(args, "user", None) or (prompt_user or default_prompt_user)()
        if not user:
            printer("No CERN username — run `kinit <user>` yourself, then retry.")
            return 1
        printer(f"No Kerberos ticket — running `kinit {user}` (enter your CERN password)…")
        if not (kinit_fn or default_kinit)(user):
            printer("kinit did not produce a valid ticket (wrong password, or no Kerberos for this account). "
                    "Try `kinit <user>` manually.")
            return 1

    printer("Logging in to McM via CERN SSO (Kerberos + 2FA)…")
    try:
        login_fn(prompt_otp=prompt_otp, cookiefile=cookiefile, printer=printer)
    except Exception as exc:
        printer(f"Login failed: {exc}")
        return 1

    try:
        accepted = authenticator(cookiefile)
    except Exception:
        accepted = False
    if accepted:
        printer(f"McM SSO cookie saved and accepted by McM: {cookiefile}")
        return 0
    printer(f"Cookie written to {cookiefile} but McM still rejects it — the 2FA flow may have a step the CLI "
            "can't complete; export a browser cookie to MCM_SSO_COOKIEFILE instead.")
    return 1


def run_menu(app, prompter, repo_root, printer) -> int:
    printer("CASINO")
    choice = prompter.choice("Select", ["Create config", "Show configs", "Log in to McM (SSO 2FA)", "Quit"], default=0)
    parser = build_parser()
    if choice == 0:
        return handle_create_config(app, parser.parse_args(["create-config"]), prompter, repo_root, printer)
    if choice == 1:
        return handle_list_configs(app, parser.parse_args(["list-configs"]), prompter, repo_root, printer)
    if choice == 2:
        return handle_mcm_login(parser.parse_args(["mcm-login"]), printer)
    return 0


def _batch_scope(app, args, parser, command: str) -> tuple[Path | None, Path, Path]:
    """(config_path or None for --all, repo_root, driver_root) for download/build-step-runners."""
    if args.all and args.config:
        parser.error(f"{command} accepts either CONFIG or --all, not both")
    if not args.all and not args.config:
        parser.error(f"{command} requires either CONFIG or --all")
    if args.all:
        config_path, repo_root = None, app.config_loader.repo_root_from_directory(Path.cwd())
    else:
        config_path = Path(args.config).resolve()
        repo_root = app.config_loader.repo_root_from_path(config_path)
    return config_path, repo_root, (repo_root / args.driver_root).resolve()


def handle_download(app: CasinoApplication, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    config_path, repo_root, driver_root = _batch_scope(app, args, parser, "download")
    if config_path is None:
        result = app.download_all(repo_root, driver_root, args.timeout, args.overwrite, args.verify_certs)
        return 1 if result.failed else 0
    app.download(config_path, driver_root, args.timeout, args.overwrite, args.verify_certs)
    return 0


def handle_build_step_runners(app: CasinoApplication, args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    config_path, repo_root, driver_root = _batch_scope(app, args, parser, "build-step-runners")
    if config_path is None:
        result = app.build_step_plans_all(repo_root, driver_root, args.overwrite)
        return 1 if result.failed else 0
    app.build_step_runners_for_config(config_path, repo_root, driver_root, args.overwrite)
    return 0


def run_cli(
    argv: list[str] | None = None, app: CasinoApplication | None = None, prompter=None, repo_root=None, printer=print,
) -> int:
    app = app or CasinoApplication()
    prompter = prompter or QuestionaryPrompter()
    parser = build_parser()
    args = parser.parse_args(argv)

    # The menu and create-config need a VOMS proxy (DAS) and may need the McM cookie.
    if args.command in (None, "create-config"):
        printer("Auth status:")
        for line in auth_status_lines():
            printer(line)

    try:
        if args.command is None:
            return run_menu(app, prompter, repo_root, printer)
        if args.command == "download":
            return handle_download(app, args, parser)
        if args.command == "build-step-runners":
            return handle_build_step_runners(app, args, parser)
        if args.command == "list-configs":
            return handle_list_configs(app, args, prompter, repo_root, printer)
        if args.command == "create-config":
            return handle_create_config(app, args, prompter, repo_root, printer)
        if args.command == "mcm-login":
            return handle_mcm_login(args, printer)
    except KeyboardInterrupt:
        printer("Aborted.")
        return 130
    parser.error(f"Unsupported command {args.command!r}")
    return 2
