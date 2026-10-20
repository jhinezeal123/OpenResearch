import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('profile_import',ROOT/'import_profiles.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PackagingTests(unittest.TestCase):
    def test_profile_import_owns_tokens_cookies_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'old';home=Path(d)/'new';profile=source/'profiles/qa';profile.mkdir(parents=True)
            (source/'profiles/accounts.json').write_text(json.dumps({'accounts':{'qa.txt':{'alias':'qa','username':'qa-user','profile_dir':'C:/external','cookie_file':'C:/external/cookie','unrelated':'discard'}}}))
            (profile/'token.txt').write_text('KGAT_PRIVATE')
            (profile/'web-session.json').write_text('{"private":"cookie"}')
            module.import_profiles(source,home)
            registry=json.loads((home/'profiles/accounts.json').read_text(encoding="utf-8"))
            self.assertEqual(registry['accounts']['qa.txt']['cookie_file'],'profiles/qa/web-session.json')
            self.assertEqual((home/'profiles/qa/token.txt').read_text(encoding="utf-8"),'KGAT_PRIVATE')
            self.assertEqual((home/'profiles/qa/web-session.json').read_text(encoding="utf-8"),'{"private":"cookie"}')
            self.assertNotIn('C:/external',json.dumps(registry))
    def test_binary_package_manifest_embeds_every_python_runtime_file(self):
        manifest=(ROOT/'package.rs').read_text(encoding="utf-8")
        for file in (ROOT/'python').rglob('*.py'):
            self.assertIn('python/'+file.relative_to(ROOT/'python').as_posix(),manifest)
    def test_auto_login_uses_private_home_and_its_own_script(self):
        recovery=(ROOT/'python/native/auto_login/recovery.py').read_text(encoding="utf-8")
        self.assertIn('web_session.__file__',recovery)
        self.assertNotIn("os.path.join(web_session.BASE, 'web_session.py')",recovery)
        web=(ROOT/'python/native/auto_login/web_session.py').read_text(encoding="utf-8")
        self.assertIn('BASE = os.environ["ORX_KAGGLE_HOME"]',web)
    def test_bridge_has_no_runtime_donor_directory_requirement(self):
        with tempfile.TemporaryDirectory() as home:
            request={'action':'accounts','home':home,'runtime':{'donorRoot':'Z:/nonexistent','python':'Z:/nonexistent'}}
            p=subprocess.run([sys.executable,str(ROOT/'python/bridge.py')],input=json.dumps(request),text=True,capture_output=True,cwd=home,timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(json.loads(p.stdout)['result']['accounts'],[])

if __name__=='__main__':unittest.main()
