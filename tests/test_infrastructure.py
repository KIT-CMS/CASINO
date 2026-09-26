import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from domain.configuration import ConfigLoader
from infrastructure.auth_status import AuthStatus, auth_status_lines, mcm_cookie_status, voms_proxy_status
from infrastructure.das import DASClient, DASError, _default_runner, parse_wmlhegs_root
from infrastructure.gridpack_checker import GfalGridpackChecker
from infrastructure.gridpack_fragment import CustomGridpackFragmentManager, XROOTD_GRIDPACK_SCRIPT
from infrastructure.mcm import MCM_REQUEST_GET_URL, McMResponseError, get_request, member_of_chain, validate_mcm_artifact_payload
from infrastructure.mcm_auth import McMAuthSession, McMAuthUnavailable
from infrastructure.mcm_sso_login import _is_otp_page, _otp_form_fields, _submit_otp
from tests.support import CONFIG_FIXTURE, FRAGMENT_FIXTURE, MCM_ROOT, RESPONSES, SAMPLE_NAME, CasinoTestCase, load_fixture

CUSTOM_GRIDPACK = "root://grid.example//store/user/casino/gridpacks/ggHH_kl_m1.tgz"
NANO_REQUEST = Path(__file__).resolve().parent / "testdata" / "mcm" / "nano_request.json"
SETUP_ID = "HIG-RunIII2024Summer24wmLHEGS-01175"


class McMTests(unittest.TestCase):
    def test_get_request_and_member_of_chain(self):
        calls = []

        def fetch(url, timeout, verify_certs):
            calls.append(url)
            return NANO_REQUEST.read_text(encoding="utf-8")

        prepid = "HIG-RunIII2024Summer24NanoAODv15-00308"
        self.assertEqual(get_request(prepid, fetcher=fetch)["prepid"], prepid)
        self.assertEqual(calls[0], MCM_REQUEST_GET_URL.format(prepid=prepid))
        self.assertTrue(any("NanoAODv15" in c for c in member_of_chain(prepid, fetcher=fetch)))

    def test_validate_artifact_payload(self):
        payload = "#!/bin/bash\necho ok\n"
        kwargs = dict(resource_kind="setup", setup_id="B2G-RunXwmLHEGS-00001")
        self.assertEqual(validate_mcm_artifact_payload(payload, **kwargs), payload)
        self.assertEqual(validate_mcm_artifact_payload(json.dumps(payload), **kwargs), payload)
        for text, message in [
            ("  \n", "empty response"),
            ('[{"status": "error"}]', "unexpected JSON response"),
            ("<!doctype html><html><body>SSO</body></html>", "unexpected HTML response"),
            (json.dumps({"results": False, "message": "nope"}), "nope"),
        ]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(McMResponseError, message):
                    validate_mcm_artifact_payload(text, **kwargs)


class DownloadManagerTests(CasinoTestCase):
    def _script_path(self) -> Path:
        return self.repo / MCM_ROOT / "run3" / "2024" / SAMPLE_NAME / "HIG-RunIII2024Summer24" / f"{SETUP_ID}.sh"

    def _fragment_path(self) -> Path:
        return self.repo / MCM_ROOT / "run3" / "2024" / SAMPLE_NAME / "fragments" / f"{SETUP_ID}-fragment.py"

    def test_download_creates_mirrored_layout(self):
        config_path = self.write_default_config()
        self.assertEqual(self.create_application().main(["download", str(config_path)]), 0)
        self.assertTrue(self._script_path().exists())
        self.assertTrue(self._script_path().with_name("HIG-RunIII2024Summer24DRPremix-00530.sh").exists())
        self.assertTrue(self._fragment_path().exists())

    def test_download_rejects_mcm_errors_before_writing(self):
        config_path = self.write_default_config()
        for endpoint, kind, path in [("get_setup", "setup", self._script_path()), ("get_fragment", "fragment", self._fragment_path())]:
            def fetcher(url, timeout, verify_certs):
                if endpoint in url and url.endswith(SETUP_ID):
                    return json.dumps({"results": False, "message": f"{SETUP_ID} does not exist"})
                return self.fake_fetch_text(url, timeout, verify_certs)

            with self.subTest(kind=kind):
                with self.assertRaisesRegex(RuntimeError, f"McM {kind} download failed for {SETUP_ID}: {SETUP_ID} does not exist"):
                    self.create_application(fetcher=fetcher).download(config_path, self.repo / MCM_ROOT, 30, False, False)
                self.assertFalse(path.exists())

    def test_download_repairs_invalid_cache_and_keeps_valid_cache(self):
        config_path = self.write_default_config()
        self._script_path().parent.mkdir(parents=True)
        self._script_path().write_text(json.dumps({"results": False, "message": "gone"}), encoding="utf-8")
        self._fragment_path().parent.mkdir(parents=True)
        self._fragment_path().write_text("#!/bin/bash\n# locally preserved valid artifact\n", encoding="utf-8")

        self.create_application().download(config_path, self.repo / MCM_ROOT, 30, False, False)

        self.assertEqual(self._script_path().read_text(encoding="utf-8"), RESPONSES[SETUP_ID])
        self.assertIn("locally preserved", self._fragment_path().read_text(encoding="utf-8"))

    def test_download_all_skips_complete_entries_and_reports_failures(self):
        config_path = self.write_default_config()
        (self.repo / "configs" / "run3" / "2024" / f"{SAMPLE_NAME}_copy.yaml").write_text(load_fixture(CONFIG_FIXTURE), encoding="utf-8")
        (self.repo / "configs" / "run3" / "2024" / "broken.yaml").write_text("total_events: 10\nbase_output_path: /tmp\n", encoding="utf-8")
        fetch_urls: list[str] = []

        def fetcher(url, timeout, verify_certs):
            fetch_urls.append(url)
            return self.fake_fetch_text(url, timeout, verify_certs)

        app = self.create_application(fetcher=fetcher)
        app.main(["download", str(config_path)])
        self.assertEqual(len(fetch_urls), 5)

        stderr = StringIO()
        with mock.patch.object(Path, "cwd", return_value=self.repo), redirect_stderr(stderr):
            result = app.main(["download", "--all"])

        self.assertEqual(result, 1)
        self.assertEqual(len(fetch_urls), 10)
        self.assertIn("Download summary: downloaded=1 skipped=1 failed=1", stderr.getvalue())


class FakeRunner:
    """Map a dasgoclient query substring -> canned stdout."""

    def __init__(self, table, fail=False):
        self.table, self.fail, self.calls = table, fail, []

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise DASError("dasgoclient failed")
        query = args[-1]
        return next((out for needle, out in self.table.items() if needle in query), "")


class DASClientTests(unittest.TestCase):
    NANO = "/TT/RunIII2024Summer24NanoAODv15-x/NANOAODSIM"
    MINI = "/TT/RunIII2024Summer24MiniAODv6-x/MINIAODSIM"
    AOD = "/TT/RunIII2024Summer24DRPremix-x/AODSIM"

    def test_search_normalizes_patterns(self):
        for typed, expected in [
            ("GluGluHH", "/*GluGluHH*/*/NANOAODSIM"),
            ("/A/B", "/A/B*/NANOAODSIM"),
            ("/A/B/", "/A/B*/NANOAODSIM"),
            ("/A/B*", "/A/B*/NANOAODSIM"),
            ("/TTto2L2Nu", "/TTto2L2Nu*/*/NANOAODSIM"),
            ("/A/B/MINIAODSIM", "/A/B/MINIAODSIM"),
        ]:
            with self.subTest(typed=typed):
                runner = FakeRunner({})
                DASClient(runner=runner).search_datasets(typed)
                self.assertEqual(runner.calls[0][-1], f"dataset dataset={expected}")

        runner = FakeRunner({"NANOAODSIM": "/B/c/NANOAODSIM\n/A/c/NANOAODSIM\n/A/c/NANOAODSIM\n"})
        self.assertEqual(DASClient(runner=runner).search_datasets("x"), ["/A/c/NANOAODSIM", "/B/c/NANOAODSIM"])
        runner = FakeRunner({})
        for degenerate in ["", "   ", "/", "//", " / "]:
            self.assertEqual(DASClient(runner=runner).search_datasets(degenerate), [])
        self.assertEqual(runner.calls, [])

    def test_queries_drop_dasgoclient_diagnostics(self):
        noise = ('jsonparser failure of DAS record={"mcm":[{}]}\n'
                 ", select sub keys=[mcm [0] prepid], error=Key path not found\n")
        client = DASClient(runner=FakeRunner({
            "mcm dataset=/T/": noise,
            "mcm dataset=/A/": noise + "GEN-RunIII2024Summer24NanoAODv15-00002\n",
            "parent dataset=/A/": "/A/c/MINIAODSIM\n",
            "summary dataset=/A/": '[{"nevents": 957670, "nfiles": 101}]',
        }))
        self.assertIsNone(client.mcm_prepid("/T/c/NANOAODSIM"))
        self.assertEqual(client.mcm_prepid("/A/c/NANOAODSIM"), "GEN-RunIII2024Summer24NanoAODv15-00002")
        self.assertEqual(client.parent("/A/c/NANOAODSIM"), "/A/c/MINIAODSIM")
        self.assertEqual(client.summary("/A/c/NANOAODSIM")["nevents"], 957670)
        with self.assertRaises(DASError):
            DASClient(runner=FakeRunner({}, fail=True)).summary("/A/c/NANOAODSIM")

    def test_find_generator_walks_parents_to_the_grandparent(self):
        self.assertEqual(parse_wmlhegs_root("pdmvserv_task_HIG-RunIII2024Summer24wmLHEGS-00747__v1_T_x"),
                         "HIG-RunIII2024Summer24wmLHEGS-00747")
        self.assertIsNone(parse_wmlhegs_root("garbage"))
        mini_task = "cmsunified_task_GEN-RunIII2024Summer24MiniAODv6-00002__v1_T\n"
        table = {
            f"config dataset={self.NANO}": mini_task,
            f"parent dataset={self.NANO}": self.MINI + "\n",
            f"config dataset={self.MINI}": mini_task,
            f"parent dataset={self.MINI}": self.AOD + "\n",
            f"config dataset={self.AOD}": mini_task + "cmsunified_task_GEN-RunIII2024Summer24wmLHEGS-00006__v1_T\n",
        }
        self.assertEqual(DASClient(runner=FakeRunner(table)).find_generator(self.NANO), "GEN-RunIII2024Summer24wmLHEGS-00006")
        self.assertIsNone(DASClient(runner=FakeRunner(table)).find_generator(self.NANO, max_depth=1))

        gs = FakeRunner({f"config dataset={self.NANO}": "cmsunified_task_GEN-RunIII2024Summer24GS-00054__v1_T_x\n"})
        self.assertEqual(DASClient(runner=gs).find_generator(self.NANO), "GEN-RunIII2024Summer24GS-00054")
        self.assertFalse(any("parent dataset=" in c[-1] for c in gs.calls))

        dead_end = dict(table, **{f"parent dataset={self.MINI}": ""})
        self.assertIsNone(DASClient(runner=FakeRunner(dead_end)).find_generator(self.NANO))

    def test_default_runner_adds_proxy_hint_only_for_auth_errors(self):
        def run(returncode, stderr="", stdout=""):
            completed = SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)
            with mock.patch("infrastructure.das.subprocess.run", return_value=completed):
                return _default_runner(["dasgoclient", "--query", "dataset dataset=/A/B"])

        self.assertEqual(run(0, stdout="/A/B/NANOAODSIM\n"), "/A/B/NANOAODSIM\n")
        with self.assertRaises(DASError) as ctx:
            run(1, stderr="Validation error: unmatched dataset pattern")
        self.assertIn("unmatched dataset pattern", str(ctx.exception))
        self.assertNotIn("voms-proxy-init", str(ctx.exception))
        with self.assertRaises(DASError) as ctx:
            run(1, stderr="Could not find a valid proxy: x509 credential expired")
        self.assertIn("voms-proxy-init", str(ctx.exception))


class FakeResponse(io.BytesIO):
    def __init__(self, payload, final_url):
        super().__init__(payload.encode("utf-8"))
        self._final_url = final_url

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    def __init__(self, payload, final_url):
        self.payload, self.final_url, self.opened = payload, final_url, []

    def open(self, url, timeout=None):
        self.opened.append(url)
        return FakeResponse(self.payload, self.final_url)


class McMAuthTests(unittest.TestCase):
    def test_get_chained_request_parses_results(self):
        chain = "HIG-chain_...NanoAODv15-00118"
        payload = json.dumps({"results": {"prepid": chain, "chain": ["HIG-...wmLHEGS-1", "HIG-...DRPremix-2"]}})
        opener = FakeOpener(payload, f"https://cms-pdmv-prod.web.cern.ch/mcm/restapi/chained_requests/get/{chain}")
        self.assertEqual(McMAuthSession(opener=opener).get_chained_request(chain)["chain"], ["HIG-...wmLHEGS-1", "HIG-...DRPremix-2"])
        self.assertIn(chain, opener.opened[0])
        self.assertTrue(McMAuthSession(opener=FakeOpener("{}", "https://cms-pdmv-prod.web.cern.ch/mcm/restapi/users/get_role")).is_authenticated())

    def test_unavailable_when_redirected_or_without_usable_cookie(self):
        sso = FakeOpener("<html>login</html>", "https://auth.cern.ch/auth/realms/cern/...")
        self.assertFalse(McMAuthSession(opener=sso).is_authenticated())
        fd, malformed = tempfile.mkstemp(suffix=".txt")
        self.addCleanup(os.unlink, malformed)
        with os.fdopen(fd, "w") as handle:
            handle.write("this is not a netscape cookie file\n")
        for session in [
            McMAuthSession(opener=sso),
            McMAuthSession(cookiefile="/nonexistent/cookie.txt"),
            McMAuthSession(cookiefile=malformed),
        ]:
            with self.subTest(session=session), self.assertRaises(McMAuthUnavailable):
                session.get_chained_request("HIG-chain_x-1")


OTP_HTML = """
<html><body>
<form id="kc-otp-login-form"
      action="https://auth.cern.ch/auth/realms/cern/login-actions/post-broker-login?session_code=ABC&amp;execution=XYZ"
      method="post">
  <input type="hidden" name="credentialId" value="cred-1"/>
  <input id="otp" name="otp" type="text" autocomplete="off"/>
  <input name="login" type="submit" value="Sign In"/>
</form></body></html>
"""


class FakeSsoSession:
    def __init__(self, responses):
        self._responses, self.posts = list(responses), []

    def post(self, action, data=None, verify=True):
        self.posts.append((action, dict(data or {})))
        return self._responses.pop(0)


def _resp(url, text=""):
    return SimpleNamespace(url=url, text=text, status_code=200)


class SsoLoginTests(unittest.TestCase):
    OTP_PAGE_URL = "https://auth.cern.ch/auth/realms/cern/login-actions/post-broker-login"

    def test_otp_form_parsing(self):
        self.assertTrue(_is_otp_page(OTP_HTML))
        self.assertFalse(_is_otp_page('<html><body><form action="/x"><input name="foo"/></form></body></html>'))
        action, data = _otp_form_fields(OTP_HTML)
        self.assertIn("session_code=ABC&execution=XYZ", action)
        self.assertEqual(data, {"credentialId": "cred-1", "otp": "", "login": "Sign In"})
        self.assertIsNone(_otp_form_fields("<html><body>nope</body></html>"))

    def test_submit_otp_retries_rejections_and_aborts(self):
        quiet = dict(verify=True, printer=lambda *_: None, attempts=3)
        session = FakeSsoSession([_resp(self.OTP_PAGE_URL, OTP_HTML), _resp("https://cms-pdmv-prod.web.cern.ch/mcm/x", "ok")])
        codes = iter(["000000", "123456"])

        response = _submit_otp(session, _resp(self.OTP_PAGE_URL, OTP_HTML), prompt_otp=lambda a: next(codes), **quiet)

        self.assertNotIn("auth.cern.ch", response.url)
        self.assertEqual([post[1]["otp"] for post in session.posts], ["000000", "123456"])
        self.assertEqual(session.posts[0][1]["credentialId"], "cred-1")
        with self.assertRaises(McMAuthUnavailable):
            _submit_otp(FakeSsoSession([]), _resp(self.OTP_PAGE_URL, OTP_HTML), prompt_otp=lambda a: "", **quiet)
        with self.assertRaises(McMAuthUnavailable):
            session = FakeSsoSession([_resp(self.OTP_PAGE_URL, OTP_HTML)] * 3)
            _submit_otp(session, _resp(self.OTP_PAGE_URL, OTP_HTML), prompt_otp=lambda a: "000000", **quiet)


def _runner(returncode=0, stdout=""):
    return lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class AuthStatusTests(unittest.TestCase):
    def test_voms_proxy_status(self):
        status = voms_proxy_status(runner=_runner(0, "43200\n"))
        self.assertEqual((status.ok, status.hint), (True, None))
        self.assertIn("12h", status.detail)
        self.assertFalse(voms_proxy_status(runner=_runner(0, "0\n")).ok)
        self.assertIn("voms-proxy-init", voms_proxy_status(runner=_runner(1, "0\n")).hint)

        def missing(*a, **k):
            raise FileNotFoundError()

        self.assertIn("not found", voms_proxy_status(runner=missing).detail)

    def test_mcm_cookie_status_probes_mcm(self):
        def boom():
            raise RuntimeError("connection reset")

        self.assertTrue(mcm_cookie_status(cookiefile="/x", resolver=lambda _c: "/x", authenticator=lambda: True).ok)
        rejected = mcm_cookie_status(cookiefile="/x", resolver=lambda _c: "/x", authenticator=lambda: False)
        self.assertFalse(rejected.ok)
        self.assertIn("reject", rejected.detail)
        self.assertIn("not found", mcm_cookie_status(resolver=lambda _c: None).detail)
        self.assertIn("could not verify", mcm_cookie_status(cookiefile="/x", resolver=lambda _c: "/x", authenticator=boom).detail)

    def test_status_lines_mark_failures_with_a_hint(self):
        proxy_line, cookie_line = auth_status_lines(runner=_runner(0, "43200\n"), cookie_resolver=lambda _c: None)
        self.assertTrue(proxy_line.lstrip().startswith("✓"))
        self.assertNotIn("→", proxy_line)
        self.assertIn("→", cookie_line)
        self.assertIsInstance(voms_proxy_status(runner=_runner(0, "1\n")), AuthStatus)


class GridpackFragmentTests(CasinoTestCase):
    def manager(self) -> CustomGridpackFragmentManager:
        return CustomGridpackFragmentManager(ConfigLoader())

    def test_render_rewrites_only_the_external_lhe_producer_block(self):
        source = (
            "import FWCore.ParameterSet.Config as cms\n\n"
            "helper = cms.PSet(\n    args = cms.vstring('/do/not/change.tgz'),\n"
            "    scriptName = cms.FileInPath('do/not/change.sh'),\n)\n\n"
            "externalLHEProducer = cms.EDProducer(\"ExternalLHEProducer\",\n"
            "    args = cms.vstring('/cvmfs/gridpack.tgz'),\n"
            "    scriptName = cms.FileInPath('run_generic_tarball_cvmfs.sh'),\n)\n"
        )
        rendered = self.manager().render_fragment(source, CUSTOM_GRIDPACK)
        self.assertIn("args = cms.vstring('/do/not/change.tgz')", rendered)
        self.assertIn(f"args = cms.vstring({CUSTOM_GRIDPACK!r})", rendered)
        self.assertIn(f"scriptName = cms.FileInPath({XROOTD_GRIDPACK_SCRIPT!r})", rendered)
        self.assertNotIn("run_generic_tarball_cvmfs.sh", rendered)

        fixture = self.manager().render_fragment(FRAGMENT_FIXTURE.read_text(encoding="utf-8"), CUSTOM_GRIDPACK)
        self.assertIn(CUSTOM_GRIDPACK, fixture)
        self.assertNotIn("ggHH_kl_1p00_kt_1p00", fixture)

        for source, message in [
            ("helper = cms.PSet(args = cms.vstring('/x.tgz'))\n", "exactly one externalLHEProducer block"),
            ("externalLHEProducer = cms.EDProducer(\"ExternalLHEProducer\",\n    scriptName = cms.FileInPath('a.sh'),\n)\n",
             "exactly one externalLHEProducer args assignment"),
            ("externalLHEProducer = cms.EDProducer(\"ExternalLHEProducer\",\n    args = cms.vstring('/x.tgz'),\n)\n",
             "exactly one externalLHEProducer scriptName assignment"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.manager().render_fragment(source, CUSTOM_GRIDPACK)

    def test_ensure_custom_fragment_respects_overwrite(self):
        config_path = self.write_default_config()
        target = self.manager().custom_fragment_path(config_path, self.repo)
        self.assertEqual(target, self.repo / "configs" / "run3" / "2024" / "fragments" / f"{SAMPLE_NAME}-fragment.py")
        target.parent.mkdir(parents=True)
        target.write_text("# user edited fragment\n", encoding="utf-8")
        kwargs = dict(source_fragment_path=FRAGMENT_FIXTURE, config_path=config_path, repo_root=self.repo, gridpack_uri=CUSTOM_GRIDPACK)

        self.assertEqual(self.manager().ensure_custom_fragment(overwrite=False, **kwargs), target)
        self.assertEqual(target.read_text(encoding="utf-8"), "# user edited fragment\n")
        self.manager().ensure_custom_fragment(overwrite=True, **kwargs)
        self.assertIn(CUSTOM_GRIDPACK, target.read_text(encoding="utf-8"))


class GridpackCheckerTests(unittest.TestCase):
    def test_gfal_stat_runs_with_a_clean_environment_and_system_python(self):
        env = {
            "PYTHONHOME": "/broken/python/home", "PYTHONPATH": "/work/sdaigler/casino/src", "GFAL_PYTHONBIN": "/broken/python",
            "GFAL_PLUGIN_DIR": "/stale/gfal/plugins", "GFAL_CONFIG_DIR": "/stale/gfal/config", "PATH": os.environ["PATH"],
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("shutil.which", return_value="/usr/bin/gfal-stat"), \
             mock.patch("os.access", return_value=True), \
             mock.patch("subprocess.run", side_effect=[subprocess.CompletedProcess([], 0)] * 2) as run:
            GfalGridpackChecker().ensure_exists("root://grid.example//store/gridpack.tgz")

        probe_call, stat_call = run.call_args_list
        self.assertEqual(probe_call.args[0][0], "/usr/bin/python3")
        self.assertEqual(stat_call.kwargs["env"]["GFAL_PYTHONBIN"], "/usr/bin/python3")
        self.assertFalse({"PYTHONHOME", "PYTHONPATH", "GFAL_PLUGIN_DIR", "GFAL_CONFIG_DIR"} & stat_call.kwargs["env"].keys())
